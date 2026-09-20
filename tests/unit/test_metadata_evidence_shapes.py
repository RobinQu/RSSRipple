"""Malformed external/model data must not crash identity verification."""

import json

import pytest
from langchain_core.messages import ToolMessage

from app.services.metadata_identity_evidence import ground_tmdb_identity, ground_wikipedia_identity


@pytest.mark.parametrize("media_type", [[], {}, ["tv"]])
def test_tmdb_non_scalar_media_type_is_rejected(media_type):
    message = ToolMessage(
        name="search_tmdb",
        tool_call_id="search",
        content=json.dumps({"success": True, "data": [{"tmdb_id": 123, "media_type": media_type}]}),
    )
    result = ground_tmdb_identity(
        {"found": True, "content_type": "tv", "matched_entity": {"external_id": "tmdb:123"}}, [message]
    )
    assert not result["found"] and result["matched_entity"] is None


@pytest.mark.parametrize("content_type", [[], {}, ["tv"]])
def test_tmdb_non_scalar_final_type_is_rejected(content_type):
    message = ToolMessage(
        name="search_tmdb",
        tool_call_id="search",
        content=json.dumps({"success": True, "data": [{"tmdb_id": 123, "media_type": "tv"}]}),
    )
    result = ground_tmdb_identity(
        {"found": True, "content_type": content_type, "matched_entity": {"external_id": "tmdb:123"}}, [message]
    )
    assert not result["found"]


def test_tmdb_unbounded_model_number_is_rejected_without_integer_conversion():
    result = ground_tmdb_identity(
        {"found": True, "content_type": "tv", "matched_entity": {"external_id": "tmdb:" + "9" * 5000}}, []
    )
    assert not result["found"]


@pytest.mark.parametrize("aliases,categories", [("invalid", 123), ([123], [None, {}, "Animated films"])])
def test_wikipedia_malformed_optional_enrichment_is_ignored(aliases, categories):
    result, _ = ground_wikipedia_identity(
        {"found": True, "matched_entity": {"external_id": "wikipedia:en:123"}},
        [{"lang": "en", "page_id": 123, "langlink_pageids": aliases, "categories": categories}],
    )
    assert result["found"] and result["matched_entity"]["alt_external_ids"] == []
    assert all(isinstance(category, str) for category in result["matched_entity"]["categories"])


def test_wikipedia_non_object_evidence_is_ignored():
    result, _ = ground_wikipedia_identity(
        {"found": True, "matched_entity": {"external_id": "wikipedia:en:123"}}, [None, "invalid", 123]
    )
    assert not result["found"]


def test_wikipedia_numeric_identity_is_canonical_and_duplicate_observation_is_not_ambiguous():
    result, _ = ground_wikipedia_identity(
        {"found": True, "matched_entity": {"external_id": "wikipedia:en:000123"}},
        [{"lang": "en", "page_id": 123}, {"lang": "en", "page_id": "00123"}],
    )
    assert result["found"] and result["matched_entity"]["external_id"] == "wikipedia:en:123"
