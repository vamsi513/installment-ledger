"""Test fixtures.

Everything runs against a real Postgres, reached through ``DATABASE_URL``.
Tests drive the service over HTTP against a uvicorn server in a background
thread rather than through an in process test client, because the concurrency
tests need genuinely parallel requests on separate database connections.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import uvicorn
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, text

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER_START_TIMEOUT_SECONDS = 30

TABLES_TO_CLEAR = (
    "payment_allocations",
    "payments",
    "ledger_entries",
    "installments",
    "loans",
    "idempotency_keys",
)


@pytest.fixture(scope="session")
def migrated_database() -> None:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    command.upgrade(config, "head")


@pytest.fixture(scope="session")
def engine(migrated_database: None) -> Engine:
    from app.db import get_engine

    return get_engine()


@pytest.fixture(autouse=True)
def clear_tables(engine: Engine) -> None:
    """Start every test from an empty database.

    TRUNCATE does not fire row level triggers, so this does not trip the
    ledger balance trigger on the way out.
    """
    with engine.begin() as connection:
        connection.execute(text(f"TRUNCATE {', '.join(TABLES_TO_CLEAR)} CASCADE"))


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture(scope="session")
def base_url(migrated_database: None) -> Iterator[str]:
    from app.main import app

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + SERVER_START_TIMEOUT_SECONDS
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("the test server did not start in time")
        time.sleep(0.02)

    yield f"http://127.0.0.1:{port}"

    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture
def client(base_url: str) -> Iterator[httpx.Client]:
    with httpx.Client(base_url=base_url, timeout=30.0) as http_client:
        yield http_client
