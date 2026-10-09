"""Canonical metadata search, preview, and manual-apply application service."""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select, update
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
    resolve_wikipedia_slug_id,
    upsert_episodes,
)
from app.services.metadata_source_registry import REGISTRY_SOURCES, granularity_of, wikipedia_url_lang
from app.services.metadata_sources import is_metadata_source_available

_WORK_SCOPE_CHANGED = "work season or collection changed during lookup; refresh again"


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


def _bangumi_listing_result(subject: dict[str, Any]) -> dict[str, Any]:
    """Map one Bangumi search-hit subject to a manual-search result dict.

    Thin by design (search-response fields only) — preview/apply expand the
    subject's details + episode list on demand once the user picks it.
    ``subject_season`` carries the entry's own title season marker so the UI
    can annotate the list against the requested ``season_hint``.
    """
    from app.services.metadata_bangumi import _subject_season

    sid = subject.get("id")
    platform = str(subject.get("platform") or "")
    date = str(subject.get("date") or "")
    images = subject.get("images") or {}
    return {
        "content_type": "movie" if platform in {"剧场版", "电影"} else "tv",
        "title_cn": subject.get("name_cn") or subject.get("name"),
        "original_title": subject.get("name"),
        "external_source": "bangumi",
        "external_id": f"bangumi:{sid}",
        "year": int(date[:4]) if date[:4].isdigit() else None,
        "poster_url": images.get("large") or images.get("common"),
        "description": (subject.get("summary") or "")[:2000] or None,
        "rating": (subject.get("rating") or {}).get("score"),
        "start_date": subject.get("date") or None,
        "subject_season": _subject_season(subject),
    }


async def search_metadata_candidates(
    db: AsyncSession,
    request: MetadataSearchRequest,
    *,
    season_hint: int | None = None,
    listing: bool = False,
) -> list[MetadataCandidate]:
    """Search one local or external source without mutating application data.

    ``season_hint`` is the season number of the work being refreshed (the
    batch/periodic refresh passes the target work's own ``season_number``) —
    it keeps season-granular sources (bangumi) from matching the season-1
    entry for a season>1 work.

    ``listing=True`` (the manual-search UI) turns the bangumi source into a
    LIST mode: the raw subject search hits are returned as-is (one candidate
    per subject, no auto-link/judge convergence, no LLM call). The refresh
    pipeline keeps the pick-one behavior (``listing`` stays False there).
    """
    if request.mode == "online" and not is_metadata_source_available(request.source or ""):
        raise HTTPException(status_code=400, detail="metadata source is not available")
    if listing and request.mode == "online" and request.source == "bangumi":
        from app.services.metadata_bangumi import list_bangumi_subjects

        subjects = await list_bangumi_subjects(request.query)
        return [
            _candidate_from_result(result, request)
            for result in (_bangumi_listing_result(s) for s in subjects)
            if result.get("content_type") == request.content_type
        ]
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


def _bangumi_subject_id(external_id: str | None) -> int | None:
    if not external_id or not external_id.startswith("bangumi:"):
        return None
    digits = external_id.split(":", 1)[1]
    return int(digits) if digits.isdigit() else None


async def _expanded_candidate_values(
    work: TVSeries | Movie, content_type: str, candidate: MetadataCandidate
) -> dict[str, Any]:
    """Candidate values, expanded on demand for thin bangumi listing picks.

    Listing-mode candidates carry only the search-response fields; a selected
    bangumi identity is expanded via the subject details + episodes endpoints
    (``build_entity_for_subject``) so preview diffs and apply episode upserts
    see the full entity. Any expansion failure keeps the thin values — the
    preview then simply shows fewer changes. The candidate's own identity
    always wins over the expanded entity's.
    """
    values = _candidate_values(candidate)
    if candidate.identity_source != "bangumi" or values.get("episode_list"):
        return values
    sid = _bangumi_subject_id(candidate.external_id)
    if sid is None:
        return values
    season = (getattr(work, "season_number", None) or 1) if content_type == "tv" else 1
    from app.services.metadata_bangumi import build_entity_for_subject

    try:
        entity = await build_entity_for_subject(sid, season=season)
    except Exception:  # noqa: BLE001 — best-effort expansion
        return values
    if not entity:
        return values
    entity.pop("_content_type", None)
    entity.pop("_platform", None)
    for key, value in entity.items():
        if value is not None:
            values[key] = value
    values["external_id"] = candidate.external_id
    values["external_source"] = candidate.identity_source
    if content_type == "movie" and not values.get("release_date"):
        # The bangumi entity carries start_date; the movie diff reads release_date.
        values["release_date"] = values.get("start_date")
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
    *,
    resolved_values: dict[str, Any] | None = None,
) -> dict[str, Any]:
    work = await db.get(Movie if content_type == "movie" else TVSeries, work_id)
    if work is None:
        raise HTTPException(status_code=404, detail="work not found")
    # ``resolved_values`` lets apply pass its already-expanded values through
    # (a bangumi listing pick expands via the details/episodes endpoints —
    # once per apply, not twice).
    values = (
        dict(resolved_values)
        if resolved_values is not None
        else await _expanded_candidate_values(work, content_type, candidate)
    )
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


async def _bag_candidate_identity(
    db: AsyncSession,
    work_type: str,
    work_id: str,
    candidate: MetadataCandidate,
    values: dict[str, Any],
) -> None:
    """Bag the candidate's identity; slug-form wikipedia ids additionally bag
    the MediaWiki-resolved pageid so slug and numeric forms converge."""
    await add_external_id(
        db, work_type, work_id, candidate.identity_source, candidate.external_id
    )
    if candidate.identity_source == "wikipedia":
        resolved = await resolve_wikipedia_slug_id(
            candidate.external_id, values.get("wikipedia_url") or values.get("url")
        )
        if resolved:
            await add_external_id(db, work_type, work_id, "wikipedia", resolved)


