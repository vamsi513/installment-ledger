"""API key authentication on the ledger endpoints.

The key is read from the environment per request, so these tests set it with
``monkeypatch`` and the server running in the background thread picks it up
without being restarted.
"""

from __future__ import annotations

import httpx
import pytest

from app.auth import require_api_key, verify_production_config
from app.errors import Unauthorized
from tests.helpers import DEFAULT_LOAN, new_key

KEY = "ledger-test-key"


def test_a_request_is_allowed_when_no_key_is_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LEDGER_API_KEY", raising=False)
    assert require_api_key(None) is None


def test_a_blank_key_is_treated_as_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEDGER_API_KEY", "   ")
    assert require_api_key(None) is None


def test_the_configured_key_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEDGER_API_KEY", KEY)
    assert require_api_key(KEY) is None


def test_a_wrong_key_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEDGER_API_KEY", KEY)
    with pytest.raises(Unauthorized):
        require_api_key("not-the-key")


def test_a_key_that_is_a_prefix_of_the_real_one_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LEDGER_API_KEY", KEY)
    with pytest.raises(Unauthorized):
        require_api_key(KEY[:-1])


def test_a_missing_header_is_rejected_when_a_key_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LEDGER_API_KEY", KEY)
    with pytest.raises(Unauthorized):
        require_api_key(None)


def test_a_non_ascii_header_is_rejected_rather_than_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """compare_digest raises TypeError on non ASCII str, so the bytes are compared."""
    monkeypatch.setenv("LEDGER_API_KEY", KEY)
    with pytest.raises(Unauthorized):
        require_api_key("kéy-with-an-accent")


def test_production_without_a_key_refuses_to_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEDGER_ENV", "production")
    monkeypatch.delenv("LEDGER_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="LEDGER_API_KEY"):
        verify_production_config()


def test_production_with_a_key_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEDGER_ENV", "production")
    monkeypatch.setenv("LEDGER_API_KEY", KEY)
    assert verify_production_config() is None


def test_development_without_a_key_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LEDGER_ENV", raising=False)
    monkeypatch.delenv("LEDGER_API_KEY", raising=False)
    assert verify_production_config() is None


def test_health_stays_open_when_a_key_is_configured(
    client: httpx.Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEDGER_API_KEY", KEY)
    response = client.get("/health")
    assert response.status_code == 200


def test_a_protected_endpoint_is_rejected_without_the_key(
    client: httpx.Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEDGER_API_KEY", KEY)
    response = client.post("/loans", json=DEFAULT_LOAN, headers={"Idempotency-Key": new_key()})
    assert response.status_code == 401


def test_a_rejection_uses_the_standard_error_envelope(
    client: httpx.Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEDGER_API_KEY", KEY)
    response = client.get("/loans/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 401
    assert response.json() == {
        "error": {
            "code": "unauthorized",
            "message": "A valid X-API-Key header is required on this endpoint.",
            "details": {},
        }
    }


def test_a_protected_endpoint_accepts_the_configured_key(
    client: httpx.Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEDGER_API_KEY", KEY)
    response = client.post(
        "/loans",
        json=DEFAULT_LOAN,
        headers={"Idempotency-Key": new_key(), "X-API-Key": KEY},
    )
    assert response.status_code == 201, response.text


def test_endpoints_stay_open_when_no_key_is_configured(
    client: httpx.Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The development default: nothing changes for a caller that sends no key."""
    monkeypatch.delenv("LEDGER_API_KEY", raising=False)
    response = client.post("/loans", json=DEFAULT_LOAN, headers={"Idempotency-Key": new_key()})
    assert response.status_code == 201, response.text
