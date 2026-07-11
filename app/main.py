"""FastAPI application entry point."""

import asyncio
import logging
import traceback
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

# Import models for SQLAlchemy discovery
import app.models  # noqa: F401
from app.config import settings
from app.database import async_session_factory, committed_session, create_tables, install_db_retry_middleware

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


# ---------------------------------------------------------------------------
# Background job handlers
# ---------------------------------------------------------------------------

async def _handle_fetch_channel(payload: dict) -> dict:  # pragma: no cover
    from app.models.channel import Channel
    from app.services.fetch_service import fetch_channel_resources

    channel_id: str = payload["channel_id"]
    async with committed_session() as session:
        ch = await session.get(Channel, channel_id)
        if not ch:
            raise RuntimeError(f"Channel {channel_id} not found")
        result = await fetch_channel_resources(ch, session)
        return result


async def _handle_run_agent(payload: dict) -> dict:  # pragma: no cover
    from sqlalchemy import select

    from app.models.agent import Agent
    from app.models.agent_run import AgentRun
    from app.models.file_resource import FileResource
    from app.services.agent_service import process_resources
    from app.utils.time import utcnow

    agent_id: str = payload["agent_id"]
    resource_ids: list[str] | None = payload.get("resource_ids")
    async with committed_session() as session:
        agent = await session.get(Agent, agent_id)
        if not agent:
            raise RuntimeError(f"Agent {agent_id} not found")

        # Persist a run record up front so a "running" row exists even if the
        # handler crashes; finalised at the end with counts + matched ids.
        run = AgentRun(agent_id=agent.id, status="running", started_at=utcnow())
        session.add(run)
        await session.flush()

        if resource_ids:
            # Targeted run (scenario ③, e.g. correct_episode): process exactly
            # the given resources against the agent's *current* rules. Bypasses
            # the watermark and does NOT advance it — the resource may be old,
            # and advancing would skip its neighbours.
            result = await session.execute(
                select(FileResource)
                .where(
                    FileResource.channel_id == agent.channel_id,
                    FileResource.id.in_(resource_ids),
                )
                .order_by(FileResource.created_at.asc())
            )
            resources = result.scalars().all()
            run_result = await process_resources(agent, resources, session)
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
                resources = []
            else:
                result = await session.execute(
                    select(FileResource)
                    .where(
                        FileResource.channel_id == agent.channel_id,
                        FileResource.created_at > wm,
                    )
                    .order_by(FileResource.created_at.asc())
                )
                resources = result.scalars().all()
            run_result = await process_resources(agent, resources, session)

            # Advance the watermark past everything we just considered (delta
            # run only). Targeted runs leave it untouched.
            if resources:
                agent.last_consumed_at = max(r.created_at for r in resources)
            elif agent.last_consumed_at is None:
                agent.last_consumed_at = utcnow()

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

    items: list[dict] = payload.get("items", []) or []
    source: str | None = payload.get("source")
    results: list[dict] = []
    async with committed_session() as session:
        for item in items:
            work_id = item.get("id")
            content_type = item.get("content_type")
            try:
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


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("app").setLevel(settings.log_level)

    # Ensure poster dir exists before mounting
    poster_dir = Path(settings.poster_cache_dir)
    try:
        poster_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        # Fallback to local data/posters if configured path is unwritable
        logger.warning("Cannot create poster dir %s (%s); falling back to ./data/posters", poster_dir, e)
        poster_dir = Path("data/posters")
        poster_dir.mkdir(parents=True, exist_ok=True)
        settings.poster_cache_dir = str(poster_dir)
    # Ensure data dir exists for sqlite
    db_url = settings.database_url
    if db_url.startswith("sqlite"):
        if "sqlite:///" in db_url:
            db_path_str = db_url.split("sqlite:///", 1)[-1]
        else:
            db_path_str = db_url.split("sqlite:", 1)[-1].lstrip("/")
        db_path = Path(db_path_str)
        try:
            if db_path.parent and str(db_path.parent) != "":
                db_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning("Cannot create db dir %s (%s)", db_path.parent, e)

    logger.info("Creating database tables...")
    await create_tables()
    logger.info("Database ready.")

    # Load runtime-configurable settings (LLM + external search source keys)
    # from the DB into the in-memory cache so user overrides take effect.
    from app.services.runtime_config import load_runtime_config

    async with async_session_factory() as sess:
        await load_runtime_config(sess)
    logger.info("Runtime settings loaded.")

    # Init scheduler
    from app.services.scheduler import (
        init_scheduler,
        setup_channel_jobs,
        setup_metadata_refresh_job,
        shutdown_scheduler,
    )
    await init_scheduler()

    # Setup channel jobs with a DB session
    async with async_session_factory() as sess:
        await setup_channel_jobs(sess)
        await sess.commit()

    # Build task queue
    import app.services.task_queue as _tq_mod
    from app.services.task_queue import create_queue

    queue = create_queue(
        backend=settings.queue_backend,
        redis_url=settings.redis_url,
        max_concurrent=settings.queue_max_concurrent,
    )
    _tq_mod.task_queue = queue

    queue.register("fetch_channel", _handle_fetch_channel)
    queue.register("run_agent", _handle_run_agent)
    queue.register("refresh_works_metadata", _handle_refresh_works_metadata)

    await queue.start()
    async with async_session_factory() as sess:
        await setup_metadata_refresh_job(sess)
        await sess.commit()
    try:
        yield
    finally:
        await queue.stop()
        await shutdown_scheduler()
        logger.info("Shutting down.")


