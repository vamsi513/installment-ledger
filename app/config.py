"""Runtime configuration, read from the environment."""

import os

DEFAULT_DATABASE_URL = "postgresql+psycopg://ledger:ledger@localhost:5433/ledger"


def database_url() -> str:
    """Return the SQLAlchemy URL for the Postgres instance to use."""
    return os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)
