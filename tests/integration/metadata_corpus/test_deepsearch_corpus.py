"""Real frozen DeepSearch observations in the DEFAULT integration collection.

This tests evidence contracts, including known failures, not live model quality
or automatic semantic acceptance. The reviewed production graph gate remains
in test_scenarios.py. No docs mount or external service is needed.
"""
import hashlib
import json
import socket
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.metadata_web_fallback import web_fallback_judge
from scripts.metadata_deepsearch_eval import validate_claims
from tests.metadata_corpus.dataset import load_corpus, read_json, write_json

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures/deepsearch_corpus_v1.json.gz"
DATA = read_json(FIXTURE)
OBSERVATIONS = [(case, run) for case in DATA["cases"] for run in case["runs"]]


@pytest.fixture(scope="session", autouse=True)
def deepsearch_inventory_report(corpus_report_dir):
    write_json(corpus_report_dir / "deepsearch-audit.json", {
        "semantic_gold": False, "live_model_evaluated": False,
        "case_count": len(DATA["cases"]), "recorded_answers": len(OBSERVATIONS),
        "known_contract_invalid_answers": sum(bool(r["expected_contract_issues"]) for _, r in OBSERVATIONS),
        "note": "Inventory only; consult JUnit for test outcomes. Known bad observations are negative regressions.",
    })


@pytest.fixture(autouse=True)
def no_external_connections(monkeypatch):
    def reject(*args, **kwargs):
        raise AssertionError("DeepSearch corpus must not connect to an external service")
    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr(socket.socket, "connect_ex", reject)


def test_frozen_inventory_is_complete_and_not_semantic_gold():
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == (
        "ab6b3eed382c1b16387f962ac2f414b2e5d74999443a3ebcc6da8f6dcca2b1e0")
    assert DATA["semantic_gold"] is False
    assert len(DATA["cases"]) == 8
    assert len({c["id"] for c in DATA["cases"]}) == 8
    assert len(OBSERVATIONS) == 48
    assert sum(len(run["answer"]["fields"]) for _, run in OBSERVATIONS) == 84
    corpus_ids = {case["id"] for case in load_corpus()[1]["cases"]}
    assert {c["resource_id"] for c in DATA["cases"]} <= corpus_ids
    for case in DATA["cases"]:
        assert {(r["arm"], r["iteration"]) for r in case["runs"]} == {
            (arm, i) for arm in ("snippets", "pages") for i in (1, 2, 3)}


@pytest.mark.parametrize("case,run", OBSERVATIONS,
                         ids=[f"{c['id']}-{r['arm']}-{r['iteration']}" for c, r in OBSERVATIONS])
def test_real_recorded_answer_contract(case, run):
    payload = case["payloads"][run["arm"]]
    assert set(run["answer"]["fields"]) == set(payload["fields"])
    assert validate_claims(run["answer"], payload) == run["expected_contract_issues"]


async def test_real_anilist_identity_survives_production_fallback_boundary():
    baseline = DATA["legitimate_fallback"]
    entity = baseline["matched_entity"]
    model = AsyncMock()
    model.ainvoke.return_value = SimpleNamespace(content=json.dumps({"found": True, "matched_entity": entity}))
    result, info = await web_fallback_judge(model, baseline["clean_title"],
        web_searcher=AsyncMock(return_value=[{"url": entity["url"], "title": entity["title_en"]}]))
    assert info["error"] is None
    assert result["matched_entity"]["external_id"] == "anilist:204269"


async def test_real_identity_cannot_be_replaced_by_unshown_source():
    baseline = DATA["legitimate_fallback"]
    entity = baseline["matched_entity"]
    model = AsyncMock()
    model.ainvoke.return_value = SimpleNamespace(content=json.dumps({"found": True, "matched_entity": entity}))
    result, info = await web_fallback_judge(model, baseline["clean_title"],
        web_searcher=AsyncMock(return_value=[{"url": "https://bangumi.tv/subject/273886"}]))
    assert not result["found"]
    assert "ungrounded identity" in info["error"]
