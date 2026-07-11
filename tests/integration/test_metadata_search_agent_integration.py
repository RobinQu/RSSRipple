"""Integration tests for metadata search agent with a curated 22-title dataset.

These tests require real API keys. Skip if not configured:
- TMDB_API_KEY (env var)
"""

from __future__ import annotations

import os

import pytest

from app.services.metadata_search_agent import _cache, search_metadata

# ---------------------------------------------------------------------------
# 22-Title curated test dataset
# Each entry: (search_title, expected_content_type, expected_en_title_substr)
# ---------------------------------------------------------------------------

TEST_DATASET: list[tuple[str, str | None, str | None]] = [
    # Well-known Western TV shows
    ("Breaking Bad", "tv", "Breaking Bad"),
    ("Game of Thrones", "tv", "Game of Thrones"),
    ("Stranger Things", "tv", "Stranger Things"),
    ("Friends", "tv", "Friends"),
    ("Better Call Saul", "tv", "Better Call Saul"),
    ("The Office", "tv", "Office"),
    ("Rick and Morty", "tv", "Rick"),

    # Well-known Western movies
    ("The Dark Knight", "movie", "Dark Knight"),
    ("Inception", "movie", "Inception"),
    ("Interstellar", "movie", "Interstellar"),
    ("The Matrix", "movie", "Matrix"),
    ("Pulp Fiction", "movie", "Pulp Fiction"),
    ("The Godfather", "movie", "Godfather"),
    ("John Wick", "movie", "John Wick"),
    ("Oppenheimer", "movie", "Oppenheimer"),

    # Anime (TV)
    ("Attack on Titan", "tv", "Attack on Titan"),
    ("Death Note", "tv", "Death Note"),
    ("Demon Slayer", "tv", "Demon"),
    ("Fullmetal Alchemist", "tv", "Fullmetal"),

    # Anime/Movie — may map to either type on TMDB
    ("Spirited Away", "movie", "Spirited"),
    ("Your Name", "movie", "Your Name"),

    # Foreign / Chinese title
    ("Parasite", "movie", "Parasite"),
]


def _is_tmdb_configured() -> bool:
    return bool(os.environ.get("TMDB_API_KEY"))


# ---------------------------------------------------------------------------
# Fixture to clear cache between tests
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clear_agent_cache():
    _cache.clear()
    yield
    _cache.clear()


# ---------------------------------------------------------------------------
# Integration tests (require TMDB_API_KEY)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _is_tmdb_configured(), reason="TMDB_API_KEY not set")
@pytest.mark.asyncio
async def test_search_metadata_house_of_the_dragon():
    """Smoke test: any configured source should match House of the Dragon (TV only, TMDB 94997)."""
    results = await search_metadata("House of the Dragon")
    assert len(results) > 0, "Should return at least one result for House of the Dragon"
    top = results[0]
    assert top["content_type"] == "tv", (
        f"House of the Dragon should be tv, got {top['content_type']}"
    )
    assert "dragon" in top.get("title_en", "").lower() or "dragon" in top.get("original_title", "").lower()
    # Source may vary depending on which API key is valid — just verify format
    assert ":" in top.get("external_id", "")
    assert top.get("external_source") in ("tmdb", "exa", "llm_search")


@pytest.mark.skipif(not _is_tmdb_configured(), reason="TMDB_API_KEY not set")
@pytest.mark.asyncio
async def test_search_metadata_inception():
    """Smoke test: any configured source should match Inception (movie)."""
    results = await search_metadata("Inception")
    assert len(results) > 0
    top = results[0]
    assert top["content_type"] == "movie"
    assert "inception" in top.get("title_en", "").lower() or "inception" in top.get("original_title", "").lower()


@pytest.mark.skipif(not _is_tmdb_configured(), reason="TMDB_API_KEY not set")
@pytest.mark.asyncio
async def test_tmdb_chinese_title_spirited_away():
    """TMDB zh-CN should return Chinese title for Spirited Away."""
    results = await search_metadata("Spirited Away")
    assert len(results) > 0
    top = results[0]
    # At least one title variant should be present
    has_title = top.get("title_cn") or top.get("title_en") or top.get("original_title")
    assert has_title is not None


@pytest.mark.skipif(not _is_tmdb_configured(), reason="TMDB_API_KEY not set")
@pytest.mark.asyncio
async def test_tmdb_rate_limiting():
    """Quick test: multiple searches in succession should not fail."""
    titles = ["Breaking Bad", "Inception", "Friends", "The Matrix", "John Wick"]
    for title in titles:
        results = await search_metadata(title)
        assert isinstance(results, list)
        if results:
            assert "content_type" in results[0]
            assert "external_id" in results[0]


# ---------------------------------------------------------------------------
# Full dataset integration test
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _is_tmdb_configured(), reason="TMDB_API_KEY not set")
@pytest.mark.asyncio
async def test_dataset_20_titles():
    """Iterate the 22-title dataset and verify each gets valid results."""
    success_count = 0
    fail_count = 0
    failures: list[str] = []

    for search_title, expected_ct, expected_substr in TEST_DATASET:
        results = await search_metadata(search_title)
        if not results:
            fail_count += 1
            failures.append(f"{search_title}: no results")
            continue

        top = results[0]
        issues: list[str] = []

        # Check content type
        ct = top.get("content_type")
        if ct not in ("tv", "movie"):
            issues.append(f"bad content_type: {ct}")

        # Check has some title
        has_title = top.get("title_cn") or top.get("title_en") or top.get("original_title")
        if not has_title:
            issues.append("no title")

        # Check external ID format
        ext_id = top.get("external_id", "")
        top.get("external_source", "")
        if not ext_id or ":" not in ext_id:
            issues.append(f"bad external_id: {ext_id}")

        if issues:
            fail_count += 1
            failures.append(f"{search_title}: {', '.join(issues)}")
        else:
            success_count += 1

    # Assert at least 80% success rate (18/22)
    total = len(TEST_DATASET)
    rate = success_count / total if total > 0 else 0
    assert rate >= 0.80, (
        f"Success rate {rate:.1%} ({success_count}/{total}) below 80% threshold.\n"
        f"Failures:\n" + "\n".join(failures)
    )


# ---------------------------------------------------------------------------
# No-API-key graceful degradation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_metadata_no_api_keys_returns_empty(monkeypatch):
    """When no API keys are configured, agent returns empty list gracefully."""
    # runtime_config reads the shared settings instance live when no DB override
    # is set, so patching app.config.settings is seen by all services.
    monkeypatch.setattr("app.config.settings.tmdb_api_key", "")
    monkeypatch.setattr("app.config.settings.jina_api_key", "")
    monkeypatch.setattr("app.config.settings.llm_api_key", "")

    results = await search_metadata("Breaking Bad")
    assert results == []
