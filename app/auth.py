"""API key authentication for the ledger endpoints.

The key is read from the environment on every request rather than captured at
import time, so a deployment can rotate it by restarting the process and the
tests can set it per test without rebuilding the application.

Authentication is opt in. With ``LEDGER_API_KEY`` unset the checks pass and the
API is open, which is how it has always run locally and in CI. Setting
``LEDGER_ENV=production`` without a key is treated as a mistake rather than a
choice, and the service refuses to start.
"""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Header

from .config import api_key, environment
from .errors import Unauthorized

ApiKeyHeader = Annotated[str | None, Header(alias="X-API-Key")]

PRODUCTION = "production"


def require_api_key(x_api_key: ApiKeyHeader = None) -> None:
    """Reject a request that does not carry the configured API key.

    Comparison goes through :func:`hmac.compare_digest` on the encoded bytes so
    a wrong key takes the same time to reject whatever its contents, and so a
    non ASCII header cannot raise ``TypeError`` out of the comparison itself.
    The length of the supplied key still leaks, which is inherent to comparing
    strings of different sizes and not worth padding around here.
    """
    expected = api_key()
    if expected is None:
        return
    if x_api_key is not None and hmac.compare_digest(
        x_api_key.encode("utf-8"), expected.encode("utf-8")
    ):
        return
    raise Unauthorized("A valid X-API-Key header is required on this endpoint.")


def verify_production_config() -> None:
    """Refuse to serve production traffic without a key.

    Called from the application lifespan, so the failure happens at startup
    where it is visible, rather than on the first unauthenticated request.
    """
    if environment() == PRODUCTION and api_key() is None:
        raise RuntimeError(
            "LEDGER_API_KEY is not set while LEDGER_ENV=production. Set a key, or leave "
            "LEDGER_ENV unset to run the open development configuration."
        )
