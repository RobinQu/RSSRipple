"""Tests for metadata_service: 4-layer matching, manual search/link, poster cache."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.channel import Channel
from app.models.channel_raw_title_mapping import ChannelRawTitleMapping
from app.models.file_resource import FileResource
from app.models.movie import Movie
from app.models.series import TVSeries
from app.services import metadata_service as ms


def _uuid() -> str:
    return str(uuid.uuid4())


TEST_FIELD_MAPPING = {
    "list_locator": {"source": "entries"},
    "field_mappings": {"torrent_url": {"source": "link"}},
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
async def channel(db_session):
    ch = Channel(
        id=_uuid(), name="ch", type="rss_feed", url="https://example.com/rss",
        field_mapping=TEST_FIELD_MAPPING,
        metadata_source="none", title_extraction_method="none",
    )
    db_session.add(ch)
    await db_session.flush()
    return ch


def _resource(channel_id, **overrides):
    base = dict(
        id=_uuid(), channel_id=channel_id, guid=_uuid(),
        title_raw="[G] Title - 01 [1080p]",
        torrent_url="magnet:?xt=urn:btih:abc",
        search_title="Title", title_cn=None, title_en=None,
        episode=1, parsed_at=datetime.now(UTC),
    )
    base.update(overrides)
    return FileResource(**base)


# ---------------------------------------------------------------------------
# extract_search_title
# ---------------------------------------------------------------------------


def test_extract_search_title_prefers_parsed_titles():
    r = SimpleNamespace(title_cn="中文名", title_en=None, title_raw="raw")
    assert ms.extract_search_title(r) == "中文名"


def test_extract_search_title_falls_back_to_parser():
    r = SimpleNamespace(
        title_cn=None, title_en=None,
        title_raw="[LoliHouse] 黄泉使者 / Yomi no Tsugai - 12 [1080p HEVC]",
    )
    title = ms.extract_search_title(r)
    assert "黄泉使者" in title


# ---------------------------------------------------------------------------
# Layer 2: ChannelRawTitleMapping
# ---------------------------------------------------------------------------


async def test_raw_title_mapping_links_series(db_session, channel):
    s = TVSeries(id=_uuid(), title_cn="剧", title_en="Series", content_type="tv")
    db_session.add(s)
    await db_session.flush()
    raw = "[G] Title - 01 [1080p]"
    mapping = ChannelRawTitleMapping(
        id=_uuid(), channel_id=channel.id, raw_title=raw,
        content_type="tv", series_id=s.id, movie_id=None,
    )
    db_session.add(mapping)
    await db_session.flush()
    res = _resource(channel.id, title_raw=raw, search_title="junk")
    db_session.add(res)
    await db_session.flush()
    await ms.fetch_and_link_metadata(db_session, res, channel)
    assert res.series_id == s.id
    assert res.metadata_matched_at is not None


async def test_raw_title_mapping_links_movie(db_session, channel):
    m = Movie(id=_uuid(), title_cn="电影", title_en="Movie", content_type="movie")
    db_session.add(m)
    await db_session.flush()
    raw = "[G] Movie 2024"
    mapping = ChannelRawTitleMapping(
        id=_uuid(), channel_id=channel.id, raw_title=raw,
        content_type="movie", movie_id=m.id,
        search_title_override="clean",
    )
    db_session.add(mapping)
    await db_session.flush()
    res = _resource(channel.id, title_raw=raw)
    db_session.add(res)
    await db_session.flush()
    await ms.fetch_and_link_metadata(db_session, res, channel)
    assert res.movie_id == m.id
    assert res.search_title == "clean"


# ---------------------------------------------------------------------------
# Layer 3: local exact / fuzzy match
# ---------------------------------------------------------------------------


async def test_local_exact_match_by_title_cn(db_session, channel):
    s = TVSeries(id=_uuid(), title_cn="标题", title_en=None, content_type="tv")
    db_session.add(s)
    await db_session.flush()
    res = _resource(channel.id, search_title="标题")
    db_session.add(res)
    await db_session.flush()
    await ms.fetch_and_link_metadata(db_session, res, channel)
    assert res.series_id == s.id


async def test_local_exact_match_by_title_en(db_session, channel):
    s = TVSeries(id=_uuid(), title_en="Some Show", content_type="tv")
    db_session.add(s)
    await db_session.flush()
    res = _resource(channel.id, search_title="Some Show")
    db_session.add(res)
    await db_session.flush()
    await ms.fetch_and_link_metadata(db_session, res, channel)
    assert res.series_id == s.id


async def test_local_fuzzy_below_70_no_link(db_session, channel):
    s = TVSeries(id=_uuid(), title_en="Completely Different Name", content_type="tv")
    db_session.add(s)
    await db_session.flush()
    res = _resource(channel.id, search_title="Something Else Entirely")
    db_session.add(res)
    await db_session.flush()
    await ms.fetch_and_link_metadata(db_session, res, channel)
    assert res.series_id is None
    assert res.movie_id is None


async def test_local_fuzzy_70_to_84_no_auto_link(db_session, channel):
    """Fuzzy score in [70, 85) should not auto-link (too ambiguous)."""
    # Very dissimilar title — ratio should be well below 70, no link at all.
    s = TVSeries(id=_uuid(), title_en="Rainbow Unicorn Adventures", content_type="tv")
    db_session.add(s)
    await db_session.flush()
    res = _resource(channel.id, search_title="Quantum Physics Explained")
    db_session.add(res)
    await db_session.flush()
    await ms.fetch_and_link_metadata(db_session, res, channel)
    assert res.series_id is None


async def test_local_fuzzy_high_ratio_autolinks(db_session, channel):
    # Very close fuzzy match >85 should link.
    s = TVSeries(id=_uuid(), title_en="Demon Slayer", content_type="tv")
    db_session.add(s)
    await db_session.flush()
    res = _resource(channel.id, search_title="Demon Slayerr")
    db_session.add(res)
    await db_session.flush()
    await ms.fetch_and_link_metadata(db_session, res, channel)
    assert res.series_id == s.id


async def test_apply_title_extraction_regex_method():
    out = await ms.apply_title_extraction("Show Season 2", "regex", r"^(.+?)\s*Season")
    assert out == "Show"


async def test_apply_title_extraction_llm_method_calls_cleaner(db_session):
    from unittest.mock import AsyncMock, patch
    with patch("app.services.title_cleaner.clean_title_llm", new_callable=AsyncMock, return_value="CLEAN"):
        out = await ms.apply_title_extraction("messy", "llm", None, db=db_session)
    assert out == "CLEAN"


async def test_apply_title_extraction_none_returns_title():
    assert await ms.apply_title_extraction("Title", "none", None) == "Title"
    assert await ms.apply_title_extraction("", "regex", "x") == ""


async def test_create_or_update_movie_from_external(db_session):
    data = {
        "content_type": "movie",
        "title_cn": "电影",
        "title_en": "Movie",
        "original_title": "Movie",
        "external_id": "ext-movie",
        "external_source": "llm_search",
        "poster_url": None,
        "release_date": "2024-05-01",
        "runtime": 120,
        "genre": ["Action"],
        "rating": 7.5,
        "status": "Released",
    }
    with patch("app.services.metadata_service.download_and_cache_poster", new_callable=AsyncMock, return_value=None):
        m1 = await ms.create_or_update_movie_from_external(db_session, data)
    await db_session.flush()
    assert m1.title_en == "Movie"
    assert m1.runtime == 120
    # Update merges aliases
    data2 = dict(data)
    data2["title_cn"] = "电影别名"
    with patch("app.services.metadata_service.download_and_cache_poster", new_callable=AsyncMock, return_value=None):
        m2 = await ms.create_or_update_movie_from_external(db_session, data2)
    await db_session.flush()
    assert m2.id == m1.id
    assert "电影别名" in (m2.aliases or [])


async def test_download_poster_bad_ext_defaults_jpg(tmp_path, monkeypatch):
    ms.settings.poster_cache_dir = str(tmp_path)
    async def _fake_to_thread(fn, *a, **kw):
        return fn()
    import asyncio
    monkeypatch.setattr(asyncio, "to_thread", _fake_to_thread)
    class _Resp:
        content = b"data"
        def raise_for_status(self): pass
    class _Client:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, **kw): return _Resp()
    import httpx
    monkeypatch.setattr(httpx, "Client", _Client)
    url = await ms.download_and_cache_poster("https://x/poster.xyz")
    assert url.endswith(".jpg")


async def test_download_poster_download_failure_returns_none(tmp_path, monkeypatch):
    ms.settings.poster_cache_dir = str(tmp_path)
    async def _fake_to_thread(fn, *a, **kw):
        return fn()
    import asyncio
    monkeypatch.setattr(asyncio, "to_thread", _fake_to_thread)
    class _Client:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, **kw):
            raise RuntimeError("network down")
    import httpx
    monkeypatch.setattr(httpx, "Client", _Client)
    url = await ms.download_and_cache_poster("https://x/poster.jpg")
    assert url is None


async def test_download_poster_existing_file_returns_cached(tmp_path):
    ms.settings.poster_cache_dir = str(tmp_path)
    import hashlib
    remote = "https://example.com/existing.jpg"
    digest = hashlib.sha256(remote.encode()).hexdigest()[:16]
    (tmp_path / f"{digest}.jpg").write_bytes(b"x")
    out = await ms.download_and_cache_poster(remote)
    assert out == f"/posters/{digest}.jpg"


async def test_search_metadata_via_llm_no_key_returns_empty(monkeypatch):
    monkeypatch.setattr(ms.settings, "llm_api_key", None)
    assert await ms.search_metadata_via_llm("anything") == []


async def test_search_metadata_via_llm_parses_results(monkeypatch):
    monkeypatch.setattr(ms.settings, "llm_api_key", "k")
    monkeypatch.setattr(ms.settings, "llm_search_model", "m")
    monkeypatch.setattr(ms.settings, "llm_base_url", "http://llm")

    class _Resp:
        output_text = 'irrelevant {"results": [{"content_type":"tv","title_en":"Show","external_id":"e1","genre":["Drama"]}]}'
    class _Client:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        class responses:
            @staticmethod
            async def create(**kw):
                return _Resp()
    import openai
    monkeypatch.setattr(openai, "AsyncOpenAI", _Client)
    # Force a fresh AsyncOpenAI instance
    results = await ms.search_metadata_via_llm("query")
    assert len(results) == 1
    assert results[0]["title_en"] == "Show"
    assert results[0]["external_source"] == "llm_search"


async def test_search_metadata_via_llm_bad_json_returns_empty(monkeypatch):
    monkeypatch.setattr(ms.settings, "llm_api_key", "k")
    monkeypatch.setattr(ms.settings, "llm_search_model", "m")
    monkeypatch.setattr(ms.settings, "llm_base_url", "http://llm")
    class _Resp:
        output_text = "no json here"
    class _Client:
        def __init__(self, *a, **kw): pass
        class responses:
            @staticmethod
            async def create(**kw): return _Resp()
    import openai
    monkeypatch.setattr(openai, "AsyncOpenAI", _Client)
    assert await ms.search_metadata_via_llm("x") == []


async def test_parse_date():
    from datetime import date
    assert ms._parse_date("2024-05-01") == date(2024, 5, 1)
    assert ms._parse_date("2024") == date(2024, 1, 1)
    assert ms._parse_date(date(2024, 1, 1)) == date(2024, 1, 1)
    assert ms._parse_date("garbage") is None
    assert ms._parse_date(None) is None


async def test_get_search_model(monkeypatch):
    monkeypatch.setattr(ms.settings, "llm_search_model", None)
    monkeypatch.setattr(ms.settings, "llm_model", "main-model")
    assert ms._get_search_model() == "main-model"
    monkeypatch.setattr(ms.settings, "llm_search_model", "search-model")
    assert ms._get_search_model() == "search-model"


async def test_already_linked_resource_skips(db_session, channel):
    s = TVSeries(id=_uuid(), title_en="AlreadyLinked", content_type="tv")
    db_session.add(s)
    await db_session.flush()
    res = _resource(channel.id, series_id=s.id, search_title="whatever")
    db_session.add(res)
    await db_session.flush()
    with patch("app.services.metadata_service.search_metadata_via_llm", new_callable=AsyncMock) as m:
        await ms.fetch_and_link_metadata(db_session, res, channel)
        m.assert_not_called()
    assert res.series_id == s.id


async def test_manual_link_updates_existing_mapping(db_session, channel):
    """Calling manual_link a second time updates the existing mapping row."""
    res = _resource(channel.id, title_raw="[G] Show 01")
    db_session.add(res)
    await db_session.flush()
    sel = {
        "content_type": "tv",
        "title_en": "Show",
        "external_id": "ext-show",
        "external_source": "llm_search",
    }
    with patch("app.services.metadata_service.download_and_cache_poster", new_callable=AsyncMock, return_value=None):
        await ms.manual_link_metadata(db_session, res, channel, sel)
    await db_session.flush()
    first_sid = res.series_id
    assert first_sid is not None
    # Second link: different series
    sel2 = dict(sel, external_id="ext-show-2", title_en="Show V2")
    with patch("app.services.metadata_service.download_and_cache_poster", new_callable=AsyncMock, return_value=None):
        e2 = await ms.manual_link_metadata(db_session, res, channel, sel2)
    await db_session.flush()
    assert res.series_id == e2.id
    # Mapping row should point to the new series (no duplicates)
    from sqlalchemy import select, func
    count = (await db_session.execute(
        select(func.count()).select_from(ChannelRawTitleMapping).where(
            ChannelRawTitleMapping.channel_id == channel.id,
            ChannelRawTitleMapping.raw_title == "[G] Show 01",
        )
    )).scalar_one()
    assert count == 1


# ---------------------------------------------------------------------------
# Layer 4: LLM web search
# ---------------------------------------------------------------------------


async def test_llm_fallback_when_metadata_source_llm(db_session, channel):
    channel.metadata_source = "llm"
    fake_results = [{
        "content_type": "tv",
        "title_cn": "搜索剧",
        "title_en": "Searched Show",
        "original_title": "Searched Show",
        "description": "...",
        "poster_url": None,
        "external_id": "llm_1",
        "external_source": "llm_search",
    }]
    res = _resource(channel.id, search_title="some new show")
    db_session.add(res)
    await db_session.flush()
    with patch(
        "app.services.metadata_service.search_metadata_via_llm",
        new_callable=AsyncMock, return_value=fake_results,
    ), patch(
        "app.services.metadata_service.download_and_cache_poster",
        new_callable=AsyncMock, return_value=None,
    ):
        await ms.fetch_and_link_metadata(db_session, res, channel)
    assert res.series_id is not None
    s = await db_session.get(TVSeries, res.series_id)
    assert s.title_en == "Searched Show"


async def test_llm_fallback_skipped_when_metadata_source_none(db_session, channel):
    channel.metadata_source = "none"
    res = _resource(channel.id, search_title="unknown thing")
    db_session.add(res)
    await db_session.flush()
    with patch(
        "app.services.metadata_service.search_metadata_via_llm",
        new_callable=AsyncMock, return_value=[{"content_type": "tv", "title_en": "X",
                                              "external_id": "llm_1"}],
    ) as mock_search:
        await ms.fetch_and_link_metadata(db_session, res, channel)
        mock_search.assert_not_awaited()
    assert res.series_id is None


# ---------------------------------------------------------------------------
# manual_search_metadata
# ---------------------------------------------------------------------------


async def test_manual_search_metadata_prefers_content_type(db_session):
    fake_results = [
        {"content_type": "tv", "title_en": "TV Show", "external_id": "t1"},
        {"content_type": "movie", "title_en": "Movie Thing", "external_id": "m1"},
    ]
    with patch(
        "app.services.metadata_service.search_metadata_via_llm",
        new_callable=AsyncMock, return_value=fake_results,
    ):
        out = await ms.manual_search_metadata(db_session, "tv show", "tv")
    assert all(r["content_type"] == "tv" for r in out)
    assert len(out) == 1


# ---------------------------------------------------------------------------
# manual_link_metadata
# ---------------------------------------------------------------------------


async def test_manual_link_metadata_creates_entity_and_mapping(db_session, channel):
    res = _resource(channel.id, title_raw="[RAW] Show 01")
    db_session.add(res)
    await db_session.flush()
    selected = {
        "content_type": "movie",
        "title_cn": "新电影",
        "title_en": "New Movie",
        "original_title": "New Movie",
        "external_id": "ext-1",
        "external_source": "llm_search",
    }
    with patch(
        "app.services.metadata_service.download_and_cache_poster",
        new_callable=AsyncMock, return_value=None,
    ):
        entity = await ms.manual_link_metadata(db_session, res, channel, selected)
    await db_session.flush()
    assert res.movie_id == entity.id
    assert isinstance(entity, Movie)
    # Mapping should be upserted
    from sqlalchemy import select
    map_result = await db_session.execute(
        select(ChannelRawTitleMapping).where(
            ChannelRawTitleMapping.channel_id == channel.id,
            ChannelRawTitleMapping.raw_title == "[RAW] Show 01",
        )
    )
    mapping = map_result.scalars().first()
    assert mapping is not None
    assert mapping.movie_id == entity.id


# ---------------------------------------------------------------------------
# download_and_cache_poster
# ---------------------------------------------------------------------------


async def test_download_and_cache_poster_skips_non_http(tmp_path):
    ms.settings.poster_cache_dir = str(tmp_path)
    assert await ms.download_and_cache_poster(None) is None
    assert await ms.download_and_cache_poster("/posters/existing.jpg") == "/posters/existing.jpg"
    assert await ms.download_and_cache_poster("ftp://example.com/x.jpg") is None


async def test_download_and_cache_poster_writes_file(tmp_path, monkeypatch):
    ms.settings.poster_cache_dir = str(tmp_path)

    def _fake_download():
        return b"fakedata"

    async def _fake_to_thread(fn, *a, **kw):
        return fn()

    # Patch asyncio.to_thread to run synchronously in tests
    import asyncio
    monkeypatch.setattr(asyncio, "to_thread", _fake_to_thread)

    class _FakeResp:
        content = b"fakedata"
        def raise_for_status(self):
            return None
    class _FakeClient:
        def __init__(self, *a, **kw):
            pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, **kw): return _FakeResp()
    import httpx
    monkeypatch.setattr(httpx, "Client", _FakeClient)

    url = await ms.download_and_cache_poster("https://example.com/poster.jpg")
    assert url is not None
    assert url.startswith("/posters/")
    out_file = tmp_path / url.split("/")[-1]
    assert out_file.exists()


# ---------------------------------------------------------------------------
# create_or_update_series_from_external merges aliases
# ---------------------------------------------------------------------------


async def test_create_or_update_series_merges_aliases(db_session):
    data = {
        "content_type": "tv",
        "title_cn": "剧A",
        "title_en": "Show A",
        "original_title": "Show A",
        "external_id": "ext-a",
        "external_source": "llm_search",
    }
    with patch(
        "app.services.metadata_service.download_and_cache_poster",
        new_callable=AsyncMock, return_value=None,
    ):
        s1 = await ms.create_or_update_series_from_external(db_session, data)
    await db_session.flush()
    # Update with new alias
    data2 = dict(data)
    data2["title_cn"] = "剧A别名"
    with patch(
        "app.services.metadata_service.download_and_cache_poster",
        new_callable=AsyncMock, return_value=None,
    ):
        s2 = await ms.create_or_update_series_from_external(db_session, data2)
    await db_session.flush()
    assert s1.id == s2.id
    assert "剧A别名" in (s2.aliases or [])
