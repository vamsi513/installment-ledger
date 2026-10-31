"""Amount extraction in the fake model client. No LLM, no network, database required."""

from __future__ import annotations

import json

import pytest

from assistant.llm.base import Message
from assistant.llm.fake import FakeLlmClient, extract_amount_cents


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("pay 4500 cents", 4500),
        ("pay $45", 4500),
        ("pay $45.67", 4567),
        ("pay 45.6 dollars", 4560),
        ("pay 1,200 cents", 1200),
        ("pay -5 dollars", -500),
        ("pay -500 cents", -500),
        ("pay minus 300 cents", -300),
        ("what is my balance", None),
        ("pay something", None),
    ],
)
def test_amount_extraction_keeps_the_sign(text: str, expected: int | None) -> None:
    assert extract_amount_cents(text) == expected


@pytest.mark.anyio
async def test_the_fake_returns_the_shape_the_prompt_asks_for() -> None:
    client = FakeLlmClient()
    reply = await client.complete([Message(role="user", content="pay 4500 cents")])
    parsed = json.loads(reply.text)
    assert parsed == {"intent": "payment_request", "amount_cents": 4500, "wants_ledger": False}
    assert reply.total_tokens == 0


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("text", "intent"),
    [
        ("what is my balance", "read_question"),
        ("show me the ledger history", "read_question"),
        ("pay 4500 cents", "payment_request"),
        ("pay something off", "read_question"),
        ("tell me a joke", "out_of_scope"),
    ],
)
async def test_the_fake_classifies_by_keyword(text: str, intent: str) -> None:
    reply = await FakeLlmClient().complete([Message(role="user", content=text)])
    assert json.loads(reply.text)["intent"] == intent
