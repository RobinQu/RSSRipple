"""Tests for metadata_source_io: the thin TMDB I/O wrappers.

All external boundaries (metadata_search_agent functions, httpx) are mocked.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from app.services.metadata_source_io import (
    _execute_find_tmdb_movie_by_imdb,
    _execute_get_tmdb_details,
    _execute_search_tmdb,
)

_SEARCH_MOD = "app.services.metadata_search_agent"


# ---------------------------------------------------------------------------
# search_tmdb
# ---------------------------------------------------------------------------


async def test_search_tmdb_success():
    results = [{"tmdb_id": 1, "title": "Show"}]
    with patch(f"{_SEARCH_MOD}._search_tmdb", new_callable=AsyncMock, return_value=results) as m:
        out = await _execute_search_tmdb("some query")
    assert out == {"success": True, "data": results}
    m.assert_awaited_once_with("some query")


async def test_search_tmdb_failure_returns_error_dict():
    with patch(f"{_SEARCH_MOD}._search_tmdb", new_callable=AsyncMock, side_effect=RuntimeError("boom")):
        out = await _execute_search_tmdb("q")
    assert out["success"] is False
    assert out["data"] == []
    assert "boom" in out["error"]


# ---------------------------------------------------------------------------
# get_tmdb_details
# ---------------------------------------------------------------------------


def _httpx_client_mock(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = payload
    client = MagicMock()
    client.get = AsyncMock(return_value=resp)
    client_cls = MagicMock(return_value=client)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client_cls


async def test_get_tmdb_details_requires_api_key():
    with patch.dict("app.services.runtime_config._overrides", {"tmdb_api_key": ""}):
        out = await _execute_get_tmdb_details("42", "tv")
    assert out["success"] is False
    assert "not configured" in out["error"]


async def test_get_tmdb_details_tv_success():
    payload = {
        "id": 42,
        "name": "剧集",
        "original_name": "Show",
        "overview": "ov",
        "poster_path": "/p.jpg",
        "vote_average": 8.1,
        "genres": [{"id": 18, "name": "Drama"}],
        "status": "Returning Series",
        "number_of_episodes": 24,
        "number_of_seasons": 2,
        "first_air_date": "2020-01-01",
        "last_air_date": "2021-01-01",
        "seasons": [
            {"season_number": 0, "episode_count": 2, "name": "Specials"},
            {"season_number": 1, "episode_count": 12, "name": "Season 1"},
        ],
    }
    with (
        patch.dict("app.services.runtime_config._overrides", {"tmdb_api_key": "k"}),
        patch("httpx.AsyncClient", _httpx_client_mock(payload)),
        patch(f"{_SEARCH_MOD}._tmdb_image_base", return_value="https://img/"),
        patch(f"{_SEARCH_MOD}._resolve_genre_ids", return_value=["Drama"]),
    ):
        out = await _execute_get_tmdb_details("42", "tv")

    assert out["success"] is True
    data = out["data"]
    assert data["tmdb_id"] == 42
    assert data["title_cn"] == "剧集"
    assert data["title_en"] == "Show"
    assert data["poster_url"] == "https://img/w500/p.jpg"
    assert data["genre"] == ["Drama"]
    assert data["number_of_episodes"] == 24
    # Season 0 (specials) is filtered out
    assert data["seasons"] == [{"season_number": 1, "episode_count": 12, "name": "Season 1"}]


async def test_get_tmdb_details_movie_success():
    payload = {
        "id": 7,
        "title": "电影",
        "original_title": "Film",
        "release_date": "2019-05-01",
        "runtime": 120,
        "genres": [],
    }
    with (
        patch.dict("app.services.runtime_config._overrides", {"tmdb_api_key": "k"}),
        patch("httpx.AsyncClient", _httpx_client_mock(payload)),
        patch(f"{_SEARCH_MOD}._tmdb_image_base", return_value="https://img/"),
        patch(f"{_SEARCH_MOD}._resolve_genre_ids", return_value=[]),
    ):
        out = await _execute_get_tmdb_details("7", "movie")

    assert out["success"] is True
    data = out["data"]
    assert data["release_date"] == "2019-05-01"
    assert data["runtime"] == 120
    assert data["poster_url"] is None  # no poster_path in payload
    assert "seasons" not in data


async def test_get_tmdb_details_http_error():
    client = MagicMock()
    client.get = AsyncMock(side_effect=RuntimeError("connection refused"))
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    with (
        patch.dict("app.services.runtime_config._overrides", {"tmdb_api_key": "k"}),
        patch("httpx.AsyncClient", MagicMock(return_value=client)),
    ):
        out = await _execute_get_tmdb_details("42", "tv")
    assert out["success"] is False
    assert "connection refused" in out["error"]


async def test_find_tmdb_movie_by_imdb_uses_exact_identity_lookup():
    payload = {"movie_results": [{"id": 1370424, "title": "He's Watching You"}]}
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = payload
    client = MagicMock()
    client.get = AsyncMock(return_value=resp)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    with (
        patch.dict("app.services.runtime_config._overrides", {"tmdb_api_key": "k"}),
        patch("httpx.AsyncClient", MagicMock(return_value=client)),
    ):
        out = await _execute_find_tmdb_movie_by_imdb("imdb:tt26315134")

    assert out == {"success": True, "data": payload["movie_results"]}
    client.get.assert_awaited_once_with(
        "https://api.themoviedb.org/3/find/tt26315134",
        params={"api_key": "k", "external_source": "imdb_id"},
    )


async def test_find_tmdb_movie_by_imdb_returns_empty_results():
    with (
        patch.dict("app.services.runtime_config._overrides", {"tmdb_api_key": "k"}),
        patch("httpx.AsyncClient", _httpx_client_mock({"movie_results": []})),
    ):
        out = await _execute_find_tmdb_movie_by_imdb("tt0000000")
    assert out == {"success": True, "data": []}


async def test_find_tmdb_movie_by_imdb_rejects_invalid_id_and_missing_key():
    with patch.dict("app.services.runtime_config._overrides", {"tmdb_api_key": "k"}):
        invalid = await _execute_find_tmdb_movie_by_imdb("Black Spines")
    with patch.dict("app.services.runtime_config._overrides", {"tmdb_api_key": ""}):
        missing_key = await _execute_find_tmdb_movie_by_imdb("tt26315134")
    assert invalid["success"] is False
    assert "Invalid" in invalid["error"]
    assert missing_key["success"] is False
    assert "not configured" in missing_key["error"]


async def test_find_tmdb_movie_by_imdb_sanitizes_http_errors():
    client = MagicMock()
    client.get = AsyncMock(side_effect=RuntimeError("connection refused"))
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    with (
        patch.dict("app.services.runtime_config._overrides", {"tmdb_api_key": "k"}),
        patch("httpx.AsyncClient", MagicMock(return_value=client)),
    ):
        out = await _execute_find_tmdb_movie_by_imdb("tt26315134")
    assert out == {"success": False, "data": [], "error": "RuntimeError"}
