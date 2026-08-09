"""Identity-bag read/write service (Phase P3).

The bag (:class:`app.models.work_external_id.WorkExternalId`) reverse-maps
any known ``(source, external_id)`` pair to the work that owns it, so upserts
converge deterministically across sources/languages instead of relying on
title luck.

Conventions (see the model docstring):
  * ``source`` is a registry source name; non-registry sources are skipped.
  * ``external_id`` stores the FULL canonical ``source:id`` string, mirroring
    the ``TVSeries.external_id`` convention.
  * Creator-wins primary: the bag never overwrites a work's primary
    ``external_id`` column; later-discovered ids only enter the bag.
  * One id → at most one work. Adding an id already owned by ANOTHER work
    does NOT steal it (logged as a dedup candidate).
"""

from __future__ import annotations

import logging

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.movie import Movie
from app.models.series import TVSeries
from app.models.work_external_id import WorkExternalId
from app.services.metadata_source_registry import (
    REGISTRY_SOURCES,
    canonicalize_external_id,
)

logger = logging.getLogger(__name__)

_WORK_MODELS = {"series": TVSeries, "movie": Movie}


def _canonical(source: str | None, external_id: str | None) -> tuple[str, str] | None:
    """Normalize (source, external_id) to the bag convention, or None to skip.

    Skips empty ids and non-registry sources. The canonicalizer folds the
    inconsistent Exa/TMDB id shapes into ``source:id`` form; a raw id without
    any ``source:`` prefix (e.g. a bare wikipedia pageid) is prefixed with the
    declared source so bag writes and bag lookups converge on the same key.
    An id that self-declares a DIFFERENT registry prefix (``tmdb:1`` passed
    with source ``wikipedia``) is re-filed under its declared prefix.
    """
    src = (source or "").strip().lower()
    if not src or src not in REGISTRY_SOURCES:
        return None
    canon = canonicalize_external_id(external_id, src)
    if not canon:
        return None
    if ":" in canon:
        prefix, _ = canon.split(":", 1)
        if prefix in REGISTRY_SOURCES:
            return prefix, canon
    return src, f"{src}:{canon}"


async def add_external_id(
    db: AsyncSession,
    work_type: str,
    work_id: str,
    source: str | None,
    external_id: str | None,
) -> bool:
    """Idempotently add an external id to a work's identity bag.

    Returns True when a new bag row was inserted. No-ops for non-registry
    sources / empty ids. When the (source, id) pair is already owned by
    ANOTHER work, the existing mapping is kept (creator-wins, no stealing)
    and a warning is logged — such pairs are dedup candidates.
    """
    if work_type not in _WORK_MODELS:
        logger.warning("[external_ids] unknown work_type %r — skipped", work_type)
        return False
    norm = _canonical(source, external_id)
    if norm is None:
        return False
    src, canon = norm

    existing = (await db.execute(
        select(WorkExternalId).where(
            WorkExternalId.source == src,
            WorkExternalId.external_id == canon,
        )
    )).scalar_one_or_none()
    if existing is not None:
        if existing.work_id == work_id and existing.work_type == work_type:
            return False  # already bagged for this work
        logger.warning(
            "[external_ids] %s:%s already maps to %s %s — NOT re-pointing to %s %s "
            "(dedup candidate)",
            src, canon, existing.work_type, existing.work_id, work_type, work_id,
        )
        return False

    db.add(WorkExternalId(
        work_type=work_type, work_id=work_id, source=src, external_id=canon,
    ))
    await db.flush()
    return True


async def find_work_by_external_id(
    db: AsyncSession,
    work_type: str,
    source: str | None,
    external_id: str | None,
) -> TVSeries | Movie | None:
    """Bag reverse-lookup: canonicalize (source, id) and return the owning work.

    Only looks up ids bagged for the SAME ``work_type`` — a bag hit of the
    other type is ignored here (cross-type convergence is handled by the
    metadata_repository cross-table guard and the daily dedup).
    """
    model = _WORK_MODELS.get(work_type)
    if model is None:
        return None
    norm = _canonical(source, external_id)
    if norm is None:
        return None
    src, canon = norm
    row = (await db.execute(
        select(WorkExternalId).where(
            WorkExternalId.work_type == work_type,
            WorkExternalId.source == src,
            WorkExternalId.external_id == canon,
        )
    )).scalar_one_or_none()
    if row is None:
        return None
    return await db.get(model, row.work_id)


async def list_external_ids(
    db: AsyncSession, work_type: str, work_id: str
) -> list[WorkExternalId]:
    """All bag ids for one work (detail responses, debugging)."""
    result = await db.execute(
        select(WorkExternalId).where(
            WorkExternalId.work_type == work_type,
            WorkExternalId.work_id == work_id,
        )
    )
    return list(result.scalars().all())


async def merge_external_id_bags(
    db: AsyncSession,
    survivor: TVSeries | Movie,
    duplicates: list[TVSeries | Movie],
) -> int:
    """Union the duplicates' bag rows into the survivor's bag (dedup merge).

    Re-points each duplicate's bag rows at the survivor; on a UniqueConstraint
    collision (the survivor already owns that id) the duplicate's row is
    dropped instead. Also bags each duplicate's PRIMARY external_id (which
    stays on the duplicate's column, not the survivor's — creator-wins) so the
    survivor remains reachable by every id any merged row was ever known under.
    Returns the number of bag rows gained by the survivor.
    """
    work_type = "series" if isinstance(survivor, TVSeries) else "movie"
    gained = 0
    survivor_ids = {
        (r.source, r.external_id)
        for r in await list_external_ids(db, work_type, survivor.id)
    }

    async def _claim(source: str | None, external_id: str | None) -> None:
        nonlocal gained
        norm = _canonical(source, external_id)
        if norm is None or norm in survivor_ids:
            return
        if await add_external_id(db, work_type, survivor.id, norm[0], norm[1]):
            survivor_ids.add(norm)
            gained += 1

    for dup in duplicates:
        dup_type = "series" if isinstance(dup, TVSeries) else "movie"
        # The duplicate's primary id joins the survivor's bag.
        await _claim(dup.external_source, dup.external_id)
        # Re-point (or drop, on collision) the duplicate's own bag rows.
        for row in await list_external_ids(db, dup_type, dup.id):
            key = (row.source, row.external_id)
            if key in survivor_ids:
                await db.delete(row)
            else:
                row.work_type = work_type
                row.work_id = survivor.id
                survivor_ids.add(key)
                gained += 1
    await db.flush()
    return gained


async def delete_external_ids_for_work(
    db: AsyncSession, work_type: str, work_id: str
) -> None:
    """Drop a work's whole bag (used when the work row itself is deleted)."""
    await db.execute(
        delete(WorkExternalId).where(
            WorkExternalId.work_type == work_type,
            WorkExternalId.work_id == work_id,
        )
    )