# ---------------------------------------------------------------------------
# Exception handlers
# ---------------------------------------------------------------------------

async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    if exc.status_code >= 500:  # pragma: no cover
        logger.error(
            "HTTP %s %s %s: %s",
            exc.status_code, request.method, request.url.path, exc.detail,
        )
    code = str(exc.status_code)
    message = str(exc.detail)
    if isinstance(exc.detail, dict):  # pragma: no cover
        code = exc.detail.get("code", code)
        message = exc.detail.get("message", message)
    else:
        if exc.status_code == 404:
            code = "NOT_FOUND"
        elif exc.status_code == 409:
            code = "CONFLICT"
        elif exc.status_code == 400:
            code = "BAD_REQUEST"
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "success": False,
            "data": None,
            "error": {"code": code, "message": message},
            "meta": {},
        },
    )


async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    errors = jsonable_encoder(exc.errors(), custom_encoder={Exception: str})
    message = "; ".join(str(error.get("msg", error)) for error in errors) or "Validation error"
    logger.warning(
        "Validation error %s %s: %s",
        request.method, request.url.path, errors,
    )
    return JSONResponse(
        status_code=422,
        content={
            "success": False,
            "data": None,
            "error": {"code": "VALIDATION_ERROR", "message": message, "details": errors},
            "meta": {},
        },
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error(
        "Unhandled exception %s %s: %r",
        request.method, request.url.path, exc,
        exc_info=True,
    )
    body: dict = {
        "success": False,
        "data": None,
        "error": {"code": "INTERNAL_SERVER_ERROR", "message": "An unexpected error occurred"},
        "meta": {},
    }
    if settings.dev_mode:
        body["error"]["stack"] = traceback.format_exc()  # type: ignore[typeddict-unknown-key]
    return JSONResponse(status_code=500, content=body)


app = FastAPI(
    title=settings.app_name,
    version="0.2.0",
    lifespan=lifespan,
    exception_handlers={
        StarletteHTTPException: http_exception_handler,
        RequestValidationError: validation_exception_handler,
        Exception: unhandled_exception_handler,
    },
)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Install DB lock retry middleware (SQLite-only, no-op on PostgreSQL)
install_db_retry_middleware(app)

# API routers
from app.api.v1 import (  # noqa: E402
    agents,
    channels,
    dashboard,
    decisions,
    downloaders,
    movies,
    resources,
    series,
    system_settings,
    tasks,
    works,
)

app.include_router(dashboard.router, prefix="/api/v1", tags=["dashboard"])
app.include_router(channels.router, prefix="/api/v1", tags=["channels"])
app.include_router(agents.router, prefix="/api/v1", tags=["agents"])
app.include_router(downloaders.router, prefix="/api/v1", tags=["downloaders"])
app.include_router(tasks.router, prefix="/api/v1", tags=["tasks"])
app.include_router(decisions.router, prefix="/api/v1", tags=["decisions"])
app.include_router(resources.router, prefix="/api/v1", tags=["resources"])
app.include_router(series.router, prefix="/api/v1", tags=["series"])
app.include_router(movies.router, prefix="/api/v1", tags=["movies"])
app.include_router(works.router, prefix="/api/v1", tags=["works"])
app.include_router(system_settings.router, prefix="/api/v1", tags=["settings"])

# Poster image cache - mount even if empty/default
_poster_dir = Path(settings.poster_cache_dir)
try:  # pragma: no cover
    _poster_dir.mkdir(parents=True, exist_ok=True)
except OSError:  # pragma: no cover
    _poster_dir = Path("data/posters")
    _poster_dir.mkdir(parents=True, exist_ok=True)
    settings.poster_cache_dir = str(_poster_dir)
app.mount("/posters", StaticFiles(directory=str(_poster_dir)), name="poster-cache")

# Static files (frontend)
if STATIC_DIR.exists():  # pragma: no cover
    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="static-assets")

    def spa_index_response() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store, max-age=0"})

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        file_path = STATIC_DIR / full_path
        if file_path.is_file():
            if file_path.name == "index.html":
                return spa_index_response()
            return FileResponse(file_path)
        return spa_index_response()
else:
    @app.get("/")
    async def root():  # pragma: no cover
        return {"message": "RSSRipple API", "docs": "/docs"}
