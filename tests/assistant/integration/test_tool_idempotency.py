"""Confirming the same prepared payment more than once, at the tool boundary.

This is the test that the ledger, not the agent, is what makes a payment happen
once. The agent is bypassed entirely: the calls go straight through MCP.
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import Engine, text

from assistant.config import Settings
from assistant.mcp_client import ToolGateway
from tests.helpers import create_loan


def count(engine: Engine, table: str) -> int:
    with engine.connect() as connection:
        return int(connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one())


@pytest.mark.anyio
async def test_the_tools_are_discovered_through_tools_list(assistant_settings: Settings) -> None:
    async with ToolGateway(assistant_settings) as tools:
        assert set(tools.tool_names) == {
            "get_loan_summary",
            "get_ledger_entries",
            "prepare_payment",
            "confirm_payment",
        }
        listed = {tool["name"]: tool for tool in await tools.list_tools()}

    prepare = listed["prepare_payment"]
    assert "without writing anything" in (prepare["description"] or "").lower()
    assert set(prepare["input_schema"]["properties"]) == {"loan_id", "amount_cents"}
    assert "only tool that writes" in (listed["confirm_payment"]["description"] or "").lower()


@pytest.mark.anyio
async def test_confirming_twice_posts_one_payment(
    client: httpx.Client, assistant_settings: Settings, engine: Engine
) -> None:
    loan = create_loan(client, principal_cents=120_000, installment_count=4)

    async with ToolGateway(assistant_settings) as tools:
        prepared = await tools.call(
            "prepare_payment", {"loan_id": loan["id"], "amount_cents": 45_000}
        )
        assert prepared.status == "ok"
        pending_id = prepared.payload["pending_action_id"]

        first = await tools.call("confirm_payment", {"pending_action_id": pending_id})
        second = await tools.call("confirm_payment", {"pending_action_id": pending_id})

    assert first.status == "ok"
    assert second.status == "ok"
    assert first.payload["applied"] is True
    assert second.payload["applied"] is True
    # The same payment came back both times, because the idempotency key is
    # derived from the pending action id.
    assert first.payload["payment_id"] == second.payload["payment_id"]

    assert count(engine, "payments") == 1
    assert count(engine, "payment_allocations") == 2
    final = client.get(f"/loans/{loan['id']}").json()
    assert final["paid_cents"] == 45_000
    assert client.get(f"/loans/{loan['id']}/ledger").json()["net_cents"] == 0


@pytest.mark.anyio
async def test_each_prepared_payment_is_its_own_write(
    client: httpx.Client, assistant_settings: Settings, engine: Engine
) -> None:
    """Two separate preparations are two payments, not one deduplicated one."""
    loan = create_loan(client, principal_cents=120_000, installment_count=4)

    async with ToolGateway(assistant_settings) as tools:
        first_id = (
            await tools.call("prepare_payment", {"loan_id": loan["id"], "amount_cents": 10_000})
        ).payload["pending_action_id"]
        second_id = (
            await tools.call("prepare_payment", {"loan_id": loan["id"], "amount_cents": 10_000})
        ).payload["pending_action_id"]
        assert first_id != second_id

        await tools.call("confirm_payment", {"pending_action_id": first_id})
        await tools.call("confirm_payment", {"pending_action_id": second_id})

    assert count(engine, "payments") == 2
    assert client.get(f"/loans/{loan['id']}").json()["paid_cents"] == 20_000


@pytest.mark.anyio
async def test_a_pending_action_that_does_not_exist_is_refused(
    assistant_settings: Settings, engine: Engine
) -> None:
    async with ToolGateway(assistant_settings) as tools:
        result = await tools.call("confirm_payment", {"pending_action_id": "not-a-real-id"})

    assert result.status == "rejected"
    assert result.payload["error_code"] == "pending_action_not_found"
    assert result.payload["applied"] is False
    assert count(engine, "payments") == 0


@pytest.mark.anyio
async def test_an_expired_pending_action_cannot_be_confirmed(
    client: httpx.Client, tmp_path, base_url: str, engine: Engine
) -> None:
    from tests.assistant.conftest import make_settings

    loan = create_loan(client, principal_cents=120_000, installment_count=4)
    settings = make_settings(ledger_base_url=base_url, state_dir=tmp_path, pending_ttl_seconds=0)

    async with ToolGateway(settings) as tools:
        prepared = await tools.call(
            "prepare_payment", {"loan_id": loan["id"], "amount_cents": 10_000}
        )
        result = await tools.call(
            "confirm_payment", {"pending_action_id": prepared.payload["pending_action_id"]}
        )

    assert result.status == "rejected"
    assert result.payload["error_code"] == "pending_action_expired"
    assert count(engine, "payments") == 0


@pytest.mark.anyio
async def test_the_read_tools_write_nothing(
    client: httpx.Client, assistant_settings: Settings, engine: Engine
) -> None:
    loan = create_loan(client, principal_cents=120_000, installment_count=4)

    async with ToolGateway(assistant_settings) as tools:
        for _ in range(3):
            assert (await tools.call("get_loan_summary", {"loan_id": loan["id"]})).status == "ok"
            assert (
                await tools.call("get_ledger_entries", {"loan_id": loan["id"], "limit": 5})
            ).status == "ok"
            assert (
                await tools.call("prepare_payment", {"loan_id": loan["id"], "amount_cents": 1_000})
            ).status == "ok"

    assert count(engine, "payments") == 0
    assert client.get(f"/loans/{loan['id']}").json()["paid_cents"] == 0


@pytest.mark.anyio
async def test_a_tool_the_server_does_not_advertise_cannot_be_called(
    assistant_settings: Settings,
) -> None:
    from assistant.mcp_client import ToolTransportError

    async with ToolGateway(assistant_settings) as tools:
        with pytest.raises(ToolTransportError, match="does not advertise"):
            await tools.call("delete_loan", {"loan_id": "x"})
