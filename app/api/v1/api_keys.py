"""API-key management endpoints.

Keys are stored as SHA-256 digests; the plaintext is returned exactly once,
in the POST (create) or rotate response. Listing never exposes the digest or
plaintext. ``expires_at`` is optional (NULL = never expires); expired keys are
rejected by the auth middleware. Rotation atomically replaces a key: the new
plaintext is returned once and the old row is deleted in the same transaction.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.api_key import ApiKey
from app.schemas.api_key import (
    ApiKeyCreate,
    ApiKeyCreatedResponse,
    ApiKeyResponse,
    ApiKeyRotate,
)
from app.schemas.common import success_response
from app.services.auth_service import create_api_key

router = APIRouter()


def _to_naive_utc(v: datetime | None) -> datetime | None:
    """Convert a tz-aware API timestamp to the project's naive-UTC storage."""
    if v is None:
        return None
    return v.astimezone(UTC).replace(tzinfo=None)


@router.get("/api-keys")
async def list_api_keys(db: AsyncSession = Depends(get_db)):
    rows = (
        (await db.execute(select(ApiKey).order_by(ApiKey.created_at.asc())))
        .scalars()
        .all()
    )
    return success_response([
        ApiKeyResponse(
            id=r.id,
            name=r.name,
            prefix=r.prefix,
            created_at=r.created_at,
            expires_at=r.expires_at,
        ).model_dump()
        for r in rows
    ])


@router.post("/api-keys", status_code=201)
async def create_api_key_endpoint(
    body: ApiKeyCreate,
    db: AsyncSession = Depends(get_db),
):
    row, plaintext = await create_api_key(db, body.name.strip())
    row.expires_at = _to_naive_utc(body.expires_at)
    await db.flush()
    return success_response(
        ApiKeyCreatedResponse(
            id=row.id,
            name=row.name,
            prefix=row.prefix,
            created_at=row.created_at,
            expires_at=row.expires_at,
            key=plaintext,
        ).model_dump()
    )


@router.post("/api-keys/{key_id}/rotate")
async def rotate_api_key(
    key_id: str,
    body: ApiKeyRotate | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Atomically replace a key: create a successor (same name; expiry from the
    request body when given, otherwise inherited) and delete the old row."""
    row = await db.get(ApiKey, key_id)
    if row is None:
        raise HTTPException(status_code=404, detail="API key not found")
    new_row, plaintext = await create_api_key(db, row.name)
    new_row.expires_at = (
        _to_naive_utc(body.expires_at) if body and body.expires_at else row.expires_at
    )
    await db.delete(row)
    await db.flush()
    return success_response(
        ApiKeyCreatedResponse(
            id=new_row.id,
            name=new_row.name,
            prefix=new_row.prefix,
            created_at=new_row.created_at,
            expires_at=new_row.expires_at,
            key=plaintext,
        ).model_dump()
    )


@router.delete("/api-keys/{key_id}")
async def delete_api_key(key_id: str, db: AsyncSession = Depends(get_db)):
    row = await db.get(ApiKey, key_id)
    if row is None:
        raise HTTPException(status_code=404, detail="API key not found")
    await db.delete(row)
    return success_response({"deleted": True})
