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

# Per-work ceiling for the background metadata-refresh job. A single hung
# external search (Jina/LLM call that never returns) must not stall the whole
# batch - and with it the shared task queue.
_REFRESH_WORK_TIMEOUT = 120  # seconds


async def _handle_refresh_works_metadata(payload: dict) -> dict:  # pragma: no cover
    """Background job: refresh metadata for a batch of works sequentially.

    Each work is bounded by ``_REFRESH_WORK_TIMEOUT`` so a single hung external
    search cannot stall the whole batch - and the shared task queue - forever.
    """
    from app.services.metadata_service import refresh_work_metadata

    await _refresh_runtime_config()
    items: list[dict] = payload.get("items", []) or []
    source: str | None = payload.get("source")
    results: list[dict] = []
    # One short transaction per work. A single transaction wrapping the whole
    # batch would hold the SQLite write lock across every external metadata
    # search (up to ``_REFRESH_WORK_TIMEOUT`` each), stalling foreground writes
    # for minutes. (refresh_work_metadata also commits internally; the wrapper
    # gives per-work lock-retry semantics instead of retrying the whole batch.)
    for item in items:
        work_id = item.get("id")
        content_type = item.get("content_type")
        try:
            async with committed_session() as session:
                r = await asyncio.wait_for(
                    refresh_work_metadata(session, work_id, content_type, source),
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
    return {"status": "done", "processed": len(results), "results": results}


async def _handle_backfill_metadata(payload: dict) -> dict:  # pragma: no cover
    """Background job: globally backfill retry-eligible unmatched resources.

    Driven by the standalone ``metadata_backfill`` scheduler job (not tied to
    fetch_channel) so metadata repair progresses even when feeds are slow or
    quiet. Processes up to ``MAX_GLOBAL_BACKFILL_PER_RUN`` resources across all
    agent-enabled channels; the scheduler re-enqueues with a stable key so the
    queue dedup runs it back-to-back while unparsed resources remain.
    """
    from app.services.fetch_service import backfill_unmatched_resources_global

    await _refresh_runtime_config()
    async with committed_session() as session:
        processed = await backfill_unmatched_resources_global(session)
    logger.info("[backfill_metadata] processed %d resources", processed)
    return {"status": "done", "processed": processed}


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
    queue.register("backfill_metadata", _handle_backfill_metadata)
    queue.register("sync_progress", _handle_sync_progress)
    queue.register("daily_cleanup", _handle_daily_cleanup)
    queue.register("daily_dedup", _handle_daily_dedup)
    queue.register("check_downloaders", _handle_check_downloaders)
    queue.register("fts_drain", _handle_fts_drain)
    queue.register("fts_reconcile", _handle_fts_reconcile)
    queue.register("download_notifications", _handle_download_notifications)
