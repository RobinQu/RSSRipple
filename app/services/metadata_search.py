"""Canonical metadata search, preview, and manual-apply application service."""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.movie import Movie
from app.models.series import TVSeries
from app.schemas.metadata_search import MetadataCandidate, MetadataSearchRequest
from app.services.anime_signals import apply_is_anime
from app.services.external_ids import add_external_id, find_work_by_external_id
from app.services.genre_registry import normalize_genres
from app.services.metadata_service import (
    _collection_fallback_start_date,
    _parse_date,
    _work_end_date,
    _work_start_date,
    download_and_cache_poster,
    manual_search_metadata,
    manually_edited_fields,
    upsert_episodes,
)
from app.services.metadata_source_registry import REGISTRY_SOURCES, granularity_of
from app.services.metadata_sources import is_metadata_source_available


def _safe_float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _safe_int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None

_COMMON_FIELDS: tuple[tuple[str, str, Any], ...] = (
    ("title_cn", "title_cn", str),
    ("title_en", "title_en", str),
    ("original_title", "original_title", str),
    ("description", "description", str),
    ("rating", "rating", _safe_float),
    ("status", "status", str),
)
_TV_FIELDS: tuple[tuple[str, str, Any], ...] = (
    ("number_of_episodes", "number_of_episodes", _safe_int),
    # ``number_of_seasons`` is an inert orphan column in the per-season work
    # model — not filled here.
    ("start_date", "start_date", _parse_date),
    ("end_date", "end_date", _parse_date),
)
_MOVIE_FIELDS: tuple[tuple[str, str, Any], ...] = (
    ("release_date", "release_date", _parse_date),
    ("runtime", "runtime", _safe_int),
)


def _candidate_from_result(
    result: dict[str, Any], request: MetadataSearchRequest
) -> MetadataCandidate:
    local_id = result.get("_local_id")
    if request.mode == "local":
        return MetadataCandidate(
            origin="local",
            content_type=request.content_type,
            title_cn=result.get("title_cn"),
            title_en=result.get("title_en"),
            original_title=result.get("original_title"),
            year=result.get("year"),
            poster_url=result.get("poster_url"),
            work_id=local_id,
            match_path="local",
            selectable=bool(local_id),
            unavailable_reason=None if local_id else "local work id is missing",
            metadata={},
        )

    identity_source = str(result.get("external_source") or "").lower() or None
    external_id = result.get("external_id")
    # Web fallback candidates identify themselves by the registry source they
    # resolved from. Primary candidates normally share the requested source.
    match_path = "web_fallback" if identity_source and identity_source != request.source else "primary"
    selectable = bool(identity_source in REGISTRY_SOURCES and external_id)
    metadata = {
        key: value
        for key, value in result.items()
        if not key.startswith("_")
    }
    return MetadataCandidate(
        origin="external",
        content_type=request.content_type,
        title_cn=result.get("title_cn"),
        title_en=result.get("title_en"),
        original_title=result.get("original_title"),
        year=result.get("year"),
        poster_url=result.get("poster_url"),
        primary_source=request.source,
        identity_source=identity_source,
        external_id=external_id,
        match_path=match_path,
        selectable=selectable,
        unavailable_reason=None if selectable else "candidate has no trusted external identity",
        metadata=metadata,
    )


async def search_metadata_candidates(
    db: AsyncSession, request: MetadataSearchRequest, *, season_hint: int | None = None
) -> list[MetadataCandidate]:
    """Search one local or external source without mutating application data.

    ``season_hint`` is the season number of the work being refreshed (the
    batch/periodic refresh passes the target work's own ``season_number``) —
    it keeps season-granular sources (bangumi) from matching the season-1
    entry for a season>1 work.
    """
    if request.mode == "online" and not is_metadata_source_available(request.source or ""):
        raise HTTPException(status_code=400, detail="metadata source is not available")
    source = "local" if request.mode == "local" else request.source
    results = await manual_search_metadata(
        db,
        request.query,
        request.content_type,
        source,
        request.trusted_sites,
        season_hint=season_hint,
    )
    # Manual search historically returned another type when no preferred
    # candidate existed. The public boundary is now strict.
    return [
        _candidate_from_result(result, request)
        for result in results
        if result.get("content_type") == request.content_type
        and (request.mode != "local" or bool(result.get("_local_id")))
    ]


def _candidate_values(candidate: MetadataCandidate) -> dict[str, Any]:
    values = dict(candidate.metadata)
    for key in ("title_cn", "title_en", "original_title", "poster_url", "external_id"):
        if values.get(key) in (None, ""):
            values[key] = getattr(candidate, key)
    values["external_source"] = candidate.identity_source
    return values


