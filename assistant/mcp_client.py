"""The agent's only route to the tools.

A session is one ``python -m assistant.mcp_server`` subprocess speaking MCP over
stdio. The agent discovers what exists with ``tools/list`` and runs things with
``tools/call``; it never imports the tool functions, so the tool boundary is a
real process boundary and the tool schemas are the contract.

One session is opened per conversational turn and closed at the end of it. That
costs a subprocess spawn per turn, which is the price of keeping the anyio
cancel scope inside a single task and of never sharing a session between
concurrent turns.
"""

from __future__ import annotations

import logging
import os
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass
from types import TracebackType
from typing import Any

from mcp import ClientSession, StdioServerParameters, stdio_client
from mcp.types import TextContent

from .config import Settings

logger = logging.getLogger("assistant.tools")

SERVER_MODULE = "assistant.mcp_server"


class ToolTransportError(RuntimeError):
    """The tool server could not be started or stopped answering."""


@dataclass(frozen=True)
class ToolCallResult:
    """What came back from ``tools/call``."""

    name: str
    payload: dict[str, Any]
    is_error: bool
    text: str

    @property
    def status(self) -> str:
        """The tool's own status field, or ``error`` when the call itself failed."""
        if self.is_error:
            return "error"
        return str(self.payload.get("status", "ok"))


def _server_parameters(settings: Settings) -> StdioServerParameters:
    # The child needs the same settings as the parent, so they are passed
    # explicitly rather than inherited by accident. OPENAI_API_KEY is not among
    # them; the tool server never talks to a model.
    env = dict(os.environ)
    env.pop("OPENAI_API_KEY", None)
    env.update(
        {
            "LEDGER_BASE_URL": settings.ledger_base_url,
            "ASSISTANT_STATE_DB": settings.state_db_path,
            "ASSISTANT_MAX_PAYMENT_CENTS": str(settings.max_payment_cents),
            "ASSISTANT_PENDING_TTL_SECONDS": str(settings.pending_ttl_seconds),
            "ASSISTANT_LEDGER_TIMEOUT_SECONDS": str(settings.ledger_timeout_seconds),
            "PYTHONPATH": os.pathsep.join(
                [
                    os.getcwd(),
                    *(
                        os.environ.get("PYTHONPATH", "").split(os.pathsep)
                        if os.environ.get("PYTHONPATH")
                        else []
                    ),
                ]
            ),
        }
    )
    return StdioServerParameters(
        command=sys.executable, args=["-m", SERVER_MODULE], env=env, cwd=os.getcwd()
    )


class ToolGateway:
    """An open MCP session, used as an async context manager."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._stack = AsyncExitStack()
        self._session: ClientSession | None = None
        self._tool_names: tuple[str, ...] = ()

    async def __aenter__(self) -> ToolGateway:
        try:
            read, write = await self._stack.enter_async_context(
                stdio_client(_server_parameters(self._settings))
            )
            session = await self._stack.enter_async_context(
                ClientSession(
                    read, write, read_timeout_seconds=self._settings.ledger_timeout_seconds * 3
                )
            )
            await session.initialize()
        except Exception as exc:
            await self._stack.aclose()
            raise ToolTransportError(
                f"could not start the tool server: {type(exc).__name__}"
            ) from exc

        self._session = session
        listed = await session.list_tools()
        self._tool_names = tuple(tool.name for tool in listed.tools)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._session = None
        await self._stack.aclose()

    @property
    def tool_names(self) -> tuple[str, ...]:
        """Names from ``tools/list``, as advertised by the server."""
        return self._tool_names

    async def list_tools(self) -> list[dict[str, Any]]:
        """The full ``tools/list`` result, name, description and input schema."""
        session = self._require_session()
        listed = await session.list_tools()
        return [
            {
                "name": tool.name,
                "title": tool.title,
                "description": tool.description,
                "input_schema": tool.input_schema,
            }
            for tool in listed.tools
        ]

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolCallResult:
        """Run one tool by name through ``tools/call``."""
        session = self._require_session()
        if name not in self._tool_names:
            raise ToolTransportError(f"the tool server does not advertise a tool named {name!r}")

        try:
            result = await session.call_tool(name, arguments)
        except Exception as exc:
            raise ToolTransportError(f"tools/call {name} failed: {type(exc).__name__}") from exc

        text = "\n".join(
            block.text for block in getattr(result, "content", []) if isinstance(block, TextContent)
        )
        payload = getattr(result, "structured_content", None) or {}
        if not isinstance(payload, dict):
            payload = {}
        return ToolCallResult(
            name=name,
            payload=payload,
            is_error=bool(getattr(result, "is_error", False)),
            text=text,
        )

    def _require_session(self) -> ClientSession:
        if self._session is None:
            raise ToolTransportError("the tool session is not open")
        return self._session
