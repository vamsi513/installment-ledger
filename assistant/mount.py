"""Attaching the assistant to the ledger without touching the ledger.

``app.main:app`` is left exactly as it is. This builds a new outer application
that holds the assistant under ``/assistant`` and mounts the untouched ledger
app underneath it, so:

* ``uvicorn app.main:app`` behaves as it always did, with no assistant routes
  and no assistant imports;
* ``uvicorn assistant.asgi:app`` serves the ledger and the assistant together.

The outer app owns the lifespan and runs both inner apps' lifespans in turn, so
anything either of them does on startup or shutdown still happens. A mounted
ASGI app does not get its lifespan run by Starlette, so delegating it here is
required rather than decorative.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .api import build_assistant_app
from .config import Settings, get_settings

logger = logging.getLogger("assistant.mount")

ASSISTANT_PREFIX = "/assistant"


def mount_assistant(ledger_app: FastAPI, *, settings: Settings | None = None) -> FastAPI:
    """Return an app serving the ledger plus the assistant.

    ``ASSISTANT_ENABLED=0`` is a kill switch: the ledger app is handed straight
    back, with no assistant routes attached and nothing wrapped.
    """
    resolved = settings or get_settings()
    if not resolved.enabled:
        logger.warning("assistant not mounted", extra={"reason": "ASSISTANT_ENABLED is off"})
        return ledger_app

    assistant_app = build_assistant_app()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with (
            ledger_app.router.lifespan_context(ledger_app),
            assistant_app.router.lifespan_context(assistant_app),
        ):
            yield

    # The outer app serves no documents of its own, so /openapi.json, /docs and
    # /redoc fall through to the ledger app and stay exactly what they were.
    # The assistant's own document is at /assistant/openapi.json.
    outer = FastAPI(
        title="installment-ledger with assistant",
        version="0.1.0",
        lifespan=lifespan,
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
    )
    # Order matters: the more specific mount has to be tried first, because a
    # mounted app that answers 404 ends the match rather than falling through.
    outer.mount(ASSISTANT_PREFIX, assistant_app)
    outer.mount("/", ledger_app)
    return outer
