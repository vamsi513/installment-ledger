"""Structured request logging with a correlation id.

One JSON object per request on stdout, which is what a log shipper wants and what
`jq` can read without a parser. Every line carries a request id, so the access line
and any application line from the same request can be joined.

The id comes from an inbound `X-Request-ID` when there is one, so a trace started by
a proxy or a caller survives into these logs, and is generated otherwise. It goes
back out on the response so a client can quote it in a bug report.

Deliberately not a logging framework. The service has one process and one log
stream, and a dependency here would buy nothing.
"""

from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from contextvars import ContextVar
from typing import Any

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

REQUEST_ID_HEADER = "X-Request-ID"

# Set per request so application code can log with the same id without being handed
# the request object. asyncio copies the context into tasks spawned from it.
_request_id: ContextVar[str] = ContextVar("request_id", default="-")


def current_request_id() -> str:
    return _request_id.get()


class JsonFormatter(logging.Formatter):
    """Render a record as one JSON object.

    Anything passed through `extra` is merged in, so a caller can attach a loan id
    or an idempotency key without a bespoke formatter per call site.
    """

    _RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
        "message",
        "asctime",
        "taskName",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": current_request_id(),
        }
        for key, value in record.__dict__.items():
            if key not in self._RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    """Replace the root handlers with a single JSON handler on stdout."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    # uvicorn installs its own handlers; let its records reach the root one instead
    # so everything shares a format.
    for name in ("uvicorn", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True

    # uvicorn's access log is switched off rather than reformatted. The middleware
    # below already logs one line per request with more in it, and uvicorn's line is
    # emitted after the middleware has reset the context, so it would carry no
    # request id and read as a second, worse copy of the same event.
    access = logging.getLogger("uvicorn.access")
    access.handlers = []
    access.propagate = False
    access.disabled = True


class RequestLogMiddleware(BaseHTTPMiddleware):
    """Assign a request id, log one line per request, echo the id back."""

    def __init__(self, app: ASGIApp, logger_name: str = "ledger.request") -> None:
        super().__init__(app)
        self._log = logging.getLogger(logger_name)

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        incoming = request.headers.get(REQUEST_ID_HEADER, "").strip()
        # Bound to keep a hostile header out of every log line downstream.
        request_id = incoming[:64] if incoming else uuid.uuid4().hex
        token = _request_id.set(request_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            self._log.exception(
                "request failed",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                },
            )
            _request_id.reset(token)
            raise

        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        self._log.info(
            "request",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": duration_ms,
                # Useful for spotting a retry storm. The key itself is a caller
                # supplied opaque string, so only its presence is recorded.
                "idempotent": "Idempotency-Key" in request.headers,
            },
        )
        response.headers[REQUEST_ID_HEADER] = request_id
        _request_id.reset(token)
        return response
