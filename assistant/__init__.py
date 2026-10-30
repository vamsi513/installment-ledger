"""An optional conversational layer over the installment ledger API.

Nothing in this package changes how the ledger behaves. It talks to the ledger
over its public HTTP API, through MCP tools, and the only tool that writes goes
through ``POST /loans/{id}/payments`` with an ``Idempotency-Key`` like any other
client.

Install it with the ``assistant`` extra. The ledger runs without it.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
