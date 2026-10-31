"""What the agent says when the ledger misbehaves.

A timeout, a 5xx and a 409 are served by the stub in
``tests/assistant/stub_ledger.py``, because the real ledger will not produce
them on demand. The fake model client still does the classifying, so there is no
LLM here either.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from assistant.agent import TurnResult, run_turn
from assistant.agent.state import Outcome
from tests.assistant.conftest import free_port, make_settings, serve_in_thread
from tests.assistant.stub_ledger import PaymentBehaviour, build_stub_ledger, fixed_loan

LOAN_ID = "33333333-3333-4333-8333-333333333333"


def count(engine: Engine, table: str) -> int:
    with engine.connect() as connection:
        return int(connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one())


@contextmanager
def stub_ledger(behaviour: PaymentBehaviour, state_dir: Path, timeout: float = 1.0):
    """Run a misbehaving stub ledger and yield settings pointing at it."""
    port = free_port()
    app = build_stub_ledger(loan=fixed_loan(LOAN_ID), payment_behaviour=behaviour)
    urls = serve_in_thread(app, port)
    base_url = next(urls)
    try:
        yield make_settings(
            ledger_base_url=base_url, state_dir=state_dir, ledger_timeout_seconds=timeout
        )
    finally:
        urls.close()


async def prepare_then_confirm(settings) -> tuple[TurnResult, TurnResult]:
    session = f"stub-{uuid.uuid4().hex[:8]}"
    prepared = await run_turn(
        session_id=session,
        message="pay 100 dollars",
        bound_loan_ids=[LOAN_ID],
        settings=settings,
    )
    confirmed = await run_turn(
        session_id=session, message="yes", bound_loan_ids=[LOAN_ID], settings=settings
    )
    return prepared, confirmed


@pytest.mark.anyio
async def test_a_write_that_times_out_reports_an_unknown_outcome(
    tmp_path: Path, engine: Engine
) -> None:
    with stub_ledger("timeout", tmp_path) as settings:
        prepared, confirmed = await prepare_then_confirm(settings)

    assert prepared.outcome == Outcome.PREPARED.value
    assert confirmed.outcome == Outcome.UNKNOWN.value
    reply = confirmed.reply.lower()
    assert "do not know" in reply
    assert "nothing was paid" not in reply
    assert not reply.startswith("paid")
    assert "check your balance" in reply
    # The pending action is kept, so a later retry reuses the same key.
    assert confirmed.has_pending_action is True
    assert count(engine, "payments") == 0


@pytest.mark.anyio
async def test_a_write_the_ledger_could_not_serve_says_nothing_was_paid(
    tmp_path: Path, engine: Engine
) -> None:
    with stub_ledger("server_error", tmp_path) as settings:
        _, confirmed = await prepare_then_confirm(settings)

    assert confirmed.outcome == Outcome.UNAVAILABLE.value
    assert "nothing was paid" in confirmed.reply.lower()
    assert confirmed.has_pending_action is True
    assert count(engine, "payments") == 0


@pytest.mark.anyio
async def test_a_ledger_idempotency_conflict_is_explained_without_claiming_success(
    tmp_path: Path, engine: Engine
) -> None:
    with stub_ledger("conflict", tmp_path) as settings:
        _, confirmed = await prepare_then_confirm(settings)

    assert confirmed.outcome == Outcome.REFUSED.value
    reply = confirmed.reply
    assert "already seen this confirmation" in reply
    assert "nothing new was paid" in reply
    assert "check the balance" in reply
    assert count(engine, "payments") == 0


@pytest.mark.anyio
async def test_a_read_against_an_unreachable_ledger_admits_it(tmp_path: Path) -> None:
    """Nothing is listening on the port, so the client cannot connect at all."""
    settings = make_settings(
        ledger_base_url=f"http://127.0.0.1:{free_port()}",
        state_dir=tmp_path,
        ledger_timeout_seconds=1.0,
    )
    result = await run_turn(
        session_id="unreachable",
        message="what is my balance",
        bound_loan_ids=[LOAN_ID],
        settings=settings,
    )
    assert result.outcome in {Outcome.UNAVAILABLE.value, Outcome.UNKNOWN.value}
    assert "could not reach the ledger" in result.reply.lower()
