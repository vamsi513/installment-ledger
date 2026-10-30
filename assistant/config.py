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
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
