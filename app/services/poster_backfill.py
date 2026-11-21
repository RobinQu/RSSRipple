"""Cache remote posters that were persisted before local caching existed.

Rows are only selected while their ``poster_url`` is still an HTTP(S) URL.
Manual poster edits are never touched, and a download failure leaves the
remote URL in place so a later run can retry.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audio_work import AudioWork
from app.models.movie import Movie
from app.models.series import TVSeries
from app.models.work_collection import WorkCollection
from app.services.metadata_service import download_and_cache_poster

logger = logging.getLogger(__name__)

POSTER_MODELS: dict[str, type] = {
    "tv": TVSeries,
    "movie": Movie,
    "audio": AudioWork,
    "collection": WorkCollection,
}

DEFAULT_DAILY_LIMIT_PER_MODEL = 100


def _remote_poster_clause(model: type):
    return or_(
        model.poster_url.like("http://%"),
        model.poster_url.like("https://%"),
    )


def _manual_poster_edit(row) -> bool:
    return "poster_url" in (getattr(row, "manually_edited_fields", None) or [])


async def cache_remote_posters(
    db: AsyncSession,
    *,
    models: Iterable[str] | None = None,
    limit_per_model: int = DEFAULT_DAILY_LIMIT_PER_MODEL,
    concurrency: int = 4,
    apply: bool = True,
) -> dict[str, dict[str, int]]:
    """Cache remote posters in-place and return per-model counters.

    ``apply=False`` is a dry-run: rows are counted but no HTTP or DB writes
    happen. The caller owns the transaction/commit.
    """
    selected = list(models) if models is not None else list(POSTER_MODELS)
    unknown = [name for name in selected if name not in POSTER_MODELS]
    if unknown:
        raise ValueError(f"unknown poster model(s): {', '.join(unknown)}")

    semaphore = asyncio.Semaphore(max(1, concurrency))
    report: dict[str, dict[str, int]] = {}

    for name in selected:
        model = POSTER_MODELS[name]
        rows = (
            await db.execute(
                select(model)
                .where(_remote_poster_clause(model))
                .order_by(model.updated_at.asc())
                .limit(max(0, limit_per_model))
            )
        ).scalars().all()

        counters = {
            "scanned": len(rows),
            "eligible": 0,
            "cached": 0,
            "failed": 0,
            "skipped": 0,
        }
        eligible = []
        for row in rows:
            if _manual_poster_edit(row):
                counters["skipped"] += 1
                continue
            counters["eligible"] += 1
            eligible.append(row)

        if apply and eligible:
            async def _cache_one(row) -> bool:
                async with semaphore:
                    local = await download_and_cache_poster(row.poster_url)
                if not local:
                    return False
                row.poster_url = local
                return True

            results = await asyncio.gather(*(_cache_one(row) for row in eligible))
            counters["cached"] = sum(1 for ok in results if ok)
            counters["failed"] = len(results) - counters["cached"]
            if counters["cached"]:
                await db.flush()

        report[name] = counters
        if counters["cached"] or counters["failed"]:
            logger.info(
                "[poster] backfill %s: cached=%d failed=%d skipped=%d scanned=%d",
                name,
                counters["cached"],
                counters["failed"],
                counters["skipped"],
                counters["scanned"],
            )
    return report
