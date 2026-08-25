"""Background job handlers for the task queue.

Centralised here so both entry points register the same set:
- ``app/main.py`` lifespan (roles ``all`` and ``web``)
- ``app/worker.py`` (role ``worker``)

Every handler refreshes the process-local runtime-config override map before
doing any work: in a split web/worker deployment the worker process never
sees settings writes made through the web API, so each job reloads them from
the DB up front.
"""

import asyncio
import logging

from app.database import committed_session

logger = logging.getLogger(__name__)


async def _refresh_runtime_config() -> None:
    """Reload DB-backed runtime settings into this process.

    The override map is process-local; a worker never observes writes that
    happen in the web process, so every job refreshes it up front. Failures
    are logged and swallowed — the job runs with the last-known/env config.
    """
    from app.database import async_session_factory
    from app.services.runtime_config import load_runtime_config

    try:
        async with async_session_factory() as sess:
            await load_runtime_config(sess)
    except Exception as e:  # noqa: BLE001 — never block a job on config reload
        logger.warning("Failed to refresh runtime config: %s", e)


# ---------------------------------------------------------------------------
# Channel fetch / agent run
# ---------------------------------------------------------------------------

async def _handle_fetch_channel(payload: dict) -> dict:  # pragma: no cover
    from app.models.channel import Channel
    from app.services.fetch_service import fetch_channel_resources

    await _refresh_runtime_config()
    channel_id: str = payload["channel_id"]
    force: bool = bool(payload.get("force", False))
    async with committed_session() as session:
        ch = await session.get(Channel, channel_id)
        if not ch:
            raise RuntimeError(f"Channel {channel_id} not found")
        result = await fetch_channel_resources(ch, session, force=force)
        return result


