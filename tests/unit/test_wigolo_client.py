"""Tests for wigolo_client: request shaping, auth header, and error mapping.

No network: httpx transport is mocked via respx-style monkeypatching of the
AsyncClient post call.
"""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.services.wigolo_client import WigoloSearchError, web_search


def _resp(status_code: int = 200, payload: dict | None = None, text: str = "") -> httpx.Response:
    if payload is None:
        return httpx.Response(status_code, text=text, request=httpx.Request("POST", "http://x/v1/search"))
    return httpx.Response(
        status_code,
        json=payload,
        request=httpx.Request("POST", "http://x/v1/search"),
    )


class _StubClient:
    def __init__(self, response: httpx.Response):
        self.response = response
        self.post = AsyncMock(return_value=self.response)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


_OK_PAYLOAD = {
    "results": [
        {"title": "T1", "url": "https://bangumi.tv/subject/42", "snippet": "s1"},
        {"title": "", "url": "", "snippet": None},
    ],
    "engines_used": ["bing"],
    "total_time_ms": 900,
}


async def test_web_search_parses_results_and_sends_defaults():
    client = _StubClient(_resp(payload=_OK_PAYLOAD))
    with (
        patch.dict("app.services.runtime_config._overrides", {"wigolo_base_url": "http://wigo:3333"}),
        patch("app.services.wigolo_client.httpx.AsyncClient", return_value=client),
    ):
        hits = await web_search("query", num_results=5)
    assert [h["title"] for h in hits] == ["T1", ""]
    body = client.post.await_args.kwargs["json"]
    assert body["query"] == "query"
    assert body["max_results"] == 8  # over-fetch floor
    assert body["search_depth"] == "balanced"
    assert "include_domains" not in body


async def test_web_search_over_fetch_is_clamped_at_20():
    client = _StubClient(_resp(payload=_OK_PAYLOAD))
    with (
        patch.dict("app.services.runtime_config._overrides", {"wigolo_base_url": "http://wigo:3333"}),
        patch("app.services.wigolo_client.httpx.AsyncClient", return_value=client),
    ):
        await web_search("q", num_results=50)
    assert client.post.await_args.kwargs["json"]["max_results"] == 20


async def test_web_search_carries_domains_and_token():
    client = _StubClient(_resp(payload=_OK_PAYLOAD))
    with (
        patch.dict(
            "app.services.runtime_config._overrides",
            {
                "wigolo_base_url": "http://wigo:3333",
                "wigolo_api_token": "sekrit",
            },
        ),
        patch("app.services.wigolo_client.httpx.AsyncClient", return_value=client),
    ):
        await web_search("q", include_domains=["bangumi.tv"])
    headers = client.post.await_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer sekrit"
    assert client.post.await_args.kwargs["json"]["include_domains"] == ["bangumi.tv"]


async def test_web_search_requires_base_url():
    with patch.dict("app.services.runtime_config._overrides", {"wigolo_base_url": ""}):
        with pytest.raises(WigoloSearchError, match="not configured"):
            await web_search("q")


async def test_web_search_http_error_maps_to_wigolo_error():
    client = _StubClient(_resp(401, text='{"error":"unauthorized"}'))
    with (
        patch.dict("app.services.runtime_config._overrides", {"wigolo_base_url": "http://wigo:3333"}),
        patch("app.services.wigolo_client.httpx.AsyncClient", return_value=client),
    ):
        with pytest.raises(WigoloSearchError, match="401"):
            await web_search("q")


async def test_web_search_transport_error_maps_to_wigolo_error():
    client = _StubClient(_resp())
    client.post.side_effect = httpx.ConnectError("no route")
    with (
        patch.dict("app.services.runtime_config._overrides", {"wigolo_base_url": "http://wigo:3333"}),
        patch("app.services.wigolo_client.httpx.AsyncClient", return_value=client),
    ):
        with pytest.raises(WigoloSearchError, match="ConnectError"):
            await web_search("q")


async def test_web_search_in_body_error_raises():
    payload = {**_OK_PAYLOAD, "error": "all engines failed"}
    client = _StubClient(_resp(payload=payload))
    with (
        patch.dict("app.services.runtime_config._overrides", {"wigolo_base_url": "http://wigo:3333"}),
        patch("app.services.wigolo_client.httpx.AsyncClient", return_value=client),
    ):
        with pytest.raises(WigoloSearchError, match="all engines failed"):
            await web_search("q")
