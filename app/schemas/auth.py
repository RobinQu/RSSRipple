"""Pydantic schemas for the auth endpoints."""

from pydantic import BaseModel, Field


class OTPRequest(BaseModel):
    """TOTP code submitted by the user to obtain a session cookie."""

    # pyotp issues 6-digit codes; accept up to 8 to tolerate 8-digit
    # authenticator configs. Malformed codes are rejected at the schema
    # layer (422) so they never reach verification or consume budget.
    code: str = Field(min_length=6, max_length=8, pattern=r"^\d+$")


class AuthStatusResponse(BaseModel):
    authenticated: bool
