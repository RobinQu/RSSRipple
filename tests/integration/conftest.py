"""Pytest fixtures for integration tests."""

import os

import httpx
import pytest

TEST_SERVER_URL = os.environ.get("TEST_SERVER_URL", "http://test-server:8080")
RSSRIPPLE_URL = os.environ.get("RSSRIPPLE_URL", "http://app:9001")


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
    return httpx.Client(timeout=30.0)


@pytest.fixture(scope="session", autouse=True)
def setup_test_environment(test_server, http_client):
    """Set up the test environment before all tests.

    Creates and seeds all test torrents.
    """
    resp = http_client.post(f"{test_server}/api/setup/full")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"]
    return data["data"]
