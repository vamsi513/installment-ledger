"""Run the tool server on stdio.

    python -m assistant.mcp_server

Reads LEDGER_BASE_URL, ASSISTANT_STATE_DB, ASSISTANT_MAX_PAYMENT_CENTS and
ASSISTANT_PENDING_TTL_SECONDS from the environment.
"""

from __future__ import annotations

import logging
import sys

from .server import build_server


def main() -> None:
    # stdout is the protocol channel, so logs go to stderr.
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
