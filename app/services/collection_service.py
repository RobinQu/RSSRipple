"""WorkCollection service: deterministic TMDB collection linking + queries.

Collection linking is deliberately DETERMINISTIC (no LLM): when a Movie's
``external_id`` is in canonical ``tmdb:<digits>`` form, fetch TMDB movie
details directly and read ``belongs_to_collection`` ({id, name, poster_path}).
The LLM ``matched_entity`` path can't be used — Layers 1-3, the known-work
short-circuit and cache hits all bypass TMDB details, and bangumi mode has no
TMDB tools at all.

The external identity is ``external_source="tmdb_collection"`` + the raw
numeric id — NOT ``canonicalize_external_id`` (its TMDB rule would rewrite
``tmdb-collection:131295`` to ``tmdb:131295``, colliding with the movie id
space). Upsert is idempotent via the (external_source, external_id) unique
constraint.
"""

from __future__ import annotations

import logging
import re
import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.movie import Movie
from app.models.work_collection import WorkCollection
from app.services.runtime_config import runtime_config

logger = logging.getLogger(__name__)

TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_COLLECTION_SOURCE = "tmdb_collection"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w500"

_CANONICAL_TMDB_ID = re.compile(r"^tmdb:(\d+)$")


def _canonical_tmdb_id(external_id: str | None) -> str | None:
    """Return the numeric id when ``external_id`` is canonical ``tmdb:<digits>``."""
    m = _CANONICAL_TMDB_ID.match(external_id or "")
    return m.group(1) if m else None


async def fetch_tmdb_movie_collection(tmdb_id: str) -> dict | None:
    """Fetch ``belongs_to_collection`` from TMDB movie details.

    Returns ``{id, name, poster_path}`` or None when the movie has no
    collection, TMDB is disabled/unconfigured, or the request fails.
    """
    api_key = runtime_config.tmdb_api_key
    if not api_key or not runtime_config.tmdb_enabled:
        return None

    import httpx

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{TMDB_BASE}/movie/{tmdb_id}",
                params={"api_key": api_key, "language": "zh-CN"},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.warning(
            "[collection] TMDB movie details failed for tmdb_id=%s: %s", tmdb_id, e,
        )
        return None

    coll = data.get("belongs_to_collection")
    if not isinstance(coll, dict) or not coll.get("id"):
        return None
    return {
        "id": coll["id"],
        "name": coll.get("name"),
        "poster_path": coll.get("poster_path"),
    }


async def upsert_collection_from_tmdb(
    db: AsyncSession, coll: dict
) -> WorkCollection:
    """Idempotent upsert of a WorkCollection from TMDB collection data."""
    external_id = str(coll["id"])
    existing = (await db.execute(
        select(WorkCollection).where(
            WorkCollection.external_source == TMDB_COLLECTION_SOURCE,
            WorkCollection.external_id == external_id,
        )
    )).scalars().first()
    poster_path = coll.get("poster_path")
    poster_url = f"{TMDB_IMAGE_BASE}{poster_path}" if poster_path else None
    if existing is not None:
        if coll.get("name") and existing.title_cn != coll["name"]:
            existing.title_cn = coll["name"]
        if poster_url and not existing.poster_url:
            existing.poster_url = poster_url
        return existing
    collection = WorkCollection(
        title_cn=coll.get("name") or f"TMDB Collection {external_id}",
        external_id=external_id,
        external_source=TMDB_COLLECTION_SOURCE,
        poster_url=poster_url,
    )
    db.add(collection)
    await db.flush()
    return collection


async def link_movie_collection(
    db: AsyncSession, movie: Movie
) -> WorkCollection | None:
    """Deterministically link a Movie to its TMDB collection, if any.

    No-ops (returns None) when:
      * the movie is already linked (``collection_id`` set);
      * ``external_id`` is not canonical ``tmdb:<digits>`` form;
      * TMDB is disabled/unconfigured or the movie has no collection.

    ``description``/``title_en`` stay NULL (TMDB movie details only carry
    {id, name, poster_path}); ``poster_url`` stores the remote TMDB image URL.
    """
    if movie.collection_id is not None:
        return None
    tmdb_id = _canonical_tmdb_id(movie.external_id)
    if tmdb_id is None:
        return None
    coll = await fetch_tmdb_movie_collection(tmdb_id)
    if coll is None:
        return None
    collection = await upsert_collection_from_tmdb(db, coll)
    movie.collection_id = collection.id
    logger.info(
        "[collection] linked movie %r -> collection %r (tmdb_collection:%s)",
        movie.id, collection.title_cn, collection.external_id,
    )
    return collection


