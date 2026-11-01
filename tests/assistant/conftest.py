"""Fixtures for the assistant tests.

``tests/assistant/unit`` needs no LLM and no network. ``tests/assistant/integration``
needs no LLM either, because it runs the fake model client, but it does use a
real Postgres, a real ledger over loopback HTTP and a real MCP subprocess.

The ledger fixtures come from ``tests/conftest.py`` unchanged.

These tests need the optional ``assistant`` extra. The ledger's own CI job
installs ``requirements.txt`` alone and then runs plain ``pytest``, which reaches
this directory because ``testpaths`` is ``tests``. Without the guard below, that
job dies during collection on ``ModuleNotFoundError``, which is a failure of the
ledger's build caused by an optional package it does not use.

The guard has to stop collection rather than mark tests skipped, because the
import that fails happens while the module is being imported, before there is a
test item to attach a skip to. ``pytest_report_collectionfinish`` prints the
reason so a run that quietly collects nothing here still says why.
"""

from __future__ import annotations

import dataclasses
import importlib.util
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

# Imported at module import time by the tests, or by the assistant modules they
# import. Not the whole extra: ``openai`` is reached lazily inside a function and
# never by a test, so its absence does not stop anything being collected.
REQUIRED_MODULES = ("langgraph", "mcp", "websockets")


def _missing_modules() -> tuple[str, ...]:
    return tuple(name for name in REQUIRED_MODULES if importlib.util.find_spec(name) is None)


MISSING_MODULES = _missing_modules()

SKIP_REASON = (
    "assistant tests not collected: the assistant extra is not installed "
    f"(missing {', '.join(MISSING_MODULES) or 'nothing'}). "
    "Install it with: pip install -r requirements-assistant.txt"
)


def pytest_ignore_collect(collection_path: Path, config: pytest.Config) -> bool | None:
    """Keep this directory out of collection when the extra is absent."""
    if MISSING_MODULES:
        return True
    return None


def pytest_report_collectionfinish(config: pytest.Config, items: list[pytest.Item]) -> list[str]:
    """Say so, rather than letting the directory vanish without comment."""
    if MISSING_MODULES:
        return [SKIP_REASON]
    return []


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


#: Thresholds for the review tests, chosen so the ordinary 30000 cent payment
#: the other tests use stays under the amount rule and a 50000 cent one does not.
REVIEW_THRESHOLD_CENTS = 40_000
REVIEW_MAX_PREPARES = 3
REVIEW_WINDOW_SECONDS = 3_600
TEST_REVIEWER_ID = "reviewer-under-test"
TEST_REVIEW_TOKEN = "review-test-token"


def with_review(
    settings: Settings,
    *,
    enabled: bool = True,
    threshold_cents: int = REVIEW_THRESHOLD_CENTS,
    max_prepares: int = REVIEW_MAX_PREPARES,
    window_seconds: int = REVIEW_WINDOW_SECONDS,
    reviewer_id: str = TEST_REVIEWER_ID,
    review_api_token: str | None = TEST_REVIEW_TOKEN,
) -> Settings:
    """The same settings with the review queue switched on."""
    return dataclasses.replace(
        settings,
        review_enabled=enabled,
        review_threshold_cents=threshold_cents,
        review_max_prepares=max_prepares,
        review_window_seconds=window_seconds,
        reviewer_id=reviewer_id,
        review_api_token=review_api_token,
    )


@pytest.fixture
def review_settings(assistant_settings: Settings) -> Settings:
    return with_review(assistant_settings)


@pytest.fixture
def review_settings_offline(tmp_path: Path) -> Settings:
    """Review settings that need no ledger server.

    The reviewer interface only touches the review store, so these tests do not
    start the ledger. The URL points at a closed port on purpose, so a test that
    accidentally reaches for the ledger fails rather than passing quietly.
    """
    return with_review(make_settings(ledger_base_url="http://127.0.0.1:1", state_dir=tmp_path))


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
