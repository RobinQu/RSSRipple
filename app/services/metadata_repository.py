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
    seasons_map_from_list,
)
from app.services.metadata_resource_meta import ResourceMetadata
from app.services.metadata_sources import normalize_metadata_source_type
from app.utils.time import utcnow

logger = logging.getLogger(__name__)


def _cache_source_key(data_source_type: str | None) -> str:
    """Cache namespace for one metadata source.

    The cache is keyed by ``(title, source)`` where ``source`` carries both the
    cache type and the data source, e.g. ``"metadata_agent:jina"``. This keeps
    results from one source (e.g. wigolo) from being returned for a channel
    configured with another (e.g. Jina) - switching a channel's source no
    longer serves stale results from the old source.
    """
    ns = normalize_metadata_source_type(data_source_type)
    return f"metadata_agent:{ns}"


async def _apply_to_resource(
    meta: ResourceMetadata,
    resource: Any,
    channel: Any,
    db: AsyncSession,
) -> None:
    """Write metadata results back to the FileResource and DB."""
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
    # LLM output overrides pre-parser only when it actually returned
    # something. ``[]`` is treated as "LLM saw no marker either", still
    # useful signal — keep it.
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
            if await find_series_by_external_id(db, meta.matched_entity) is not None:
                logger.warning(
                    "[metadata] movie verdict for %r but a series already owns "
                    "external_id=%r; linking the series instead",
                    (meta.matched_entity.get("title_cn") or meta.matched_entity.get("title_en") or "")[:60],
                    meta.matched_entity.get("external_id"),
                )
                series = await create_or_update_series_from_external(db, meta.matched_entity)
                resource.series_id = series.id
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
                series = await create_or_update_series_from_external(db, meta.matched_entity)
                resource.series_id = series.id
                resource.movie_id = None
                resource.audio_work_id = None

        # Fallback reconciliation: the agent's own search result may carry no
        # usable seasons list, but the (already known) series may have
        # per-season counts persisted from an earlier upsert.
        if (
            resource.series_id
            and getattr(resource, "episode_confidence", None) is None
        ):
            from app.models.series import TVSeries
            series_row = await db.get(TVSeries, resource.series_id)
            if series_row is not None and series_row.seasons:
                apply_episode_reconcile(resource, seasons_map_from_list(series_row.seasons))

        # Season-uncertain marking. Runs AFTER both reconciliations above:
        # reconcile may legitimately derive a season from absolute_episode,
        # and only a resource whose season is STILL None afterwards is routed
        # to a human ("季号不确定" PendingDecision downstream). Batch
        # resources are excluded — a 合集 intentionally bypasses per-episode
        # flow and doesn't need a verified single season number to dispatch.
        if (
            meta.season_ambiguous
            and resource.series_id
            and resource.season is None
            and not getattr(resource, "is_batch", False)
            and getattr(resource, "episode_confidence", None) != "manual"
        ):
            resource.episode_confidence = "ambiguous"

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
