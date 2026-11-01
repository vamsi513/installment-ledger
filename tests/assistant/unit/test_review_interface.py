"""The reviewer interfaces: the HTTP endpoints and the command line.

No LLM, no network, database required. The app is driven in process through
httpx's ASGI transport, and the review store is a SQLite file in a temporary
directory, so nothing here opens a socket or needs the ledger.
"""

from __future__ import annotations

import httpx
import pytest

from assistant.api.routes import build_assistant_app
from assistant.config import Settings
from assistant.pending import ActionStatus
from assistant.review import ReviewService
from assistant.review.__main__ import main as review_cli
from assistant.review.audit import AuditAction
from tests.assistant.conftest import TEST_REVIEW_TOKEN, TEST_REVIEWER_ID, with_review

LOAN = "11111111-1111-4111-8111-111111111111"
HELD_AMOUNT = 50_000


@pytest.fixture
def service(review_settings_offline: Settings) -> ReviewService:
    return ReviewService(review_settings_offline)


@pytest.fixture
def held_id(service: ReviewService) -> str:
    """One prepared payment, held for review."""
    action = service.store.create(
        loan_id=LOAN, amount_cents=HELD_AMOUNT, status=ActionStatus.NEEDS_REVIEW
    )
    service.record_prepared(action, service.decide(loan_id=LOAN, amount_cents=HELD_AMOUNT))
    return action.pending_action_id


@pytest.fixture
def api(review_settings_offline: Settings, monkeypatch: pytest.MonkeyPatch):
    """The assistant app with the review settings in place."""
    monkeypatch.setattr("assistant.api.review_routes.get_settings", lambda: review_settings_offline)
    return build_assistant_app()


def client_for(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        headers={"X-Review-Token": TEST_REVIEW_TOKEN},
    )


# ------------------------------------------------------------------ the token


@pytest.mark.anyio
async def test_a_request_without_the_token_is_refused(api) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api), base_url="http://testserver"
    ) as client:
        response = await client.get("/review/held")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "bad_review_token"


@pytest.mark.anyio
async def test_a_request_with_the_wrong_token_is_refused(api) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api),
        base_url="http://testserver",
        headers={"X-Review-Token": "not-the-token"},
    ) as client:
        response = await client.get("/review/held")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "bad_review_token"


