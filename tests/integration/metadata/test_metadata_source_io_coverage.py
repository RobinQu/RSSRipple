"""In-process coverage for metadata_source_io (TMDB I/O primitives).

The docker HTTP suite only reaches these wrappers incidentally; these tests
mock the external boundaries (``metadata_search_agent`` helpers and
``httpx.AsyncClient``) and cover:

- ``_execute_search_tmdb`` success/failure envelopes.
- ``_execute_get_tmdb_details``: missing API key, tv payload shaping
  (season-0 filtering, genre-id resolution, deterministic is_anime), movie
  payload shaping (non-dict genres skipped), and the HTTP error envelope.
- ``fetch_tmdb_episode_list``: configuration/argument guards, season
  normalization, the >30-season fan-out cap, per-season failure tolerance,
  episode filtering, and the all-failed → None outcome.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from app.services.metadata_source_io import (
    _execute_get_tmdb_details,
    _execute_search_tmdb,
    fetch_tmdb_episode_list,
)

_SEARCH_MOD = "app.services.metadata_search_agent"
_OVERRIDES = "app.services.runtime_config._overrides"


def _client_cls(payload=None, *, get_side_effect=None) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = payload or {}
    client = MagicMock()
    client.get = AsyncMock(return_value=resp, side_effect=get_side_effect)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=client)


# ---------------------------------------------------------------------------
# _execute_search_tmdb
# ---------------------------------------------------------------------------


class TestSearchTmdb:
    async def test_success_envelope(self):
        results = [{"tmdb_id": 1, "title": "Show"}]
        with patch(
            f"{_SEARCH_MOD}._search_tmdb", new_callable=AsyncMock, return_value=results
        ):
            out = await _execute_search_tmdb("q")
        assert out == {"success": True, "data": results}

    async def test_failure_envelope(self):
        with patch(
            f"{_SEARCH_MOD}._search_tmdb",
            new_callable=AsyncMock,
            side_effect=RuntimeError("upstream down"),
        ):
            out = await _execute_search_tmdb("q")
        assert out["success"] is False
        assert out["data"] == []
        assert "upstream down" in out["error"]


# ---------------------------------------------------------------------------
# _execute_get_tmdb_details
# ---------------------------------------------------------------------------


class TestGetTmdbDetails:
    async def test_missing_api_key_fails_fast(self):
        with patch.dict(_OVERRIDES, {"tmdb_api_key": ""}):
            out = await _execute_get_tmdb_details("42", "tv")
        assert out == {
            "success": False, "data": {}, "error": "TMDB API key not configured"
        }

    async def test_tv_payload_shaping(self):
        payload = {
            "id": 42,
            "name": "动画剧集",
            "original_name": "Anime Show",
            "overview": "ov",
            "poster_path": "/p.jpg",
            "vote_average": 8.1,
            "genres": [{"id": 16, "name": "Animation"}, {"name": "no id"}],
            "status": "Ended",
            "original_language": "ja",
            "origin_country": ["JP"],
            "number_of_episodes": 12,
            "number_of_seasons": 1,
            "first_air_date": "2020-01-01",
            "last_air_date": "2020-03-01",
            "seasons": [
                {"season_number": 0, "episode_count": 2, "name": "Specials"},
                {"season_number": 1, "episode_count": 12, "name": "Season 1"},
            ],
        }
        with (
            patch.dict(_OVERRIDES, {"tmdb_api_key": "k"}),
            patch("httpx.AsyncClient", _client_cls(payload)),
            patch(f"{_SEARCH_MOD}._tmdb_image_base", return_value="https://img/"),
            patch(f"{_SEARCH_MOD}._resolve_genre_ids", return_value=["Animation"]),
        ):
            out = await _execute_get_tmdb_details("42", "tv")

        assert out["success"] is True
        data = out["data"]
        assert data["tmdb_id"] == 42
        assert data["media_type"] == "tv"
        assert data["title_cn"] == "动画剧集"
        assert data["poster_url"] == "https://img/w500/p.jpg"
        assert data["genre"] == ["Animation"]
        # Animation + Japanese → deterministic True
        assert data["is_anime"] is True
        # Season 0 (specials) filtered out of the season summaries
        assert data["seasons"] == [
            {"season_number": 1, "episode_count": 12, "name": "Season 1"}
        ]
        assert data["first_air_date"] == "2020-01-01"

    async def test_movie_payload_with_non_dict_genres_skips_resolution(self):
        payload = {
            "id": 7,
            "title": "实拍电影",
            "original_title": "Live Action Film",
            "release_date": "2019-05-01",
            "runtime": 118,
            # Some endpoints/best-effort payloads carry bare ids; only dict
            # genre entries are resolved.
            "genres": [28],
            "original_language": "en",
            "origin_country": ["US"],
        }
        with (
            patch.dict(_OVERRIDES, {"tmdb_api_key": "k"}),
            patch("httpx.AsyncClient", _client_cls(payload)),
            patch(f"{_SEARCH_MOD}._tmdb_image_base", return_value="https://img/"),
            patch(f"{_SEARCH_MOD}._resolve_genre_ids", return_value=[]) as resolve,
        ):
            out = await _execute_get_tmdb_details("7", "movie")

        assert out["success"] is True
        data = out["data"]
        resolve.assert_not_called()
        assert data["genre"] == []
        assert data["is_anime"] is None  # no usable genre data → unknown
        assert data["poster_url"] is None
        assert data["release_date"] == "2019-05-01"
        assert data["runtime"] == 118
        assert "seasons" not in data

    async def test_http_error_returns_error_envelope(self):
        client = MagicMock()
        client.get = AsyncMock(side_effect=RuntimeError("connection refused"))
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        with (
            patch.dict(_OVERRIDES, {"tmdb_api_key": "k"}),
            patch("httpx.AsyncClient", MagicMock(return_value=client)),
        ):
            out = await _execute_get_tmdb_details("42", "tv")
        assert out["success"] is False
        assert out["data"] == {}
        assert "connection refused" in out["error"]


# ---------------------------------------------------------------------------
# fetch_tmdb_episode_list
# ---------------------------------------------------------------------------


class TestFetchTmdbEpisodeList:
    async def test_missing_api_key_returns_none(self):
        with patch.dict(_OVERRIDES, {"tmdb_api_key": ""}):
            assert await fetch_tmdb_episode_list(42, [{"season_number": 1}]) is None

    async def test_non_numeric_tmdb_id_returns_none(self):
        with patch.dict(_OVERRIDES, {"tmdb_api_key": "k"}):
            assert await fetch_tmdb_episode_list("abc", [{"season_number": 1}]) is None

    async def test_no_usable_season_numbers_returns_none(self):
        with patch.dict(_OVERRIDES, {"tmdb_api_key": "k"}):
            # season 0 (specials) is out of the main numbering; junk entries
            # are skipped.
            seasons = [
                {"season_number": 0},
                {"season_number": "x"},
                "not-a-dict",
                {"name": "no number"},
            ]
            assert await fetch_tmdb_episode_list(42, seasons) is None
            assert await fetch_tmdb_episode_list(42, None) is None

    async def test_over_max_seasons_returns_none(self):
        seasons = [{"season_number": n} for n in range(1, 32)]
        with patch.dict(_OVERRIDES, {"tmdb_api_key": "k"}):
            assert await fetch_tmdb_episode_list(42, seasons) is None

    async def test_fetches_per_season_and_filters_episodes(self):
        def _get(url, params=None):
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            if url.endswith("/season/1"):
                resp.json.return_value = {
                    "episodes": [
                        {"episode_number": 1, "name": "E1", "air_date": "2020-01-04"},
                        # Non-int / out-of-range episode numbers are dropped.
                        {"episode_number": "x", "name": "bad"},
                        {"episode_number": 0, "name": "zero"},
                        {"episode_number": 2, "name": "E2", "air_date": ""},
                    ]
                }
            else:  # season 2: per-season failure is tolerated
                raise RuntimeError("season fetch boom")
            return resp

        with (
            patch.dict(_OVERRIDES, {"tmdb_api_key": "k"}),
            patch("httpx.AsyncClient", _client_cls(get_side_effect=_get)),
        ):
            episodes = await fetch_tmdb_episode_list(
                "tv-42", [{"season_number": 2}, {"season_number": 1}]
            )

        assert episodes == [
            {"season": 1, "episode": 1, "title": "E1", "air_date": "2020-01-04"},
            {"season": 1, "episode": 2, "title": "E2", "air_date": None},
        ]

    async def test_all_seasons_failed_returns_none(self):
        def _get(url, params=None):
            raise RuntimeError("boom")

        with (
            patch.dict(_OVERRIDES, {"tmdb_api_key": "k"}),
            patch("httpx.AsyncClient", _client_cls(get_side_effect=_get)),
        ):
            assert await fetch_tmdb_episode_list(42, [{"season_number": 1}]) is None

    async def test_non_dict_season_payload_is_skipped(self):
        def _get(url, params=None):
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json.return_value = ["unexpected", "list"]
            return resp

        with (
            patch.dict(_OVERRIDES, {"tmdb_api_key": "k"}),
            patch("httpx.AsyncClient", _client_cls(get_side_effect=_get)),
        ):
            assert await fetch_tmdb_episode_list(42, [{"season_number": 1}]) is None
