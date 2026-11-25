"""Shared HTTP error-response builders for API routers.

Every router must emit the same envelope (``success``/``data``/``error``/
``meta``). Centralising the ``JSONResponse`` construction keeps status codes,
the error shape and the ``meta`` contract from drifting between modules.
"""

from fastapi.responses import JSONResponse

from app.schemas.common import error_response_dict


def error_response(
    status_code: int,
    code: str,
    message: str,
    details: dict | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=error_response_dict(code, message, details),
    )
