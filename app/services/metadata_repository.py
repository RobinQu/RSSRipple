"""Persistence layer for the metadata agent: cache + write-back.

Extracted from ``metadata_agent`` (Phase 2). The agent keeps thin delegating
methods (``_apply_to_resource`` / ``_get_cache`` / ``_set_cache``) that forward
to these module functions, so tests that monkeypatch the methods on the agent
instance (``agent._get_cache = AsyncMock()``) still intercept calls from
``process``.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.metadata_audio import AUDIO_CONTENT_TYPES
from app.services.metadata_episode_reconcile import _seasons_map_from, reconcile_episode
from app.services.metadata_resource_meta import ResourceMetadata
from app.services.metadata_sources import normalize_metadata_source_type
from app.utils.time import utcnow


def _cache_source_key(data_source_type: str | None) -> str:
    """Cache namespace for one metadata source.

    The cache is keyed by ``(title, source)`` where ``source`` carries both the
    cache type and the data source, e.g. ``"metadata_agent:jina"``. This keeps
    results from one source (e.g. Exa) from being returned for a channel
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
    if (
        meta.found
        and meta.content_type == "tv"
        and not resource.is_batch
        and resource.episode is not None
        and resource.season is not None
        and getattr(resource, "episode_confidence", None) not in ("manual", "reconciled")
    ):
        seasons_map = _seasons_map_from(meta.matched_entity)
        reconciled = reconcile_episode(
            raw_episode=resource.episode,
            raw_season=resource.season,
            seasons_map=seasons_map,
        )
        if reconciled is not None:
            new_episode, abs_ep, confidence = reconciled
            resource.episode = new_episode
            if abs_ep is not None:
                resource.absolute_episode = abs_ep
            resource.episode_confidence = confidence
        elif getattr(resource, "episode_confidence", None) is None:
            # No seasons_map / no basis to reconcile — mark as raw so
            # downstream code can distinguish "never reconciled" from
            # "reconciled and unchanged".
            resource.episode_confidence = "raw"

    # Link to TVSeries / Movie / AudioWork
    if meta.found and meta.matched_entity:
        from app.services.metadata_service import (
            create_or_update_audio_work_from_external,
            create_or_update_movie_from_external,
            create_or_update_series_from_external,
        )

        if meta.content_type in AUDIO_CONTENT_TYPES:
            audio = await create_or_update_audio_work_from_external(
                db, meta.matched_entity
            )
            resource.audio_work_id = audio.id
            resource.series_id = None
            resource.movie_id = None
        elif meta.content_type == "movie":
            movie = await create_or_update_movie_from_external(db, meta.matched_entity)
            resource.movie_id = movie.id
            resource.series_id = None
            resource.audio_work_id = None
        else:
            series = await create_or_update_series_from_external(db, meta.matched_entity)
            resource.series_id = series.id
            resource.movie_id = None
            resource.audio_work_id = None

        resource.metadata_matched_at = utcnow()


async def _get_cache(
    raw_title: str, data_source_type: str | None, db: AsyncSession
) -> ResourceMetadata | None:
    from sqlalchemy import select

    from app.models.metadata_cache import MetadataCache

    source_key = _cache_source_key(data_source_type)
    result = await db.execute(
        select(MetadataCache).where(
            MetadataCache.title == raw_title.strip(),
            MetadataCache.source == source_key,
        )
    )
    cached = result.scalar_one_or_none()
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
            "search_method": meta.search_method,
            "data_sources_used": meta.data_sources_used,
            "source_errors": meta.source_errors,
            "search_error": meta.search_error,
        },
    )
    db.add(cache_entry)
    await db.flush()
