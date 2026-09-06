"""The corpus must detect corrupt assets, answer leakage and empty gates."""

import copy

import pytest

from tests.metadata_corpus.dataset import FIELDS, ROOT, audit, build_corpus, load_corpus, read_json, write_json
from tests.metadata_corpus.runner import compare, isolated_database


def test_export_has_no_production_answers_in_inputs(tmp_path):
    graph = {table: [] for table in FIELDS}
    graph["file_resources"] = [{"id": "r", "channel_id": "c", "title_raw": "Show S01E01", "series_id": "known"}]
    build_corpus(graph, tmp_path / "missing", tmp_path / "corpus")
    _, corpus, _ = load_corpus(tmp_path / "corpus")
    case = corpus["cases"][0]
    assert set(case["input"]) == {"kind", "title_raw", "raw_rss_available"}
    assert case["review"]["status"] == "pending"
    assert case["review"]["issues"] == ["torrent_missing"]
    with pytest.raises(ValueError, match="already contains"):
        build_corpus(graph, tmp_path, tmp_path / "corpus")


def test_corpus_checksums_and_all_resources_accounted_for():
    manifest, corpus, _ = load_corpus(ROOT)
    assert len(corpus["cases"]) == manifest["counts"]["file_resources"] > 0
    assert len({c["id"] for c in corpus["cases"]}) == len(corpus["cases"])
    # Audit is generated explicitly, tests never rewrite tracked evidence.
    report = read_json(ROOT / "audit.json")
    assert sum(report["review_status"].values()) == len(corpus["cases"])


def test_changed_candidate_snapshot_is_rejected(tmp_path):
    graph = {table: [] for table in FIELDS}
    build_corpus(graph, tmp_path, tmp_path / "corpus")
    write_json(tmp_path / "corpus/candidates.json.gz", {})
    with pytest.raises(ValueError, match="checksum"):
        load_corpus(tmp_path / "corpus")


def test_confirmed_requires_evidence_and_answer(tmp_path):
    graph = {table: [] for table in FIELDS}
    graph["file_resources"] = [{"id": "r", "channel_id": "c", "title_raw": "Show"}]
    build_corpus(graph, tmp_path, tmp_path / "corpus")
    write_json(tmp_path / "corpus/reviews.json", {"r": {"status": "confirmed"}})
    with pytest.raises(ValueError, match="lacks independent"):
        audit(tmp_path / "corpus")


def test_comparison_reports_wrong_season_and_missing_files():
    expected = {"works": ["series:x:s2"], "assignments": [{"file_path": "S02E01.mkv", "season": 2}]}
    actual = copy.deepcopy(expected)
    actual["works"] = ["series:x:s1"]
    actual["assignments"] = []
    assert {d["path"] for d in compare(expected, actual)} == {"/works", "/assignments"}


async def test_production_database_name_is_rejected():
    with pytest.raises(ValueError, match="metadata_corpus_test"):
        async with isolated_database("postgresql+asyncpg://localhost/rssripple"):
            pytest.fail("must reject before connecting")


def test_draft_with_scenarios_cannot_be_refreshed(tmp_path):
    graph = {table: [] for table in FIELDS}
    root = tmp_path / "corpus"
    build_corpus(graph, tmp_path, root)
    manifest = read_json(root / "manifest.json")
    manifest["scenarios"] = [{"id": "existing", "case_ids": ["old"]}]
    write_json(root / "manifest.json", manifest)
    with pytest.raises(ValueError, match="already contains"):
        build_corpus(graph, tmp_path, root, refresh_unreviewed=True)
