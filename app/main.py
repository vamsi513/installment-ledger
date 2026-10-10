"""Application entry point and error handling.

Every error, including validation failures, leaves through the same envelope:
``{"error": {"code": ..., "message": ..., "details": {...}}}``.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .api.routes import router
from .errors import ApiError
from .logging_setup import REQUEST_ID_HEADER, RequestLogMiddleware, configure_logging

configure_logging()

app = FastAPI(
    title="installment-ledger",
    version="0.1.0",
    summary="A loan and payments API with an enforced double entry ledger.",
    description=(
        "Amounts are integer cents everywhere. Installments sum exactly to the total "
        "payable. Every payment writes balanced ledger entries, enforced by a database "
        "trigger rather than by application code. Payment and loan creation are "
        "idempotent on an `Idempotency-Key` header.\n\n"
        "Errors all share one envelope: "
        '`{"error": {"code": ..., "message": ..., "details": {...}}}`. '
        "The `code` is stable and safe to branch on; the `message` is for humans.\n\n"
        "No authentication, no real money, and no benchmark. See the repository README."
    ),
)
app.add_middleware(RequestLogMiddleware)
app.include_router(router)


@app.exception_handler(ApiError)
def handle_api_error(request: Request, error: ApiError) -> JSONResponse:
    # The request id is echoed on errors too, so a caller reporting a failure can
    # quote one string that finds the exact log line.
    return JSONResponse(
        status_code=error.status_code,
        content=error.body(),
        headers={REQUEST_ID_HEADER: request.headers.get(REQUEST_ID_HEADER, "")}
        if request.headers.get(REQUEST_ID_HEADER)
        else None,
    )


@app.exception_handler(RequestValidationError)
def handle_validation_error(_: Request, error: RequestValidationError) -> JSONResponse:
    fields = [
        {
            "location": list(item.get("loc", ())),
            "message": item.get("msg", ""),
            "type": item.get("type", ""),
        }
        for item in error.errors()
    ]
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "validation_error",
                "message": "The request body or path is not valid.",
                "details": {"fields": fields},
            }
        },
    )
