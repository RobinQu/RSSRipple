"""Real local HTTP → production TMDB adapter → evidence validator.

TMDB protocol responses and model selections are synthetic. No external API
or real credentials are used. Persistence has its own three-source matrix.
"""

import json
import socket
import subprocess
import sys
import time

import httpx
import pytest
from langchain_core.messages import ToolMessage

from app.services import metadata_search_agent
from app.services.metadata_identity_evidence import ground_tmdb_identity
from app.services.metadata_source_io import _execute_search_tmdb
from tests.integration.server.source_redirect import redirect_tmdb


@pytest.fixture(scope="module")
def tmdb_http():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        command = (
            "from fastapi import FastAPI; import uvicorn; "
            "from tests.integration.server.mock_tmdb import router; "
            "app=FastAPI(); app.include_router(router); "
            f"uvicorn.run(app, fd={listener.fileno()}, log_level='error')"
        )
        process = subprocess.Popen([sys.executable, "-c", command], pass_fds=(listener.fileno(),))
        base = f"http://127.0.0.1:{port}/tmdb/3"
        try:
            for _ in range(100):
                assert process.poll() is None, "source fixture server exited"
                try:
                    if httpx.get(base + "/configuration", timeout=0.2).status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                time.sleep(0.05)
            else:
                pytest.fail("source fixture did not become healthy")
            yield base
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "identity,kind,expected",
    [
        ("tmdb:900001", "tv", True),
        ("tmdb:900003", "movie", True),
        ("tmdb:999999", "tv", False),
        ("tmdb:900001", "movie", False),
        ("mock-exa-daemons-drama-cd", "drama_cd", False),
    ],
)
async def test_http_identity_evidence(tmdb_http, monkeypatch, identity, kind, expected):
    monkeypatch.setattr(metadata_search_agent, "TMDB_BASE", tmdb_http)
    monkeypatch.setattr(metadata_search_agent, "_TMDB_GENRE_MAP", None)
    monkeypatch.setattr("app.services.runtime_config._overrides", {"tmdb_api_key": "mock-tmdb"})
    monkeypatch.setattr(metadata_search_agent, "_cache_get", lambda *a: None)
    monkeypatch.setattr(metadata_search_agent, "_cache_set", lambda *a: None)
    payload = await _execute_search_tmdb("mock search query")
    assert payload["success"] is True
    assert {row["external_id"] for row in payload["data"]} == {"tmdb:900001", "tmdb:900002", "tmdb:900003"}
    result = ground_tmdb_identity(
        {"found": True, "content_type": kind, "matched_entity": {"external_id": identity}},
        [ToolMessage(name="search_tmdb", tool_call_id="source", content=json.dumps(payload))],
    )
    assert result["found"] is expected


def test_redirect_preserves_query_and_other_hosts():
    request = httpx.Request("GET", "https://api.themoviedb.org/3/search/multi?query=x&api_key=mock")
    redirect_tmdb(request)
    assert str(request.url) == "http://test-server:8080/tmdb/3/search/multi?query=x&api_key=mock"
    assert request.headers["host"] == "test-server:8080"
    other = httpx.Request("GET", "https://example.org/test")
    redirect_tmdb(other)
    assert str(other.url) == "https://example.org/test"


def test_conflicting_source_type_is_rejected():
    result = ground_tmdb_identity(
        {"found": True, "content_type": "tv", "matched_entity": {"external_id": "tmdb:900001"}},
        [
            ToolMessage(
                name="search_tmdb",
                tool_call_id="source",
                content=json.dumps(
                    {
                        "success": True,
                        "data": [{"external_id": "tmdb:900001", "media_type": "tv", "content_type": "movie"}],
                    }
                ),
            )
        ],
    )
    assert result["found"] is False