def _comparable(value: Any) -> Any:
    if isinstance(value, date):
        return value.isoformat()
    return value


async def preview_work_metadata(
    db: AsyncSession,
    work_id: str,
    content_type: str,
    candidate: MetadataCandidate,
    override_manual_edits: bool,
    only_missing: bool = False,
) -> dict[str, Any]:
    work = await db.get(Movie if content_type == "movie" else TVSeries, work_id)
    if work is None:
        raise HTTPException(status_code=404, detail="work not found")
    values = _candidate_values(candidate)
    if content_type == "tv":
        # Season-scoped dates (same semantics as the upsert path): a
        # series-level entity's premiere/finale belongs to season 1 and must
        # never be written into a later-season work.
        season = getattr(work, "season_number", None) or 1
        granularity = granularity_of(candidate.identity_source, "tv") or "series"
        values["start_date"] = _work_start_date(values, season, granularity)
        values["end_date"] = _work_end_date(values, season, granularity)
    manual = manually_edited_fields(work)
    fields = _COMMON_FIELDS + (_MOVIE_FIELDS if content_type == "movie" else _TV_FIELDS)
    changes: list[dict[str, Any]] = []
    for attr, key, caster in fields:
        incoming = values.get(key)
        if incoming in (None, ""):
            continue
        incoming = caster(incoming)
        if incoming is None:
            continue
        current = getattr(work, attr)
        if only_missing and current not in (None, "", [], ()):
            continue
        if _comparable(current) == _comparable(incoming):
            continue
        protected = attr in manual and not override_manual_edits
        changes.append({
            "field": attr,
            "current": _comparable(current),
            "incoming": _comparable(incoming),
            "protected": protected,
            "action": "skip" if protected else "update",
        })
    genre = normalize_genres(values.get("genre"))
    if genre and genre != (work.genre or []) and not (only_missing and work.genre):
        protected = "genre" in manual and not override_manual_edits
        changes.append({"field": "genre", "current": work.genre or [], "incoming": genre,
                        "protected": protected, "action": "skip" if protected else "update"})
    for field in ("poster_url", "is_anime"):
        incoming = values.get(field)
        current = getattr(work, field)
        if incoming in (None, "") or incoming == current:
            continue
        if only_missing and current not in (None, ""):
            continue
        protected = field in manual and not override_manual_edits
        changes.append({"field": field, "current": current, "incoming": incoming,
                        "protected": protected, "action": "skip" if protected else "update"})
    # Identity is creator-wins and bag-only: the candidate's
    # external_id/external_source go to the WorkExternalId bag in
    # apply_work_metadata, NEVER onto the primary columns (even when empty) —
    # so they are deliberately not previewed here.
    # ``seasons`` / ``number_of_seasons`` are inert orphan columns in the
    # per-season work model — never previewed or written here.
    return {"changes": changes, "warnings": []}


async def apply_work_metadata(
    db: AsyncSession,
    work_id: str,
    content_type: str,
    candidate: MetadataCandidate,
    override_manual_edits: bool,
    only_missing: bool = False,
) -> dict[str, Any]:
    work_type = "movie" if content_type == "movie" else "series"
    work = await db.get(Movie if content_type == "movie" else TVSeries, work_id)
    if work is None:
        raise HTTPException(status_code=404, detail="work not found")
    owner = await find_work_by_external_id(
        db, work_type, candidate.identity_source, candidate.external_id
    )
    if owner is not None and owner.id != work.id:
        raise HTTPException(status_code=409, detail="external identity belongs to another work")
    if candidate.external_id:
        # The bag lookup above misses ids only held in another work's PRIMARY
        # column (never bagged) — check the column too before filling it.
        model = Movie if work_type == "movie" else TVSeries
        column_taken = (await db.execute(
            select(model.id).where(
                model.external_id == candidate.external_id, model.id != work.id,
            )
        )).first()
        if column_taken:
            raise HTTPException(
                status_code=409, detail="external identity belongs to another work",
            )
    other_type = "series" if work_type == "movie" else "movie"
    if await find_work_by_external_id(
        db, other_type, candidate.identity_source, candidate.external_id
    ) is not None:
        raise HTTPException(status_code=409, detail="external identity belongs to another work type")

    preview = await preview_work_metadata(
        db, work_id, content_type, candidate, override_manual_edits, only_missing
    )
    applied: list[str] = []
    for change in preview["changes"]:
        if change["action"] != "update":
            continue
        field = change["field"]
        if field in {"poster_url", "is_anime", "seasons"}:
            continue
        value = change["incoming"]
        if field in {"start_date", "end_date", "release_date"}:
            value = _parse_date(value)
        setattr(work, field, value)
        applied.append(field)

    if candidate.poster_url and not (only_missing and work.poster_url) and (
        override_manual_edits or "poster_url" not in manually_edited_fields(work)
    ):
        cached = await download_and_cache_poster(candidate.poster_url)
        poster = cached or candidate.poster_url
        if work.poster_url != poster:
            work.poster_url = poster
            applied.append("poster_url")

    await add_external_id(
        db, work_type, work.id, candidate.identity_source, candidate.external_id
    )
    values = _candidate_values(candidate)
    if values.get("is_anime") is not None and not (only_missing and work.is_anime is not None):
        previous_is_anime = work.is_anime
        if override_manual_edits:
            work.is_anime = bool(values["is_anime"])
        else:
            apply_is_anime(work, values)
        if work.is_anime != previous_is_anime:
            applied.append("is_anime")
    if content_type == "tv":
        # ``seasons`` is an inert orphan column (per-season work model) — not
        # written. Episode rows still upsert, season-scoped by upsert_episodes.
        if values.get("episode_list"):
            await upsert_episodes(db, work, values["episode_list"])
    await db.commit()
    return {"applied": applied, "skipped": [c["field"] for c in preview["changes"] if c["action"] == "skip"]}