async def apply_work_metadata(
    db: AsyncSession,
    work_id: str,
    content_type: str,
    candidate: MetadataCandidate,
    override_manual_edits: bool,
    only_missing: bool = False,
    *,
    identity_only: bool = False,
) -> dict[str, Any]:
    work_type = "movie" if content_type == "movie" else "series"
    from app.services.task_queue import require_execution_ownership

    await require_execution_ownership()
    work = await db.get(Movie if content_type == "movie" else TVSeries, work_id)
    if work is None:
        raise HTTPException(status_code=404, detail="work not found")
    expected_scope = (getattr(work, "season_number", None), work.collection_id)
    # identity_only (web-fallback refresh picks) needs no content values —
    # skip the on-demand bangumi expansion, which may hit the network.
    values = (
        _candidate_values(candidate)
        if identity_only
        else await _expanded_candidate_values(work, content_type, candidate)
    )
    poster_url = None if identity_only else (candidate.poster_url or values.get("poster_url"))
    cached_poster = None
    if poster_url and not (only_missing and work.poster_url) and (
        override_manual_edits or "poster_url" not in manually_edited_fields(work)
    ):
        cached_poster = await download_and_cache_poster(poster_url)
    await require_execution_ownership()

    # Take write ownership only after remote work. PostgreSQL serializes the
    # row here; Turso rejects a stale MVCC writer so its caller can retry.
    # Refresh the identity map before calculating manual-field exclusions.
    model = Movie if content_type == "movie" else TVSeries
    await db.execute(
        update(model).where(model.id == work_id)
        .values(updated_at=model.updated_at)
        .execution_options(synchronize_session=False)
    )
    work = await db.get(model, work_id, populate_existing=True)
    if work is None:
        raise HTTPException(status_code=404, detail="work not found")
    if (getattr(work, "season_number", None), work.collection_id) != expected_scope:
        raise HTTPException(status_code=409, detail=_WORK_SCOPE_CHANGED)
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

    if identity_only:
        # Web-fallback refresh pick: the fallback grounds identity/links only
        # (its matched_entity never carries trustworthy content — seasons,
        # counts and episode lists are stripped at the fallback exit), so the
        # refresh applies NO content fields; content stays primary-source
        # authoritative. Bag the identity and fill the source URL when empty.
        applied: list[str] = []
        url = values.get("wikipedia_url") or values.get("url")
        if (
            candidate.identity_source == "wikipedia"
            and url
            and wikipedia_url_lang(url)
            and work.wikipedia_url != url
            and (override_manual_edits or not work.wikipedia_url)
            and (override_manual_edits or "wikipedia_url" not in manually_edited_fields(work))
        ):
            work.wikipedia_url = url
            applied.append("wikipedia_url")
        await require_execution_ownership()
        await _bag_candidate_identity(db, work_type, work.id, candidate, values)
        await require_execution_ownership()
        await db.commit()
        return {"applied": applied, "skipped": [], "identity_only": True}

    preview = await preview_work_metadata(
        db, work_id, content_type, candidate, override_manual_edits, only_missing,
        resolved_values=values,
    )
    await require_execution_ownership()
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

    if poster_url and not (only_missing and work.poster_url) and (
        override_manual_edits or "poster_url" not in manually_edited_fields(work)
    ):
        poster = cached_poster or poster_url
        if work.poster_url != poster:
            work.poster_url = poster
            applied.append("poster_url")

    await require_execution_ownership()
    await _bag_candidate_identity(db, work_type, work.id, candidate, values)
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
    await require_execution_ownership()
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
    episode count, identity) into the specials work. When the requested
    primary source misses and the picked candidate comes from the ordered web
    fallback (``match_path="web_fallback"``), only identity/link fields are
    applied (identity bag + empty ``wikipedia_url``) — content stays
    authoritative to the primary source (``identity_only`` apply).
    """
    from app.services.task_queue import require_execution_ownership

    await require_execution_ownership()
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
            fallback_collection_id = work.collection_id
            fallback = await _collection_fallback_start_date(
                db, work.collection_id, work.id
            )
            if fallback:
                await require_execution_ownership()
                work_id = work.id
                await db.execute(
                    update(TVSeries).where(TVSeries.id == work_id)
                    .values(updated_at=TVSeries.updated_at)
                    .execution_options(synchronize_session=False)
                )
                work = await db.get(TVSeries, work_id, populate_existing=True)
                if work is None:
                    return {"found": False, "applied": [], "message": "work not found"}
                if work.season_number != 0 or work.collection_id != fallback_collection_id:
                    return {"found": True, "applied": [], "message": "work scope changed; fallback skipped"}
                if work.start_date is None and "start_date" not in manually_edited_fields(work):
                    work.start_date = fallback
                    await require_execution_ownership()
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
        # A web-fallback pick (the requested primary source missed and the
        # ordered web fallback supplied the identity) applies identity/links
        # only — content fields stay authoritative to the primary source.
        applied = await apply_work_metadata(
            db, work.id, content_type, candidate,
            override_manual_edits=override_manual_edits, only_missing=only_missing,
            identity_only=candidate.match_path == "web_fallback",
        )
    except HTTPException as e:
        if e.status_code == 409 and e.detail == _WORK_SCOPE_CHANGED:
            return {"found": False, "applied": [], "scope_changed": True, "message": e.detail}
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
