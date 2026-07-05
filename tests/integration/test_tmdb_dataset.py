"""TMDB title test dataset — verifies that known CBC anime titles match their TMDB IDs.

These tests require TMDB_API_KEY. Skip if not configured.
"""

from __future__ import annotations

import os

import pytest

# ---------------------------------------------------------------------------
# Curated dataset: 17 anime titles from TMDB CBC channel
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


@pytest.mark.skipif(not os.environ.get("TMDB_API_KEY"), reason="TMDB_API_KEY not set")
@pytest.mark.asyncio
class TestTMDBDataset:
    @pytest.mark.parametrize("title,expected_id,expected_ct", TMDB_CBC_DATASET)
    async def test_tmdb_matches_cbc_title(self, title, expected_id, expected_ct):
        from app.services.metadata_search_agent import _cache, _search_tmdb

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
        from app.services.metadata_search_agent import _cache, _search_tmdb

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
