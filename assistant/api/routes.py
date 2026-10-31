"""The assistant's endpoints.

``POST /assistant/chat`` runs one turn. ``WS /assistant/ws`` does the same thing
over a socket and streams the reply back in chunks.

Three limits sit on the socket, all enforced here rather than in a proxy: the
first frame has to carry the shared token, a frame larger than the cap closes
the socket, and a process serves at most a fixed number of sockets at once.

What the socket streams is the composed reply broken into chunks, not raw model
tokens. The model never writes the reply, so there are no model tokens to
forward; this is the honest version of streaming for this design.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from starlette.websockets import WebSocketState

from ..agent import run_turn
from ..config import Settings, get_settings
from ..llm import build_llm_client
from ..redaction import redact_session_id
from ..sessions import SessionLoanStore, TooManyLoansForSession, is_loan_id
from .schemas import MAX_MESSAGE_CHARS, MAX_SESSION_ID_CHARS, ChatRequest, ChatResponse

logger = logging.getLogger("assistant.api")

router = APIRouter(tags=["assistant"])

WS_CLOSE_UNAUTHORIZED = 1008
WS_CLOSE_TOO_LARGE = 1009
WS_CLOSE_TRY_AGAIN_LATER = 1013
WS_CLOSE_BAD_REQUEST = 1003
STREAM_CHUNK_WORDS = 6


class ConnectionLimiter:
    """A counter with a non blocking acquire.

    A semaphore would make the connection past the cap wait for a slot. This
    refuses it instead, which is the behaviour a client can act on.
    """

    def __init__(self, cap: int) -> None:
        self._cap = max(1, cap)
        self._active = 0
        self._lock = asyncio.Lock()

    async def try_acquire(self) -> bool:
        async with self._lock:
            if self._active >= self._cap:
                return False
            self._active += 1
            return True

    async def release(self) -> None:
        async with self._lock:
            self._active = max(0, self._active - 1)

    @property
    def active(self) -> int:
        return self._active


_limiters: dict[int, ConnectionLimiter] = {}


def _limiter(settings: Settings) -> ConnectionLimiter:
    """One limiter per cap value, created lazily inside the running loop."""
    cap = max(1, settings.ws_max_connections)
    if cap not in _limiters:
        _limiters[cap] = ConnectionLimiter(cap)
    return _limiters[cap]


def _store(settings: Settings) -> SessionLoanStore:
    return SessionLoanStore(settings.state_db_path)


def _error(status: int, code: str, message: str, **details: Any) -> JSONResponse:
    """The ledger's error envelope, so a caller parses one shape for both."""
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "details": details}},
    )


async def _run(settings: Settings, session_id: str, message: str, loans: list[str]) -> ChatResponse:
    result = await run_turn(
        session_id=session_id,
        message=message,
        bound_loan_ids=loans,
        settings=settings,
        llm=build_llm_client(settings),
    )
    return ChatResponse(
        session_id=session_id,
        reply=result.reply,
        intent=result.intent,
        outcome=result.outcome,
        refusal_code=result.refusal_code,
        tool_calls=result.tool_calls,
        awaiting_confirmation=result.has_pending_action,
        loan_ids=loans,
    )


@router.get("/health")
def assistant_health() -> JSONResponse:
    """Report that the assistant is mounted and which model provider it would use."""
    settings = get_settings()
    return JSONResponse(
        status_code=200,
        content={
            "status": "ok",
            "llm_provider": settings.llm_provider,
            "model_configured": bool(settings.openai_api_key) or settings.llm_provider == "fake",
            "max_payment_cents": settings.max_payment_cents,
        },
    )


@router.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest) -> Any:
    settings = get_settings()
    try:
        loans = _store(settings).bind(request.session_id, request.loan_ids)
    except TooManyLoansForSession as exc:
        return _error(422, "too_many_loans_for_session", str(exc))

    return await _run(settings, request.session_id, request.message, loans)


async def _reject(socket: WebSocket, code: int, reason: str) -> None:
    if socket.client_state is WebSocketState.CONNECTED:
        await socket.send_text(json.dumps({"type": "error", "reason": reason}))
        await socket.close(code=code, reason=reason)


async def _read_frame(socket: WebSocket, *, max_bytes: int) -> dict[str, Any] | None:
    """Read one JSON frame, enforcing the size cap. None means the socket closed."""
    raw = await socket.receive_text()
    if len(raw.encode("utf-8", errors="ignore")) > max_bytes:
        await _reject(socket, WS_CLOSE_TOO_LARGE, "frame_too_large")
        return None
    try:
        frame = json.loads(raw)
    except ValueError:
        await _reject(socket, WS_CLOSE_BAD_REQUEST, "frame_not_json")
        return None
    if not isinstance(frame, dict):
        await _reject(socket, WS_CLOSE_BAD_REQUEST, "frame_not_an_object")
        return None
    return frame


