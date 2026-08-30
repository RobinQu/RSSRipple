"""Canonical metadata search, preview, and manual-apply application service."""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.movie import Movie
from app.models.series import TVSeries
from app.schemas.metadata_search import MetadataCandidate, MetadataSearchRequest
from app.services.anime_signals import apply_is_anime
from app.services.external_ids import add_external_id, find_work_by_external_id
from app.services.genre_registry import normalize_genres
from app.services.metadata_service import (
    _parse_date,
    _safe_float,
    _safe_int,
    download_and_cache_poster,
    manual_search_metadata,
    manually_edited_fields,
    seasons_overwrite_allowed,
    upsert_episodes,
)
from app.services.metadata_source_registry import REGISTRY_SOURCES
from app.services.metadata_sources import is_metadata_source_available

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
    ("number_of_seasons", "number_of_seasons", _safe_int),
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
    db: AsyncSession, request: MetadataSearchRequest
) -> list[MetadataCandidate]:
    """Search one local or external source without mutating application data."""
    if request.mode == "online" and not is_metadata_source_available(request.source or ""):
        raise HTTPException(status_code=400, detail="metadata source is not available")
    source = "local" if request.mode == "local" else request.source
    results = await manual_search_metadata(
        db,
        request.query,
        request.content_type,
        source,
        request.trusted_sites,
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
    if content_type == "tv":
        seasons = values.get("seasons")
        current_seasons = getattr(work, "seasons", None)
        if seasons and seasons != current_seasons and not (only_missing and current_seasons):
            allowed = seasons_overwrite_allowed(current_seasons, seasons)
            changes.append({"field": "seasons", "current": current_seasons,
                            "incoming": seasons, "protected": not allowed,
                            "action": "update" if allowed else "skip"})
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
        seasons = values.get("seasons")
        if (
            seasons
            and not (only_missing and getattr(work, "seasons", None))
            and seasons != getattr(work, "seasons", None)
            and seasons_overwrite_allowed(getattr(work, "seasons", None), seasons)
        ):
            work.seasons = seasons
            applied.append("seasons")
        if values.get("episode_list"):
            await upsert_episodes(db, work, values["episode_list"])
    await db.commit()
    return {"applied": applied, "skipped": [c["field"] for c in preview["changes"] if c["action"] == "skip"]}
