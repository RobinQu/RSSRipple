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
from app.services.runtime_config import runtime_config
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
    # Resource ids that matched the agent's rules this run (passed work-scope
    # + filter). Populated for run-history display.
    matched_resource_ids: list[str] = field(default_factory=list)


_RESOLUTION_SCORE = {"2160p": 3, "4k": 3, "1080p": 2, "720p": 1}


# Reason prefix used for ambiguous-episode PendingDecisions. The cleanup
# pass in process_resources identifies these decisions by this prefix and
# resolves them once the user has manually corrected the episode number
# (episode_confidence becomes "manual"). Keep the prefix stable.
_AMBIGUOUS_EPISODE_REASON = "集号不确定，需要人工确认集号: {title}"


@dataclass
class RuleSet:
    """A snapshot of the subscription rules used to test resource matching.

    Captured separately from the ``Agent`` ORM object so the diff logic can
    evaluate old vs new rules without mutating the persisted agent.
    """
    scope_channel_wide: bool
    filter_config: dict | None
    work_by_series_id: dict[str, Any] = field(default_factory=dict)
    work_by_movie_id: dict[str, Any] = field(default_factory=dict)


def _build_rule_set(agent: Agent) -> RuleSet:
    by_series: dict[str, Any] = {}
    by_movie: dict[str, Any] = {}
    for w in (agent.works or []):
        if w.series_id:
            by_series[w.series_id] = w
        if w.movie_id:
            by_movie[w.movie_id] = w
    return RuleSet(
        scope_channel_wide=agent.scope_channel_wide,
        filter_config=agent.filter_config,
        work_by_series_id=by_series,
        work_by_movie_id=by_movie,
    )


def _resource_matches_rules(
    resource: FileResource, rules: RuleSet
) -> tuple[bool, Any]:
    """Filter-level match: does ``resource`` fall under this rule set?

    Returns ``(matched, work)`` where ``work`` is the subscribed AgentWork the
    resource resolved to (None for channel-wide). Matched is True when the
    resource is in scope (subscribed work, or channel-wide) AND passes the
    merged effective filter. This is intentionally *not* a dispatch decision
    — dedup / ambiguous / conflict handling are runtime concerns layered on
    top in ``process_resources``.
    """
    work = None
    if not rules.scope_channel_wide:
        if resource.series_id and resource.series_id in rules.work_by_series_id:
            work = rules.work_by_series_id[resource.series_id]
        elif resource.movie_id and resource.movie_id in rules.work_by_movie_id:
            work = rules.work_by_movie_id[resource.movie_id]
        else:
            return False, None
    effective = merge_filters(
        rules.filter_config, work.filter_overrides if work else None
    )
    if effective is not None and not evaluate_filter_config(effective, resource):
        return False, work
    return True, work


async def compute_rule_diff(
    old: RuleSet,
    new: RuleSet,
    resources: list[FileResource],
    db: AsyncSession,
) -> dict:
    """Diff resource matching between two rule sets over ``resources``.

    Used by the rules-preview endpoint (scenario ②): when subscription rules
    change, the user sees newly-matching resources (backfill candidates) and
    no-longer-matching ones (informational; in-queue tasks are never revoked).

    - ``newly_matching``: matches new rules, did NOT match old rules, and has
      no active DownloadTask → eligible for user-selected backfill.
    - ``no_longer_matching``: matched old rules, does NOT match new rules.
    - ``in_queue_skipped``: count of newly-matching resources skipped because
      they already have an active DownloadTask.
    """
    res_ids = [r.id for r in resources]
    tasked: set[str] = set()
    if res_ids:
        rows = (await db.execute(
            select(DownloadTask.file_resource_id).where(
                DownloadTask.file_resource_id.in_(res_ids),
                DownloadTask.status.in_(
                    ["pending", "queued", "downloading", "paused", "completed"]
                ),
            )
        )).all()
        tasked = {row[0] for row in rows}

    newly_matching: list[FileResource] = []
    no_longer_matching: list[FileResource] = []
    in_queue_skipped = 0
    for r in resources:
        old_m, _ = _resource_matches_rules(r, old)
        new_m, _ = _resource_matches_rules(r, new)
        if new_m and not old_m:
            if r.id in tasked:
                in_queue_skipped += 1
            else:
                newly_matching.append(r)
        elif old_m and not new_m:
            no_longer_matching.append(r)
    return {
        "newly_matching": newly_matching,
        "no_longer_matching": no_longer_matching,
        "in_queue_skipped": in_queue_skipped,
    }


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