async def refresh_work_by_source(
    db: AsyncSession,
    work: TVSeries | Movie | None,
    content_type: str,
    source: str,
    *,
    trusted_sites: list[str] | None = None,
    override_manual_edits: bool = False,
    only_missing: bool = True,
) -> dict[str, Any]:
    """Re-search one work against ``source`` and apply the best candidate.

    This is THE single refresh execution path: the manual batch endpoint and
    the per-channel periodic refresh (both via ``_refresh_works_batch``) and
    maintenance scripts all funnel through here. Season works are searched
    with their own ``season_number`` as ``season_hint`` (bangumi season-aware
    auto-link/judge picks the right season's entry) and dates are written
    back with season-level semantics inside ``preview_work_metadata`` — a
    series-level entity's premiere belongs to season 1 and never lands on a
    later-season work. Season-0 specials works are skipped: a title search
    would match the MAIN entry and stuff its series-level data (premiere,
    episode count, identity) into the specials work.
    """
    if work is None:
        return {"found": False, "applied": [], "message": "work not found"}
    season = getattr(work, "season_number", None) if content_type == "tv" else None
    if season == 0:
        # No network search for specials (a title search would match the main
        # entry), but a NULL start_date still converges deterministically from
        # the collection's regular seasons — the periodic channel refresh then
        # repairs existing specials works over time.
        applied: list[str] = []
        if (
            getattr(work, "collection_id", None)
            and work.start_date is None
            and "start_date" not in manually_edited_fields(work)
        ):
            fallback = await _collection_fallback_start_date(
                db, work.collection_id, work.id
            )
            if fallback:
                work.start_date = fallback
                await db.commit()
                applied.append("start_date")
        return {
            "found": True, "applied": applied,
            "message": "season-0 specials work — refresh skipped",
        }
    query = next((value for value in (
        getattr(work, "title_en", None), getattr(work, "title_cn", None),
        getattr(work, "original_title", None),
    ) if value), None)
    if not query:
        return {"found": False, "applied": [], "message": "no title available to search"}
    candidates = await search_metadata_candidates(
        db,
        MetadataSearchRequest(
            query=query, content_type=content_type, mode="online",
            source=source, trusted_sites=trusted_sites,
        ),
        season_hint=season,
    )
    candidate = next(
        (c for c in candidates if c.selectable and not c.metadata.get("ambiguous")), None
    )
    if candidate is None:
        return {"found": False, "applied": [], "message": "no deterministic candidate"}
    echo = {
        "title_cn": candidate.title_cn,
        "title_en": candidate.title_en,
        "external_id": candidate.external_id,
        "external_source": candidate.identity_source,
    }
    try:
        applied = await apply_work_metadata(
            db, work.id, content_type, candidate,
            override_manual_edits=override_manual_edits, only_missing=only_missing,
        )
    except HTTPException as e:
        if e.status_code == 409:
            # The matched identity belongs to another work — never steal it;
            # the pair is a dedup candidate (merge via POST /works/merge).
            return {
                "found": True, "applied": [], "identity_conflict": True,
                "message": str(e.detail), "candidate": echo,
            }
        raise
    label = candidate.title_cn or candidate.title_en or candidate.original_title or ""
    return {
        "found": True, **applied,
        "message": f"matched: {label}" if label else "matched",
        "candidate": echo,
    }
