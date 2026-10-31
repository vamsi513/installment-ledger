"""The entry point that serves the ledger and the assistant together.

    uvicorn assistant.asgi:app

``uvicorn app.main:app`` is unaffected and has no assistant routes.
"""

from __future__ import annotations

from fastapi import FastAPI

from app.main import app as ledger_app

from .mount import mount_assistant

app: FastAPI = mount_assistant(ledger_app)

__all__ = ["app"]
