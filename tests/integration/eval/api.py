"""FastAPI router for the Metadata Eval labeling tool.

Endpoints: load RSS feed titles, run MetadataAgent in batch, search
metadata manually, and manage GroundTruth datasets (DB + JSON + Parquet).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from pathlib import Path
from typing import Any

import feedparser
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from tests.integration.eval.dataset import (
    db_list_datasets,
    db_load_entries,
    db_save_entries,
    db_delete_dataset,
    export_parquet,
    list_json_datasets,
    load_json,
    new_dataset,
    DEFAULT_DATA_SOURCE_TYPE,
    normalize_data_source_type,
    save_json,
)

logger = logging.getLogger("rssripple.eval")

router = APIRouter(prefix="/api")

# ── Feed paths ──────────────────────────────────────────────────────────

FEEDS_DIR = Path(__file__).parent.parent / "server" / "feeds"

KNOWN_FEEDS = {
    "mikanani": FEEDS_DIR / "mikanani.xml",
    "kisssub": FEEDS_DIR / "kisssub.xml",
    "eztv": FEEDS_DIR / "eztv.xml",
    "dmhy": FEEDS_DIR / "dmhy.xml",
}


# ── DB session dependency for eval tool ─────────────────────────────────

async def _get_db():
    """Yield an async DB session (reuses project database config)."""
    from app.database import async_session_factory

    async with async_session_factory() as session:
        yield session


# ── Pydantic schemas ────────────────────────────────────────────────────


class LoadTitlesResponse(BaseModel):
    titles: list[dict[str, Any]]
    total: int


class RunAgentRequest(BaseModel):
    title_ids: list[str] | None = None
    titles: list[dict[str, Any]] | None = None
    data_source_type: str = DEFAULT_DATA_SOURCE_TYPE


class RunAgentResponse(BaseModel):
    """Returned immediately — the actual results come via polling."""

    job_id: str
    title_ids: list[str]
    total: int


class SearchMetadataRequest(BaseModel):
    search_title: str
    content_type: str
    data_source_type: str = DEFAULT_DATA_SOURCE_TYPE


class SaveDatasetRequest(BaseModel):
    name: str
    entries: list[dict[str, Any]]
    data_source_type: str = DEFAULT_DATA_SOURCE_TYPE
    save_to_db: bool = True
    save_to_json: bool = True


# ── Title loading ───────────────────────────────────────────────────────


def _parse_feed_xml(path: Path, source_name: str) -> list[dict[str, Any]]:
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Feed file not found: {path}")

    parsed = feedparser.parse(path.read_text(encoding="utf-8"))
    titles: list[dict[str, Any]] = []

    for entry in parsed.entries:
        raw_title = entry.get("title", "").strip()
        if not raw_title:
            continue

        enclosure_url = None
        for link_obj in entry.get("links", []):
            if link_obj.get("rel") == "enclosure" or link_obj.get("type") == "application/x-bittorrent":
                enclosure_url = link_obj.get("href") or link_obj.get("url")
                break

        titles.append({
            "id": hashlib.sha256(f"{source_name}:{raw_title}".encode("utf-8")).hexdigest()[:16],
            "raw_title": raw_title,
            "source_feed": source_name,
            "enclosure_url": enclosure_url,
        })

    return titles


@router.post("/load-titles", response_model=LoadTitlesResponse)
async def load_titles(
    feeds: str | None = Query(
        None, description="Comma-separated feed names: mikanani,kisssub,eztv,dmhy"
    ),
    sample_size: int = Query(0, description="Max titles to return; 0 means all"),
):
    selected = [f.strip() for f in (feeds or "").split(",") if f.strip()]
    if not selected:
        selected = list(KNOWN_FEEDS.keys())

    logger.info("[eval][api] load_titles feeds=%s sample_size=%d", selected, sample_size)
    all_titles: list[dict[str, Any]] = []
    for name in selected:
        path = KNOWN_FEEDS.get(name)
        if path is None:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown feed '{name}'. Known: {', '.join(KNOWN_FEEDS)}",
            )
        all_titles.extend(await asyncio.to_thread(_parse_feed_xml, path, name))

    # Dedup by exact title
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for t in all_titles:
        key = t["raw_title"]
        if key not in seen:
            seen.add(key)
            deduped.append(t)

    if sample_size > 0 and sample_size < len(deduped):
        deduped = deduped[:sample_size]

    logger.info("[eval][api] load_titles done total=%d returned=%d", len(all_titles), len(deduped))
    return LoadTitlesResponse(titles=deduped, total=len(deduped))


# ── Agent runner ────────────────────────────────────────────────────────


@router.post("/run-agent", response_model=RunAgentResponse)
async def run_agent(
    body: RunAgentRequest,
    max_concurrency: int = Query(3, ge=1, le=10),
):
    """Start a background agent run. Returns immediately with a job_id.

    Poll ``GET /run-agent/{job_id}/status`` for results.
    The job is persisted to the DB and survives server restarts.
    """
    from tests.integration.eval.job_store import create_job, set_job_status

    titles: list[dict[str, Any]] = body.titles or []
    data_source_type = normalize_data_source_type(body.data_source_type)
    for title in titles:
        title["data_source_type"] = data_source_type
    if not titles and body.title_ids:
        raise HTTPException(
            status_code=422,
            detail=(
                "Pass the full 'titles' list (from /load-titles). "
                "title_ids alone cannot resolve raw titles."
            ),
        )

    job_id = str(uuid.uuid4())
    title_ids = [t["id"] for t in titles]
    logger.info(
        "[eval][api] run_agent create job_id=%s total=%d source=%s max_concurrency=%d title_ids_sample=%s",
        job_id, len(titles), data_source_type, max_concurrency, title_ids[:10],
    )

    if not titles:
        await create_job(job_id, [], [], max_concurrency)
        await set_job_status(job_id, "completed")
        logger.info("[eval][api] run_agent empty job_id=%s completed immediately", job_id)
        return RunAgentResponse(job_id=job_id, title_ids=[], total=0)

    await create_job(job_id, titles, title_ids, max_concurrency)

    # Fire and forget — survives client disconnect AND server restart
    asyncio.create_task(
        _run_agent_background(job_id, titles, max_concurrency)
    )

    return RunAgentResponse(job_id=job_id, title_ids=title_ids, total=len(titles))


async def _run_agent_background(
    job_id: str,
    titles: list[dict[str, Any]],
    max_concurrency: int,
) -> None:
    """Process titles concurrently, storing results incrementally to DB.

    This function is called both for new jobs and for resumed jobs (after
    server restart).  In the resume case, *titles* contains only the titles
    that don't have results yet.
    """
    from tests.integration.eval.agent_runner import _process_single
    from tests.integration.eval.job_store import update_job_result, set_job_status

    try:
        logger.info(
            "[eval][job] start job_id=%s total=%d max_concurrency=%d",
            job_id, len(titles), max_concurrency,
        )
        semaphore = asyncio.Semaphore(max_concurrency)
        tasks = [asyncio.ensure_future(_process_single(t, semaphore)) for t in titles]
        pending = set(tasks)
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                r = t.result()  # AgentRunResult
                result_dict = {
                    "title_id": r.title_id,
                    "title_raw": r.title_raw,
                    "source_feed": r.source_feed,
                    "resource_metadata": r.resource_metadata,
                    "latency_ms": r.latency_ms,
                    "error": r.error,
                }
                await update_job_result(job_id, r.title_id, result_dict)
                logger.info(
                    "[eval][job] progress job_id=%s title_id=%s error=%s latency_ms=%s",
                    job_id, r.title_id, r.error, r.latency_ms,
                )
        await set_job_status(job_id, "completed")
        logger.info("[eval][job] completed job_id=%s total=%d", job_id, len(titles))
    except Exception as exc:
        logger.exception("Background agent job %s failed", job_id, exc_info=True)
        await set_job_status(job_id, "failed", error=str(exc))


@router.get("/run-agent/{job_id}/status")
async def get_job_status(job_id: str):
    """Poll for job status and partial/complete results (DB-backed)."""
    from tests.integration.eval.job_store import get_job

    job = await get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return {
        "status": job["status"],
        "total": job["total"],
        "completed": job["completed"],
        "title_ids": job["title_ids"],
        "results": job["results"],
        "error": job.get("error"),
    }


# ── Manual metadata search ──────────────────────────────────────────────


@router.post("/search-metadata")
async def search_metadata(body: SearchMetadataRequest):
    if body.content_type not in ("tv", "movie"):
        raise HTTPException(status_code=422, detail="content_type must be 'tv' or 'movie'")

    from app.database import async_session_factory
    from app.services.metadata_service import manual_search_metadata

    try:
        data_source_type = normalize_data_source_type(body.data_source_type)
        logger.info(
            "[eval][api] search_metadata start source=%s content_type=%s title=%r",
            data_source_type, body.content_type, body.search_title[:240],
        )
        async with async_session_factory() as db:
            candidates = await manual_search_metadata(
                db,
                body.search_title,
                body.content_type,
                data_source_type,
            )
        logger.info(
            "[eval][api] search_metadata done source=%s title=%r candidates=%d sample=%s",
            data_source_type,
            body.search_title[:240],
            len(candidates),
            candidates[:3],
        )
        return {"candidates": candidates}
    except Exception as exc:
        logger.exception(
            "[eval][api] search_metadata failed source=%s content_type=%s title=%r",
            body.data_source_type, body.content_type, body.search_title[:240],
        )
        raise HTTPException(status_code=502, detail=str(exc))


# ── Dataset CRUD ────────────────────────────────────────────────────────


@router.get("/datasets")
async def api_list_datasets(db=Depends(_get_db)):
    """List all datasets from DB, with JSON-fallback metadata."""
    db_datasets = await db_list_datasets(db)
    json_datasets = {d["name"]: d for d in list_json_datasets()}

    # Merge: prefer DB, fallback to JSON
    merged: dict[str, dict] = {}
    for d in json_datasets.values():
        merged[d["name"]] = d
    for d in db_datasets:
        merged[d["name"]] = d

    logger.info(
        "[eval][api] list_datasets db=%d json=%d merged=%d",
        len(db_datasets), len(json_datasets), len(merged),
    )
    return {"datasets": list(merged.values())}


@router.get("/datasets/{name}")
async def api_get_dataset(name: str, db=Depends(_get_db)):
    """Load a dataset — tries DB first, then JSON file."""
    entries = await db_load_entries(db, name)
    if entries:
        data_source_type = entries[0].get("data_source_type") or normalize_data_source_type(None)
        return {
            "version": DATASET_VERSION,
            "name": name,
            "data_source_type": data_source_type,
            "total_entries": len(entries),
            "entries": entries,
            "source": "db",
        }

    try:
        dataset = load_json(name)
        dataset["source"] = "json"
        return dataset
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Dataset '{name}' not found")


async def _get_dataset_version() -> str:
    from tests.integration.eval.dataset import DATASET_VERSION as _v
    return _v


DATASET_VERSION = "1.0"


@router.post("/datasets")
async def api_save_dataset(body: SaveDatasetRequest, db=Depends(_get_db)):
    """Save a GroundTruth dataset. Persists to DB and optionally JSON."""
    if not body.name.strip():
        raise HTTPException(status_code=422, detail="Dataset name is required")

    data_source_type = normalize_data_source_type(body.data_source_type)
    logger.info(
        "[eval][api] save_dataset start name=%s entries=%d source=%s save_to_db=%s save_to_json=%s",
        body.name, len(body.entries), data_source_type, body.save_to_db, body.save_to_json,
    )
    for entry in body.entries:
        entry["data_source_type"] = data_source_type
        resource_metadata = entry.get("resource_metadata")
        if isinstance(resource_metadata, dict):
            resource_metadata["eval_data_source_type"] = data_source_type
        agent_result = entry.get("agent_result")
        if isinstance(agent_result, dict):
            agent_result["eval_data_source_type"] = data_source_type
    results: dict[str, Any] = {
        "name": body.name,
        "data_source_type": data_source_type,
        "total_entries": len(body.entries),
    }

    # Save to DB
    if body.save_to_db:
        count = await db_save_entries(db, body.name, body.entries, data_source_type)
        results["db_saved"] = count

    # Save to JSON
    if body.save_to_json:
        dataset = new_dataset(body.name, data_source_type)
        dataset["total_entries"] = len(body.entries)
        dataset["entries"] = body.entries
        path = await asyncio.to_thread(save_json, body.name, dataset)
        results["json_path"] = str(path)

    results["saved"] = True
    logger.info("[eval][api] save_dataset done name=%s result=%s", body.name, results)
    return results


@router.delete("/datasets/{name}")
async def api_delete_dataset(name: str, db=Depends(_get_db)):
    """Delete a dataset — removes from DB and JSON file if it exists."""
    count = await db_delete_dataset(db, name)

    # Also delete the JSON file if it exists
    from tests.integration.eval.dataset import _json_path
    json_path = _json_path(name)
    if json_path.exists():
        try:
            json_path.unlink()
        except OSError:
            pass

    return {"deleted": True, "name": name, "db_entries_removed": count}


# ── Parquet export ──────────────────────────────────────────────────────


@router.post("/datasets/{name}/export-parquet")
async def api_export_parquet(name: str, db=Depends(_get_db)):
    """Export a dataset from DB to Parquet format for integration tests.

    Returns the path to the generated .parquet file.
    """
    try:
        path = await export_parquet(db, name)
        return {
            "saved": True,
            "path": str(path),
            "name": name,
            "format": "parquet",
        }
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ImportError as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Import dataset from JSON into DB ────────────────────────────────────


@router.post("/datasets/{name}/import-to-db")
async def api_import_to_db(name: str, db=Depends(_get_db)):
    """Import a JSON dataset into the database."""
    try:
        dataset = load_json(name)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"JSON dataset '{name}' not found")

    entries = dataset.get("entries", [])
    count = await db_save_entries(
        db,
        name,
        entries,
        normalize_data_source_type(dataset.get("data_source_type")),
    )
    return {"imported": True, "name": name, "db_entries": count}
