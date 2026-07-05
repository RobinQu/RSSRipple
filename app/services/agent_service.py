"""Agent service: DSL-based resource filtering and dispatch."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.agent import Agent
from app.models.agent_suggestion import AgentSuggestion
from app.models.download_task import DownloadTask
from app.models.file_resource import FileResource
from app.models.movie import Movie
from app.models.pending_decision import PendingDecision
from app.models.series import TVSeries
from app.services.filter_engine import evaluate_filter_config, merge_filters
from app.services.text_normalizer import partial_similarity_score
from app.utils.download_paths import DownloadPathError, resolve_download_dir
from app.utils.time import utcnow

logger = logging.getLogger(__name__)


@dataclass
class RunResult:
    total_resources: int = 0
    matched: int = 0
    dispatched: int = 0
    pending_decisions: int = 0
    filter_failed: int = 0
    duplicates_skipped: int = 0
    unrecognized: int = 0
    suggestions: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


_RESOLUTION_SCORE = {"2160p": 3, "4k": 3, "1080p": 2, "720p": 1}


def _resolution_score(resolution: str | None) -> int:
    if not resolution:
        return 0
    return _RESOLUTION_SCORE.get(resolution.lower().strip(), 0)


async def dispatch_download(
    agent: Agent, resource: FileResource, db: AsyncSession
) -> DownloadTask:
    """Create a DownloadTask and attempt to add it to Transmission."""
    from app.models.downloader import DownloaderInstance
    downloader = await db.get(DownloaderInstance, agent.downloader_id)
    if not downloader:
        task = DownloadTask(
            agent_id=agent.id,
            file_resource_id=resource.id,
            downloader_id=agent.downloader_id,
            download_dir=agent.download_subdir or "",
            status="error",
            error_message=f"Downloader {agent.downloader_id} not found",
            max_retries=settings.max_retry_count,
        )
        db.add(task)
        await db.flush()
        return task

    # Resolve the effective download directory, falling back to the downloader
    # root directory if subdir resolution fails.
    download_dir: str
    try:
        download_dir = resolve_download_dir(downloader.download_dir, agent.download_subdir)
    except DownloadPathError as e:
        download_dir = downloader.download_dir
        task = DownloadTask(
            agent_id=agent.id,
            file_resource_id=resource.id,
            downloader_id=agent.downloader_id,
            download_dir=download_dir,
            status="error",
            error_message=str(e),
            max_retries=settings.max_retry_count,
        )
        db.add(task)
        await db.flush()
        return task

    task = DownloadTask(
        agent_id=agent.id,
        file_resource_id=resource.id,
        downloader_id=agent.downloader_id,
        download_dir=download_dir,
        status="pending",
        max_retries=settings.max_retry_count,
    )
    db.add(task)
    await db.flush()

    from app.clients.downloader import get_downloader_client

    wrapper = get_downloader_client(downloader)
    try:
        result = await asyncio.wait_for(
            wrapper.add_torrent(
                resource.torrent_url,
                download_dir=task.download_dir,
            ),
            timeout=settings.transmission_timeout,
        )
        task.status = "downloading"
        task.transmission_torrent_id = result["torrent_id"]
        task.confirmed_at = utcnow()
    except Exception as e:
        logger.warning("Failed to add torrent for resource %s: %s", resource.id, e)
        task.status = "error"
        task.error_message = str(e)[:2000]

    return task


async def _generate_llm_suggestion(
    agent: Agent,
    candidates: list[FileResource],
    key: tuple,
) -> str | None:
    """Best-effort LLM suggestion for conflict resolution."""
    if not agent.llm_enabled or not settings.llm_api_key:
        return None
    try:
        from app.services.feed_analyzer import call_llm
        lines = ["Multiple resources matched the same item. Pick the best one:"]
        for i, c in enumerate(candidates, 1):
            lines.append(
                f"{i}. subtitle_group={c.subtitle_group} resolution={c.resolution} "
                f"source={c.source} video_codec={c.video_codec} audio_codec={c.audio_codec} "
                f"size={c.file_size} published={c.published_at}"
            )
        lines.append("Respond with the candidate number and a brief reason (one line).")
        messages = [
            {"role": "system", "content": "You help choose the best media release from multiple candidates."},
            {"role": "user", "content": "\n".join(lines)},
        ]
        return await call_llm(messages)
    except Exception as e:
        logger.debug("LLM suggestion failed: %s", e)
        return None


async def create_pending_decision(
    agent: Agent,
    key: tuple,
    candidates: list[FileResource],
    db: AsyncSession,
) -> PendingDecision:
    """Upsert a PendingDecision for multiple conflicting candidates.

    Same ``(agent, series_id | movie_id, episode)`` triple must always map to
    a single row in ``status='pending'``. Repeated agent runs re-merge new
    candidate ids into the existing row instead of piling up duplicates
    (which used to cause the 76-rows-for-4-episodes explosion).
    """
    type_, target_id, episode = key
    series_id = target_id if type_ == "series" else None
    movie_id = target_id if type_ == "movie" else None

    title = ""
    if type_ == "series":
        s = await db.get(TVSeries, target_id) if target_id else None
        title = (s.title_cn or s.title_en or "") if s else ""
    else:
        m = await db.get(Movie, target_id) if target_id else None
        title = (m.title_cn or m.title_en or "") if m else ""

    if type_ == "series" and episode is not None:
        reason = f"多个资源匹配 {title} 第{episode:02d}集"
    elif type_ == "series":
        reason = f"多个资源匹配 {title}"
    else:
        reason = f"多个资源匹配电影 {title}"

    # Look for an existing pending row for the same key. ``episode`` may be
    # None (movies) — treat that as a proper NULL match.
    stmt = select(PendingDecision).where(
        PendingDecision.agent_id == agent.id,
        PendingDecision.status == "pending",
    )
    if series_id is not None:
        stmt = stmt.where(PendingDecision.series_id == series_id)
    else:
        stmt = stmt.where(PendingDecision.series_id.is_(None))
    if movie_id is not None:
        stmt = stmt.where(PendingDecision.movie_id == movie_id)
    else:
        stmt = stmt.where(PendingDecision.movie_id.is_(None))
    if episode is not None:
        stmt = stmt.where(PendingDecision.episode == episode)
    else:
        stmt = stmt.where(PendingDecision.episode.is_(None))
    existing = (await db.execute(stmt)).scalars().first()

    new_candidate_ids = [c.id for c in candidates]
    if existing is not None:
        # Merge candidates preserving order — new ones appended, duplicates
        # dropped. Refresh reason + expiry so a re-run of an ageing decision
        # bumps its TTL.
        merged: list[str] = list(existing.candidates or [])
        for cid in new_candidate_ids:
            if cid not in merged:
                merged.append(cid)
        existing.candidates = merged
        existing.reason = reason
        existing.expires_at = utcnow() + timedelta(days=7)
        # Only re-generate LLM suggestion if the candidate set actually
        # changed (skip the LLM call on no-op re-runs).
        if merged != (existing.candidates or []) or not existing.llm_suggestion:
            existing.llm_suggestion = await _generate_llm_suggestion(agent, candidates, key)
        await db.flush()
        return existing

    llm = await _generate_llm_suggestion(agent, candidates, key)

    pd = PendingDecision(
        agent_id=agent.id,
        series_id=series_id,
        movie_id=movie_id,
        episode=episode,
        candidates=new_candidate_ids,
        reason=reason,
        llm_suggestion=llm,
        status="pending",
        expires_at=utcnow() + timedelta(days=7),
    )
    db.add(pd)
    await db.flush()
    return pd


def score_and_pick(
    candidates: list[FileResource],
    work: Any,
    agent: Agent,
) -> FileResource:
    """Heuristic ranking: resolution > file_size > published_at."""
    def score(r: FileResource) -> tuple:
        return (
            _resolution_score(r.resolution),
            r.file_size or 0,
            r.published_at or datetime.min.replace(tzinfo=UTC),
        )
    return max(candidates, key=score)


async def _persist_suggestions(
    agent_id: str,
    suggestions: list[dict],
    db: AsyncSession,
) -> None:
    """Replace the persisted suggestion snapshot for an agent."""
    await db.execute(delete(AgentSuggestion).where(AgentSuggestion.agent_id == agent_id))
    for group in suggestions:
        sample_title = (group.get("sample_title") or "").strip()
        resources = group.get("resources") or []
        if not sample_title or not resources:
            continue
        db.add(
            AgentSuggestion(
                agent_id=agent_id,
                sample_title=sample_title,
                resources=list(resources),
                status="active",
            )
        )


async def process_resources(
    agent: Agent,
    resources: list[FileResource],
    db: AsyncSession,
) -> RunResult:
    """Process a list of resources through filtering, dedup, and dispatch."""
    result = RunResult()

    work_by_series_id: dict[str, Any] = {}
    work_by_movie_id: dict[str, Any] = {}
    for w in (agent.works or []):
        if w.series_id:
            work_by_series_id[w.series_id] = w
        if w.movie_id:
            work_by_movie_id[w.movie_id] = w

    candidates_by_key: dict[tuple, list[FileResource]] = {}
    suggestions: dict[str, dict] = {}

    for resource in resources:
        result.total_resources += 1

        # Metadata pre-check
        if not resource.series_id and not resource.movie_id:
            result.unrecognized += 1
            key = resource.search_title or resource.title_raw
            if key:
                grouped = False
                for existing_key in list(suggestions.keys()):
                    try:
                        if partial_similarity_score(key, existing_key) >= 80:
                            suggestions[existing_key]["resources"].append(resource.id)
                            suggestions[existing_key]["sample_title"] = key
                            grouped = True
                            break
                    except Exception:
                        continue
                if not grouped:
                    suggestions[key] = {"sample_title": key, "resources": [resource.id]}
            continue

        # Ambiguous episode number — MetadataAgent had seasons evidence but
        # couldn't decide whether the raw number is per-season or absolute.
        # Route to AgentSuggestion so the user can pick before we dispatch;
        # never auto-download something we're unsure about.
        if getattr(resource, "episode_confidence", None) == "ambiguous":
            result.unrecognized += 1
            key = (resource.search_title or resource.title_raw or f"ambiguous-{resource.id}")
            group = suggestions.setdefault(key, {"sample_title": key, "resources": []})
            group.setdefault("reason", "集号不确定，需要人工确认")
            group["resources"].append(resource.id)
            continue

        # Work scope
        work = None
        if not agent.scope_channel_wide:
            if resource.series_id and resource.series_id in work_by_series_id:
                work = work_by_series_id[resource.series_id]
            elif resource.movie_id and resource.movie_id in work_by_movie_id:
                work = work_by_movie_id[resource.movie_id]
            else:
                continue

        effective_filter = merge_filters(
            agent.filter_config, work.filter_overrides if work else None
        )

        if effective_filter is not None and not evaluate_filter_config(effective_filter, resource):
            result.filter_failed += 1
            continue

        # Batch (合集) resources bypass per-episode dedup and conflict
        # resolution entirely — per the design agreed with the product owner:
        # a batch torrent is treated as a distinct payload that the user
        # opted into via the filter DSL. We still avoid dispatching the same
        # FileResource twice (crash recovery / re-run).
        if getattr(resource, "is_batch", False):
            existing_stmt = select(DownloadTask).where(
                and_(
                    DownloadTask.agent_id == agent.id,
                    DownloadTask.file_resource_id == resource.id,
                    DownloadTask.status.in_(
                        ["pending", "queued", "downloading", "paused", "completed"]
                    ),
                )
            )
            if (await db.execute(existing_stmt)).scalars().first():
                result.duplicates_skipped += 1
                continue
            try:
                await dispatch_download(agent, resource, db)
                result.dispatched += 1
                result.matched += 1
            except Exception as e:
                logger.exception("Failed to dispatch batch resource %s: %s", resource.id, e)
                result.errors.append(str(e))
            continue

        # Dedup check
        if resource.movie_id:
            stmt = select(DownloadTask).where(
                and_(
                    DownloadTask.agent_id == agent.id,
                    DownloadTask.status.in_(["pending", "queued", "downloading", "paused", "completed"]),
                    DownloadTask.file_resource.has(movie_id=resource.movie_id),
                )
            )
            existing = (await db.execute(stmt)).scalars().first()
            if existing:
                result.duplicates_skipped += 1
                continue
            key = ("movie", resource.movie_id, None)
        else:
            dedup_enabled = work.enable_episode_dedup if work else True
            if dedup_enabled and resource.episode is not None:
                stmt = select(DownloadTask).where(
                    and_(
                        DownloadTask.agent_id == agent.id,
                        DownloadTask.status.in_(["pending", "queued", "downloading", "paused", "completed"]),
                        DownloadTask.file_resource.has(
                            series_id=resource.series_id,
                            episode=resource.episode,
                        ),
                    )
                )
                existing = (await db.execute(stmt)).scalars().first()
                if existing:
                    result.duplicates_skipped += 1
                    continue
            key = ("series", resource.series_id, resource.episode)

        candidates_by_key.setdefault(key, []).append(resource)
        result.matched += 1

    for key, cands in candidates_by_key.items():
        try:
            if len(cands) == 1:
                await dispatch_download(agent, cands[0], db)
                result.dispatched += 1
            else:
                if agent.conflict_resolution == "ask":
                    await create_pending_decision(agent, key, cands, db)
                    result.pending_decisions += 1
                else:
                    chosen = score_and_pick(cands, None, agent)
                    await dispatch_download(agent, chosen, db)
                    result.dispatched += 1
        except Exception as e:
            logger.exception("Failed to process candidates for %s: %s", key, e)
            result.errors.append(str(e))

    result.suggestions = list(suggestions.values())
    await _persist_suggestions(agent.id, result.suggestions, db)
    return result
