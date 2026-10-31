"""Fixtures for the assistant tests.

``tests/assistant/unit`` needs no LLM and no network. ``tests/assistant/integration``
needs no LLM either, because it runs the fake model client, but it does use a
real Postgres, a real ledger over loopback HTTP and a real MCP subprocess.

The ledger fixtures come from ``tests/conftest.py`` unchanged.
"""

from __future__ import annotations

import socket
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import uvicorn

from assistant.config import Settings

SERVER_START_TIMEOUT_SECONDS = 30
TEST_WS_TOKEN = "assistant-test-token"


@pytest.fixture
def anyio_backend() -> str:
    """anyio's pytest plugin drives the async tests; asyncio is the only backend used."""
    return "asyncio"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def make_settings(
    *,
    ledger_base_url: str,
    state_dir: Path,
    max_payment_cents: int = 60_000,
    pending_ttl_seconds: int = 300,
    ledger_timeout_seconds: float = 10.0,
    ws_auth_token: str | None = TEST_WS_TOKEN,
    ws_max_frame_bytes: int = 512,
    ws_max_connections: int = 2,
    llm_provider: str = "fake",
) -> Settings:
    """Settings built explicitly, so a test never depends on the ambient environment."""
    return Settings(
        ledger_base_url=ledger_base_url,
        ledger_timeout_seconds=ledger_timeout_seconds,
        max_payment_cents=max_payment_cents,
        pending_ttl_seconds=pending_ttl_seconds,
        max_message_chars=2_000,
        state_db_path=str(state_dir / f"state-{uuid.uuid4().hex}.sqlite3"),
        llm_provider=llm_provider,
        model="fake-classifier",
        openai_api_key=None,
        ws_auth_token=ws_auth_token,
        ws_max_frame_bytes=ws_max_frame_bytes,
        ws_max_connections=ws_max_connections,
        enabled=True,
    )


@pytest.fixture
def assistant_settings(tmp_path: Path, base_url: str) -> Settings:
    """Settings pointing at the ledger test server, with the fake model client."""
    return make_settings(ledger_base_url=base_url, state_dir=tmp_path)


def serve_in_thread(app: object, port: int) -> Iterator[str]:
    """Run an ASGI app on a background thread and yield its base URL."""
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")  # type: ignore[arg-type]
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + SERVER_START_TIMEOUT_SECONDS
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("the test server did not start in time")
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture(scope="session")
def mounted_app_url(migrated_database: None) -> Iterator[str]:
    """``assistant.asgi``-shaped app: the untouched ledger plus the assistant.

    Built here rather than imported from ``assistant.asgi`` so the port, and
    therefore ``LEDGER_BASE_URL``, is known before the settings are resolved.
    """
    import os

    from app.main import app as ledger_app
    from assistant.config import get_settings
    from assistant.mount import mount_assistant

    port = free_port()
    previous = {
        key: os.environ.get(key)
        for key in (
            "LEDGER_BASE_URL",
            "ASSISTANT_LLM",
            "ASSISTANT_WS_TOKEN",
            "ASSISTANT_WS_MAX_FRAME_BYTES",
            "ASSISTANT_WS_MAX_CONNECTIONS",
            "ASSISTANT_MAX_PAYMENT_CENTS",
            "ASSISTANT_STATE_DB",
        )
    }
    os.environ.update(
        {
            "LEDGER_BASE_URL": f"http://127.0.0.1:{port}",
            "ASSISTANT_LLM": "fake",
            "ASSISTANT_WS_TOKEN": TEST_WS_TOKEN,
            "ASSISTANT_WS_MAX_FRAME_BYTES": "512",
            "ASSISTANT_WS_MAX_CONNECTIONS": "2",
            "ASSISTANT_MAX_PAYMENT_CENTS": "60000",
            "ASSISTANT_STATE_DB": str(
                Path(__file__).resolve().parent / f".pytest-state-{uuid.uuid4().hex}.sqlite3"
            ),
        }
    )
    get_settings.cache_clear()
    state_db = os.environ["ASSISTANT_STATE_DB"]

    try:
        yield from serve_in_thread(mount_assistant(ledger_app), port)
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        get_settings.cache_clear()
        for suffix in ("", "-checkpoints", "-wal", "-shm"):
            Path(state_db + suffix).unlink(missing_ok=True)


@pytest.fixture
def mounted_client(mounted_app_url: str) -> Iterator[httpx.Client]:
    with httpx.Client(base_url=mounted_app_url, timeout=60.0) as client:
        yield client
