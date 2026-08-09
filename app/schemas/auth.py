"""Pydantic schemas for the auth endpoints."""

from pydantic import BaseModel


class OTPRequest(BaseModel):
    """TOTP code submitted by the user to obtain a session cookie."""

    code: str


class AuthStatusResponse(BaseModel):
    authenticated: bool