async def _handle_run_agent(payload: dict) -> dict:  # pragma: no cover
    from datetime import datetime

    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.models.agent import Agent
    from app.models.agent_run import AgentRun
    from app.models.file_resource import FileResource
    from app.models.movie import Movie
    from app.models.series import TVSeries
    from app.services.agent_service import process_resources
    from app.utils.time import utcnow

    await _refresh_runtime_config()
    agent_id: str = payload["agent_id"]
    resource_ids: list[str] | None = payload.get("resource_ids")
    # Manual windowed run (scenario ④): the key's presence marks the run as
    # "scan from a user-chosen start time"; a null value means "no limit"
    # (full channel history). Already normalised to naive UTC by the API.
    scan_since: datetime | None = None
    scan_windowed = "scan_since" in payload
    if scan_windowed and payload["scan_since"] is not None:
        scan_since = datetime.fromisoformat(payload["scan_since"])
    # Lower bound recorded on the AgentRun for run-history display. None =
    # delta/targeted run; 1970-01-01 = explicit "no limit" full scan.
    run_scan_since: datetime | None = None

    # Phase 1 (short transaction): persist the "running" record up front and
    # pick the resources this run covers, then COMMIT. The slow processing
    # phase below must not run inside this transaction — holding the SQLite
    # write lock across LLM calls / Transmission RPCs stalls every foreground
    # write request until the retry middleware gives up.
    async with committed_session() as session:
        agent = await session.get(Agent, agent_id)
        if not agent:
            raise RuntimeError(f"Agent {agent_id} not found")

        run = AgentRun(agent_id=agent.id, status="running", started_at=utcnow())
        session.add(run)
        await session.flush()
        run_id = run.id
        channel_id = agent.channel_id

        advance_to = None
        if resource_ids:
            # Targeted run (scenario ③, e.g. correct_episode): process exactly
            # the given resources against the agent's *current* rules. Bypasses
            # the watermark and does NOT advance it — the resource may be old,
            # and advancing would skip its neighbours.
            stmt = (
                select(FileResource.id)
                .where(
                    FileResource.channel_id == channel_id,
                    FileResource.id.in_(resource_ids),
                )
                .order_by(FileResource.created_at.asc())
            )
            selected_ids = list((await session.execute(stmt)).scalars().all())
        elif scan_windowed:
            # Windowed run (scenario ④): scan channel resources created after
            # the user-chosen start time, or the full channel history when
            # scan_since is null ("no limit"). Only the scan range of THIS
            # run is affected — the watermark still advances past everything
            # considered (dedup makes re-processing idempotent), so the next
            # delta run resumes normal incremental behaviour.
            stmt = (
                select(FileResource.id, FileResource.created_at)
                .where(FileResource.channel_id == channel_id)
                .order_by(FileResource.created_at.asc())
            )
            if scan_since is not None:
                stmt = stmt.where(FileResource.created_at > scan_since)
            rows = (await session.execute(stmt)).all()
            selected_ids = [r.id for r in rows]
            if rows:
                advance_to = max(r.created_at for r in rows)
            run_scan_since = scan_since if scan_since is not None else datetime(1970, 1, 1)
        else:
            # Delta run (scenario ①): only resources newer than the agent's
            # consumption watermark. Replaces the old hard-coded ``limit(200)``
            # which silently dropped anything beyond the latest 200.
            wm = agent.last_consumed_at
            if wm is None:
                # No watermark yet (e.g. migration skipped this row): treat as
                # "caught up to now" and process nothing, so we never silently
                # auto-dispatch historical backfill — that must go through the
                # rules-preview selection flow.
                agent.last_consumed_at = utcnow()
                selected_ids = []
            else:
                stmt = (
                    select(FileResource.id, FileResource.created_at)
                    .where(
                        FileResource.channel_id == channel_id,
                        FileResource.created_at > wm,
                    )
                    .order_by(FileResource.created_at.asc())
                )
                rows = (await session.execute(stmt)).all()
                selected_ids = [r.id for r in rows]
                # Advance the watermark past everything we just considered
                # (delta run only). Targeted runs leave it untouched.
                if rows:
                    advance_to = max(r.created_at for r in rows)

    # Phase 2 (incremental commits): the slow part — filtering, LLM picks,
    # Transmission RPCs. ``autocommit=True`` makes process_resources commit
    # after each dispatch/decision, so the write lock is never held across an
    # external call. The block-level retry of committed_session is safe here:
    # every unit is idempotent (task dedup / decision upsert), so a re-run
    # after a lock error just skips already-committed work.
    async with committed_session() as session:
        agent = await session.get(Agent, agent_id)
        if not agent:
            raise RuntimeError(f"Agent {agent_id} not found")
        run = await session.get(AgentRun, run_id)
        if not run:
            raise RuntimeError(f"AgentRun {run_id} disappeared before finalisation")

        resources: list[FileResource] = []
        if selected_ids:
            result = await session.execute(
                select(FileResource)
                .where(FileResource.id.in_(selected_ids))
                # series/movie are read by the filter DSL (movie.rating …) and
                # the LLM pick summary — eager-load to avoid async lazy loads.
                # The work's collection feeds the series.collection /
                # movie.collection DSL fields, so chain-load it too; the
                # resource's own collection feeds the resource-level
                # ``collection`` field (franchise packs).
                .options(
                    selectinload(FileResource.series).selectinload(TVSeries.collection),
                    selectinload(FileResource.movie).selectinload(Movie.collection),
                    selectinload(FileResource.collection),
                )
                .order_by(FileResource.created_at.asc())
            )
            resources = list(result.scalars().all())
        run_result = await process_resources(agent, resources, session, autocommit=True)

        if advance_to is not None:
            agent.last_consumed_at = advance_to

        agent.last_run_at = utcnow()
        # More granular status so the UI can badge "待决策" instead of a
        # deceptively-green "success" when the run generated PDs but
        # dispatched nothing.
        if run_result.errors:
            agent.last_run_status = "failed"
        elif run_result.dispatched == 0 and run_result.pending_decisions > 0:
            agent.last_run_status = "pending_decisions"
        else:
            agent.last_run_status = "success"

        # Finalise the run record.
        run.status = agent.last_run_status
        run.finished_at = utcnow()
        run.scan_since = run_scan_since
        run.total_resources = run_result.total_resources
        run.matched = run_result.matched
        run.dispatched = run_result.dispatched
        run.pending_decisions = run_result.pending_decisions
        run.filter_failed = run_result.filter_failed
        run.duplicates_skipped = run_result.duplicates_skipped
        run.unrecognized = run_result.unrecognized
        run.matched_resource_ids = list(run_result.matched_resource_ids)
        run.errors = list(run_result.errors)

        return {
            "agent_id": agent_id,
            "run_id": run.id,
            "total_resources": run_result.total_resources,
            "matched": run_result.matched,
            "dispatched": run_result.dispatched,
            "pending_decisions": run_result.pending_decisions,
            "filter_failed": run_result.filter_failed,
            "duplicates_skipped": run_result.duplicates_skipped,
            "unrecognized": run_result.unrecognized,
            "errors": run_result.errors,
        }


