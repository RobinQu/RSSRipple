"""Pytest fixtures for HTTP-level integration tests (tests/integration/http/).

These fixtures target the dockerized RSSRipple app + test-server stack and
apply ONLY to tests under tests/integration/http/. The session-scoped autouse
``setup_test_environment`` seeds the test-server before any HTTP test runs.

Direct-Python tests under tests/integration/external/ and the eval app tests
under tests/integration/eval/ do NOT inherit ``setup_test_environment``, so
they can run without the docker test-server stack (they only need the
relevant API keys: LLM_API_KEY / TMDB_API_KEY).
"""

import os

import httpx
import pytest

from tests.integration.http._http import API_HEADERS

TEST_SERVER_URL = os.environ.get("TEST_SERVER_URL", "http://test-server:8080")
RSSRIPPLE_URL = os.environ.get("RSSRIPPLE_URL", "http://app:9001")


@pytest.fixture(scope="session", autouse=True)
def _inject_api_key_header():
    """Merge ``X-API-Key`` into top-level ``httpx.<method>()`` calls.

    Several test modules bypass the shared client factory and call
    ``httpx.get``/``post``/... directly; wrapping the module-level functions
    here keeps the auth header centralized instead of touching every call
    site. Callers that construct their own ``httpx.Client`` must pass
    ``API_HEADERS`` themselves (see ``_http._client``).
    """
    originals = {}
    for name in ("get", "post", "put", "delete", "patch", "head", "options", "stream"):
        orig = getattr(httpx, name, None)
        if orig is None:
            continue
        originals[name] = orig

        def _wrapped(*args, _orig=orig, **kwargs):
            headers = dict(API_HEADERS)
            headers.update(kwargs.get("headers") or {})
            kwargs["headers"] = headers
            return _orig(*args, **kwargs)

        setattr(httpx, name, _wrapped)
    yield
    for name, orig in originals.items():
        setattr(httpx, name, orig)


@pytest.fixture(scope="session")
def test_server():
    """Base URL for the test server."""
    return TEST_SERVER_URL


@pytest.fixture(scope="session")
def rssripple_url():
    """Base URL for RSSRipple app."""
    return RSSRIPPLE_URL


@pytest.fixture(scope="session")
def http_client():
    """Shared HTTP client for tests."""
    return httpx.Client(timeout=30.0, headers=API_HEADERS)


@pytest.fixture(scope="session", autouse=True)
def setup_test_environment(test_server, http_client):
    """Set up the test environment before all HTTP tests.

    Creates and seeds all test torrents on the test-server.
    """
    resp = http_client.post(f"{test_server}/api/setup/full")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"]
    return data["data"]
