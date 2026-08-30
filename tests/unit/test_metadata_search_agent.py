"""Unit tests for the remaining TMDB metadata-search helpers."""

from app.services.metadata_search_agent import (
    _cache_get,
    _cache_key,
    _cache_set,
    _fmt_date,
    _parse_year,
    _tmdb_poster_url,
    _validate_candidate,
)


def test_validate_candidate_accepts_supported_work_types():
    assert _validate_candidate({"content_type": "tv", "title_en": "Breaking Bad"})
    assert _validate_candidate({"content_type": "movie", "title_cn": "盗梦空间"})


def test_validate_candidate_requires_title_and_supported_content_type():
    assert not _validate_candidate({"content_type": "tv"})
    assert not _validate_candidate({"title_en": "Show"})
    assert not _validate_candidate({"title_en": "Show", "content_type": "person"})


def test_cache_round_trip_and_normalized_key():
    _cache_set("tmdb", "Breaking Bad", [{"title_en": "Breaking Bad"}])
    assert _cache_get("tmdb", "breaking bad") == [{"title_en": "Breaking Bad"}]
    assert _cache_key("tmdb", " Breaking Bad ") == _cache_key("tmdb", "breaking bad")


def test_parse_year():
    assert _parse_year("2008") == 2008
    assert _parse_year("2008-01-20") == 2008
    assert _parse_year(2008) == 2008
    assert _parse_year(None) is None
    assert _parse_year("") is None


def test_fmt_date():
    assert _fmt_date("2008-01-20") == "2008-01-20"
    assert _fmt_date("2008") == "2008-01-01"
    assert _fmt_date(None) is None


def test_tmdb_poster_url():
    base = "https://image.tmdb.org/t/p/"
    assert _tmdb_poster_url("/abc.jpg", base) == "https://image.tmdb.org/t/p/w500/abc.jpg"
    assert _tmdb_poster_url(None) is None
    assert _tmdb_poster_url("") is None
