"""Tests for the genre unification layer: registry, clamp, write-back, DSL, payload."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import get_args
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from app.models.download_task import DownloadTask
from app.models.file_resource import FileResource
from app.models.series import TVSeries
from app.schemas.genre import GenreName
from app.schemas.series import TVSeriesCreate
from app.services import metadata_service as ms
from app.services.filter_engine import evaluate_field_condition, validate_filter_config
from app.services.genre_registry import (
    GENRE_NAMES,
    TMDB_ID_TO_NAME,
    genre_prompt_block,
    genre_zh,
    normalize_genres,
)
from app.services.metadata_agent import _clamp_finalize_genre
from app.services.notify_service import build_payload


def _uuid() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# normalize_genres
# ---------------------------------------------------------------------------


def test_normalize_tmdb_ids():
    assert normalize_genres([16, 35]) == ["Animation", "Comedy"]


def test_normalize_case_insensitive_names():
    assert normalize_genres(["animation", "SCIENCE FICTION"]) == [
        "Animation",
        "Science Fiction",
    ]


def test_normalize_aliases():
    assert normalize_genres(["Sci-Fi", "anime"]) == ["Science Fiction", "Animation"]


def test_normalize_drops_unknown():
    assert normalize_genres(["Action", "Isekai", "bogus"]) == ["Action"]


def test_normalize_dedupes_preserving_order():
    assert normalize_genres(["Drama", "drama", 18]) == ["Drama"]


def test_normalize_edge_inputs():
    assert normalize_genres(None) == []
    assert normalize_genres([]) == []
    assert normalize_genres("Comedy") == ["Comedy"]
    assert normalize_genres([None, "", True, {}]) == []


def test_registry_integrity():
    assert len(GENRE_NAMES) == 27
    assert len(set(GENRE_NAMES)) == 27
    assert set(TMDB_ID_TO_NAME.values()) == set(GENRE_NAMES)
    assert genre_zh("Animation") == "动画"
    assert genre_zh("Nope") is None


def test_prompt_block_lists_all_genres():
    block = genre_prompt_block()
    for name in GENRE_NAMES:
        assert f'"{name}"' in block


def test_schema_literal_matches_registry():
    assert set(get_args(GenreName)) == set(GENRE_NAMES)


def test_create_schema_rejects_non_canonical_genre():
    with pytest.raises(ValidationError):
        TVSeriesCreate(title_en="X", genre=["Isekai"])
    ok = TVSeriesCreate(title_en="X", genre=["Animation", "Drama"])
    assert ok.genre == ["Animation", "Drama"]


# ---------------------------------------------------------------------------
# _clamp_finalize_genre (metadata_agent exit gate)
# ---------------------------------------------------------------------------


def test_clamp_finalize_genre():
    fd = {"found": True, "matched_entity": {"genre": ["Anime", "Drama", "bogus", 16]}}
    _clamp_finalize_genre(fd)
    assert fd["matched_entity"]["genre"] == ["Animation", "Drama"]


def test_clamp_finalize_genre_empty_becomes_none():
    fd = {"found": True, "matched_entity": {"genre": ["bogus"]}}
    _clamp_finalize_genre(fd)
    assert fd["matched_entity"]["genre"] is None


def test_clamp_finalize_genre_no_entity():
    fd = {"found": False}
    _clamp_finalize_genre(fd)  # no-op, no crash
    assert "matched_entity" not in fd


# ---------------------------------------------------------------------------
# Write-back normalization (metadata_service)
# ---------------------------------------------------------------------------


async def test_write_back_normalizes_genre_on_create(db_session):
    data = {
        "content_type": "tv",
        "title_en": "Genre Show",
        "external_id": "tmdb:999001",
        "external_source": "tmdb",
        "poster_url": None,
        "genre": ["Anime", "Isekai", "drama"],
    }
    with patch(
        "app.services.metadata_service.download_and_cache_poster",
        new_callable=AsyncMock,
        return_value=None,
    ):
        s = await ms.create_or_update_series_from_external(db_session, data)
    await db_session.flush()
    assert s.genre == ["Animation", "Drama"]


async def test_write_back_empty_genre_does_not_wipe_existing(db_session):
    s = TVSeries(
        id=_uuid(), title_en="Kept Show", genre=["Comedy"], content_type="tv",
        external_id="tmdb:999002", external_source="tmdb",
    )
    db_session.add(s)
    await db_session.flush()
    data = {
        "content_type": "tv",
        "title_en": "Kept Show",
        "external_id": "tmdb:999002",
        "external_source": "tmdb",
        "poster_url": None,
        "genre": ["totally-unknown-genre"],  # normalizes to [] → treated as absent
    }
    with patch(
        "app.services.metadata_service.download_and_cache_poster",
        new_callable=AsyncMock,
        return_value=None,
    ):
        s2 = await ms.create_or_update_series_from_external(db_session, data)
    await db_session.flush()
    assert s2.id == s.id
    assert s2.genre == ["Comedy"]


# ---------------------------------------------------------------------------
# Filter DSL: series.genre / movie.genre
# ---------------------------------------------------------------------------


def _res_with_series(genre):
    return SimpleNamespace(series=SimpleNamespace(genre=genre, collection=None))


def test_dsl_series_genre_contains():
    cond = {"field": "series.genre", "operator": "contains", "value": "Animation"}
    assert evaluate_field_condition(cond, _res_with_series(["Animation", "Drama"])) is True
    assert evaluate_field_condition(cond, _res_with_series(["Comedy"])) is False
    # case-insensitive
    cond2 = {"field": "series.genre", "operator": "contains", "value": "animation"}
    assert evaluate_field_condition(cond2, _res_with_series(["Animation"])) is True


def test_dsl_series_genre_in_and_empty_semantics():
    cond = {"field": "series.genre", "operator": "in", "value": ["Horror", "Drama"]}
    assert evaluate_field_condition(cond, _res_with_series(["Drama"])) is True
    assert evaluate_field_condition(cond, _res_with_series(["Comedy"])) is False
    # no linked series → null semantics: positive ops fail, is_empty passes
    res = SimpleNamespace()
    assert evaluate_field_condition(cond, res) is False
    cond_empty = {"field": "series.genre", "operator": "is_empty"}
    assert evaluate_field_condition(cond_empty, res) is True
    assert evaluate_field_condition(cond_empty, _res_with_series([])) is True


def test_dsl_genre_fields_validate():
    cfg = {
        "combinator": "and",
        "conditions": [{"field": "movie.genre", "operator": "contains", "value": "Drama"}],
    }
    assert validate_filter_config(cfg) == []
    bad = {
        "combinator": "and",
        "conditions": [{"field": "movie.genre", "operator": "regex", "value": "x"}],
    }
    assert validate_filter_config(bad) != []


# ---------------------------------------------------------------------------
# Notification payload carries normalized genre
# ---------------------------------------------------------------------------


def test_build_payload_includes_normalized_genre():
    series = TVSeries(
        id=_uuid(), title_en="Notify Show", content_type="tv",
        genre=["Anime", "Drama"], seasons=None, episodes=[],
    )
    resource = FileResource(
        id=_uuid(), channel_id=_uuid(), guid=_uuid(),
        title_raw="raw", torrent_url="magnet:?xt=urn:btih:x",
    )
    resource.series = series
    task = DownloadTask(
        id=_uuid(), file_resource_id=resource.id, downloader_id=_uuid(),
        download_dir="/downloads", status="completed",
    )
    payload = build_payload(_uuid(), None, task, resource, None)
    assert payload["work"]["type"] == "series"
    assert payload["work"]["genre"] == ["Animation", "Drama"]


def test_build_payload_genre_defaults_empty_for_unlinked():
    task = DownloadTask(
        id=_uuid(), file_resource_id=None, downloader_id=_uuid(),
        download_dir="/downloads", status="completed",
    )
    payload = build_payload(_uuid(), None, task, None, None)
    assert payload["work"] == {"type": None}


# ---------------------------------------------------------------------------
# Synopsis-based genre inference fallback
# ---------------------------------------------------------------------------


def test_parse_genre_array():
    from app.services.metadata_agent import _parse_genre_array

    assert _parse_genre_array('["Drama", "Thriller"]') == ["Drama", "Thriller"]
    assert _parse_genre_array('sure! ["Anime", "bogus", "comedy"] ok') == [
        "Animation",
        "Comedy",
    ]
    assert _parse_genre_array("no array here") == []
    assert _parse_genre_array("[not json]") == []


def test_genre_inference_prompt_lists_enum_and_rule():
    from app.services.genre_registry import genre_inference_system_prompt

    p = genre_inference_system_prompt()
    for name in GENRE_NAMES:
        assert f'"{name}"' in p
    assert "JSON array" in p


def test_prompt_block_requires_best_effort():
    block = genre_prompt_block()
    assert "INFER" in block
    assert "best-effort" in block


async def test_ensure_genre_infers_from_synopsis():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.services.metadata_agent import UnifiedMetadataAgent as MetadataAgent

    agent = MetadataAgent.__new__(MetadataAgent)
    agent._model = SimpleNamespace(
        ainvoke=AsyncMock(return_value=SimpleNamespace(content='["Science Fiction", "Horror"]'))
    )
    fd = {
        "matched_entity": {
            "title_cn": "弗兰肯斯坦",
            "genre": None,
            "description": "A mad scientist reanimates a creature.",
        }
    }
    await agent._ensure_genre(fd)
    assert fd["matched_entity"]["genre"] == ["Science Fiction", "Horror"]


async def test_ensure_genre_skips_when_genre_present_or_no_desc():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.services.metadata_agent import UnifiedMetadataAgent as MetadataAgent

    agent = MetadataAgent.__new__(MetadataAgent)
    agent._model = SimpleNamespace(ainvoke=AsyncMock())
    fd = {"matched_entity": {"genre": ["Drama"], "description": "x"}}
    await agent._ensure_genre(fd)
    assert fd["matched_entity"]["genre"] == ["Drama"]
    agent._model.ainvoke.assert_not_called()

    fd2 = {"matched_entity": {"genre": None, "description": ""}}
    await agent._ensure_genre(fd2)
    assert fd2["matched_entity"].get("genre") is None
    agent._model.ainvoke.assert_not_called()


async def test_ensure_genre_tolerates_llm_failure():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.services.metadata_agent import UnifiedMetadataAgent as MetadataAgent

    agent = MetadataAgent.__new__(MetadataAgent)
    agent._model = SimpleNamespace(ainvoke=AsyncMock(side_effect=RuntimeError("llm down")))
    fd = {"matched_entity": {"genre": None, "description": "something"}}
    await agent._ensure_genre(fd)  # must not raise
    assert fd["matched_entity"].get("genre") is None