_DEFAULT_LLM_PICK_PROMPT = (
    "从以下候选中为同一集挑选最佳资源。优先级：metadata 字段最完整 > "
    "清晰度最高（2160p>1080p>720p）> 带字幕（subtitle_langs 非空）> "
    "发布时间最新。"
)


def _parse_llm_pick(text: str, candidate_count: int) -> tuple[int | None, str | None]:
    """Extract a 1-based candidate index + reason from an LLM text response.

    Tries JSON first (``{"pick": <n>, "reason": "..."}``), then falls back to
    a leading integer. Returns ``(None, reason)`` when no valid pick is found.
    """
    if not text:
        return None, None
    import json as _json
    import re

    m = re.search(r"\{[^{}]*\}", text)
    if m:
        try:
            obj = _json.loads(m.group(0))
            pick = obj.get("pick")
            reason = obj.get("reason")
            if isinstance(pick, int) and 1 <= pick <= candidate_count:
                return pick, (reason if isinstance(reason, str) else None)
        except Exception:
            pass

    m = re.match(r"\s*(?:pick[:\s]*)?(\d+)", text, re.IGNORECASE)
    if m:
        pick = int(m.group(1))
        if 1 <= pick <= candidate_count:
            return pick, text.strip() or None
    return None, text.strip() or None


async def _generate_llm_pick(
    agent: Agent,
    candidates: list[FileResource],
    key: tuple,
) -> tuple[str | None, str | None]:
    """Ask the LLM to pick the best candidate.

    Returns ``(picked_resource_id, reason)``. ``picked_resource_id`` is None
    when the LLM is disabled, unreachable, or didn't return a valid pick.
    Uses ``agent.llm_prompt`` when set, else the built-in default prompt.
    """
    if not agent.llm_enabled or not runtime_config.llm_api_key or not candidates:
        return None, None
    try:
        from app.services.feed_analyzer import call_llm

        instruction = (agent.llm_prompt or "").strip() or _DEFAULT_LLM_PICK_PROMPT
        lines = [instruction, ""]
        for i, c in enumerate(candidates, 1):
            meta_fields = sum(
                1 for v in (
                    c.subtitle_group, c.resolution, c.source, c.video_codec,
                    c.audio_codec, c.subtitle_type, c.container, c.file_size,
                ) if v not in (None, "", [])
            )
            has_sub = bool(getattr(c, "subtitle_langs", None)) or bool(c.subtitle_type)
            lines.append(
                f"{i}. subtitle_group={c.subtitle_group} resolution={c.resolution} "
                f"source={c.source} video_codec={c.video_codec} audio_codec={c.audio_codec} "
                f"size={c.file_size} subtitle_langs={getattr(c, 'subtitle_langs', None)} "
                f"has_subtitle={has_sub} meta_completeness={meta_fields}/8 "
                f"published={c.published_at}"
            )
        lines.append("")
        lines.append('只返回 JSON：{"pick": <候选编号>, "reason": "<一句话理由>"}。')
        messages = [
            {"role": "system", "content": "You help choose the best media release from multiple candidates."},
            {"role": "user", "content": "\n".join(lines)},
        ]
        raw = await call_llm(messages)
        pick_idx, reason = _parse_llm_pick(raw or "", len(candidates))
        picked_id = candidates[pick_idx - 1].id if pick_idx else None
        return picked_id, reason
    except Exception as e:
        logger.debug("LLM pick failed: %s", e)
        return None, None


