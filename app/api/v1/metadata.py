"""Unified metadata source catalog, search, preview, and apply API."""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas.common import success_response
from app.schemas.metadata_search import MetadataSearchRequest, WorkMetadataActionRequest
from app.services.metadata_search import (
    apply_work_metadata,
    preview_work_metadata,
    search_metadata_candidates,
)
from app.services.metadata_source_registry import DEFAULT_FALLBACK_SOURCES, SITE_DOMAINS
from app.services.metadata_sources import get_metadata_source_catalog

router = APIRouter()


@router.get("/metadata/sources")
async def list_metadata_sources():
    return success_response({
        "primary_sources": get_metadata_source_catalog(),
        "trusted_sites": [
            {"value": name, "domains": domains}
            for name, domains in SITE_DOMAINS.items()
        ],
        "default_trusted_sites": DEFAULT_FALLBACK_SOURCES,
    })


@router.post("/metadata/search")
async def search_metadata(body: MetadataSearchRequest, db: AsyncSession = Depends(get_db)):
    candidates = await search_metadata_candidates(db, body)
    return success_response({
        "query": body.query,
        "mode": body.mode,
        "source": body.source,
        "trusted_sites": body.trusted_sites,
        "candidates": [candidate.model_dump() for candidate in candidates],
    })


@router.post("/works/metadata/preview")
async def preview_metadata(body: WorkMetadataActionRequest, db: AsyncSession = Depends(get_db)):
    return success_response(await preview_work_metadata(
        db, body.id, body.content_type, body.candidate, body.override_manual_edits
    ))


@router.post("/works/metadata/apply")
async def apply_metadata(body: WorkMetadataActionRequest, db: AsyncSession = Depends(get_db)):
    return success_response(await apply_work_metadata(
        db, body.id, body.content_type, body.candidate, body.override_manual_edits
    ))

