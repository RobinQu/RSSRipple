"""Offline contract tests for the experimental runner, never invoke live search."""

import socket

import pytest

from scripts.metadata_deepsearch_eval import evidence_input, parse_answer, public_url, reuse_artifact, validate_claims
from tests.metadata_corpus.dataset import write_json


def test_resume_requires_matching_provenance(tmp_path):
    path = tmp_path / "result.json"
    expected = {"case_id": "test", "prompt_sha256": "current"}
    assert not reuse_artifact(path, expected)
    write_json(path, expected)
    assert reuse_artifact(path, expected)
    with pytest.raises(ValueError, match="Cannot resume"):
        reuse_artifact(path, {**expected, "prompt_sha256": "changed"})


@pytest.mark.parametrize("saved", [{}, [], {"case_id": "test", "error_type": "APIConnectionError"}])
def test_resume_rejects_legacy_or_failed_artifacts(tmp_path, saved):
    path = tmp_path / "result.json"
    write_json(path, saved)
    with pytest.raises(ValueError, match="Cannot resume"):
        reuse_artifact(path, {"case_id": "test"})


def payload(field="special_episode_count"):
    return {"fields": [field], "sources": [{"url": "https://example.org/a", "text": "one OAD episode"}]}


def answer(value=1, quote="one OAD episode", field="special_episode_count"):
    return {"fields": {field: {"status": "resolved", "value": value,
            "evidence": [{"url": "https://example.org/a", "quote": quote}]}}}


def test_quote_presence_is_necessary_but_not_entailment():
    assert validate_claims(answer(), payload()) == []
    assert "special_episode_count:invalid_quote" in validate_claims(answer(quote="invented"), payload())
    # Deliberately documents the remaining limitation: semantic review is required.
    assert validate_claims(answer(value=22), payload()) == []


@pytest.mark.parametrize("value", [True, -1, 0, "1", None])
def test_episode_count_requires_positive_integer(value):
    assert "special_episode_count:invalid_value_type" in validate_claims(answer(value=value), payload())


def test_dates_and_abstentions_are_not_guessed():
    assert "special_release_date:invalid_value_type" in validate_claims(
        answer(value="2011-02-30", field="special_release_date"), payload("special_release_date"))
    assert validate_claims({"fields": {"special_episode_count": {
        "status": "unresolved", "value": None, "evidence": []}}}, payload()) == []
    assert validate_claims([], payload()) == ["invalid_answer_shape"]


def test_candidates_and_gold_are_never_given_to_model():
    case = {"title": "Show", "scope": "OAD", "fields": ["special_episode_count"],
            "snapshot_candidate_not_gold": {"answer": 999}}
    evidence = {"hits": [{"url": "https://example.org", "title": "Show", "snippet": "short"}],
                "pages": [{"url": "https://example.org", "text": "long"}]}
    snippets = evidence_input(case, evidence, "snippets")
    pages = evidence_input(case, evidence, "pages")
    assert "999" not in str(pages)
    assert snippets["sources"][0]["text"] == "short"
    assert "long" in pages["sources"][0]["text"]


@pytest.mark.parametrize("url", ["file:///etc/passwd", "http://user:pass@example.org", "http://example.org:1234"])
def test_reader_rejects_unsafe_urls_before_network(url):
    with pytest.raises(ValueError):
        public_url(url)


def test_reader_rejects_private_dns_resolution(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))])
    with pytest.raises(ValueError, match="non-public"):
        public_url("http://example.org")


def test_parse_requires_real_json():
    assert parse_answer('```json\n{"fields": {}}\n```') == {"fields": {}}
    with pytest.raises(ValueError):
        parse_answer("")