async def create_pending_decision(
    agent: Agent,
    key: tuple,
    candidates: list[FileResource],
    db: AsyncSession,
    *,
    reason_override: str | None = None,
    skip_llm: bool = False,
) -> PendingDecision:
    """Upsert a PendingDecision for multiple conflicting candidates.

    Same ``(agent, series_id | movie_id, episode)`` triple must always map to
    a single row in ``status='pending'``. Repeated agent runs re-merge new
    candidate ids into the existing row instead of piling up duplicates
    (which used to cause the 76-rows-for-4-episodes explosion).

    ``reason_override`` lets callers reuse this upsert for non-conflict
    decisions — notably ambiguous-episode resources that need manual episode
    confirmation rather than candidate picking. When set, the LLM suggestion
    (which is about choosing among candidates) is skipped via ``skip_llm``.
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

    if reason_override is not None:
        reason = reason_override.format(title=title) if "{" in reason_override else reason_override
    elif type_ == "series" and episode is not None:
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
        # Only re-generate the LLM pick if the candidate set actually changed
        # (skip the LLM call on no-op re-runs). Ambiguous-episode decisions
        # carry no "pick the best candidate" semantics, so the LLM is skipped.
        if not skip_llm and (
            merged != (existing.candidates or []) or not existing.llm_picked_resource_id
        ):
            picked_id, reason_txt = await _generate_llm_pick(agent, candidates, key)
            existing.llm_picked_resource_id = picked_id
            existing.llm_suggestion = reason_txt
        await db.flush()
        return existing

    if skip_llm:
        picked_id, reason_txt = None, None
    else:
        picked_id, reason_txt = await _generate_llm_pick(agent, candidates, key)

    pd = PendingDecision(
        agent_id=agent.id,
        series_id=series_id,
        movie_id=movie_id,
        episode=episode,
        candidates=new_candidate_ids,
        reason=reason,
        llm_suggestion=reason_txt,
        llm_picked_resource_id=picked_id,
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

    rule_set = _build_rule_set(agent)
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

        # Work scope + filter (filter-level match).
        matched, work = _resource_matches_rules(resource, rule_set)
        if not matched:
            # Distinguish "in scope but filter failed" from "out of scope" for
            # the filter_failed counter; out-of-scope is silently skipped.
            if (
                rule_set.scope_channel_wide
                or (resource.series_id and resource.series_id in rule_set.work_by_series_id)
                or (resource.movie_id and resource.movie_id in rule_set.work_by_movie_id)
            ):
                result.filter_failed += 1
            continue

        # Ambiguous episode number — MetadataAgent had seasons evidence but
        # couldn't decide whether the raw number is per-season or absolute.
        # Route to a PendingDecision (not a dispatch, not a suggestion) so the
        # user can manually confirm the per-season episode number before we
        # download — never auto-download something we're unsure about. This
        # runs AFTER work-scope + filter so we only ask about resources the
        # agent would actually download.
        if getattr(resource, "episode_confidence", None) == "ambiguous":
            try:
                await create_pending_decision(
                    agent,
                    ("series", resource.series_id, resource.episode),
                    [resource],
                    db,
                    reason_override=_AMBIGUOUS_EPISODE_REASON,
                    skip_llm=True,
                )
                result.pending_decisions += 1
                result.unrecognized += 1
            except Exception as e:
                logger.exception("Failed to create ambiguous-episode decision for %s: %s", resource.id, e)
                result.errors.append(str(e))
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
                result.matched_resource_ids.append(resource.id)
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
        result.matched_resource_ids.append(resource.id)

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

    # Cleanup: resolve ambiguous-episode PendingDecisions whose candidate the
    # user has already corrected (episode_confidence != "ambiguous"). Once the
    # episode is hand-confirmed, the resource re-enters the normal
    # filter→dispatch flow on this run, so the stale "please confirm episode"
    # decision is no longer relevant. Mark it "decided" so it leaves the
    # pending queue.
    await _resolve_corrected_ambiguous_decisions(agent, db)

    result.suggestions = list(suggestions.values())
    await _persist_suggestions(agent.id, result.suggestions, db)
    return result


async def _resolve_corrected_ambiguous_decisions(agent: Agent, db: AsyncSession) -> None:
    """Mark pending ambiguous-episode decisions as decided once their
    candidate resource is no longer ambiguous (user ran correct_episode)."""
    pd_rows = (await db.execute(
        select(PendingDecision).where(
            PendingDecision.agent_id == agent.id,
            PendingDecision.status == "pending",
        )
    )).scalars().all()
    for pd in pd_rows:
        if not (pd.reason or "").startswith("集号不确定"):
            continue
        cand_ids = list(pd.candidates or [])
        if not cand_ids:
            pd.status = "decided"
            continue
        cand_rows = (await db.execute(
            select(FileResource).where(FileResource.id.in_(cand_ids))
        )).scalars().all()
        # Resolve only when every candidate has been corrected away from
        # "ambiguous" (typically to "manual"). If any candidate is still
        # ambiguous the decision stays pending for the user to act on.
        if cand_rows and all(
            getattr(c, "episode_confidence", None) != "ambiguous" for c in cand_rows
        ):
            pd.status = "decided"
    await db.flush()
