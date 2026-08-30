"""Agent service: DSL-based resource filtering and dispatch."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import and_, delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.models.agent import Agent
from app.models.agent_suggestion import AgentSuggestion
from app.models.download_task import DownloadTask
from app.models.downloader import DownloaderInstance
from app.models.file_resource import FileResource
from app.models.movie import Movie
from app.models.pending_decision import PendingDecision
from app.models.series import TVSeries
from app.services.filter_engine import (
    evaluate_field_condition,
    evaluate_filter_config,
    loaded_relation,
    merge_filters,
)
from app.services.resource_confirmation import (
    LEGACY_CONFIRMATION_REASON_PREFIXES,
    inspect_resource_confirmation,
)
from app.services.runtime_config import runtime_config
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


def resolve_torrent_payload(resource: FileResource) -> str | bytes:
    """Pick the payload handed to the downloader's ``add_torrent``.

    When the fetch pipeline cached the .torrent file locally
    (``resource.torrent_file``), push its raw bytes so the daemon does not
    re-download it (dead links, private-tracker cookie requirements). Falls
    back to the URL/magnet when there is no cached file or it cannot be read.
    """
    if resource.torrent_file:
        try:
            return Path(resource.torrent_file).read_bytes()
        except OSError as e:
            logger.warning(
                "Cached torrent file unreadable for resource %s (%s): %s; "
                "falling back to torrent_url",
                resource.id, resource.torrent_file, e,
            )
    return resource.torrent_url


async def create_and_submit_task(
    resource: FileResource,
    downloader: DownloaderInstance,
    db: AsyncSession,
    agent_id: str | None = None,
    download_dir: str = "",
) -> DownloadTask:
    """Create a DownloadTask and attempt to add it to the downloader.

    Shared by agent dispatch and manual creation (``POST /tasks``).
    """
    task = DownloadTask(
        agent_id=agent_id,
        file_resource_id=resource.id,
        downloader_id=downloader.id,
        download_dir=download_dir,
        status="pending",
        max_retries=settings.max_retry_count,
    )
    db.add(task)
    # NOTE: no flush before the RPC on purpose. Flushing would emit the INSERT
    # and acquire the SQLite write lock for the whole duration of the
    # downloader call (up to ``transmission_timeout`` seconds), stalling all
    # foreground writes. The RPC only needs constructor-set attributes, so we
    # flush once at the end instead.
    from app.clients.downloader import get_downloader_client

    wrapper = get_downloader_client(downloader)
    try:
        result = await asyncio.wait_for(
            wrapper.add_torrent(
                resolve_torrent_payload(resource),
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

    await db.flush()
    return task


async def dispatch_download(
    agent: Agent, resource: FileResource, db: AsyncSession
) -> DownloadTask:
    """Create a DownloadTask and attempt to add it to Transmission."""
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

    return await create_and_submit_task(
        resource,
        downloader,
        db,
        agent_id=agent.id,
        download_dir=download_dir,
    )


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
        # Field glossary so custom prompts can rely on the extra work fields.
        lines.append(
            "候选字段说明：title 为资源原始标题；year/rating 来自关联作品"
            "（电影取 release_date 年份、剧集取首播年份；rating 满分 10 分），"
            "null 表示无关联作品或该字段无数据。"
        )
        lines.append("")
        for i, c in enumerate(candidates, 1):
            meta_fields = sum(
                1 for v in (
                    c.subtitle_group, c.resolution, c.source, c.video_codec,
                    c.audio_codec, c.subtitle_type, c.container, c.file_size,
                ) if v not in (None, "", [])
            )
            has_sub = bool(getattr(c, "subtitle_langs", None)) or bool(c.subtitle_type)
            work = loaded_relation(c, "movie") or loaded_relation(c, "series")
            work_year, work_rating = None, None
            if work is not None:
                work_date = getattr(work, "release_date", None) or getattr(work, "start_date", None)
                work_year = work_date.year if work_date else None
                work_rating = getattr(work, "rating", None)
            lines.append(
                f"{i}. title={c.title_raw} year={work_year} rating={work_rating} "
                f"subtitle_group={c.subtitle_group} resolution={c.resolution} "
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

    Same ``(agent, series_id | movie_id, season, episode)`` tuple must always
    map to a single row in ``status='pending'``. Repeated agent runs re-merge
    new candidate ids into the existing row instead of piling up duplicates
    (which used to cause the 76-rows-for-4-episodes explosion).

    ``key`` is ``(type, target_id, season, episode)``; the legacy 3-element
    ``(type, target_id, episode)`` shape is still accepted (season=None).
    ``reason_override`` supplies a scope-aware message for batch conflicts.
    """
    if len(key) == 4:
        type_, target_id, season, episode = key
    else:
        type_, target_id, episode = key
        season = None
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
    elif type_ == "series" and episode is not None and season is not None:
        reason = f"多个资源匹配 {title} 第{season}季第{episode:02d}集"
    elif type_ == "series" and episode is not None:
        reason = f"多个资源匹配 {title} 第{episode:02d}集"
    elif type_ == "series":
        reason = f"多个资源匹配 {title}"
    else:
        reason = f"多个资源匹配电影 {title}"

    # Look for an existing pending row for the same key. ``episode``/``season``
    # may be None (movies, season-less series) — treat that as a proper NULL
    # match.
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
    if season is not None:
        stmt = stmt.where(PendingDecision.season == season)
    else:
        stmt = stmt.where(PendingDecision.season.is_(None))
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
        # Only re-generate the LLM pick when needed; repeated no-op runs keep
        # the existing recommendation.
        if not skip_llm and (
            merged != (existing.candidates or []) or not existing.llm_picked_resource_id
        ):
            picked_id, reason_txt = await _suggest_pick(agent, candidates, key)
            existing.llm_picked_resource_id = picked_id
            existing.llm_suggestion = reason_txt
        await db.flush()
        return existing

    if skip_llm:
        picked_id, reason_txt = None, None
    else:
        picked_id, reason_txt = await _suggest_pick(agent, candidates, key)

    pd = PendingDecision(
        agent_id=agent.id,
        series_id=series_id,
        movie_id=movie_id,
        season=season,
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
    """Heuristic ranking: resolution > file_size > published_at, with a
    subtitle-language bonus as the final tie-break (zh-CN > zh-TW > other)
    so otherwise-identical variants resolve towards 简体 when the preference
    rules and the LLM could not discriminate."""
    lang_bonus_map = {"zh-TW": 1, "zh-CN": 2}

    def lang_bonus(r: FileResource) -> int:
        langs = r.subtitle_langs or []
        return max(
            (lang_bonus_map[lang] for lang in langs if lang in lang_bonus_map),
            default=0,
        )

    def score(r: FileResource) -> tuple:
        return (
            _resolution_score(r.resolution),
            r.file_size or 0,
            r.published_at or datetime.min.replace(tzinfo=UTC),
            lang_bonus(r),
        )
    return max(candidates, key=score)


def pick_by_preferences(
    candidates: list[FileResource],
    preferences: list[dict] | None,
) -> tuple[list[FileResource], dict | None]:
    """Narrow ``candidates`` to the best tier under ordered preference rules.

    Lexicographic ("ordered should") semantics: the first rule splits the pool
    into match / no-match and the matching tier wins; later rules break
    remaining ties within the winning tier. Rules that match everything or
    nothing in the current tier cannot discriminate and are skipped.

    Preferences only ever REORDER — they never filter candidates out of the
    conflict set: with no preferences, a single candidate, or an all-way tie
    the original list is returned unchanged.

    Returns ``(tier, deciding_rule)`` — the rule that last narrowed the pool
    (None when no rule discriminated). A malformed rule is skipped rather
    than breaking dispatch.
    """
    best = list(candidates)
    deciding: dict | None = None
    if not preferences or len(best) <= 1:
        return best, None
    for pref in preferences:
        if not isinstance(pref, dict):
            continue
        try:
            winners = [c for c in best if evaluate_field_condition(pref, c)]
        except Exception:  # noqa: BLE001 — a bad rule must never break dispatch
            logger.debug("pick preference rule skipped: %s", pref)
            continue
        if 0 < len(winners) < len(best):
            best = winners
            deciding = pref
            if len(best) == 1:
                break
    return best, deciding


def _describe_preference(pref: dict) -> str:
    field = pref.get("field", "?")
    op = pref.get("operator", "?")
    if op in ("is_empty", "is_not_empty"):
        return f"{field} {op}"
    return f"{field} {op} {pref.get('value')}"


async def _suggest_pick(
    agent: Agent,
    candidates: list[FileResource],
    key: tuple,
) -> tuple[str | None, str | None]:
    """Suggestion for ask-mode decisions: deterministic preference rules
    first (the reason is labeled as rule-sourced, not LLM), the LLM breaks
    any remaining tie on the narrowed tier."""
    tier, deciding = pick_by_preferences(candidates, agent.pick_preferences)
    if len(tier) == 1 and len(candidates) > 1 and deciding is not None:
        return tier[0].id, f"命中优选偏好规则（确定性选择）：{_describe_preference(deciding)}"
    return await _generate_llm_pick(agent, tier, key)


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


# ── Batch (合集) content-coverage dedup ─────────────────────────────────────
# Batch resources carry no per-episode identity, so the (series, season,
# episode) dedup key doesn't apply. Instead they dedup/conflict by *content
# coverage*: two batches are duplicates only when they cover exactly the same
# content. Same-coverage versions (different encodes / subtitle groups) go
# through the same conflict resolution as single episodes (ask →
# PendingDecision / auto → LLM pick → heuristic scorer); different coverage
# (S1 pack vs S2 pack, S1-S2 vs S1-S3) never conflicts.

# PendingDecision.episode marker for batch-conflict decisions. Distinguishes
# them from episode-less single-episode decisions, which would otherwise
# share the (series, season, NULL) idempotency key and merge. The decisions
# UI renders only reason + candidates, so the sentinel is never displayed.
_BATCH_EPISODE_SENTINEL = -1



def _batch_coverage_key(resource: FileResource) -> tuple | None:
    """Content-coverage signature of a batch resource.

    - movie-linked batch → ``("movie",)`` (a movie pack covers the movie).
    - season pack → ``("season", season)``; requires a known season (title
      marker or torrent analysis) — the season number is never guessed.
    - multi-season pack → ``("multi_season", tuple(sorted(seasons)))``;
      requires ``batch_seasons`` from the torrent content analysis.

    Returns None when the coverage is unknown: such resources keep the legacy
    behavior (dispatched immediately, guarded only against re-dispatching the
    same FileResource), since strict duplication cannot be proven. Franchise
    packs never reach the batch branch (their work FKs are cleared).
    """
    if resource.movie_id:
        return ("movie",)
    scope = resource.batch_scope or "season"  # legacy title-marked packs
    if scope == "season":
        return ("season", resource.season) if resource.season is not None else None
    if scope == "multi_season":
        seasons = tuple(sorted(resource.batch_seasons or []))
        return ("multi_season", seasons) if seasons else None
    return None


async def _find_existing_episode_task(
    agent: Agent,
    resource: FileResource,
    db: AsyncSession,
) -> DownloadTask | None:
    """Existing active/completed task of this agent occupying the same
    episode slot.

    Slot identity is *season-compatible* rather than season-exact: a task
    whose resource has no season number matches its numbered sibling of the
    same episode (and vice versa). Strict season equality let the same
    episode download twice when two release variants were attributed
    different seasons across runs — e.g. one linked before the work's
    seasons data was known (season=None) and another after reconciliation
    (S1): each variant formed its own single-candidate group, both were
    dispatched directly, and pick preferences never got a chance to engage.
    """
    stmt = (
        select(DownloadTask)
        .join(FileResource, DownloadTask.file_resource_id == FileResource.id)
        .where(
            DownloadTask.agent_id == agent.id,
            DownloadTask.status.in_(
                ["pending", "queued", "downloading", "paused", "completed"]
            ),
            FileResource.series_id == resource.series_id,
            FileResource.episode == resource.episode,
        )
        .options(selectinload(DownloadTask.file_resource))
    )
    for task in (await db.execute(stmt)).scalars():
        season = task.file_resource.season
        if season is None or resource.season is None or season == resource.season:
            return task
    return None


async def _find_active_batch_duplicate(
    agent: Agent,
    resource: FileResource,
    coverage: tuple,
    db: AsyncSession,
) -> DownloadTask | None:
    """Active/completed task of this agent whose batch resource covers exactly
    the same content as ``resource``. Compared in Python because the coverage
    of multi-season packs lives in a JSON list (small N per agent+work)."""
    work_filter = (
        FileResource.movie_id == resource.movie_id
        if resource.movie_id
        else FileResource.series_id == resource.series_id
    )
    stmt = (
        select(DownloadTask)
        .join(FileResource, DownloadTask.file_resource_id == FileResource.id)
        .where(
            DownloadTask.agent_id == agent.id,
            DownloadTask.status.in_(
                ["pending", "queued", "downloading", "paused", "completed"]
            ),
            FileResource.is_batch.is_(True),
            work_filter,
        )
        .options(selectinload(DownloadTask.file_resource))
    )
    for task in (await db.execute(stmt)).scalars():
        if _batch_coverage_key(task.file_resource) == coverage:
            return task
    return None


def _batch_decision_key(key: tuple) -> tuple[tuple, str]:
    """Translate an internal batch candidate key ``("batch", target_id, cov)``
    into a ``create_pending_decision`` key + reason template.

    Known limitation: multi-season packs of the same work share one
    PendingDecision row even when their season sets differ — the
    (series, season, episode) idempotency columns can't encode a season set.
    """
    _, target_id, cov = key
    if cov[0] == "movie":
        return ("movie", target_id, None, _BATCH_EPISODE_SENTINEL), (
            "多个合集资源匹配电影 {title}，内容相同，请选择一个版本"
        )
    if cov[0] == "season":
        return ("series", target_id, cov[1], _BATCH_EPISODE_SENTINEL), (
            f"多个合集资源匹配 {{title}} 第{cov[1]}季，内容相同，请选择一个版本"
        )
    seasons = "/".join(str(s) for s in cov[1])
    return ("series", target_id, None, _BATCH_EPISODE_SENTINEL), (
        f"多个合集资源匹配 {{title}}（第{seasons}季），内容相同，请选择一个版本"
    )


async def process_resources(
    agent: Agent,
    resources: list[FileResource],
    db: AsyncSession,
    *,
    autocommit: bool = False,
    required_metadata_fields: list[str] | None = None,
) -> RunResult:
    """Process a list of resources through filtering, dedup, and dispatch.

    ``autocommit`` is used by the background run-agent handler: commit after
    each dispatch / decision so the SQLite write lock is released between
    units of work instead of being held for the entire run (which contains
    slow LLM calls and Transmission RPCs). Request-scoped callers keep the
    default (single commit at request end). Operations are idempotent
    (task-dedup / decision upsert), so a mid-run crash simply leaves the
    already-committed units in place.
    """
    result = RunResult()

    rule_set = _build_rule_set(agent)
    candidates_by_key: dict[tuple, list[FileResource]] = {}
    suggestions: dict[str, dict] = {}

    for resource in resources:
        result.total_resources += 1

        # Metadata pre-check. Metadata completeness is owned by the resource's
        # Channel, not by an Agent decision. Unlinked resources therefore do
        # not create AgentSuggestion/PendingDecision rows.
        if not resource.series_id and not resource.movie_id:
            result.unrecognized += 1
            continue

        confirmation = inspect_resource_confirmation(
            resource, required_metadata_fields
        )
        if confirmation.required:
            result.unrecognized += 1
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

        # Batch (合集) resources dedup/conflict by *content coverage* (see
        # _batch_coverage_key): strictly identical coverage (same work + same
        # season / same season set) goes through the normal conflict
        # resolution; different coverage never conflicts. Coverage is a
        # **mandatory** field for downstream organize planning (覆盖度校验),
        # so unknown coverage is stopped by the Channel confirmation gate.
        # Franchise packs never reach here (work FKs are cleared →
        # unrecognized bucket).
        if getattr(resource, "is_batch", False):
            # Same-FileResource guard (crash recovery / re-run).
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
            coverage = _batch_coverage_key(resource)
            # Unknown coverage is caught by the Channel confirmation gate
            # above. Keep this defensive guard for callers that supplied a
            # non-ORM resource shape without the expected batch fields.
            if coverage is None:
                result.unrecognized += 1
                continue
            # Cross-run dedup against active/completed tasks with the
            # exact same coverage. Series packs honor the per-work
            # enable_episode_dedup toggle (same as single episodes);
            # movie packs dedup unconditionally (same as movie singles).
            dedup_enabled = (
                True
                if resource.movie_id
                else (work.enable_episode_dedup if work else True)
            )
            if dedup_enabled and await _find_active_batch_duplicate(
                agent, resource, coverage, db
            ):
                result.duplicates_skipped += 1
                continue
            # Aggregate same-coverage versions for conflict resolution
            # (ask → PendingDecision / auto → LLM pick → heuristic).
            key = ("batch", resource.series_id or resource.movie_id, coverage)
            candidates_by_key.setdefault(key, []).append(resource)
            result.matched += 1
            result.matched_resource_ids.append(resource.id)
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
            key = ("movie", resource.movie_id, None, None)
        else:
            dedup_enabled = work.enable_episode_dedup if work else True
            if dedup_enabled and resource.episode is not None:
                existing = await _find_existing_episode_task(agent, resource, db)
                if existing:
                    result.duplicates_skipped += 1
                    continue
            key = ("series", resource.series_id, resource.season, resource.episode)

        candidates_by_key.setdefault(key, []).append(resource)
        result.matched += 1
        result.matched_resource_ids.append(resource.id)

    # Season-unknown singles share the conflict slot of their numbered
    # sibling: ("series", sid, None, E5) merges into ("series", sid, S, E5)
    # so both release variants reach preference/LLM/heuristic resolution
    # instead of dispatching as two independent single-candidate groups
    # (which downloaded the same episode twice). When several numbered
    # seasons exist for the same episode the first match wins — upstream,
    # season-less tv resources are ambiguous-flagged and routed to
    # PendingDecision, so this only fires for legacy/unattributed rows.
    for key in [
        k for k in candidates_by_key if k[0] == "series" and k[2] is None
    ]:
        _, sid, _season, ep = key
        twin = next(
            (
                k
                for k in candidates_by_key
                if k[0] == "series" and k[1] == sid and k[3] == ep and k[2] is not None
            ),
            None,
        )
        if twin is not None:
            candidates_by_key[twin].extend(candidates_by_key.pop(key))

    for key, cands in candidates_by_key.items():
        try:
            if len(cands) == 1:
                await dispatch_download(agent, cands[0], db)
                result.dispatched += 1
            else:
                if agent.conflict_resolution == "ask":
                    if key[0] == "batch":
                        pd_key, reason = _batch_decision_key(key)
                        await create_pending_decision(
                            agent, pd_key, cands, db, reason_override=reason
                        )
                    else:
                        await create_pending_decision(agent, key, cands, db)
                    result.pending_decisions += 1
                else:
                    # "auto": deterministic preference rules first — they only
                    # rank, never filter. A unique winner dispatches without
                    # any LLM call; a tied shortlist goes to the LLM pick
                    # (self-gated on llm_enabled + API key); the heuristic
                    # scorer stays the final fallback.
                    tier, _deciding = pick_by_preferences(cands, agent.pick_preferences)
                    if len(tier) == 1:
                        chosen = tier[0]
                    else:
                        picked_id, _pick_reason = await _generate_llm_pick(agent, tier, key)
                        chosen = next((c for c in tier if c.id == picked_id), None)
                        if chosen is None:
                            chosen = score_and_pick(tier, None, agent)
                    await dispatch_download(agent, chosen, db)
                    result.dispatched += 1
            if autocommit:
                await db.commit()
        except Exception as e:
            logger.exception("Failed to process candidates for %s: %s", key, e)
            result.errors.append(str(e))

    await _retire_legacy_resource_confirmation_decisions(agent, db)

    result.suggestions = list(suggestions.values())
    await _persist_suggestions(agent.id, result.suggestions, db)
    return result


async def _retire_legacy_resource_confirmation_decisions(
    agent: Agent, db: AsyncSession
) -> None:
    """Remove legacy resource-metadata issues from the Agent decision queue."""
    pd_rows = (await db.execute(
        select(PendingDecision).where(
            PendingDecision.agent_id == agent.id,
            PendingDecision.status == "pending",
        )
    )).scalars().all()
    for pd in pd_rows:
        reason = pd.reason or ""
        if not reason.startswith(LEGACY_CONFIRMATION_REASON_PREFIXES):
            continue
        pd.status = "skipped"
        pd.decided_at = utcnow()
    await db.flush()