def _chunk(text: str, words_per_chunk: int = STREAM_CHUNK_WORDS) -> list[str]:
    words = text.split(" ")
    return [
        " ".join(words[index : index + words_per_chunk])
        for index in range(0, len(words), words_per_chunk)
    ] or [""]


@router.websocket("/ws")
async def assistant_socket(socket: WebSocket) -> None:
    settings = get_settings()
    limiter = _limiter(settings)

    if not await limiter.try_acquire():
        # Accepted only to say why, then closed straight away.
        await socket.accept()
        await _reject(socket, WS_CLOSE_TRY_AGAIN_LATER, "connection_limit_reached")
        return

    try:
        await socket.accept()

        if not settings.ws_auth_token:
            # No token configured means the socket is not open for business. A
            # socket that authenticates against nothing is worse than no socket.
            await _reject(socket, WS_CLOSE_UNAUTHORIZED, "socket_auth_not_configured")
            return

        hello = await _read_frame(socket, max_bytes=settings.ws_max_frame_bytes)
        if hello is None:
            return

        token = str(hello.get("token") or "")
        session_id = str(hello.get("session_id") or "")
        if token != settings.ws_auth_token:
            logger.warning("socket rejected", extra={"reason": "bad_token"})
            await _reject(socket, WS_CLOSE_UNAUTHORIZED, "bad_token")
            return
        if not session_id or len(session_id) > MAX_SESSION_ID_CHARS:
            await _reject(socket, WS_CLOSE_BAD_REQUEST, "bad_session_id")
            return

        raw_loans = hello.get("loan_ids") or []
        if not isinstance(raw_loans, list) or any(not is_loan_id(item) for item in raw_loans):
            await _reject(socket, WS_CLOSE_BAD_REQUEST, "bad_loan_ids")
            return

        try:
            loans = _store(settings).bind(session_id, [str(item) for item in raw_loans])
        except TooManyLoansForSession:
            await _reject(socket, WS_CLOSE_BAD_REQUEST, "too_many_loans_for_session")
            return

        await socket.send_text(
            json.dumps({"type": "ready", "session_id": session_id, "loan_ids": loans})
        )
        logger.info("socket open", extra={"session": redact_session_id(session_id)})

        while True:
            frame = await _read_frame(socket, max_bytes=settings.ws_max_frame_bytes)
            if frame is None:
                return

            message = str(frame.get("message") or "")
            if not message or len(message) > MAX_MESSAGE_CHARS:
                await socket.send_text(json.dumps({"type": "error", "reason": "bad_message"}))
                continue

            response = await _run(settings, session_id, message, loans)
            await socket.send_text(json.dumps({"type": "start"}))
            for piece in _chunk(response.reply):
                await socket.send_text(json.dumps({"type": "token", "text": piece}))
            await socket.send_text(
                json.dumps(
                    {
                        "type": "done",
                        "intent": response.intent,
                        "outcome": response.outcome,
                        "refusal_code": response.refusal_code,
                        "tool_calls": response.tool_calls,
                        "awaiting_confirmation": response.awaiting_confirmation,
                    }
                )
            )
    except WebSocketDisconnect:
        return
    finally:
        await limiter.release()


def build_assistant_app() -> FastAPI:
    """The assistant as its own ASGI app, so it carries its own middleware.

    Mounted at ``/assistant`` by :mod:`assistant.mount`, which is where the
    ``/assistant`` prefix on the public paths comes from. Keeping it a separate
    app means it gets the ledger's request logging without the ledger app
    logging the same request a second time, and it reuses the ledger's error
    handlers so both halves of the service answer with one error envelope.
    """
    from fastapi.exceptions import RequestValidationError

    from app.errors import ApiError
    from app.logging_setup import RequestLogMiddleware
    from app.main import handle_api_error, handle_validation_error

    application = FastAPI(
        title="installment-ledger-assistant",
        version="0.1.0",
        summary="A conversational layer over the loan and payments API.",
    )
    application.add_middleware(RequestLogMiddleware, logger_name="assistant.request")
    application.add_exception_handler(ApiError, handle_api_error)  # type: ignore[arg-type]
    application.add_exception_handler(
        RequestValidationError,
        handle_validation_error,  # type: ignore[arg-type]
    )
    application.include_router(router)
    return application
