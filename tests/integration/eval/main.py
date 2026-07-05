"""Standalone FastAPI application for the RSSRipple Metadata Eval Tool.

Launch with::

    uv run uvicorn tests.integration.eval.main:app --reload --port 8090
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create all DB tables on startup, then resume any running jobs."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("rssripple.eval").setLevel(logging.INFO)
    logging.getLogger("app.services.metadata_agent").setLevel(logging.INFO)
    logging.getLogger("app.services.metadata_search_agent").setLevel(logging.INFO)
    logger = logging.getLogger("rssripple.eval")
    logger.info("[eval] startup: creating tables and resuming jobs")

    # Ensure all models are imported before create_all
    import app.models  # noqa: F401 — triggers model registration
    from app.database import Base, engine

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Resume any jobs that were "running" when the server stopped
    from tests.integration.eval.job_store import resume_running_jobs
    resumed = await resume_running_jobs()
    logger.info("[eval] startup complete: resumed_jobs=%d", resumed)

    yield


app = FastAPI(title="RSSRipple Metadata Eval Tool", lifespan=lifespan)

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

from tests.integration.eval.api import router as api_router

app.include_router(api_router)


@app.get("/")
async def index(request: Request):
    """Serve the labeling page."""
    return templates.TemplateResponse("index.html", {"request": request})
