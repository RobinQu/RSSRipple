"""Additional tests for app.clients.rss_parser covering download URL extraction
and date/content-length helpers."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.clients import rss_parser


class _Entry(SimpleNamespace):
    def keys(self):
        return list(self.__dict__.keys())

    def get(self, key, default=None):
        return getattr(self, key, default)

    def __getitem__(self, key):
        return getattr(self, key)


def test_extract_download_urls_torrent_enclosure():
    entry = _Entry(
        enclosures=[{"url": "https://x.torrent", "type": "application/x-bittorrent"}],
        link="https://example.com/page",
        description="",
    )
    t, m = rss_parser._extract_download_urls(entry)
    assert t == "https://x.torrent"
    assert m is None


def test_extract_download_urls_magnet_enclosure_fallback_to_generic_enclosure():
    entry = _Entry(
        enclosures=[{"url": "magnet:?xt=urn:btih:abc", "type": ""}],
        link="",
        description=""
    )
    t, m = rss_parser._extract_download_urls(entry)
    assert m == "magnet:?xt=urn:btih:abc"
    assert t == "magnet:?xt=urn:btih:abc"  # falls back to magnet


def test_extract_download_urls_magnet_in_description():
    entry = _Entry(
        enclosures=[],
        link="",
        description="download <a href=\"magnet:?xt=urn:btih:deadbeef\">here</a>"
    )
    t, m = rss_parser._extract_download_urls(entry)
    assert m and "deadbeef" in m
    assert t == m  # magnet used as torrent_url fallback


def test_extract_download_urls_generic_enclosure_used_as_torrent_url():
    entry = _Entry(
        enclosures=[{"url": "https://x/random", "type": "application/octet-stream"}],
        link="",
        description=""
    )
    t, _ = rss_parser._extract_download_urls(entry)
    assert t == "https://x/random"


def test_extract_content_length_from_enclosure():
    entry = _Entry(enclosures=[{"length": "1234"}], torrent_contentlength=None)
    assert rss_parser._extract_content_length(entry) == 1234


def test_extract_content_length_from_mikan_namespace():
    entry = _Entry(enclosures=[], torrent_contentlength="9999")
    assert rss_parser._extract_content_length(entry) == 9999


def test_extract_content_length_invalid():
    entry = _Entry(enclosures=[{"length": "abc"}], torrent_contentlength=None)
    assert rss_parser._extract_content_length(entry) is None


def test_extract_published_at_mikan():
    entry = _Entry(
        torrent_pubdate="2024-05-01T12:00:00",
        published_parsed=(2024, 1, 1, 0, 0, 0, 0, 0, 0),
    )
    dt = rss_parser._extract_published_at(entry)
    assert dt.year == 2024 and dt.month == 5


def test_extract_published_at_falls_back_to_published_parsed():
    entry = _Entry(torrent_pubdate=None, published_parsed=(2024, 3, 15, 10, 30, 0, 0, 0, 0))
    dt = rss_parser._extract_published_at(entry)
    assert dt == datetime(2024, 3, 15, 10, 30, 0)


def test_extract_published_at_invalid_mikan():
    entry = _Entry(torrent_pubdate="not-a-date", published_parsed=None)
    assert rss_parser._extract_published_at(entry) is None


def test_entry_to_dict_handles_lists_and_scalars():
    entry = _Entry(
        title="t", link="https://x", id="g",
        tags=[{"term": "a"}, {"term": "b"}],
        num=42,
        fl=1.5,
        b=True,
        n=None,
    )
    out = rss_parser._entry_to_dict(entry)
    assert out["title"] == "t"
    assert out["tags"] == [{"term": "a"}, {"term": "b"}]
    assert out["num"] == 42


@pytest.mark.asyncio
async def test_get_raw_entries_converts_to_dicts():
    fake_feed = MagicMock()
    fake_feed.entries = [
        _Entry(title="t1", link="https://x", id="g1")
    ]
    with patch("app.clients.rss_parser._parse_feed_sync", return_value=fake_feed):
        entries = await rss_parser.get_raw_entries("https://example.com/rss", limit=5)
    assert len(entries) == 1
    assert entries[0]["title"] == "t1"


@pytest.mark.asyncio
async def test_validate_rss_url_network_error():
    with patch("app.clients.rss_parser._parse_feed_sync", side_effect=Exception("DNS fail")):
        ok, msg, n, dl = await rss_parser.validate_rss_url("https://bad/rss")
    assert ok is False
    assert "Cannot reach" in msg


@pytest.mark.asyncio
async def test_validate_rss_url_empty_feed():
    feed = MagicMock()
    feed.bozo = False
    feed.entries = []
    with patch("app.clients.rss_parser._parse_feed_sync", return_value=feed):
        ok, msg, n, dl = await rss_parser.validate_rss_url("https://empty/rss")
    assert ok is False
    assert n == 0


@pytest.mark.asyncio
async def test_validate_rss_url_counts_downloadable():
    entry_magnet = _Entry(
        enclosures=[], link="magnet:?xt=urn:btih:a", description=""
    )
    entry_plain = _Entry(enclosures=[], link="https://example.com", description="")
    feed = MagicMock()
    feed.bozo = False
    feed.entries = [entry_magnet, entry_plain]
    with patch("app.clients.rss_parser._parse_feed_sync", return_value=feed):
        ok, msg, n, dl = await rss_parser.validate_rss_url("https://ok/rss")
    assert ok is True
    assert n == 2
    assert dl == 1
