"""Mounting the assistant without touching the ledger app.

No LLM, no network, database required. The ASGI apps here are exercised in
process with httpx's ASGI transport, so nothing binds a socket.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from assistant.mount import ASSISTANT_PREFIX, mount_assistant
from tests.assistant.conftest import make_settings


@pytest.fixture
def settings(tmp_path: Path):
    return make_settings(ledger_base_url="http://127.0.0.1:1", state_dir=tmp_path)


def build_probe_app(name: str, events: list[str]) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        events.append(f"{name}:startup")
        yield
        events.append(f"{name}:shutdown")

    application = FastAPI(lifespan=lifespan)

    @application.get("/probe")
    def probe() -> dict[str, str]:
        return {"app": name}

    return application


@pytest.mark.anyio
async def test_the_outer_app_runs_both_mounted_lifespans(settings) -> None:
    """Starlette does not run a mounted app's lifespan, so the outer app must."""
    events: list[str] = []
    inner = build_probe_app("ledger", events)
    outer = mount_assistant(inner, settings=settings)

    assert events == []
    async with outer.router.lifespan_context(outer):
        assert events == ["ledger:startup"]
    assert events == ["ledger:startup", "ledger:shutdown"]


@pytest.mark.anyio
async def test_requests_work_after_the_lifespan_has_started(settings) -> None:
    events: list[str] = []
    outer = mount_assistant(build_probe_app("ledger", events), settings=settings)

    async with (
        outer.router.lifespan_context(outer),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=outer), base_url="http://testserver"
        ) as client,
    ):
        response = await client.get("/probe")
    assert response.status_code == 200
    assert response.json() == {"app": "ledger"}
    assert events == ["ledger:startup", "ledger:shutdown"]


@pytest.mark.anyio
async def test_ledger_paths_still_reach_the_ledger_app(settings) -> None:
    events: list[str] = []
    outer = mount_assistant(build_probe_app("ledger", events), settings=settings)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=outer), base_url="http://testserver"
    ) as client:
        assert (await client.get("/probe")).json() == {"app": "ledger"}
        assert (await client.get(f"{ASSISTANT_PREFIX}/health")).status_code == 200


@pytest.mark.anyio
async def test_the_assistant_is_reachable_under_its_own_prefix(settings) -> None:
    outer = mount_assistant(build_probe_app("ledger", []), settings=settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=outer), base_url="http://testserver"
    ) as client:
        body = (await client.get(f"{ASSISTANT_PREFIX}/health")).json()
    assert body["status"] == "ok"
    assert body["llm_provider"] in {"fake", "openai"}


def test_the_ledger_app_itself_gains_no_assistant_routes(settings) -> None:
    """app.main:app has to behave exactly as it did before this package existed."""
    from app.main import app as ledger_app

    before = {getattr(route, "path", None) for route in ledger_app.routes}
    mount_assistant(ledger_app, settings=settings)
    after = {getattr(route, "path", None) for route in ledger_app.routes}

    assert before == after
    assert not any(str(path).startswith("/assistant") for path in after if path)


def test_the_kill_switch_hands_the_ledger_app_straight_back(tmp_path: Path) -> None:
    from app.main import app as ledger_app

    disabled = make_settings(ledger_base_url="http://127.0.0.1:1", state_dir=tmp_path).__class__(
        **{
            **make_settings(ledger_base_url="http://127.0.0.1:1", state_dir=tmp_path).__dict__,
            "enabled": False,
        }
    )
    assert mount_assistant(ledger_app, settings=disabled) is ledger_app
