"""Persistence layer for the metadata agent: cache + write-back.

Extracted from ``metadata_agent`` (Phase 2). The agent keeps thin delegating
methods (``_apply_to_resource`` / ``_get_cache`` / ``_set_cache``) that forward
to these module functions, so tests that monkeypatch the methods on the agent
instance (``agent._get_cache = AsyncMock()``) still intercept calls from
``process``.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.metadata_audio import AUDIO_CONTENT_TYPES
from app.services.metadata_episode_reconcile import (
    _seasons_map_from,
    apply_episode_reconcile,
    resource_season_hint,
)
from app.services.metadata_resource_meta import ResourceMetadata
from app.services.metadata_sources import normalize_metadata_source_type
from app.services.subtitle_groups import (
    join_legacy_subtitle_group,
    normalize_group_key,
    normalize_subtitle_groups,
    subtitle_groups_for_resource,
)
from app.utils.time import utcnow

logger = logging.getLogger(__name__)


async def _series_has_episode_evidence(db: AsyncSession, series_id: str) -> bool:
    """Whether a cross-table owner has evidence that it really is episodic."""
    from sqlalchemy import func, select

    from app.models.episode import Episode
    from app.models.file_resource import FileResource

    episode_rows = int((await db.execute(
        select(func.count()).select_from(Episode).where(Episode.series_id == series_id)
    )).scalar_one() or 0)
    if episode_rows:
        return True
    resource_rows = int((await db.execute(
        select(func.count()).select_from(FileResource).where(
            FileResource.series_id == series_id,
            FileResource.is_batch.is_(False),
            FileResource.episode.is_not(None),
        )
    )).scalar_one() or 0)
    return bool(resource_rows)


def _cache_source_key(data_source_type: str | None) -> str:
    """Cache namespace for one metadata source.

    The cache is keyed by ``(title, source)`` where ``source`` carries both the
    cache type and the data source, e.g. ``"metadata_agent:tmdb"``. This keeps
    results from one source (e.g. wigolo) from being returned for a channel
    configured with another (e.g. Jina) - switching a channel's source no
    longer serves stale results from the old source.
    """
    ns = normalize_metadata_source_type(data_source_type)
    return f"metadata_agent:{ns}"


def _fill_subtitle_group(meta: ResourceMetadata, resource: Any) -> None:
    """Fill a missing release group from parsed metadata without overwriting it.

    Release-title extraction remains useful even when the work lookup itself
    returns ``found=false``.  Existing non-empty values win because they may
    come from a channel mapping or a manual correction.
    """
    current = subtitle_groups_for_resource(resource)
    if not getattr(resource, "subtitle_groups", None) and current:
        resource.subtitle_groups = list(current)
        resource.subtitle_groups_source = "legacy"
    candidate = meta.subtitle_groups
    if candidate is None:
        candidate = normalize_subtitle_groups(meta.subtitle_group)
    if candidate and (
        not current
        or getattr(resource, "subtitle_groups_source", None) == "unresolved"
    ):
        resource.subtitle_groups = list(candidate)
        resource.subtitle_groups_source = "llm"
        resource.subtitle_group = join_legacy_subtitle_group(candidate)


async def _register_subtitle_group_mapping(
    meta: ResourceMetadata, resource: Any, db: AsyncSession,
) -> None:
    """Persist a constrained LLM split for reuse by future feed items.

    The model may only register groups that are exact members of the parser's
    candidate split.  This keeps the learned registry from inventing labels
    while still allowing an unresolved compound value to be promoted after a
    successful metadata pass.
    """
    raw = getattr(resource, "subtitle_group", None)
    candidate = normalize_subtitle_groups(meta.subtitle_groups, split=False)
    if not raw or len(candidate) < 2:
        return
    allowed = {normalize_group_key(x) for x in normalize_subtitle_groups(raw)}
    if not candidate or not all(normalize_group_key(x) in allowed for x in candidate):
        return
    from sqlalchemy import select

    from app.models.subtitle_group_mapping import SubtitleGroupMapping
    key = normalize_group_key(raw)
    row = (await db.execute(
        select(SubtitleGroupMapping).where(SubtitleGroupMapping.normalized_key == key)
    )).scalar_one_or_none()
    if row is None:
        row = SubtitleGroupMapping(
            raw_value=str(raw), normalized_key=key,
            groups=candidate, resolution="llm",
        )
        db.add(row)
    elif row.resolution not in {"manual", "llm"}:
        row.groups = candidate
        row.resolution = "llm"


async def _apply_to_resource(
    meta: ResourceMetadata,
    resource: Any,
    channel: Any,
    db: AsyncSession,
) -> None:
    """Write metadata results back to the FileResource and DB."""
    # A failed source lookup is not title-parsing evidence.  In particular,
    # web-fallback misses can echo the whole release title (group / episode /
    # codec decorations included) as ``clean_title``.  Overwriting the
    # parser-derived search title with that value makes every later retry
    # strictly worse and also prevents sibling-title reuse.  Only a confirmed
    # match may refine the persisted search title.
    if meta.found and meta.clean_title:
        resource.search_title = meta.clean_title

    if meta.found and meta.content_type == "tv":
        if meta.episode is not None:
            resource.episode = resource.episode or meta.episode
        if meta.season is not None:
            resource.season = resource.season or meta.season
    # Batch info — LLM output overrides pre-parser only when non-null.
    if meta.is_batch:
        resource.is_batch = True
        # Default to a single-season pack when the LLM gave no scope; a
        # later torrent content analysis may correct this to multi_season /
        # franchise. Never downgrade an existing multi_season / franchise
        # (torrent analysis may already have run ahead of the LLM) — only
        # write when the current value is None or "season".
        if resource.batch_scope in (None, "season"):
            resource.batch_scope = meta.batch_scope or "season"
    if meta.episode_start is not None:
        resource.episode_start = meta.episode_start
    if meta.episode_end is not None:
        resource.episode_end = meta.episode_end
    # A batch resource must not carry a stray single ``episode`` — that
    # would confuse downstream dedup logic. Clear it if the LLM committed.
    if resource.is_batch:
        resource.episode = None
    if meta.title_cn:
        resource.title_cn = resource.title_cn or meta.title_cn
    if meta.title_en:
        resource.title_en = resource.title_en or meta.title_en
    _fill_subtitle_group(meta, resource)
    await _register_subtitle_group_mapping(meta, resource, db)
    # All release-description fields are part of the metadata result.  The
    # old write-back only persisted subtitle languages, leaving resolution,
    # source, codecs, subtitle type and container permanently empty even when
    # the title/LLM had already resolved them.  Non-empty parser values remain
    # authoritative for subtitle_group; every other non-null result is safe to
    # write back (including an explicit empty subtitle-language list).
    for field_name in (
        "resolution",
        "source",
        "video_codec",
        "audio_codec",
        "subtitle_type",
        "container",
    ):
        value = getattr(meta, field_name, None)
        if value is not None and (not isinstance(value, str) or value.strip()):
            setattr(resource, field_name, value)
    if meta.subtitle_langs is not None:
        resource.subtitle_langs = list(meta.subtitle_langs)

    # Cross-season episode reconciliation. Runs on single-episode TV
    # resources only — batches are aggregated ranges and movies don't
    # carry an episode number. The pre-parser's NN(MM) hit is already
    # recorded on the resource (episode_confidence == "reconciled");
    # skip further work when that ran.
    if meta.found and meta.content_type == "tv":
        apply_episode_reconcile(resource, _seasons_map_from(meta.matched_entity))

    # Link to TVSeries / Movie / AudioWork
    if meta.found and meta.matched_entity:
        # Franchise resources are collection-owned.  Torrent enrichment may
        # already have created the parent collection before this ordinary
        # metadata pass runs; never let a title-level single-work match write
        # a flat FK back onto the resource and undo that invariant.
        from app.services.franchise_service import (
            enforce_franchise_resource_invariant,
            is_franchise_resource,
        )
        if is_franchise_resource(resource):
            enforce_franchise_resource_invariant(resource)
            resource.metadata_matched_at = utcnow()
            return

        from app.services.metadata_service import (
            create_or_update_audio_work_from_external,
            create_or_update_movie_from_external,
            create_or_update_series_from_external,
            find_movie_by_external_id,
            find_series_by_external_id,
        )

        if meta.content_type in AUDIO_CONTENT_TYPES:
            # The LLM verdict's content_type lives on the ResourceMetadata,
            # never inside matched_entity — inject it so the upsert does not
            # fall back to "other". When the matched entity carries no title
            # at all, fall back to the titles on the metadata itself; if even
            # those are empty, skip linking instead of inserting a shell row.
            audio_data = dict(meta.matched_entity)
            audio_data.setdefault("content_type", meta.content_type)
            title_keys = ("title_cn", "title_en", "original_title")
            if not any(audio_data.get(k) for k in title_keys):
                fallback_title = meta.title_cn or meta.title_en or meta.clean_title
                if fallback_title:
                    audio_data["title_cn"] = fallback_title
            if not any(audio_data.get(k) for k in title_keys):
                logger.warning(
                    "[metadata] audio verdict for %r has no usable title; "
                    "leaving the resource unmatched instead of creating an empty AudioWork",
                    meta.clean_title[:60],
                )
            else:
                audio = await create_or_update_audio_work_from_external(db, audio_data)
                if audio is not None:
                    resource.audio_work_id = audio.id
                    resource.series_id = None
                    resource.movie_id = None
        elif meta.content_type == "movie":
            # Cross-table guard: the same external entity may already exist
            # as a TVSeries (an earlier tv classification, or a manual
            # correction). Creating a second row in the movies table would
            # split one work across tables - flip the dispatch instead.
            existing_series = await find_series_by_external_id(db, meta.matched_entity)
            if existing_series is not None:
                # A legacy row may itself prove that it was filed in the wrong
                # table (TVSeries.content_type="movie").  The old guard kept
                # that corruption forever and made required-field validation
                # ask a film for season/episode.  Repair it online when there
                # is no episodic evidence; otherwise retain the conservative
                # creator-wins behaviour for a genuinely conflicting verdict.
                if (
                    getattr(existing_series, "content_type", None) == "movie"
                    and not await _series_has_episode_evidence(db, existing_series.id)
                ):
                    movie = await create_or_update_movie_from_external(
                        db, meta.matched_entity
                    )
                    from app.services.metadata_dedup import rehome_series_as_movie
                    await rehome_series_as_movie(db, existing_series, movie)
                    resource.movie_id = movie.id
                    resource.series_id = None
                    resource.audio_work_id = None
                    from app.services.collection_service import link_movie_collection
                    await link_movie_collection(db, movie)
                else:
                    logger.warning(
                        "[metadata] movie verdict for %r but a series already owns "
                        "external_id=%r; linking the series instead",
                        (meta.matched_entity.get("title_cn") or meta.matched_entity.get("title_en") or "")[:60],
                        meta.matched_entity.get("external_id"),
                    )
                    series = await create_or_update_series_from_external(
                        db, meta.matched_entity,
                        season_hint=resource_season_hint(resource, meta.matched_entity),
                    )
                    if series is not None:
                        resource.series_id = series.id
                        resource.movie_id = None
                        resource.audio_work_id = None
                    else:
                        # Season indeterminate: link the known owner row.
                        resource.series_id = existing_series.id
                        resource.movie_id = None
                        resource.audio_work_id = None
            else:
                movie = await create_or_update_movie_from_external(db, meta.matched_entity)
                resource.movie_id = movie.id
                resource.series_id = None
                resource.audio_work_id = None
                # Deterministic TMDB collection link (no-op unless canonical
                # tmdb: id and the movie belongs to a collection).
                from app.services.collection_service import link_movie_collection
                await link_movie_collection(db, movie)
        else:
            # Symmetric guard: a tv verdict for an entity that already owns
            # a Movie row links the movie instead of duplicating.
            if await find_movie_by_external_id(db, meta.matched_entity) is not None:
                logger.warning(
                    "[metadata] tv verdict for %r but a movie already owns "
                    "external_id=%r; linking the movie instead",
                    (meta.matched_entity.get("title_cn") or meta.matched_entity.get("title_en") or "")[:60],
                    meta.matched_entity.get("external_id"),
                )
                movie = await create_or_update_movie_from_external(db, meta.matched_entity)
                resource.movie_id = movie.id
                resource.series_id = None
                resource.audio_work_id = None
                from app.services.collection_service import link_movie_collection
                await link_movie_collection(db, movie)
            else:
                series = await create_or_update_series_from_external(
                    db, meta.matched_entity,
                    season_hint=resource_season_hint(resource, meta.matched_entity),
                )
                if series is not None:
                    resource.series_id = series.id
                    resource.movie_id = None
                    resource.audio_work_id = None
                else:
                    # Season indeterminate over a collection: park the
                    # resource on the collection for Channel confirmation
                    # (挂合集待确认), never guess a season work.
                    from app.services.metadata_episode_reconcile import (
                        park_resource_on_collection,
                    )
                    from app.services.metadata_service import (
                        find_collection_for_entity,
                    )
                    collection = await find_collection_for_entity(db, meta.matched_entity)
                    if collection is not None:
                        park_resource_on_collection(resource, collection)

        # Post-link reconciliation + season-uncertain marking (P3): shared
        # helper — history-backed convention, collection-member absolute
        # locate, per-season arithmetic, then the verified-season default
        # (resolve_missing_work with the linked work's identity). Runs AFTER
        # the upsert so a season-less resource whose season can't be verified
        # is routed to a human ("季号不确定" Channel confirmation downstream).
        if resource.series_id:
            from app.services.metadata_service import (
                reconcile_linked_series_resource,
            )
            await reconcile_linked_series_resource(
                db, resource, entity=meta.matched_entity
            )

        resource.metadata_matched_at = utcnow()

        # Post-link is_anime classification: the channel's "默认标记为 Anime"
        # flag first, then the Bangumi layer-1 verification.
        from app.services.metadata_service import classify_is_anime_post_link
        await classify_is_anime_post_link(db, channel, resource)


async def _get_cache(
    raw_title: str, data_source_type: str | None, db: AsyncSession
) -> ResourceMetadata | None:
    from sqlalchemy import select

    from app.models.metadata_cache import METADATA_CACHE_GENERATION, MetadataCache

    source_key = _cache_source_key(data_source_type)
    result = await db.execute(
        select(MetadataCache).where(
            MetadataCache.title == raw_title.strip(),
            MetadataCache.source == source_key,
        )
    )
    cached = result.scalar_one_or_none()
    if cached and cached.generation != METADATA_CACHE_GENERATION:
        # Verdict produced by superseded matching/classification logic -
        # drop it and miss, so the fixed logic re-runs instead of being
        # short-circuited by its own predecessor's mistakes.
        logger.info(
            "[metadata_cache] discarding generation-%s verdict for %r (current=%s)",
            cached.generation, raw_title[:60], METADATA_CACHE_GENERATION,
        )
        await db.delete(cached)
        await db.flush()
        return None
    if cached and isinstance(cached.metadata_json, dict):
        return ResourceMetadata.from_dict(cached.metadata_json)
    return None


async def _set_cache(
    raw_title: str, data_source_type: str | None, meta: ResourceMetadata, db: AsyncSession
) -> None:
    import uuid

    from sqlalchemy import delete

    from app.models.metadata_cache import MetadataCache

    source_key = _cache_source_key(data_source_type)
    title = raw_title.strip()
    # Upsert: clear any existing row for this (title, source) so a
    # force_refresh re-run replaces the stale result instead of violating
    # the unique constraint, and different sources coexist as separate rows.
    await db.execute(
        delete(MetadataCache).where(
            MetadataCache.title == title,
            MetadataCache.source == source_key,
        )
    )
    cache_entry = MetadataCache(
        id=str(uuid.uuid4()),
        title=title,
        source=source_key,
        content_type=meta.content_type,
        metadata_json={
            "clean_title": meta.clean_title,
            "content_type": meta.content_type,
            "found": meta.found,
            "inferred_episode": meta.episode,
            "inferred_season": meta.season,
            "is_batch": meta.is_batch,
            "inferred_episode_start": meta.episode_start,
            "inferred_episode_end": meta.episode_end,
            "title_cn": meta.title_cn,
            "title_en": meta.title_en,
            "subtitle_group": meta.subtitle_group,
            "subtitle_groups": meta.subtitle_groups,
            "resolution": meta.resolution,
            "source": meta.source,
            "video_codec": meta.video_codec,
            "audio_codec": meta.audio_codec,
            "subtitle_type": meta.subtitle_type,
            "subtitle_langs": meta.subtitle_langs,
            "container": meta.container,
            "matched_entity": meta.matched_entity,
            "confidence": meta.confidence,
            "reason": meta.reason,
            "ambiguous": meta.ambiguous,
            "ambiguous_candidates": meta.ambiguous_candidates,
            "season_ambiguous": meta.season_ambiguous,
            "search_method": meta.search_method,
            "data_sources_used": meta.data_sources_used,
            "source_errors": meta.source_errors,
            "search_error": meta.search_error,
        },
    )
    db.add(cache_entry)
    await db.flush()