# ---------------------------------------------------------------------------
# Metadata refresh / backfill
# ---------------------------------------------------------------------------

# Per-work ceiling for the background metadata-refresh jobs. A single hung
# external search (Jina/LLM call that never returns) must not stall the whole
# batch - and with it the shared task queue.
_REFRESH_WORK_TIMEOUT = 120  # seconds


async def _refresh_works_batch(
    items: list[dict], source: str, override_manual_edits: bool = False
) -> list[dict]:
    """Shared per-work refresh loop for every metadata-refresh caller.

    The manual batch endpoint and the per-channel periodic refresh both run
    through here — there is exactly one refresh execution path underneath:
    ``refresh_work_metadata`` (which funnels into ``process_title_only`` with
    ``source`` as the only branch parameter). One short transaction per work;
    a single hung external search cannot stall the whole batch.
    """
    from app.services.metadata_service import refresh_work_metadata

    results: list[dict] = []
    for item in items:
        work_id = item.get("id")
        content_type = item.get("content_type")
        try:
            async with committed_session() as session:
                r = await asyncio.wait_for(
                    refresh_work_metadata(
                        session, work_id, content_type, source,
                        override_manual_edits=override_manual_edits,
                    ),
                    timeout=_REFRESH_WORK_TIMEOUT,
                )
            results.append({"id": work_id, "content_type": content_type, **r})
        except TimeoutError:
            logger.warning(
                "[refresh_works] timed out after %ds for %s/%s",
                _REFRESH_WORK_TIMEOUT, content_type, work_id,
            )
            results.append(
                {"id": work_id, "content_type": content_type, "found": False, "error": "timeout"}
            )
        except Exception as e:  # noqa: BLE001 — keep processing the rest
            logger.warning(
                "[refresh_works] failed for %s/%s: %s", content_type, work_id, e
            )
            results.append(
                {"id": work_id, "content_type": content_type, "found": False, "error": str(e)}
            )
    return results


async def _handle_refresh_works_metadata(payload: dict) -> dict:  # pragma: no cover
    """Background job (manual batch refresh): refresh a batch of works."""
    await _refresh_runtime_config()
    items: list[dict] = payload.get("items", []) or []
    source = str(payload.get("source") or "")
    if not source:
        return {"status": "error", "processed": 0,
                "results": [{"error": "source is required"}]}
    results = await _refresh_works_batch(items, source)
    return {"status": "done", "processed": len(results), "results": results}


async def _handle_refresh_channel_works(payload: dict) -> dict:  # pragma: no cover
    """Background job: periodic work-metadata refresh scoped to one channel.

    Parameter derivation only — the fetch itself is the shared
    ``refresh_work_metadata`` pipeline:

    * ``source`` ← the channel's own ``metadata_source`` (resolved);
    * ``override_manual_edits`` is always False (manual edits are protected);
    * work selection ← works linked via the channel's FileResources, gated to
      those with fillable empty fields unless the channel opted into full
      scope (``metadata_refresh_full_scope``).
    """
    from app.models.channel import Channel
    from app.services.metadata_service import select_channel_works_for_refresh
    from app.services.metadata_sources import resolve_metadata_source

    await _refresh_runtime_config()
    channel_id = payload.get("channel_id")
    async with committed_session() as session:
        channel = await session.get(Channel, channel_id)
        if channel is None:
            logger.warning("[channel_refresh] channel %s not found", channel_id)
            return {"status": "done", "processed": 0, "results": []}
        if channel.status == "inactive":
            logger.info("[channel_refresh] channel %s inactive; skipping", channel_id)
            return {"status": "done", "processed": 0, "results": []}
        source = resolve_metadata_source(channel.metadata_source)
        items = await select_channel_works_for_refresh(
            session, channel_id,
            full_scope=bool(channel.metadata_refresh_full_scope),
        )

    logger.info(
        "[channel_refresh] channel %s source=%s scope=%s works=%d",
        channel_id, source,
        "full" if channel.metadata_refresh_full_scope else "gapped",
        len(items),
    )
    results = await _refresh_works_batch(
        items, source, override_manual_edits=False
    )
    return {"status": "done", "processed": len(results), "results": results}