@pytest.mark.anyio
async def test_with_no_token_configured_the_endpoints_are_closed(
    review_settings_offline: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same default the streaming socket takes."""
    closed = with_review(review_settings_offline, review_api_token=None)
    monkeypatch.setattr("assistant.api.review_routes.get_settings", lambda: closed)
    app = build_assistant_app()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        headers={"X-Review-Token": "anything"},
    ) as client:
        response = await client.get("/review/held")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "review_token_not_configured"


@pytest.mark.anyio
async def test_with_the_queue_off_the_endpoints_say_so(
    review_settings_offline: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    off = with_review(review_settings_offline, enabled=False)
    monkeypatch.setattr("assistant.api.review_routes.get_settings", lambda: off)
    app = build_assistant_app()

    async with client_for(app) as client:
        response = await client.get("/review/held")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "review_not_enabled"


# ------------------------------------------------------------------- the queue


@pytest.mark.anyio
async def test_the_held_list_shows_the_policy_and_the_waiting_payments(
    api, held_id: str, review_settings_offline: Settings
) -> None:
    async with client_for(api) as client:
        body = (await client.get("/review/held")).json()

    assert body["count"] == 1
    assert body["threshold_cents"] == review_settings_offline.review_threshold_cents
    assert body["max_prepares_in_window"] == review_settings_offline.review_max_prepares
    held = body["held"][0]
    assert held["pending_action_id"] == held_id
    assert held["loan_id"] == LOAN
    assert held["amount_cents"] == HELD_AMOUNT
    assert held["status"] == "needs_review"
    assert held["expired"] is False


@pytest.mark.anyio
async def test_approving_over_http_records_the_configured_reviewer(
    api, held_id: str, service: ReviewService
) -> None:
    async with client_for(api) as client:
        response = await client.post(
            f"/review/{held_id}/approve", json={"reason": "spoke to the borrower"}
        )

    body = response.json()
    assert response.status_code == 200
    assert body["outcome"] == "approved"
    assert body["changed"] is True
    assert body["reviewer_id"] == TEST_REVIEWER_ID
    assert body["action"]["status"] == "approved"

    trail = service.entries_for(held_id)
    approval = next(entry for entry in trail if entry.action is AuditAction.APPROVED)
    assert approval.actor == TEST_REVIEWER_ID
    assert approval.reason == "spoke to the borrower"


@pytest.mark.anyio
async def test_approving_twice_over_http_changes_nothing_the_second_time(
    api, held_id: str, service: ReviewService
) -> None:
    async with client_for(api) as client:
        first = await client.post(f"/review/{held_id}/approve", json={})
        second = await client.post(f"/review/{held_id}/approve", json={})

    assert first.json()["changed"] is True
    assert second.status_code == 200
    assert second.json()["outcome"] == "already_approved"
    assert second.json()["changed"] is False
    assert len([e for e in service.entries_for(held_id) if e.action is AuditAction.APPROVED]) == 1


@pytest.mark.anyio
async def test_rejecting_requires_a_reason(api, held_id: str) -> None:
    async with client_for(api) as client:
        missing = await client.post(f"/review/{held_id}/reject", json={})
        empty = await client.post(f"/review/{held_id}/reject", json={"reason": ""})
        good = await client.post(f"/review/{held_id}/reject", json={"reason": "not expected"})

    assert missing.status_code == 422
    assert missing.json()["error"]["code"] == "validation_error"
    assert empty.status_code == 422
    assert good.status_code == 200
    assert good.json()["outcome"] == "rejected"


@pytest.mark.anyio
async def test_an_unknown_payment_is_a_not_found(api) -> None:
    async with client_for(api) as client:
        response = await client.post("/review/nope/approve", json={})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


@pytest.mark.anyio
async def test_a_payment_that_was_never_held_cannot_be_decided(api, service: ReviewService) -> None:
    ready = service.store.create(loan_id=LOAN, amount_cents=100)
    async with client_for(api) as client:
        response = await client.post(f"/review/{ready.pending_action_id}/approve", json={})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "not_held"


@pytest.mark.anyio
async def test_an_unknown_field_is_rejected(api, held_id: str) -> None:
    async with client_for(api) as client:
        response = await client.post(
            f"/review/{held_id}/approve", json={"reason": "fine", "reviewer": "someone else"}
        )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


@pytest.mark.anyio
async def test_the_audit_endpoint_returns_the_trail_in_order(api, held_id: str) -> None:
    async with client_for(api) as client:
        await client.post(f"/review/{held_id}/approve", json={"reason": "fine"})
        body = (await client.get(f"/review/{held_id}/audit")).json()

    assert body["pending_action_id"] == held_id
    assert [entry["action"] for entry in body["entries"]] == [
        "prepared",
        "held_for_review",
        "approved",
    ]
    assert body["entries"][-1]["actor"] == TEST_REVIEWER_ID
    assert body["entries"][-1]["old_status"] == "needs_review"
    assert body["entries"][-1]["new_status"] == "approved"


@pytest.mark.anyio
async def test_a_reviewer_id_is_required_to_decide(
    review_settings_offline: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    nameless = with_review(review_settings_offline, reviewer_id="  ")
    service = ReviewService(nameless)
    action = service.store.create(
        loan_id=LOAN, amount_cents=HELD_AMOUNT, status=ActionStatus.NEEDS_REVIEW
    )
    monkeypatch.setattr("assistant.api.review_routes.get_settings", lambda: nameless)
    app = build_assistant_app()

    async with client_for(app) as client:
        response = await client.post(f"/review/{action.pending_action_id}/approve", json={})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "reviewer_id_not_configured"


# ----------------------------------------------------------------- the command line


@pytest.fixture
def cli(review_settings_offline: Settings, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("assistant.review.__main__.get_settings", lambda: review_settings_offline)
    return review_cli


def test_the_cli_lists_the_policy_and_an_empty_queue(cli, capsys) -> None:
    assert cli(["list"]) == 0
    printed = capsys.readouterr().out
    assert "Nothing is waiting for review." in printed
    assert "hold above 40000 cents" in printed


def test_the_cli_lists_a_held_payment(cli, held_id: str, capsys) -> None:
    assert cli(["list"]) == 0
    printed = capsys.readouterr().out
    assert "1 waiting for review" in printed
    assert held_id in printed
    assert f"{HELD_AMOUNT} cents" in printed


def test_the_cli_approves_and_says_a_confirmation_is_still_needed(
    cli, held_id: str, service: ReviewService, capsys
) -> None:
    assert cli(["approve", held_id, "--reason", "checked"]) == 0
    printed = capsys.readouterr().out
    assert f"{held_id} approved by {TEST_REVIEWER_ID}." in printed
    assert "still has to confirm it" in printed
    assert service.store.get(held_id).status is ActionStatus.APPROVED


def test_the_cli_reports_a_second_approval_as_a_no_op(cli, held_id: str, capsys) -> None:
    cli(["approve", held_id])
    capsys.readouterr()
    assert cli(["approve", held_id]) == 0
    assert "was already approved; nothing changed." in capsys.readouterr().out


def test_the_cli_rejects_with_a_reason(cli, held_id: str, service: ReviewService, capsys) -> None:
    assert cli(["reject", held_id, "--reason", "borrower did not ask"]) == 0
    assert f"{held_id} rejected" in capsys.readouterr().out
    assert service.store.get(held_id).status is ActionStatus.REJECTED
    trail = service.entries_for(held_id)
    assert trail[-1].reason == "borrower did not ask"


def test_the_cli_refuses_an_unknown_payment(cli, capsys) -> None:
    assert cli(["approve", "nope"]) == 1
    assert "no prepared payment with that id" in capsys.readouterr().err


def test_the_cli_prints_the_audit_trail(cli, held_id: str, capsys) -> None:
    cli(["approve", held_id, "--reason", "fine"])
    capsys.readouterr()
    assert cli(["audit", held_id]) == 0
    printed = capsys.readouterr().out
    assert "3 audit entries" in printed
    assert "prepared" in printed
    assert "held_for_review" in printed
    assert f"{TEST_REVIEWER_ID}" in printed
    assert "needs_review -> approved" in printed
    assert "reason: fine" in printed


def test_the_cli_says_when_there_is_no_trail(cli, capsys) -> None:
    assert cli(["audit", "nope"]) == 0
    assert "No audit entries for nope." in capsys.readouterr().out


def test_rejecting_from_the_cli_needs_a_reason(cli) -> None:
    with pytest.raises(SystemExit):
        cli(["reject", "some-id"])
