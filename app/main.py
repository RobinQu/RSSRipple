"""FastAPI application entry point."""

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
from starlette.middleware.gzip import GZipMiddleware

# Import models for SQLAlchemy discovery
import app.models  # noqa: F401
from app.config import settings
from app.database import async_session_factory, create_tables, install_db_retry_middleware

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


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

    # Auth bootstrap: ensure the TOTP + cookie secrets exist (generated on
    # first run and persisted in app_settings), and surface the provisioning
    # URI every startup so the operator can enroll an authenticator.
    from app.services.auth_service import (
        get_or_create_cookie_secret,
        get_or_create_totp_secret,
        totp_provisioning_uri,
    )

    async with async_session_factory() as sess:
        totp_secret = await get_or_create_totp_secret(sess)
        await get_or_create_cookie_secret(sess)
        await sess.commit()
    logger.warning(
        "OTP provisioning URI (add to your authenticator): %s",
        totp_provisioning_uri(totp_secret),
    )

    # Web/worker separation: with APP_ROLE=web this process only serves HTTP
    # and enqueues jobs — the scheduler and queue consumer live in the worker
    # process (app/worker.py). APP_ROLE=all (default) keeps everything in one
    # process. Handler registration happens in every role so status/clear
    # semantics stay consistent.
    is_web_only = settings.app_role == "web"

    # Init scheduler
    from app.services.scheduler import (
        init_scheduler,
        setup_channel_jobs,
        shutdown_scheduler,
    )
    if not is_web_only:
        await init_scheduler()

        # Setup channel jobs with a DB session
        async with async_session_factory() as sess:
            await setup_channel_jobs(sess)
            await sess.commit()

    # Build task queue
    import app.services.task_queue as _tq_mod
    from app.job_handlers import register_all_handlers
    from app.services.task_queue import create_queue

    queue = create_queue(
        backend=settings.queue_backend,
        redis_url=settings.redis_url,
        max_concurrent=settings.queue_max_concurrent,
    )
    _tq_mod.task_queue = queue

    register_all_handlers(queue)

    # The web role never consumes: jobs it enqueues are executed by a worker.
    await queue.start(consume=not is_web_only)
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
app.add_middleware(GZipMiddleware, minimum_size=1024)

# Auth gate for /api/v1/* and /posters/* (no-op when AUTH_ENABLED=false).
from app.middleware.auth import AuthMiddleware  # noqa: E402

app.add_middleware(AuthMiddleware)

# Install DB lock retry middleware (SQLite-only, no-op on PostgreSQL)
install_db_retry_middleware(app)

# API routers
from app.api.v1 import (  # noqa: E402
    agents,
    api_keys,
    audio_works,
    auth,
    channels,
    collections,
    dashboard,
    decisions,
    downloaders,
    media_servers,
    metadata,
    movies,
    notifications,
    organize,
    resources,
    series,
    system_settings,
    tasks,
    volumes,
    works,
)

app.include_router(dashboard.router, prefix="/api/v1", tags=["dashboard"])
app.include_router(channels.router, prefix="/api/v1", tags=["channels"])
app.include_router(agents.router, prefix="/api/v1", tags=["agents"])
app.include_router(downloaders.router, prefix="/api/v1", tags=["downloaders"])
app.include_router(volumes.router, prefix="/api/v1", tags=["volumes"])
app.include_router(media_servers.router, prefix="/api/v1", tags=["media-servers"])
app.include_router(tasks.router, prefix="/api/v1", tags=["tasks"])
app.include_router(decisions.router, prefix="/api/v1", tags=["decisions"])
app.include_router(resources.router, prefix="/api/v1", tags=["resources"])
app.include_router(series.router, prefix="/api/v1", tags=["series"])
app.include_router(movies.router, prefix="/api/v1", tags=["movies"])
app.include_router(audio_works.router, prefix="/api/v1", tags=["audio-works"])
app.include_router(metadata.router, prefix="/api/v1", tags=["metadata"])
app.include_router(works.router, prefix="/api/v1", tags=["works"])
app.include_router(collections.router, prefix="/api/v1", tags=["collections"])
app.include_router(system_settings.router, prefix="/api/v1", tags=["settings"])
app.include_router(notifications.router, prefix="/api/v1", tags=["notifications"])
app.include_router(organize.router, prefix="/api/v1", tags=["organize"])
app.include_router(auth.router, prefix="/api/v1", tags=["auth"])
app.include_router(api_keys.router, prefix="/api/v1", tags=["api-keys"])

# Container healthcheck probe. Auth-exempt: lives at the app root, outside the
# AuthMiddleware-protected /api/v1/* and /posters/* prefixes, so it must be
# registered before the SPA catch-all below. Verifies DB reachability so the
# probe doubles as a readiness signal, not just process liveness.
@app.get("/health")
async def health():
    from sqlalchemy import text

    try:
        async with async_session_factory() as session:
            await session.execute(text("SELECT 1"))
    except Exception:
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy", "database": "unreachable"},
        )
    return {"status": "ok", "database": "ok"}


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
class ImmutableStaticFiles(StaticFiles):
    """Serve content-hashed build assets with a long-lived browser cache."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


if STATIC_DIR.exists():  # pragma: no cover
    app.mount(
        "/assets",
        ImmutableStaticFiles(directory=STATIC_DIR / "assets"),
        name="static-assets",
    )

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
