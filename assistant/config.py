"""Settings, all from the environment.

No secret has a default. ``OPENAI_API_KEY`` is read here and nowhere else, and
it is never logged or echoed in a response.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from functools import lru_cache

DEFAULT_LEDGER_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_MAX_PAYMENT_CENTS = 50_000
DEFAULT_PENDING_TTL_SECONDS = 300
# Defaults outside the working tree on purpose, so running the assistant never
# drops an untracked database file into the repository. A deployment should set
# ASSISTANT_STATE_DB to somewhere durable.
DEFAULT_STATE_DB = os.path.join(tempfile.gettempdir(), "installment-ledger-assistant-state.sqlite3")
DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_LEDGER_TIMEOUT_SECONDS = 10.0
DEFAULT_WS_MAX_FRAME_BYTES = 4_096
DEFAULT_WS_MAX_CONNECTIONS = 8
DEFAULT_MAX_MESSAGE_CHARS = 2_000

# Review queue defaults. The thresholds are examples picked to be easy to reason
# about in a demo, not advice about where a real limit belongs. The whole feature
# is off unless ASSISTANT_REVIEW_ENABLED says otherwise, so the assistant behaves
# exactly as it did before this existed.
# Consent classifier defaults. The mode is "rule" so the assistant behaves
# exactly as it did before this existed: the classifier is not even called.
#
# CONSENT_THRESHOLD is a PLACEHOLDER. It has not been chosen from any
# measurement and must not be read as one. The threshold is meant to come from
# the dev sweep in evals/consent/run_eval.py, and until that sweep has been run
# against a real model this number is only a value that lets the code run.
DEFAULT_CONSENT_MODE = "rule"
DEFAULT_CONSENT_THRESHOLD = 0.85
DEFAULT_CONSENT_TIMEOUT_SECONDS = 3.0
CONSENT_MODES = ("rule", "shadow", "live")

DEFAULT_REVIEW_THRESHOLD_CENTS = 25_000
DEFAULT_REVIEW_MAX_PREPARES = 3
DEFAULT_REVIEW_WINDOW_SECONDS = 3_600


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _consent_mode_env(name: str, default: str) -> str:
    """Read the rollout mode, refusing anything that is not one of the three.

    A typo must not quietly land on a mode nobody intended, and least of all on
    a more permissive one than was meant.
    """
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw not in CONSENT_MODES:
        raise ValueError(f"{name} must be one of {', '.join(CONSENT_MODES)}, got {raw!r}")
    return raw


def _threshold_env(name: str, default: float) -> float:
    """Read a threshold and insist it is a probability."""
    value = _float_env(name, default)
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be between 0 and 1, got {value}")
    return value


@dataclass(frozen=True)
class Settings:
    """Everything the assistant needs to run, resolved once."""

    ledger_base_url: str
    ledger_timeout_seconds: float
    max_payment_cents: int
    pending_ttl_seconds: int
    max_message_chars: int
    state_db_path: str
    llm_provider: str
    model: str
    openai_api_key: str | None
    ws_auth_token: str | None
    ws_max_frame_bytes: int
    ws_max_connections: int
    enabled: bool

    # Review queue. Appended with defaults so existing call sites are unchanged
    # and so the feature is off unless it is switched on deliberately.
    review_enabled: bool = False
    review_threshold_cents: int = DEFAULT_REVIEW_THRESHOLD_CENTS
    review_max_prepares: int = DEFAULT_REVIEW_MAX_PREPARES
    review_window_seconds: int = DEFAULT_REVIEW_WINDOW_SECONDS
    reviewer_id: str = ""
    review_api_token: str | None = None

    # Consent classifier. Appended with defaults for the same reason the review
    # queue settings were: existing call sites stay unchanged, and the feature
    # is inert unless it is switched on deliberately.
    consent_mode: str = DEFAULT_CONSENT_MODE
    consent_threshold: float = DEFAULT_CONSENT_THRESHOLD
    consent_timeout_seconds: float = DEFAULT_CONSENT_TIMEOUT_SECONDS

    def require_reviewer_id(self) -> str:
        """The configured reviewer identity.

        There is no authentication anywhere in this service. This is a name read
        from configuration so the audit trail records who a change is attributed
        to. It proves nothing about who actually made the call.
        """
        if not self.reviewer_id.strip():
            raise RuntimeError(
                "ASSISTANT_REVIEWER_ID is not set. Set it to the name the audit "
                "trail should attribute review decisions to."
            )
        return self.reviewer_id.strip()

    def require_openai_api_key(self) -> str:
        if not self.openai_api_key:
            raise RuntimeError(
                "OPENAI_API_KEY is not set. Set it in the environment, or run with "
                "ASSISTANT_LLM=fake to use the deterministic stand in."
            )
        return self.openai_api_key


def load_settings() -> Settings:
    """Read settings from the environment without caching, for tests."""
    return Settings(
        ledger_base_url=os.environ.get("LEDGER_BASE_URL", DEFAULT_LEDGER_BASE_URL).rstrip("/"),
        ledger_timeout_seconds=_float_env(
            "ASSISTANT_LEDGER_TIMEOUT_SECONDS", DEFAULT_LEDGER_TIMEOUT_SECONDS
        ),
        max_payment_cents=_int_env("ASSISTANT_MAX_PAYMENT_CENTS", DEFAULT_MAX_PAYMENT_CENTS),
        pending_ttl_seconds=_int_env("ASSISTANT_PENDING_TTL_SECONDS", DEFAULT_PENDING_TTL_SECONDS),
        max_message_chars=_int_env("ASSISTANT_MAX_MESSAGE_CHARS", DEFAULT_MAX_MESSAGE_CHARS),
        state_db_path=os.environ.get("ASSISTANT_STATE_DB", DEFAULT_STATE_DB),
        llm_provider=os.environ.get("ASSISTANT_LLM", "openai").strip().lower(),
        model=os.environ.get("ASSISTANT_MODEL", DEFAULT_MODEL),
        openai_api_key=os.environ.get("OPENAI_API_KEY") or None,
        ws_auth_token=os.environ.get("ASSISTANT_WS_TOKEN") or None,
        ws_max_frame_bytes=_int_env("ASSISTANT_WS_MAX_FRAME_BYTES", DEFAULT_WS_MAX_FRAME_BYTES),
        ws_max_connections=_int_env("ASSISTANT_WS_MAX_CONNECTIONS", DEFAULT_WS_MAX_CONNECTIONS),
        enabled=_bool_env("ASSISTANT_ENABLED", True),
        review_enabled=_bool_env("ASSISTANT_REVIEW_ENABLED", False),
        review_threshold_cents=_int_env(
            "ASSISTANT_REVIEW_THRESHOLD_CENTS", DEFAULT_REVIEW_THRESHOLD_CENTS
        ),
        review_max_prepares=_int_env("ASSISTANT_REVIEW_MAX_PREPARES", DEFAULT_REVIEW_MAX_PREPARES),
        review_window_seconds=_int_env(
            "ASSISTANT_REVIEW_WINDOW_SECONDS", DEFAULT_REVIEW_WINDOW_SECONDS
        ),
        reviewer_id=os.environ.get("ASSISTANT_REVIEWER_ID", ""),
        review_api_token=os.environ.get("ASSISTANT_REVIEW_TOKEN") or None,
        consent_mode=_consent_mode_env("CONSENT_MODE", DEFAULT_CONSENT_MODE),
        consent_threshold=_threshold_env("CONSENT_THRESHOLD", DEFAULT_CONSENT_THRESHOLD),
        consent_timeout_seconds=_float_env(
            "CONSENT_TIMEOUT_SECONDS", DEFAULT_CONSENT_TIMEOUT_SECONDS
        ),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