async def _handle_backfill_metadata(payload: dict) -> dict:  # pragma: no cover
    """Background job: globally backfill retry-eligible unmatched resources.

    Driven by the standalone ``metadata_backfill`` scheduler job (not tied to
    fetch_channel) so metadata repair progresses even when feeds are slow or
    quiet. Processes up to ``MAX_GLOBAL_BACKFILL_PER_RUN`` resources across all
    agent-enabled channels; the scheduler re-enqueues with a stable key so the
    queue dedup runs it back-to-back while unparsed resources remain.
    """
    from app.services.fetch_service import (
        backfill_unmatched_resources_global,
        reconcile_stale_raw_episodes,
    )

    await _refresh_runtime_config()
    async with committed_session() as session:
        reconciled = await reconcile_stale_raw_episodes(session)
        processed = await backfill_unmatched_resources_global(session)
    logger.info(
        "[backfill_metadata] processed %d resources, reconciled %d stale episodes",
        processed, reconciled,
    )
    return {"status": "done", "processed": processed, "reconciled": reconciled}


# ---------------------------------------------------------------------------
# Interactive batch-file analysis
# ---------------------------------------------------------------------------

async def _handle_analyze_batch_files(payload: dict) -> dict:
    """Resolve a torrent listing and run its file/work LLM analysis."""
    from app.api.v1.resources import _resolve_resource_files, _store_batch_analysis
    from app.models.file_resource import FileResource
    from app.services import task_queue as task_queue_module
    from app.services.batch_content_analysis import (
        _valid_paths,
        analyze_listing_stream,
        build_candidate_works,
        resolve_fractional_specials,
    )
    from app.services.torrent_inspect import analyze_torrent_files

    await _refresh_runtime_config()
    resource_id: str = payload["resource_id"]
    fingerprint: str = payload["fingerprint"]
    job_key: str = payload["job_key"]
    output = ""

    async def progress(message: str, **extra) -> None:
        await task_queue_module.task_queue.update_progress(job_key, {
            "message": message, "output": output, **extra,
        })

    async with committed_session() as session:
        resource = await session.get(FileResource, resource_id)
        if resource is None:
            raise RuntimeError(f"Resource {resource_id} not found")
        files, source = await _resolve_resource_files(session, resource)
        candidate_works = await build_candidate_works(session, resource)
        await progress("正在解析 torrent 文件清单")
        if not files:
            result = {"suggestion": None, "listing_source": source}
            await _store_batch_analysis(fingerprint, result)
            return result

        report = analyze_torrent_files(files)
        if resource.series_id and resource.season is not None:
            for parsed in report.file_parses:
                if parsed["season"] is None and parsed["episode"] is not None:
                    parsed["season"] = resource.season
        if resource.series_id:
            overrides = await resolve_fractional_specials(
                session, resource.series_id, [item["name"] for item in files],
            )
            for parsed in report.file_parses:
                special_episode = overrides.get(parsed["path"])
                if special_episode is not None:
                    parsed["season"] = 0
                    parsed["episode"] = special_episode

        grouped_episodes: dict[int, list[int]] = {}
        for parsed in report.file_parses:
            if parsed["season"] is not None and parsed["episode"] is not None:
                grouped_episodes.setdefault(parsed["season"], []).append(parsed["episode"])
        season_ranges = [
            {"season": season, "episode_start": min(episodes), "episode_end": max(episodes)}
            for season, episodes in sorted(grouped_episodes.items())
        ]

        deterministic = {
            "scope_hint": report.scope,
            "seasons": sorted({
                fp["season"] for fp in report.file_parses if fp["season"] is not None
            }),
            "season_ranges": season_ranges,
            "files": report.file_parses,
            "clusters": [
                {"title": cluster.title, "files": list(cluster.files)}
                for cluster in report.clusters
            ],
        }
        await progress(
            f"已完成确定性解析：{len(report.file_parses)} 个媒体文件",
            deterministic=deterministic,
        )
        listing = [{"name": fp["path"], "size": fp.get("size")} for fp in report.file_parses]
        llm_works = []
        if listing:
            await progress("正在请求 LLM 分析作品归属", deterministic=deterministic)
            title = resource.search_title or resource.title_cn or resource.title_raw
            async for kind, value in analyze_listing_stream(
                title, listing, [cluster.title for cluster in report.clusters],
                candidate_works,
            ):
                if kind == "delta":
                    output = f"{output}{value}"[-50_000:]
                    await progress("正在请求 LLM 分析作品归属", deterministic=deterministic)
                elif kind == "result" and value:
                    llm_works = _valid_paths(
                        value, {fp["path"] for fp in report.file_parses}, candidate_works,
                    )

        # Merge validated LLM season/episode placements back into the
        # deterministic view. This preserves one canonical file list for the
        # wizard and makes season_ranges reflect the complete analysis.
        parsed_by_path = {fp["path"]: fp for fp in deterministic["files"]}
        for work in llm_works:
            for placement in work["files"]:
                parsed = parsed_by_path.get(placement["path"])
                if parsed is None:
                    continue
                if parsed["season"] is None and placement["season"] is not None:
                    parsed["season"] = placement["season"]
                if parsed["episode"] is None:
                    start, end = placement["episode_start"], placement["episode_end"]
                    if start is not None and start == end:
                        parsed["episode"] = start
        merged: dict[int, list[int]] = {}
        for parsed in deterministic["files"]:
            if parsed["season"] is not None and parsed["episode"] is not None:
                merged.setdefault(parsed["season"], []).append(parsed["episode"])
        deterministic["seasons"] = sorted(merged)
        deterministic["season_ranges"] = [
            {"season": season, "episode_start": min(episodes), "episode_end": max(episodes)}
            for season, episodes in sorted(merged.items())
        ]

        result = {
            "suggestion": {"deterministic": deterministic, "works": llm_works},
            "listing_source": source,
        }
        await _store_batch_analysis(fingerprint, result)
        return {**result, "output": output}


