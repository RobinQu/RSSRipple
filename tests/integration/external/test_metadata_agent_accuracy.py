"""MetadataAgent accuracy test against GroundTruth dataset.

Compares the agent's process_title_only() output for each labeled title
against the human-verified ground truth. Runs as part of the integration
test suite.

Data sources (tried in order):
1. Parquet file: ``tests/data/ground_truth_v1.parquet`` (preferred)
2. JSON file: ``tests/data/ground_truth_v1.json`` (legacy)
3. DB: queries ``ground_truth_entries`` table for dataset "v1"

All three sources are skipped with a clear message if not available.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

_DATA_DIR = Path(__file__).parents[2] / "data"  # tests/integration/external/ -> tests/data
_DATASET_NAME = "v1"
_PARQUET_PATH = _DATA_DIR / f"ground_truth_{_DATASET_NAME}.parquet"
_JSON_PATH = _DATA_DIR / f"ground_truth_{_DATASET_NAME}.json"


# ── Data loading (tries Parquet → JSON → DB) ────────────────────────────


def _load_entries() -> list[dict[str, Any]]:
    """Load ground truth entries from the best available source."""
    # 1. Parquet (preferred — flattened, typed, fast)
    if _PARQUET_PATH.exists():
        try:
            import pyarrow.parquet as pq

            table = pq.read_table(_PARQUET_PATH)
            entries: list[dict[str, Any]] = []
            for i in range(table.num_rows):
                row: dict[str, Any] = {}
                for col in table.column_names:
                    val = table.column(col)[i].as_py()
                    row[col] = val
                entries.append(row)
            if entries:
                return entries
        except Exception as e:
            print(f"[accuracy_test] Parquet load failed: {e}")

    # 2. JSON (legacy format)
    if _JSON_PATH.exists():
        with open(_JSON_PATH) as f:
            data = json.load(f)
        entries = data.get("entries", [])
        if entries:
            return entries

    # 3. DB (only in integration test environment)
    try:
        import asyncio

        from sqlalchemy import select

        from app.database import async_session_factory
        from app.models.ground_truth import GroundTruthEntry

        async def _load():
            async with async_session_factory() as db:
                result = await db.execute(
                    select(GroundTruthEntry)
                    .where(GroundTruthEntry.dataset_name == _DATASET_NAME)
                )
                rows = result.scalars().all()
                entries = []
                for row in rows:
                    gt = row.ground_truth_json or {}
                    entries.append({
                        "raw_title": row.raw_title,
                        "source_feed": row.source_feed,
                        "resource_metadata": gt,
                        "agent_result": row.agent_result_json,
                        "review_status": row.review_status,
                    })
                return entries

        return asyncio.run(_load())
    except Exception as e:
        print(f"[accuracy_test] DB load failed: {e}")

    pytest.skip(
        f"GroundTruth dataset '{_DATASET_NAME}' not found. "
        f"Expected at {_PARQUET_PATH}, {_JSON_PATH}, or in DB."
    )
    return []


# ── Helper: extract ground truth dict from entry ─────────────────────────


def _get_gt(entry: dict) -> dict | None:
    """Extract ground truth from the entry, handling various key names."""
    # Parquet format uses flat columns (gt_clean_title, etc.)
    if "gt_clean_title" in entry:
        return {
            "clean_title": entry.get("gt_clean_title"),
            "content_type": entry.get("gt_content_type"),
            "episode": entry.get("gt_episode"),
            "season": entry.get("gt_season"),
            "title_cn": entry.get("gt_title_cn"),
            "title_en": entry.get("gt_title_en"),
            "matched_entity": {
                "external_id": entry.get("gt_matched_eid"),
            } if entry.get("gt_matched_eid") else None,
        }
    # JSON/DB format uses nested resource_metadata
    return entry.get("resource_metadata") or entry.get("ground_truth")


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def ground_truth_entries() -> list[dict[str, Any]]:
    return _load_entries()


@pytest.fixture(scope="module")
async def agent():
    if not os.environ.get("LLM_API_KEY"):
        pytest.skip("LLM_API_KEY not set; metadata accuracy test requires the LLM")
    from app.services.metadata_agent import get_agent

    return get_agent()


# ── Per-entry field accuracy ────────────────────────────────────────────


class TestMetadataAgentAccuracy:
    """Per-entry field-level accuracy checks."""

    @pytest.mark.parametrize(
        "entry",
        _load_entries(),
        ids=lambda e: e.get("raw_title", "")[:60] if e.get("raw_title") else "unknown",
    )
    async def test_per_entry_core_fields(self, agent, entry):
        """Core fields (clean_title, content_type) must match ground truth."""
        gt = _get_gt(entry)
        if not gt:
            pytest.skip("Entry has no ground truth data")

        raw_title = entry["raw_title"]
        result = await agent.process_title_only(raw_title)

        assert result.clean_title == gt.get("clean_title", ""), (
            f"clean_title mismatch for '{raw_title[:60]}': "
            f"agent='{result.clean_title}', gt='{gt.get('clean_title', '')}'"
        )
        assert result.content_type == gt.get("content_type", "tv"), (
            f"content_type mismatch for '{raw_title[:40]}': "
            f"agent={result.content_type}, gt={gt.get('content_type')}"
        )

    @pytest.mark.parametrize(
        "entry",
        [e for e in _load_entries()
         if (_get_gt(e) or {}).get("episode") is not None],
        ids=lambda e: e.get("raw_title", "")[:60] if e.get("raw_title") else "unknown",
    )
    async def test_episode_extraction(self, agent, entry):
        """Episode numbers must match for entries where GT has an episode value."""
        gt = _get_gt(entry)
        result = await agent.process_title_only(entry["raw_title"])
        assert result.episode == gt["episode"], (
            f"episode mismatch: agent={result.episode}, gt={gt['episode']}"
        )

    @pytest.mark.parametrize(
        "entry",
        [e for e in _load_entries()
         if (_get_gt(e) or {}).get("season") is not None],
        ids=lambda e: e.get("raw_title", "")[:60] if e.get("raw_title") else "unknown",
    )
    async def test_season_extraction(self, agent, entry):
        """Season numbers must match for entries where GT has a season value."""
        gt = _get_gt(entry)
        result = await agent.process_title_only(entry["raw_title"])
        assert result.season == gt["season"], (
            f"season mismatch: agent={result.season}, gt={gt['season']}"
        )


# ── Aggregate accuracy ──────────────────────────────────────────────────


class TestMetadataAgentAggregateAccuracy:
    """Aggregate accuracy benchmarks."""

    @pytest.mark.asyncio
    async def test_overall_accuracy_above_80_percent(self, agent, ground_truth_entries):
        """At least 80% of labeled entries must have correct clean_title + content_type."""
        passed = 0
        failed: list[dict[str, str]] = []
        total = len(ground_truth_entries)

        for entry in ground_truth_entries:
            gt = _get_gt(entry)
            if not gt:
                total -= 1
                continue
            result = await agent.process_title_only(entry["raw_title"])
            if (result.clean_title == gt.get("clean_title", "")
                    and result.content_type == gt.get("content_type", "tv")):
                passed += 1
            else:
                failed.append({
                    "raw": entry.get("raw_title", "")[:80],
                    "agent_clean": result.clean_title,
                    "gt_clean": str(gt.get("clean_title", "")),
                    "agent_type": result.content_type,
                    "gt_type": str(gt.get("content_type", "")),
                })

        accuracy = passed / total if total > 0 else 1.0

        if failed:
            print(f"\nFailed entries ({len(failed)}/{total}):")
            for f_item in failed[:10]:
                print(f"  {f_item['raw']}")
                print(f"    agent: {f_item['agent_clean']} ({f_item['agent_type']})")
                print(f"    gt:    {f_item['gt_clean']} ({f_item['gt_type']})")

        print(f"\nAccuracy: {accuracy:.1%} ({passed}/{total})")
        assert accuracy >= 0.80, (
            f"Overall accuracy {accuracy:.1%} below 80% threshold"
        )

    @pytest.mark.asyncio
    async def test_confident_predictions_more_accurate(self, agent, ground_truth_entries):
        """High-confidence predictions (>=0.9) should be >=90% accurate."""
        high_conf: list[tuple[dict, Any]] = []
        for entry in ground_truth_entries:
            result = await agent.process_title_only(entry["raw_title"])
            if result.confidence >= 0.9:
                high_conf.append((entry, result))

        if len(high_conf) < 5:
            pytest.skip("Not enough high-confidence predictions to measure")

        correct = 0
        for entry, result in high_conf:
            gt = _get_gt(entry)
            if not gt:
                continue
            if (result.clean_title == gt.get("clean_title", "")
                    and result.content_type == gt.get("content_type", "tv")):
                correct += 1

        high_conf_acc = correct / len(high_conf)
        print(f"\nHigh-confidence accuracy (>=0.9): {high_conf_acc:.1%} "
              f"({correct}/{len(high_conf)})")
        assert high_conf_acc >= 0.90, (
            f"High-confidence accuracy {high_conf_acc:.1%} below 90% threshold"
        )