async def collection_work_summaries(
    db: AsyncSession, collection_id: str, exclude: tuple[str, str] | None = None
) -> list[dict]:
    """Summaries of works in a collection: ``{id, title, year, type}``;
    series entries additionally carry ``season_number`` (per-season works —
    a series work IS one season) so detail pages can offer a season switcher.

    ``exclude`` is a ``(work_type, work_id)`` pair to skip (the work whose
    detail page lists its siblings).
    """
    from app.models.series import TVSeries

    ex_type, ex_id = exclude if exclude else (None, None)
    out: list[dict] = []
    series_rows = (await db.execute(
        select(TVSeries).where(TVSeries.collection_id == collection_id)
    )).scalars().all()
    for s in series_rows:
        if ex_type == "series" and s.id == ex_id:
            continue
        out.append({
            "id": s.id,
            "title": s.title_cn or s.title_en or s.original_title,
            "year": s.start_date.year if s.start_date else None,
            "type": "series",
            "season_number": s.season_number,
        })
    movie_rows = (await db.execute(
        select(Movie).where(Movie.collection_id == collection_id)
    )).scalars().all()
    for m in movie_rows:
        if ex_type == "movie" and m.id == ex_id:
            continue
        out.append({
            "id": m.id,
            "title": m.title_cn or m.title_en or m.original_title,
            "year": m.release_date.year if m.release_date else None,
            "type": "movie",
        })
    out.sort(key=lambda w: (w["year"] is None, w["year"] or 0))
    return out


# ---------------------------------------------------------------------------
# TMDB collection parts — on-demand untracked-sibling visibility (NOT persisted)
#
# ``GET /collections/{id}?include_parts=true`` surfaces franchise works that
# are NOT yet in the local DB. Parts are fetched live from TMDB
# ``/collection/{id}`` and never written to WorkCollection — the collection
# row stays a lightweight grouping entity.
# ---------------------------------------------------------------------------

_PARTS_CACHE_TTL_SECONDS = 600  # 10 min — shields TMDB from page refreshes
_parts_cache: dict[str, tuple[float, list[dict]]] = {}


async def fetch_tmdb_collection_parts(
    collection: WorkCollection,
) -> list[dict] | None:
    """Fetch a TMDB-backed collection's parts on demand.

    Returns lightweight dicts ``{tmdb_id, title, year, poster_url}``, or None
    for non-TMDB collections, missing/blank TMDB config, or request failure.
    Successful results are cached in-process for ``_PARTS_CACHE_TTL_SECONDS``.
    """
    if collection.external_source != TMDB_COLLECTION_SOURCE:
        return None
    if not collection.external_id:
        return None
    api_key = runtime_config.tmdb_api_key
    if not api_key or not runtime_config.tmdb_enabled:
        return None

    cached = _parts_cache.get(collection.external_id)
    if cached and time.monotonic() - cached[0] < _PARTS_CACHE_TTL_SECONDS:
        return cached[1]

    import httpx

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{TMDB_BASE}/collection/{collection.external_id}",
                params={"api_key": api_key, "language": "zh-CN"},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.warning(
            "[collection] TMDB collection parts failed for id=%s: %s",
            collection.external_id, e,
        )
        return None

    parts: list[dict] = []
    for part in data.get("parts") or []:
        if not part.get("id"):
            continue
        release = part.get("release_date") or ""
        poster_path = part.get("poster_path")
        parts.append({
            "tmdb_id": str(part["id"]),
            "title": part.get("title"),
            "year": int(release[:4]) if release[:4].isdigit() else None,
            "poster_url": f"{TMDB_IMAGE_BASE}{poster_path}" if poster_path else None,
        })
    _parts_cache[collection.external_id] = (time.monotonic(), parts)
    return parts


async def tracked_movie_tmdb_ids(db: AsyncSession) -> set[str]:
    """Numeric TMDB ids of locally tracked movies (canonical ``tmdb:<digits>``
    external_ids only)."""
    rows = (await db.execute(
        select(Movie.external_id).where(Movie.external_id.is_not(None))
    )).scalars().all()
    return {tid for tid in (_canonical_tmdb_id(e) for e in rows) if tid}


def filter_untracked_parts(parts: list[dict], tracked_tmdb_ids: set[str]) -> list[dict]:
    """Parts whose TMDB id is not among locally tracked movies."""
    return [p for p in parts if p["tmdb_id"] not in tracked_tmdb_ids]
