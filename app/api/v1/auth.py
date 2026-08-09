"""Auth API — TOTP login (session cookie), logout, and auth status.

These endpoints are exempt from :class:`app.middleware.auth.AuthMiddleware`
(paths under ``/api/v1/auth/`` are always open).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.schemas.auth import AuthStatusResponse, OTPRequest
from app.schemas.common import success_response
from app.services.auth_service import (
    AUTH_COOKIE_NAME,
    COOKIE_MAX_AGE_DAYS,
    check_api_key,
    get_or_create_cookie_secret,
    get_or_create_totp_secret,
    make_cookie,
    validate_cookie,
    verify_totp,
)

router = APIRouter()


async def _request_authenticated(request: Request, db: AsyncSession) -> bool:
    """Whether the incoming request carries a valid credential (cookie or API key)."""
    presented_key: str | None = None
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        presented_key = auth_header[7:].strip()
    if not presented_key:
        presented_key = request.headers.get("x-api-key") or None

    if presented_key:
        if settings.api_key and presented_key == settings.api_key:
            return True
        if await check_api_key(db, presented_key):
            return True

    cookie_value = request.cookies.get(AUTH_COOKIE_NAME)
    if cookie_value:
        secret = await get_or_create_cookie_secret(db)
        if validate_cookie(cookie_value, secret):
            return True
    return False


@router.post("/auth/otp")
async def auth_otp(
    body: OTPRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """Verify a TOTP code and issue a session cookie on success."""
    totp_secret = await get_or_create_totp_secret(db)
    if not verify_totp(totp_secret, body.code.strip()):
        raise HTTPException(
            status_code=401,
            detail={"code": "UNAUTHORIZED", "message": "invalid OTP code"},
        )
    cookie_secret = await get_or_create_cookie_secret(db)
    response.set_cookie(
        AUTH_COOKIE_NAME,
        make_cookie(cookie_secret),
        max_age=COOKIE_MAX_AGE_DAYS * 86400,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return success_response({"authenticated": True})


@router.post("/auth/logout")
async def auth_logout(response: Response):
    """Clear the session cookie."""
    response.delete_cookie(AUTH_COOKIE_NAME, path="/")
    return success_response({"authenticated": False})


@router.get("/auth/status")
async def auth_status(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Report whether the current request is authenticated. Always 200."""
    authenticated = await _request_authenticated(request, db)
    return success_response(AuthStatusResponse(authenticated=authenticated).model_dump())
