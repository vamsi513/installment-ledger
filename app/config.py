"""Runtime configuration, read from the environment."""

import os

DEFAULT_DATABASE_URL = "postgresql+psycopg://ledger:ledger@localhost:5433/ledger"


def database_url() -> str:
    """Return the SQLAlchemy URL for the Postgres instance to use."""
    return os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)


def api_key() -> str | None:
    """Return the API key callers must present, or ``None`` when auth is off.

    A missing or blank ``LEDGER_API_KEY`` means the same thing: the API is open.
    That is the local development default and the reason the check has to be
    opted into rather than switched off.
    """
    value = os.environ.get("LEDGER_API_KEY", "").strip()
    return value or None


def environment() -> str:
    """Return the deployment environment name, lowercased.

    Only ``production`` is given any meaning: it is the setting under which a
    missing API key stops the service from starting at all.
    """
    return os.environ.get("LEDGER_ENV", "development").strip().lower()
