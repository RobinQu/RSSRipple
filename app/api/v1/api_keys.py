"""API-key management endpoints.

Keys are stored as SHA-256 digests; the plaintext is returned exactly once,
in the POST response. Listing never exposes the digest or plaintext.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.api_key import ApiKey
from app.schemas.api_key import ApiKeyCreate, ApiKeyCreatedResponse, ApiKeyResponse
from app.schemas.common import success_response
from app.services.auth_service import create_api_key

router = APIRouter()


@router.get("/api-keys")
async def list_api_keys(db: AsyncSession = Depends(get_db)):
    rows = (
        (await db.execute(select(ApiKey).order_by(ApiKey.created_at.asc())))
        .scalars()
        .all()
    )
    return success_response([
        ApiKeyResponse(
            id=r.id, name=r.name, prefix=r.prefix, created_at=r.created_at
        ).model_dump(mode="json")
        for r in rows
    ])


@router.post("/api-keys", status_code=201)
async def create_api_key_endpoint(
    body: ApiKeyCreate,
    db: AsyncSession = Depends(get_db),
):
    row, plaintext = await create_api_key(db, body.name.strip())
    return success_response(
        ApiKeyCreatedResponse(
            id=row.id,
            name=row.name,
            prefix=row.prefix,
            created_at=row.created_at,
            key=plaintext,
        ).model_dump(mode="json")
    )


@router.delete("/api-keys/{key_id}")
async def delete_api_key(key_id: str, db: AsyncSession = Depends(get_db)):
    row = await db.get(ApiKey, key_id)
    if row is None:
        raise HTTPException(status_code=404, detail="API key not found")
    await db.delete(row)
    return success_response({"deleted": True})