# ---------------------------------------------------------------------------
# Periodic scheduler jobs (enqueued by the scheduler's thin wrappers, executed
# here by the queue consumer)
# ---------------------------------------------------------------------------

async def _handle_sync_progress(payload: dict) -> dict:
    from app.services.scheduler import _sync_download_progress

    await _refresh_runtime_config()
    await _sync_download_progress()
    return {"status": "done"}


async def _handle_daily_cleanup(payload: dict) -> dict:
    from app.services.scheduler import _cleanup_expired

    await _refresh_runtime_config()
    await _cleanup_expired()
    return {"status": "done"}


async def _handle_daily_dedup(payload: dict) -> dict:
    from app.services.scheduler import _dedup_metadata

    await _refresh_runtime_config()
    await _dedup_metadata()
    return {"status": "done"}


async def _handle_check_downloaders(payload: dict) -> dict:
    from app.services.scheduler import _check_downloader_connections

    await _refresh_runtime_config()
    await _check_downloader_connections()
    return {"status": "done"}


async def _handle_fts_drain(payload: dict) -> dict:
    from app.services.scheduler import _drain_fts_outbox

    await _refresh_runtime_config()
    await _drain_fts_outbox()
    return {"status": "done"}


async def _handle_fts_reconcile(payload: dict) -> dict:
    from app.services.scheduler import _reconcile_fts

    await _refresh_runtime_config()
    await _reconcile_fts()
    return {"status": "done"}


async def _handle_download_notifications(payload: dict) -> dict:
    from app.services.scheduler import _process_download_notifications

    await _refresh_runtime_config()
    await _process_download_notifications()
    return {"status": "done"}


async def _handle_refresh_resource_organize(payload: dict) -> dict:
    from app.services.notify_service import regenerate_resource_notifications

    await _refresh_runtime_config()
    async with committed_session() as session:
        return await regenerate_resource_notifications(session, payload["resource_id"])


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register_all_handlers(queue) -> None:
    """Register every job handler on *queue*.

    Called by both entry points (web/all lifespan and the worker process) so
    job_type registration is consistent everywhere; only consumers (``all`` /
    ``worker`` roles) actually execute them.
    """
    queue.register("fetch_channel", _handle_fetch_channel)
    queue.register("run_agent", _handle_run_agent)
    queue.register("refresh_works_metadata", _handle_refresh_works_metadata)
    queue.register("refresh_channel_works", _handle_refresh_channel_works)
    queue.register("backfill_metadata", _handle_backfill_metadata)
    queue.register("analyze_batch_files", _handle_analyze_batch_files)
    queue.register("sync_progress", _handle_sync_progress)
    queue.register("daily_cleanup", _handle_daily_cleanup)
    queue.register("daily_dedup", _handle_daily_dedup)
    queue.register("check_downloaders", _handle_check_downloaders)
    queue.register("fts_drain", _handle_fts_drain)
    queue.register("fts_reconcile", _handle_fts_reconcile)
    queue.register("download_notifications", _handle_download_notifications)
    queue.register("refresh_resource_organize", _handle_refresh_resource_organize)
