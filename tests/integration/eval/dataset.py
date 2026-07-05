"""GroundTruth dataset I/O — DB persistence + JSON export + Parquet export.

Supports three storage backends:
1. SQLite DB   — ``GroundTruthEntry`` ORM model (primary)
2. JSON files  — ``tests/data/ground_truth_{name}.json`` (portable)
3. Parquet     — ``tests/data/ground_truth_{name}.parquet`` (integration tests)
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("rssripple.eval")

GROUND_TRUTH_DIR = Path(__file__).parent.parent.parent / "data"
DATASET_VERSION = "1.0"
DATA_SOURCE_TYPES = {"combined", "tmdb", "exa", "wikipedia"}
DEFAULT_DATA_SOURCE_TYPE = "exa"


# ── DB helpers ──────────────────────────────────────────────────────────


async def db_save_entries(
    db: AsyncSession,
    dataset_name: str,
    entries: list[dict[str, Any]],
    data_source_type: str = DEFAULT_DATA_SOURCE_TYPE,
) -> int:
    """Upsert GroundTruth entries into the database.

    Deletes existing entries for the dataset, then bulk-inserts new ones.
    Returns the count of saved entries.
    """
    from app.models.ground_truth import GroundTruthEntry

    # Delete existing entries for this dataset
    await db.execute(
        delete(GroundTruthEntry).where(GroundTruthEntry.dataset_name == dataset_name)
    )

    saved = 0
    for entry in entries:
        resource_metadata = entry.get("resource_metadata")
        agent_result = entry.get("agent_result")
        if isinstance(resource_metadata, dict):
            resource_metadata["eval_data_source_type"] = data_source_type
        if isinstance(agent_result, dict):
            agent_result["eval_data_source_type"] = data_source_type

        gt_entry = GroundTruthEntry(
            id=_db_entry_id(dataset_name, entry),
            dataset_name=dataset_name,
            raw_title=entry.get("raw_title", ""),
            source_feed=entry.get("source_feed", ""),
            ground_truth_json=resource_metadata if isinstance(resource_metadata, dict) else {},
            agent_result_json=agent_result if isinstance(agent_result, dict) else None,
            review_status=entry.get("review_status", "pending"),
            reviewer_notes=entry.get("notes"),
        )
        db.add(gt_entry)
        saved += 1

    await db.commit()
    return saved


async def db_load_entries(
    db: AsyncSession,
    dataset_name: str,
) -> list[dict[str, Any]]:
    """Load all GroundTruth entries for a dataset from the database."""
    from app.models.ground_truth import GroundTruthEntry

    result = await db.execute(
        select(GroundTruthEntry)
        .where(GroundTruthEntry.dataset_name == dataset_name)
        .order_by(GroundTruthEntry.created_at)
    )
    rows = result.scalars().all()

    return [
        {
            "id": _public_entry_id(row.source_feed, row.raw_title),
            "raw_title": row.raw_title,
            "source_feed": row.source_feed,
            "resource_metadata": row.ground_truth_json,
            "agent_result": row.agent_result_json,
            "data_source_type": _infer_data_source_type(
                row.dataset_name,
                row.ground_truth_json,
                row.agent_result_json,
            ),
            "review_status": row.review_status,
            "reviewed_at": row.updated_at.isoformat() if row.updated_at else None,
            "notes": row.reviewer_notes,
        }
        for row in rows
    ]


async def db_delete_dataset(db: AsyncSession, dataset_name: str) -> int:
    """Delete all GroundTruth entries for a dataset. Returns count deleted."""
    from app.models.ground_truth import GroundTruthEntry

    result = await db.execute(
        delete(GroundTruthEntry).where(GroundTruthEntry.dataset_name == dataset_name)
    )
    await db.commit()
    return result.rowcount


async def db_list_datasets(db: AsyncSession) -> list[dict]:
    """List distinct dataset names from the database with entry counts."""
    from sqlalchemy import func as sa_func

    from app.models.ground_truth import GroundTruthEntry

    result = await db.execute(
        select(
            GroundTruthEntry.dataset_name,
            sa_func.count(GroundTruthEntry.id),
            sa_func.min(GroundTruthEntry.created_at),
        )
        .group_by(GroundTruthEntry.dataset_name)
        .order_by(sa_func.min(GroundTruthEntry.created_at).desc())
    )
    return [
        {
            "name": row[0],
            "data_source_type": _infer_data_source_type(row[0]),
            "total_entries": row[1],
            "created_at": row[2].isoformat() if row[2] else None,
            "version": DATASET_VERSION,
        }
        for row in result.all()
    ]


# ── JSON file I/O ───────────────────────────────────────────────────────


def save_json(name: str, dataset: dict) -> Path:
    """Persist a GroundTruth dataset as a JSON file."""
    GROUND_TRUTH_DIR.mkdir(parents=True, exist_ok=True)
    path = _json_path(name)
    path.write_text(json.dumps(dataset, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_json(name: str) -> dict:
    """Load a GroundTruth dataset from a JSON file."""
    path = _json_path(name)
    if not path.exists():
        raise FileNotFoundError(f"Dataset '{name}' not found at {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def list_json_datasets() -> list[dict]:
    """List JSON-based datasets."""
    datasets: list[dict] = []
    if not GROUND_TRUTH_DIR.exists():
        return datasets
    for path in sorted(GROUND_TRUTH_DIR.glob("ground_truth_*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        datasets.append({
            "name": data.get("name", path.stem.replace("ground_truth_", "")),
            "data_source_type": data.get("data_source_type") or _infer_data_source_type(
                data.get("name", path.stem.replace("ground_truth_", ""))
            ),
            "total_entries": data.get("total_entries", 0),
            "created_at": data.get("created_at"),
            "version": data.get("version"),
        })
    return datasets


# ── Parquet export ──────────────────────────────────────────────────────


async def export_parquet(db: AsyncSession, dataset_name: str) -> Path:
    """Export a dataset from DB as a Parquet file for integration tests.

    Flattens the JSON fields into separate columns for easy test consumption.
    """
    entries = await db_load_entries(db, dataset_name)
    if not entries:
        raise ValueError(f"No entries found for dataset '{dataset_name}'")

    # Normalize nested dicts into flat rows
    rows: list[dict[str, Any]] = []
    for e in entries:
        gt = e.get("resource_metadata") or {}
        agent = e.get("agent_result") or {}

        row = {
            "raw_title": e["raw_title"],
            "source_feed": e["source_feed"],
            "review_status": e["review_status"],
            # Ground truth fields
            "gt_clean_title": gt.get("clean_title"),
            "gt_content_type": gt.get("content_type"),
            "gt_episode": gt.get("episode") or gt.get("inferred_episode"),
            "gt_season": gt.get("season") or gt.get("inferred_season"),
            "gt_title_cn": gt.get("title_cn"),
            "gt_title_en": gt.get("title_en"),
            "gt_matched_eid": (gt.get("matched_entity") or {}).get("external_id") if isinstance(gt.get("matched_entity"), dict) else None,
            # Agent fields (for comparison)
            "agent_clean_title": agent.get("clean_title"),
            "agent_content_type": agent.get("content_type"),
            "agent_episode": agent.get("episode") or agent.get("inferred_episode"),
            "agent_season": agent.get("season") or agent.get("inferred_season"),
            "agent_confidence": agent.get("confidence"),
            "agent_matched_eid": (agent.get("matched_entity") or {}).get("external_id") if isinstance(agent.get("matched_entity"), dict) else None,
        }
        rows.append(row)

    try:
        import pyarrow as pa
        import pyarrow.parquet as pq

        table = pa.Table.from_pylist(rows)
        GROUND_TRUTH_DIR.mkdir(parents=True, exist_ok=True)
        path = GROUND_TRUTH_DIR / f"ground_truth_{dataset_name}.parquet"
        pq.write_table(table, path)
        return path
    except ImportError:
        raise ImportError(
            "pyarrow is required for Parquet export. Install with: uv add pyarrow"
        )


# ── Helpers ─────────────────────────────────────────────────────────────


def _json_path(name: str) -> Path:
    return GROUND_TRUTH_DIR / f"ground_truth_{name}.json"


def _public_entry_id(source_feed: str, raw_title: str) -> str:
    """Return the browser/title id used by /load-titles."""
    return hashlib.sha256(f"{source_feed}:{raw_title}".encode()).hexdigest()[:16]


def _db_entry_id(dataset_name: str, entry: dict[str, Any]) -> str:
    """Return a dataset-scoped DB primary key.

    The browser id is title-scoped, so reusing it as the DB primary key makes
    the same title collide across datasets. Keep the public id stable for the
    UI and scope the persisted row id by dataset.
    """
    public_id = entry.get("id") or _public_entry_id(
        entry.get("source_feed", ""),
        entry.get("raw_title", ""),
    )
    return hashlib.sha256(f"{dataset_name}:{public_id}".encode()).hexdigest()[:36]


def _infer_data_source_type(
    name: str,
    resource_metadata: dict[str, Any] | None = None,
    agent_result: dict[str, Any] | None = None,
) -> str:
    """Infer the eval target data source type for old datasets."""
    lowered = (name or "").lower()
    for source_type in DATA_SOURCE_TYPES:
        if lowered.startswith(f"{source_type}-") or lowered.endswith(f"-{source_type}"):
            return source_type
    for payload in (resource_metadata, agent_result):
        if isinstance(payload, dict):
            explicit = payload.get("eval_data_source_type")
            if explicit in DATA_SOURCE_TYPES:
                return explicit
    return "combined"


def normalize_data_source_type(value: str | None) -> str:
    """Return a supported data source type, defaulting to Exa Agent search."""
    normalized = (value or DEFAULT_DATA_SOURCE_TYPE).strip().lower()
    return normalized if normalized in DATA_SOURCE_TYPES else DEFAULT_DATA_SOURCE_TYPE


def new_dataset(name: str, data_source_type: str = DEFAULT_DATA_SOURCE_TYPE) -> dict:
    """Factory that returns a minimal empty GroundTruth dataset dict."""
    source_type = normalize_data_source_type(data_source_type)
    return {
        "version": DATASET_VERSION,
        "name": name,
        "data_source_type": source_type,
        "created_at": datetime.now(UTC).isoformat(),
        "total_entries": 0,
        "entries": [],
    }
