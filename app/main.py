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

app = FastAPI(title="installment-ledger", version="0.1.0")
app.include_router(router)


@app.exception_handler(ApiError)
def handle_api_error(_: Request, error: ApiError) -> JSONResponse:
    return JSONResponse(status_code=error.status_code, content=error.body())


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
