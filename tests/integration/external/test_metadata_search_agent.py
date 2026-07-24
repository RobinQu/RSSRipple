"""Integration tests for the metadata search agent.

Combines two previously separate files:
- Multi-source ``search_metadata()`` against a curated 22-title
  Western/anime/movie dataset (any source: TMDB / Exa / LLM).
- TMDB-only ``_search_tmdb()`` accuracy against 17 CBC anime titles
  (exact TMDB ID match).

Requires ``TMDB_API_KEY`` (and optionally ``LLM_API_KEY`` / Exa for the
multi-source path). Tests skip gracefully when keys are not configured.
"""

from __future__ import annotations

import os

import pytest

from app.services.metadata_search_agent import _cache, _search_tmdb, search_metadata

# ---------------------------------------------------------------------------
# 22-title curated multi-source dataset
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

    # Anime/Movie - may map to either type on TMDB
    ("Spirited Away", "movie", "Spirited"),
    ("Your Name", "movie", "Your Name"),

    # Foreign / Chinese title
    ("Parasite", "movie", "Parasite"),
]

# ---------------------------------------------------------------------------
# 17 CBC anime titles (TMDB-only, exact ID match)
# https://www.themoviedb.org/network/201-cbc
# Each entry: (search_title, expected_tmdb_id, content_type)
# ---------------------------------------------------------------------------

TMDB_CBC_DATASET: list[tuple[str, int, str]] = [
    ("Jujutsu Kaisen", 95479, "tv"),
    ("Fullmetal Alchemist Brotherhood", 31911, "tv"),
    ("Wistoria Wand and Sword", 245842, "tv"),
    ("Rent a Girlfriend", 96316, "tv"),
    ("Haikyu", 60863, "tv"),
    ("Mission Yozakura Family", 216467, "tv"),
    ("Dan Da Dan", 240411, "tv"),
    ("Blue Exorcist", 38464, "tv"),
    ("Mobile Suit Gundam The Witch from Mercury", 196400, "tv"),
    ("Wind Breaker", 223500, "tv"),
    ("Shangri-La Frontier", 205050, "tv"),
    ("Gachiakuta", 256721, "tv"),
    ("Infinite Stratos", 46025, "tv"),
    ("Blue Box", 207347, "tv"),
    ("My Hero Academia", 65930, "tv"),
    ("Zom 100 Bucket List of the Dead", 217766, "tv"),
    ("JUJUTSU KAISEN", 95479, "tv"),  # alternate casing
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
# Multi-source smoke + dataset tests (require TMDB_API_KEY)
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
    # Source may vary depending on which API key is valid - just verify format
    assert ":" in top.get("external_id", "")
    assert top.get("external_source") in ("tmdb", "exa", "llm_search")


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


@pytest.mark.skipif(not _is_tmdb_configured(), reason="TMDB_API_KEY not set")
@pytest.mark.asyncio
async def test_dataset_22_titles():
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


# ---------------------------------------------------------------------------
# TMDB-only accuracy (CBC anime, exact ID match)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not os.environ.get("TMDB_API_KEY"), reason="TMDB_API_KEY not set")
@pytest.mark.asyncio
class TestTMDBDataset:
    @pytest.mark.parametrize("title,expected_id,expected_ct", TMDB_CBC_DATASET)
    async def test_tmdb_matches_cbc_title(self, title, expected_id, expected_ct):
        _cache.clear()
        results = await _search_tmdb(title)
        assert len(results) > 0, f"No results for '{title}'"
        # Find exact ID match
        found = any(r["external_id"] == f"tmdb:{expected_id}" for r in results)
        assert found, (
            f"Expected tmdb:{expected_id} not found in results for '{title}'. "
            f"Got: {[r['external_id'] for r in results[:3]]}"
        )
        # Content type should match
        for r in results[:3]:
            if r["external_id"] == f"tmdb:{expected_id}":
                assert r["content_type"] == expected_ct

    async def test_dataset_success_rate(self):
        """At least 80% of titles should be found."""
        _cache.clear()
        success = 0
        failures: list[str] = []
        for title, expected_id, _ in TMDB_CBC_DATASET:
            results = await _search_tmdb(title)
            if any(r["external_id"] == f"tmdb:{expected_id}" for r in results):
                success += 1
            else:
                failures.append(title)
        rate = success / len(TMDB_CBC_DATASET)
        assert rate >= 0.80, (
            f"Success rate {rate:.1%} ({success}/{len(TMDB_CBC_DATASET)}) below 80%. "
            f"Failures: {failures}"
        )
