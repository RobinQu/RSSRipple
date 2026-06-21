"""FastAPI application entry point."""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from app.config import settings
from app.database import create_tables

# Import models for SQLAlchemy discovery
import app.models  # noqa: F401

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown lifecycle."""
    logging.basicConfig(level=settings.log_level)
    logger.info("Creating database tables...")
    await create_tables()
    logger.info("Database ready.")
    # TODO: Start APScheduler
    yield
    # TODO: Shutdown scheduler
    logger.info("Shutting down.")


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# API routers
from app.api.v1 import channels, agents, filters, downloaders, tasks, decisions, dashboard, resources, series, movies  # noqa: E402

app.include_router(dashboard.router, prefix="/api/v1", tags=["dashboard"])
app.include_router(channels.router, prefix="/api/v1", tags=["channels"])
app.include_router(agents.router, prefix="/api/v1", tags=["agents"])
app.include_router(filters.router, prefix="/api/v1", tags=["filters"])
app.include_router(downloaders.router, prefix="/api/v1", tags=["downloaders"])
app.include_router(tasks.router, prefix="/api/v1", tags=["tasks"])
app.include_router(decisions.router, prefix="/api/v1", tags=["decisions"])
app.include_router(resources.router, prefix="/api/v1", tags=["resources"])
app.include_router(series.router, prefix="/api/v1", tags=["series"])
app.include_router(movies.router, prefix="/api/v1", tags=["movies"])

# Static files (frontend)
if STATIC_DIR.exists():
    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="static-assets")

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        """Serve the React SPA for all non-API routes."""
        file_path = STATIC_DIR / full_path
        if file_path.is_file():
            return FileResponse(file_path)
        return FileResponse(STATIC_DIR / "index.html")
else:
    @app.get("/")
    async def root():
        return {"message": "RSS Downloader API", "docs": "/docs"}
