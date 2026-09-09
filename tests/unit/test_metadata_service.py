"""Tests for metadata_service: 4-layer matching, manual search/link, poster cache."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.models.channel import Channel
from app.models.channel_raw_title_mapping import ChannelRawTitleMapping
from app.models.file_resource import FileResource
from app.models.movie import Movie
from app.models.series import TVSeries
from app.models.work_collection import WorkCollection
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
        metadata_agent_enabled=False,
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


def test_extract_search_title_strips_alt_title_and_season():
    # " / " alt-title split (keep Chinese primary) + bare "3期" season strip.
    r = SimpleNamespace(
        title_cn=None, title_en=None,
        title_raw="[LoliHouse] 无职转生 3期 / Mushoku Tensei S3 - 03 [WebRip 1080p]",
    )
    assert ms.extract_search_title(r) == "无职转生"


def test_extract_search_title_strips_full_width_group_bracket():
    r = SimpleNamespace(
        title_cn=None, title_en=None,
        title_raw="【字幕组】 作品名 / English Title - 01 [1080p]",
    )
    assert ms.extract_search_title(r) == "作品名"


def test_extract_search_title_strips_trailing_quality_bracket():
    r = SimpleNamespace(
        title_cn=None, title_en=None,
        title_raw="[G] Movie 2024 [2160p]",
    )
    assert ms.extract_search_title(r) == "Movie 2024"


def test_extract_search_title_strips_season_from_parsed_title():
    # The title_cn path also strips a trailing season suffix.
    r = SimpleNamespace(title_cn="某剧 第三季", title_en=None, title_raw="raw")
    assert ms.extract_search_title(r) == "某剧"


# ---------------------------------------------------------------------------
# Layer 2: ChannelRawTitleMapping
# ---------------------------------------------------------------------------


async def test_raw_title_mapping_links_series(db_session, channel):
    s = TVSeries(id=_uuid(), title_cn="剧", title_en="Series", content_type="tv")
    db_session.add(s)
    await db_session.flush()
    raw = "[G] Title - 01 [1080p]"
    # search_title_key = normalize_title(extract_search_title(raw)) = "title"
    mapping = ChannelRawTitleMapping(
        id=_uuid(), channel_id=channel.id, raw_title=raw,
        search_title_key="title",
        content_type="tv", series_id=s.id, movie_id=None,
    )
    db_session.add(mapping)
    await db_session.flush()
    # Different episode — same search_title_key should still match
    res = _resource(channel.id, title_raw="[G] Title - 02 [1080p]", search_title="junk")
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
    # search_title_key = normalize_title(extract_search_title(raw)) = "movie 2024"
    mapping = ChannelRawTitleMapping(
        id=_uuid(), channel_id=channel.id, raw_title=raw,
        search_title_key="movie 2024",
        content_type="movie", movie_id=m.id,
        search_title_override="clean",
    )
    db_session.add(mapping)
    await db_session.flush()
    # Different format — same search_title_key should still match
    res = _resource(channel.id, title_raw="[G] Movie 2024 [2160p]")
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


async def test_download_poster_unrecognized_content_not_cached(tmp_path, monkeypatch):
    """Non-image bytes (e.g. an HTML error page) must not be cached as .jpg."""
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
    assert url is None
    assert list(tmp_path.iterdir()) == []


def _patch_poster_download(monkeypatch, tmp_path, content: bytes):
    ms.settings.poster_cache_dir = str(tmp_path)
    async def _fake_to_thread(fn, *a, **kw):
        return fn()
    import asyncio
    monkeypatch.setattr(asyncio, "to_thread", _fake_to_thread)
    class _Resp:
        def raise_for_status(self): pass
    _Resp.content = content
    class _Client:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, **kw): return _Resp()
    import httpx
    monkeypatch.setattr(httpx, "Client", _Client)


async def test_download_poster_sniffs_svg_at_jpg_url(tmp_path, monkeypatch):
    """SVG bytes served at a .jpg URL are cached as .svg — a .jpg file with
    SVG content renders as a broken image (the 黑貓與魔女的教室 bug)."""
    _patch_poster_download(monkeypatch, tmp_path, b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"/>')
    url = await ms.download_and_cache_poster("https://upload.wikimedia.org/x/poster.jpg")
    assert url is not None and url.endswith(".svg")


async def test_download_poster_sniffs_png_at_extensionless_url(tmp_path, monkeypatch):
    _patch_poster_download(monkeypatch, tmp_path, b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    url = await ms.download_and_cache_poster("https://image.tmdb.org/t/p/w500/abc")
    assert url is not None and url.endswith(".png")


async def test_download_poster_sniffs_jpeg(tmp_path, monkeypatch):
    _patch_poster_download(monkeypatch, tmp_path, b"\xff\xd8\xff\xe0" + b"\x00" * 32)
    url = await ms.download_and_cache_poster("https://x/poster")
    assert url is not None and url.endswith(".jpg")



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


async def test_search_metadata_via_llm_delegates_to_agent(monkeypatch):
    """search_metadata_via_llm now delegates to the multi-source agent."""

    async def fake_agent_search(title: str):
        return [{"content_type": "tv", "title_en": "AgentResult", "external_id": "a1", "external_source": "tmdb"}]

    monkeypatch.setattr("app.services.metadata_service.search_metadata_via_llm",
                        lambda title: fake_agent_search(title))
    results = await ms.search_metadata_via_llm("anything")
    assert len(results) == 1
    assert results[0]["title_en"] == "AgentResult"


async def test_parse_date():
    from datetime import date
    assert ms._parse_date("2024-05-01") == date(2024, 5, 1)
    assert ms._parse_date("2024") == date(2024, 1, 1)
    assert ms._parse_date(date(2024, 1, 1)) == date(2024, 1, 1)
    assert ms._parse_date("garbage") is None
    assert ms._parse_date(None) is None


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
    from sqlalchemy import func, select
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


async def test_llm_fallback_when_metadata_agent_enabled(db_session, channel):
    channel.metadata_agent_enabled = True
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


async def test_llm_fallback_skipped_when_metadata_agent_disabled(db_session, channel):
    channel.metadata_agent_enabled = False
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
        return b"\xff\xd8\xff\xe0fakedata"

    async def _fake_to_thread(fn, *a, **kw):
        return fn()

    # Patch asyncio.to_thread to run synchronously in tests
    import asyncio
    monkeypatch.setattr(asyncio, "to_thread", _fake_to_thread)

    class _FakeResp:
        content = b"\xff\xd8\xff\xe0fakedata"
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


# ---------------------------------------------------------------------------
# canonicalize_external_id — Exa's inconsistent shapes must collapse
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw_id,source,expected",
    [
        ("TMDB:82684", "exa", "tmdb:82684"),
        ("TMDB 82684", "exa", "tmdb:82684"),
        ("TMDB TV 82684 / season 4", "exa", "tmdb:82684"),
        ("tmdb:82684", "exa", "tmdb:82684"),
        ("82684", "tmdb", "tmdb:82684"),
        ("tt31889371", "exa", "imdb:tt31889371"),
        (None, "exa", None),
        ("", "exa", None),
    ],
)
def test_canonicalize_external_id(raw_id, source, expected):
    assert ms.canonicalize_external_id(raw_id, source) == expected


# ---------------------------------------------------------------------------
# create_or_update_series_from_external — dedup by canonical external_id
# ---------------------------------------------------------------------------


async def test_create_or_update_series_dedups_by_canonical_external_id(db_session):
    """Exa returning different string shapes of the same TMDB id must upsert
    into a single row, not spawn duplicates."""
    with patch(
        "app.services.metadata_service.download_and_cache_poster",
        new_callable=AsyncMock, return_value=None,
    ):
        s1 = await ms.create_or_update_series_from_external(db_session, {
            "content_type": "tv",
            "title_cn": "关于我转生变成史莱姆这档事 第四季",
            "title_en": "That Time I Got Reincarnated as a Slime Season 4",
            "original_title": "転生したらスライムだった件 第4期",
            "external_id": "TMDB:82684",
            "external_source": "exa",
        })
        await db_session.flush()

        s2 = await ms.create_or_update_series_from_external(db_session, {
            "content_type": "tv",
            "title_cn": "关于我转生变成史莱姆这档事 第四季",
            "title_en": "That Time I Got Reincarnated as a Slime Season 4",
            "original_title": "転生したらスライムだった件 第4期",
            "external_id": "TMDB 82684",
            "external_source": "exa",
        })
        await db_session.flush()

        s3 = await ms.create_or_update_series_from_external(db_session, {
            "content_type": "tv",
            "title_cn": "关于我转生变成史莱姆这档事 第四季",
            "title_en": "That Time I Got Reincarnated as a Slime Season 4",
            "original_title": "転生したらスライムだった件 第4期",
            "external_id": "TMDB TV 82684 / season 4",
            "external_source": "exa",
        })
        await db_session.flush()

    assert s1.id == s2.id == s3.id
    # Per-season work model: the season work's primary is the synthetic
    # per-season identity; the series-level tmdb id lives on the collection's
    # identity bag.
    assert s3.external_id == "tmdb:82684#s4"  # canonicalized + season-tagged
    assert s3.season_number == 4
    assert s3.collection_id is not None


async def test_create_or_update_series_dedups_by_title_fallback(db_session):
    """When external_id shapes don't overlap at all but titles match, still
    reuse the existing row (Exa returned a fresh id but same work)."""
    with patch(
        "app.services.metadata_service.download_and_cache_poster",
        new_callable=AsyncMock, return_value=None,
    ):
        s1 = await ms.create_or_update_series_from_external(db_session, {
            "content_type": "tv",
            "title_cn": "杖与剑的魔剑谭 第二季",
            "title_en": "Wistoria: Wand and Sword Season 2",
            "external_id": "TMDB 245842",
            "external_source": "exa",
        })
        await db_session.flush()

        # Different external_id entirely (e.g. Exa hashed it), same titles
        s2 = await ms.create_or_update_series_from_external(db_session, {
            "content_type": "tv",
            "title_cn": "杖与剑的魔剑谭 第二季",
            "title_en": "Wistoria: Wand and Sword Season 2",
            "external_id": "59983",
            "external_source": "exa",
        })
        await db_session.flush()

    assert s1.id == s2.id


async def test_create_or_update_movie_dedups_by_canonical_external_id(db_session):
    with patch(
        "app.services.metadata_service.download_and_cache_poster",
        new_callable=AsyncMock, return_value=None,
    ):
        m1 = await ms.create_or_update_movie_from_external(db_session, {
            "content_type": "movie",
            "title_cn": "某电影",
            "title_en": "Some Movie",
            "external_id": "TMDB:12345",
            "external_source": "exa",
        })
        await db_session.flush()
        m2 = await ms.create_or_update_movie_from_external(db_session, {
            "content_type": "movie",
            "title_cn": "某电影",
            "title_en": "Some Movie",
            "external_id": "TMDB 12345",
            "external_source": "exa",
        })
        await db_session.flush()

    assert m1.id == m2.id
    assert m2.external_id == "tmdb:12345"


# ---------------------------------------------------------------------------
# Layer 4 must respect the channel's configured source (not hardcoded Exa)
# ---------------------------------------------------------------------------


async def test_fetch_and_link_metadata_layer4_uses_channel_source(db_session):
    """Per-resource refresh (Layer 4) must run the channel's resolved source,
    not a hardcoded default. Channel resolution is two-source (Phase P1): a
    deprecated jina channel converges on wikipedia."""
    ch = Channel(
        id=_uuid(), name="ch", type="rss_feed", url="https://example.com/rss",
        field_mapping=TEST_FIELD_MAPPING,
        metadata_agent_enabled=True, metadata_source="jina",
    )
    db_session.add(ch)
    await db_session.flush()
    res = _resource(
        ch.id, title_raw="[G] Some Unique Show - 01 [1080p]",
        search_title="Some Unique Show",
    )
    db_session.add(res)
    await db_session.flush()

    with patch("app.services.metadata_service.search_metadata_via_llm", new_callable=AsyncMock) as mock_search:
        mock_search.return_value = []
        await ms.fetch_and_link_metadata(db_session, res, ch)

    mock_search.assert_called_once()
    assert mock_search.call_args.args[1] == "wikipedia"


# ---------------------------------------------------------------------------
# create_or_update_*_from_external — cross-language convergence via alt_titles
# ---------------------------------------------------------------------------


async def test_create_or_update_series_converges_cross_language_wiki_pages(db_session):
    """The same work matched once via its zhwiki page and once via its enwiki
    page (different external_ids, disjoint title slots) must upsert into ONE
    row when the second match carries langlink alt_titles."""
    with patch(
        "app.services.metadata_service.download_and_cache_poster",
        new_callable=AsyncMock, return_value=None,
    ):
        s1 = await ms.create_or_update_series_from_external(db_session, {
            "content_type": "tv",
            "title_cn": "黃泉使者",
            "title_en": None,
            "external_id": "wikipedia:7727654",
            "external_source": "wikipedia",
        })
        await db_session.flush()

        s2 = await ms.create_or_update_series_from_external(db_session, {
            "content_type": "tv",
            "title_cn": "黃泉使者",  # backfilled from langlinks by the judge
            "title_en": "Daemons of the Shadow Realm",
            "alt_titles": ["黃泉使者"],
            "external_id": "wikipedia:70545449",
            "external_source": "wikipedia",
        })
        await db_session.flush()

    assert s1.id == s2.id
    assert s2.title_en == "Daemons of the Shadow Realm"
    assert "黃泉使者" in (s2.aliases or [])


async def test_create_or_update_series_alt_titles_alone_can_bridge(db_session):
    """Even if the judge only supplied the en title in the en slot, an
    alt_titles entry equal to the existing row's title_cn still converges."""
    with patch(
        "app.services.metadata_service.download_and_cache_poster",
        new_callable=AsyncMock, return_value=None,
    ):
        s1 = await ms.create_or_update_series_from_external(db_session, {
            "content_type": "tv",
            "title_cn": "黃泉使者",
            "external_id": "wikipedia:7727654",
            "external_source": "wikipedia",
        })
        await db_session.flush()

        s2 = await ms.create_or_update_series_from_external(db_session, {
            "content_type": "tv",
            "title_en": "Daemons of the Shadow Realm",
            "alt_titles": ["黃泉使者"],
            "external_id": "wikipedia:70545449",
            "external_source": "wikipedia",
        })
        await db_session.flush()

    assert s1.id == s2.id


async def test_create_or_update_movie_converges_on_alt_titles(db_session):
    with patch(
        "app.services.metadata_service.download_and_cache_poster",
        new_callable=AsyncMock, return_value=None,
    ):
        m1 = await ms.create_or_update_movie_from_external(db_session, {
            "content_type": "movie",
            "title_cn": "劇場版 甲",
            "external_id": "wikipedia:111",
            "external_source": "wikipedia",
        })
        await db_session.flush()

        m2 = await ms.create_or_update_movie_from_external(db_session, {
            "content_type": "movie",
            "title_en": "Movie A",
            "alt_titles": ["劇場版 甲"],
            "external_id": "wikipedia:222",
            "external_source": "wikipedia",
        })
        await db_session.flush()

    assert m1.id == m2.id
    assert m2.title_en == "Movie A"
    assert "劇場版 甲" in (m2.aliases or [])


# ---------------------------------------------------------------------------
# Layer-3 auto-link guards (year mismatch + same-title collision)
# ---------------------------------------------------------------------------


class TestYearMismatch:
    def test_no_title_year_never_mismatches(self):
        from datetime import date
        assert ms._year_mismatch(None, date(1995, 1, 1)) is False
        assert ms._year_mismatch(None, None) is False

    def test_no_work_date_never_mismatches(self):
        assert ms._year_mismatch(2026, None) is False
        assert ms._year_mismatch(2026, "") is False

    def test_date_and_str_work_dates(self):
        from datetime import date, datetime
        assert ms._year_mismatch(2026, date(1995, 11, 18)) is True
        assert ms._year_mismatch(2026, datetime(2026, 4, 1)) is False
        assert ms._year_mismatch(2026, "1995-11-18") is True
        assert ms._year_mismatch(2026, "2026-04-01") is False

    def test_plus_minus_one_slack(self):
        from datetime import date
        assert ms._year_mismatch(2026, date(2025, 12, 1)) is False
        assert ms._year_mismatch(2026, date(2027, 1, 1)) is False
        assert ms._year_mismatch(2026, date(2024, 1, 1)) is True


async def test_layer3_year_mismatch_blocks_autolink(db_session, channel):
    """攻壳机动队 2026 must not auto-link to the 1995 攻壳机动队 series."""
    from datetime import date
    s = TVSeries(
        id=_uuid(), title_cn="攻壳机动队", title_en="The Ghost in the Shell",
        content_type="tv", start_date=date(1995, 11, 18),
    )
    db_session.add(s)
    await db_session.flush()
    res = _resource(channel.id, search_title="攻壳机动队", title_year=2026)
    db_session.add(res)
    await db_session.flush()
    await ms.fetch_and_link_metadata(db_session, res, channel)
    assert res.series_id is None  # blocked → falls through to (disabled) Layer 4


async def test_layer3_year_match_still_autolinks(db_session, channel):
    from datetime import date
    s = TVSeries(
        id=_uuid(), title_cn="攻壳机动队", content_type="tv",
        start_date=date(2026, 4, 1),
    )
    db_session.add(s)
    await db_session.flush()
    res = _resource(channel.id, search_title="攻壳机动队", title_year=2026)
    db_session.add(res)
    await db_session.flush()
    await ms.fetch_and_link_metadata(db_session, res, channel)
    assert res.series_id == s.id


async def test_layer3_same_title_collision_blocks_autolink(db_session, channel):
    """Two works with the exact same normalized title → no top-1 auto-link."""
    s1 = TVSeries(id=_uuid(), title_cn="攻壳机动队", content_type="tv")
    s2 = TVSeries(id=_uuid(), title_cn="攻壳机动队", title_en="GitS 2026", content_type="tv")
    db_session.add_all([s1, s2])
    await db_session.flush()
    res = _resource(channel.id, search_title="攻壳机动队")
    db_session.add(res)
    await db_session.flush()
    await ms.fetch_and_link_metadata(db_session, res, channel)
    assert res.series_id is None
    assert res.movie_id is None


async def test_has_same_title_collision(db_session):
    s1 = TVSeries(id=_uuid(), title_cn="标题甲", content_type="tv")
    db_session.add(s1)
    await db_session.flush()
    # A single matching work is not a collision — empty list (falsy).
    assert await ms._find_same_title_works(db_session, "标题甲") == []
    m1 = Movie(id=_uuid(), title_cn="标题甲", content_type="movie")
    db_session.add(m1)
    await db_session.flush()
    # Same title across TVSeries + Movie counts as a collision; the returned
    # list carries both works for the prompt injection.
    works = await ms._find_same_title_works(db_session, "标题甲")
    assert len(works) == 2
    by_type = {w["content_type"]: w for w in works}
    assert by_type["tv"]["id"] == s1.id
    assert by_type["movie"]["id"] == m1.id
    assert await ms._find_same_title_works(db_session, "不存在的标题") == []


async def test_find_same_title_works_fields(db_session):
    """Collision list carries year/seasons for the prompt injection."""
    from datetime import date
    s1 = TVSeries(
        id=_uuid(), title_cn="攻壳机动队", content_type="tv",
        start_date=date(2002, 10, 1), number_of_seasons=2,
    )
    m1 = Movie(
        id=_uuid(), title_cn="攻壳机动队", content_type="movie",
        release_date=date(1995, 11, 18),
    )
    db_session.add_all([s1, m1])
    await db_session.flush()
    works = await ms._find_same_title_works(db_session, "攻壳机动队")
    assert len(works) == 2
    by_type = {w["content_type"]: w for w in works}
    assert by_type["tv"]["year"] == 2002
    assert by_type["tv"]["number_of_seasons"] == 2
    assert by_type["movie"]["year"] == 1995

    text = ms.format_same_title_works_context(works)
    assert "本地库存在同名作品" in text
    assert "攻壳机动队 (2002, tv, 2 seasons)" in text
    assert "攻壳机动队 (1995, movie)" in text
    assert "结合标题年份选择正确作品" in text


# ---------------------------------------------------------------------------
# Batch 2: agent-free verified season rule (_reconcile_with_series)
# ---------------------------------------------------------------------------


def _mapping(channel_id, series_id=None, movie_id=None, raw="[G] Title - 01 [1080p]"):
    return ChannelRawTitleMapping(
        id=_uuid(), channel_id=channel_id, raw_title=raw,
        search_title_key="title", content_type="tv",
        series_id=series_id, movie_id=movie_id,
    )


async def test_single_season_series_defaults_season_1(db_session, channel):
    """Linked to a provably single-season series → season=1 (verified)."""
    s = TVSeries(id=_uuid(), title_cn="单季剧", content_type="tv", number_of_seasons=1,
                 seasons=[{"season_number": 1, "episode_count": 12}])
    db_session.add(s)
    db_session.add(_mapping(channel.id, series_id=s.id))
    res = _resource(channel.id, season=None, episode=3)
    db_session.add(res)
    await db_session.flush()

    await ms.fetch_and_link_metadata(db_session, res, channel)

    assert res.series_id == s.id
    assert res.season == 1
    assert res.episode_confidence == "raw"  # in-range per-season number


async def test_multi_season_series_marks_season_uncertain(db_session, channel):
    """Multi-season series + no season marker → ambiguous, never a guess."""
    s = TVSeries(id=_uuid(), title_cn="多季剧", content_type="tv", number_of_seasons=3,
                 seasons=[{"season_number": n, "episode_count": 24} for n in (1, 2, 3)])
    db_session.add(s)
    db_session.add(_mapping(channel.id, series_id=s.id))
    res = _resource(channel.id, season=None, episode=3)
    db_session.add(res)
    await db_session.flush()

    await ms.fetch_and_link_metadata(db_session, res, channel)

    assert res.series_id == s.id
    assert res.season is None
    assert res.episode_confidence == "ambiguous"


async def test_unknown_season_count_marks_season_uncertain(db_session, channel):
    """Series without any seasons evidence → season can't be verified."""
    s = TVSeries(id=_uuid(), title_cn="无季数据剧", content_type="tv")
    db_session.add(s)
    db_session.add(_mapping(channel.id, series_id=s.id))
    res = _resource(channel.id, season=None, episode=3)
    db_session.add(res)
    await db_session.flush()

    await ms.fetch_and_link_metadata(db_session, res, channel)

    assert res.series_id == s.id
    assert res.season is None
    assert res.episode_confidence == "ambiguous"


async def test_batch_resource_not_marked_season_uncertain(db_session, channel):
    """A 合集 bypasses per-episode flow — no season-uncertain marking even
    for a multi-season series."""
    s = TVSeries(id=_uuid(), title_cn="多季剧B", content_type="tv", number_of_seasons=2,
                 seasons=[{"season_number": n, "episode_count": 24} for n in (1, 2)])
    db_session.add(s)
    db_session.add(_mapping(channel.id, series_id=s.id))
    res = _resource(channel.id, season=None, episode=None, is_batch=True)
    db_session.add(res)
    await db_session.flush()

    await ms.fetch_and_link_metadata(db_session, res, channel)

    assert res.series_id == s.id
    assert res.season is None
    assert res.episode_confidence is None


async def test_movie_link_untouched_by_season_rule(db_session, channel):
    m = Movie(id=_uuid(), title_en="Film", content_type="movie")
    db_session.add(m)
    db_session.add(_mapping(channel.id, movie_id=m.id))
    res = _resource(channel.id, season=None, episode=None)
    db_session.add(res)
    await db_session.flush()

    await ms.fetch_and_link_metadata(db_session, res, channel)

    assert res.movie_id == m.id
    assert res.season is None
    assert res.episode_confidence is None


async def test_layer4_work_level_ambiguous_not_linked(db_session):
    """Layer 4: an agent verdict flagged work-level ambiguous is never
    auto-linked — the resource stays manually linkable (not_found)."""
    ch = Channel(
        id=_uuid(), name="ch-llm", type="rss_feed", url="https://example.com/rss2",
        field_mapping=TEST_FIELD_MAPPING, metadata_agent_enabled=True,
    )
    db_session.add(ch)
    res = _resource(ch.id, title_raw="[G] NoLocalMatch - 01 [1080p]",
                    search_title="NoLocalMatch", title_cn=None, title_en=None)
    db_session.add(res)
    await db_session.flush()

    with patch.object(
        ms, "search_metadata_via_llm",
        AsyncMock(return_value=[{
            "ambiguous": True, "content_type": "tv",
            "external_id": "tmdb:777", "external_source": "tmdb",
            "title_en": "Maybe This Show",
        }]),
    ):
        await ms.fetch_and_link_metadata(db_session, res, ch)

    assert res.series_id is None
    assert res.movie_id is None
    assert res.metadata_failure_type == "not_found"
    from sqlalchemy import select
    assert (await db_session.execute(select(TVSeries))).scalars().all() == []


# ---------------------------------------------------------------------------
# Batch 3: manual_link_metadata season rule (resolve_missing_season)
# ---------------------------------------------------------------------------


async def test_manual_link_applies_verified_season_default(db_session, channel):
    """Manual link to a provably single-season series defaults a season-less
    resource to season=1 (the manual-link path bypasses _apply_to_resource)."""
    res = _resource(channel.id, title_raw="[G] Solo Show - 03 [1080p]",
                    season=None, episode=3)
    db_session.add(res)
    await db_session.flush()
    selected = {
        "content_type": "tv",
        "title_en": "Solo Show",
        "external_id": "ext-solo",
        "external_source": "llm_search",
        "number_of_seasons": 1,
        "seasons": [{"season_number": 1, "episode_count": 12}],
    }
    with patch(
        "app.services.metadata_service.download_and_cache_poster",
        new_callable=AsyncMock, return_value=None,
    ):
        entity = await ms.manual_link_metadata(db_session, res, channel, selected)
    assert res.series_id == entity.id
    assert res.season == 1
    assert res.episode_confidence == "raw"


async def test_manual_bangumi_link_uses_entry_scoped_season_default(
    db_session, channel,
):
    res = _resource(
        channel.id,
        title_raw="[G] Single Bangumi Entry - 03 [1080p]",
        season=None,
        episode=3,
    )
    db_session.add(res)
    await db_session.flush()
    selected = {
        "content_type": "tv",
        "title_en": "Single Bangumi Entry",
        "external_id": "bangumi:single-entry",
        "external_source": "bangumi",
        "single_season_entry": True,
    }
    with patch(
        "app.services.metadata_service.download_and_cache_poster",
        new_callable=AsyncMock,
        return_value=None,
    ):
        entity = await ms.manual_link_metadata(
            db_session, res, channel, selected,
        )
    assert res.season == 1
    assert entity.number_of_seasons is None


async def test_manual_link_marks_season_uncertain(db_session, channel):
    """Manual link to a multi-season series cannot pin a season: the resource
    is parked on the matched collection (挂合集待确认) and marked 季号不确定
    instead of guessing a season work (作品单季化 P3)."""
    res = _resource(channel.id, title_raw="[G] Multi Show - 03 [1080p]",
                    season=None, episode=3)
    db_session.add(res)
    await db_session.flush()
    selected = {
        "content_type": "tv",
        "title_en": "Multi Show",
        "external_id": "ext-multi",
        "external_source": "llm_search",
        "number_of_seasons": 3,
        "seasons": [{"season_number": n, "episode_count": 24} for n in (1, 2, 3)],
    }
    with patch(
        "app.services.metadata_service.download_and_cache_poster",
        new_callable=AsyncMock, return_value=None,
    ):
        entity = await ms.manual_link_metadata(db_session, res, channel, selected)
    # No season work is materialized for an indeterminate season.
    assert entity is None
    assert res.series_id is None
    assert res.collection_id is not None
    assert res.season is None
    assert res.episode_confidence == "ambiguous"


async def test_manual_link_reconciles_absolute_episode(db_session, channel):
    """Manual link also runs episode reconciliation against the freshly
    upserted series' seasons (absolute number -> per-season)."""
    res = _resource(channel.id, title_raw="[G] Abs Show - 30 [1080p]",
                    season=None, episode=30, absolute_episode=30,
                    episode_confidence="reconciled")
    db_session.add(res)
    await db_session.flush()
    selected = {
        "content_type": "tv",
        "title_en": "Abs Show",
        "external_id": "ext-abs",
        "external_source": "llm_search",
        "number_of_seasons": 2,
        "seasons": [{"season_number": 1, "episode_count": 24},
                    {"season_number": 2, "episode_count": 24}],
    }
    with patch(
        "app.services.metadata_service.download_and_cache_poster",
        new_callable=AsyncMock, return_value=None,
    ):
        await ms.manual_link_metadata(db_session, res, channel, selected)
    # absolute 30 across two 24-episode seasons -> S2E6, no season-uncertain.
    assert (res.season, res.episode) == (2, 6)
    assert res.episode_confidence == "reconciled"


async def test_upsert_skips_manually_edited_fields(db_session):
    """Auto-scan upsert must not overwrite a manually-edited field."""
    work = TVSeries(
        id=_uuid(), title_en="Upsert Show", content_type="tv",
        external_id="tmdb:upsert-1", external_source="tmdb",
        rating=7.5, manually_edited_fields=["rating", "is_anime"],
    )
    db_session.add(work)
    await db_session.flush()
    data = {
        "content_type": "tv",
        "title_en": "Upsert Show",
        "external_id": "tmdb:upsert-1",
        "external_source": "tmdb",
        "rating": 8.0,
        "is_anime": True,
    }
    with patch(
        "app.services.metadata_service.download_and_cache_poster",
        new_callable=AsyncMock, return_value=None,
    ):
        updated = await ms.create_or_update_series_from_external(db_session, data)
    assert updated.id == work.id
    assert updated.rating == 7.5  # manual edit preserved
    assert updated.is_anime is None  # manual edit preserved (identity-ish is_anime True ignored)


# ---------------------------------------------------------------------------
# create_or_update_audio_work_from_external: no-title creation guard
# ---------------------------------------------------------------------------


async def test_audio_work_create_without_any_title_is_refused(db_session, caplog):
    """A brand-new AudioWork with title_cn/title_en/original_title all empty is
    a useless shell — creation is refused with a warning, no row inserted."""
    import logging

    from sqlalchemy import select

    from app.models.audio_work import AudioWork

    with caplog.at_level(logging.WARNING, logger="app.services.metadata_service"):
        result = await ms.create_or_update_audio_work_from_external(db_session, {
            "external_id": "wikipedia:900",
            "external_source": "wikipedia",
            "content_type": "asmr",
        })
    assert result is None
    assert (await db_session.execute(select(AudioWork))).scalars().all() == []
    assert any("without any title" in r.message for r in caplog.records)


async def test_audio_work_create_with_title_still_works(db_session):
    with patch(
        "app.services.metadata_service.download_and_cache_poster",
        new_callable=AsyncMock, return_value=None,
    ):
        audio = await ms.create_or_update_audio_work_from_external(db_session, {
            "external_id": "wikipedia:901",
            "external_source": "wikipedia",
            "title_cn": "音声作品",
            "content_type": "asmr",
        })
    assert audio is not None
    assert audio.title_cn == "音声作品"
    assert audio.content_type == "asmr"


async def test_audio_work_titleless_update_of_existing_row_allowed(db_session):
    """The guard only applies to creation: a titleless payload that resolves to
    an existing row (by external_id) still updates it."""
    from app.models.audio_work import AudioWork

    existing = AudioWork(
        id=_uuid(), title_cn="既存作品", external_id="wikipedia:902",
        external_source="wikipedia", content_type="music",
    )
    db_session.add(existing)
    await db_session.flush()

    audio = await ms.create_or_update_audio_work_from_external(db_session, {
        "external_id": "wikipedia:902",
        "external_source": "wikipedia",
        "description": "updated desc",
    })
    assert audio is not None
    assert audio.id == existing.id
    assert audio.description == "updated desc"


# ---------------------------------------------------------------------------
# Wikipedia language-qualified ids: primary merge + incoming qualification
# ---------------------------------------------------------------------------


def test_merge_primary_external_id_wikipedia_same_pageid_upgrades():
    # Legacy bare primary + qualified incoming of the SAME pageid -> upgrade.
    assert ms._merge_primary_external_id("wikipedia:7301786", "wikipedia:zh:7301786") == (
        "wikipedia:zh:7301786"
    )
    # Already-qualified primary is not degraded by a bare incoming id.
    assert ms._merge_primary_external_id("wikipedia:zh:7301786", "wikipedia:7301786") == (
        "wikipedia:zh:7301786"
    )


def test_merge_primary_external_id_wikipedia_different_pageid_keeps_creator():
    # Different pageids = the same work's pages in different editions; the
    # creator's primary wins, the incoming id only joins the identity bag.
    assert ms._merge_primary_external_id("wikipedia:zh:7301786", "wikipedia:en:65944845") == (
        "wikipedia:zh:7301786"
    )


def test_merge_primary_external_id_non_wikipedia_incoming_wins():
    assert ms._merge_primary_external_id("wikipedia:zh:7301786", "tmdb:82684") == "tmdb:82684"
    assert ms._merge_primary_external_id(None, "tmdb:82684") == "tmdb:82684"
    assert ms._merge_primary_external_id("TMDB 82684", "tmdb:82684") == "tmdb:82684"


def test_qualify_incoming_wikipedia_id_via_url():
    data = {
        "external_id": "wikipedia:7301786",
        "external_source": "wikipedia",
        "wikipedia_url": "https://zh.wikipedia.org/wiki/X",
    }
    assert ms._qualify_incoming_wikipedia_id(data) == "wikipedia:zh:7301786"
    # The dict itself is updated so downstream bagging uses the qualified form.
    assert data["external_id"] == "wikipedia:zh:7301786"


def test_qualify_incoming_wikipedia_id_passthrough():
    # Non-wikipedia sources and unqualifiable ids are untouched.
    data = {"external_id": "tmdb:82684", "external_source": "tmdb"}
    assert ms._qualify_incoming_wikipedia_id(data) == "tmdb:82684"
    data = {"external_id": "wikipedia:7301786", "external_source": "wikipedia"}
    assert ms._qualify_incoming_wikipedia_id(data) == "wikipedia:7301786"
    data = {"external_id": None, "external_source": "wikipedia"}
    assert ms._qualify_incoming_wikipedia_id(data) is None


async def test_upsert_converges_qualified_incoming_on_legacy_bare_row(db_session):
    """A row created with the legacy bare primary converges when a later
    upsert arrives with the qualified form of the same pageid (column lookup),
    and the primary is upgraded in place."""
    s = TVSeries(
        id=_uuid(), title_cn="剧集X", content_type="tv",
        external_id="wikipedia:7301786", external_source="wikipedia",
    )
    db_session.add(s)
    await db_session.flush()

    out = await ms.create_or_update_series_from_external(db_session, {
        "external_id": "wikipedia:zh:7301786",
        "external_source": "wikipedia",
        "title_cn": "剧集X",
    })
    assert out.id == s.id
    assert out.external_id == "wikipedia:zh:7301786"


# ---------------------------------------------------------------------------
# Manual-edit protection for content_type / external_id / external_source
# ---------------------------------------------------------------------------

async def test_upsert_preserves_manually_edited_identity_series(db_session):
    """Auto-scan upsert must not overwrite a hand-set external identity."""
    work = TVSeries(
        id=_uuid(), title_en="Id Show", content_type="tv",
        external_id="tmdb:900", external_source="tmdb",
        manually_edited_fields=["external_id", "external_source"],
    )
    db_session.add(work)
    await db_session.flush()
    data = {
        "content_type": "tv",
        "title_en": "Id Show",
        "external_id": "tmdb:901",
        "external_source": "tmdb",
    }
    with patch(
        "app.services.metadata_service.download_and_cache_poster",
        new_callable=AsyncMock, return_value=None,
    ):
        updated = await ms.create_or_update_series_from_external(db_session, data)
    assert updated.id == work.id
    assert updated.external_id == "tmdb:900"  # manual edit preserved
    assert updated.external_source == "tmdb"


async def test_upsert_preserves_manually_edited_identity_movie(db_session):
    work = Movie(
        id=_uuid(), title_en="Id Movie", content_type="movie",
        external_id="tmdb:800", external_source="tmdb",
        manually_edited_fields=["external_id", "external_source"],
    )
    db_session.add(work)
    await db_session.flush()
    data = {
        "content_type": "movie",
        "title_en": "Id Movie",
        "external_id": "tmdb:801",
        "external_source": "tmdb",
    }
    with patch(
        "app.services.metadata_service.download_and_cache_poster",
        new_callable=AsyncMock, return_value=None,
    ):
        updated = await ms.create_or_update_movie_from_external(db_session, data)
    assert updated.id == work.id
    assert updated.external_id == "tmdb:800"  # manual edit preserved


async def test_upsert_preserves_manually_edited_content_type(db_session):
    """A user who reclassified a work keeps their content_type on upsert."""
    work = TVSeries(
        id=_uuid(), title_en="Type Show", content_type="movie",
        external_id="tmdb:700", external_source="tmdb",
        manually_edited_fields=["content_type"],
    )
    db_session.add(work)
    await db_session.flush()
    data = {
        "content_type": "tv",
        "title_en": "Type Show",
        "external_id": "tmdb:700",
        "external_source": "tmdb",
    }
    with patch(
        "app.services.metadata_service.download_and_cache_poster",
        new_callable=AsyncMock, return_value=None,
    ):
        updated = await ms.create_or_update_series_from_external(db_session, data)
    assert updated.content_type == "movie"  # manual edit preserved




# ---------------------------------------------------------------------------
# Collection fallback start_date (specials / missing-date season works)
# ---------------------------------------------------------------------------


async def _mk_collection(db_session) -> WorkCollection:
    coll = WorkCollection(
        id=_uuid(), title_cn="合集X", external_source="series_group",
    )
    db_session.add(coll)
    await db_session.flush()
    return coll


def _season_work(collection_id, season, **overrides):
    base = dict(
        id=_uuid(), title_cn="剧集X", content_type="tv",
        season_number=season, collection_id=collection_id,
    )
    base.update(overrides)
    return TVSeries(**base)


async def _create_specials_work(db_session, collection, data=None):
    with patch(
        "app.services.metadata_service.download_and_cache_poster",
        new_callable=AsyncMock, return_value=None,
    ):
        return await ms._create_season_work(
            db_session,
            data or {"content_type": "tv", "title_cn": "剧集X"},
            collection,
            0,
            raw_source=None, raw_external_id=None, canonical_id=None,
            granularity="series", series_level_id=None,
        )


async def test_create_season_work_borrows_earliest_regular_sibling_date(db_session):
    """A season-0 specials work never carries its own date — it borrows the
    earliest start_date of the collection's non-specials members."""
    coll = await _mk_collection(db_session)
    db_session.add_all([
        _season_work(coll.id, 2, start_date=date(2022, 4, 1)),
        _season_work(coll.id, 1, start_date=date(2020, 1, 1)),
    ])
    await db_session.flush()
    sp = await _create_specials_work(db_session, coll)
    assert sp.start_date == date(2020, 1, 1)


async def test_create_season_work_without_dated_regular_sibling_stays_null(db_session):
    """No dated non-specials member → no fallback: specials' own dates are
    never borrowed, and undated siblings contribute nothing."""
    coll = await _mk_collection(db_session)
    db_session.add_all([
        _season_work(coll.id, 1),  # no date
        _season_work(coll.id, 0, start_date=date(2019, 6, 1)),  # specials date
    ])
    await db_session.flush()
    sp = await _create_specials_work(db_session, coll)
    assert sp.start_date is None


async def test_create_season_work_prefers_own_date_over_fallback(db_session):
    """An entity carrying per-season evidence keeps its own date."""
    coll = await _mk_collection(db_session)
    db_session.add(_season_work(coll.id, 1, start_date=date(2020, 1, 1)))
    await db_session.flush()
    data = {
        "content_type": "tv", "title_cn": "剧集X",
        "seasons": [{"season_number": 2, "air_date": "2022-04-01"}],
    }
    with patch(
        "app.services.metadata_service.download_and_cache_poster",
        new_callable=AsyncMock, return_value=None,
    ):
        s2 = await ms._create_season_work(
            db_session, data, coll, 2,
            raw_source=None, raw_external_id=None, canonical_id=None,
            granularity="series", series_level_id=None,
        )
    assert s2.start_date == date(2022, 4, 1)


async def test_update_series_fills_null_start_date_from_sibling(db_session):
    coll = await _mk_collection(db_session)
    db_session.add(_season_work(coll.id, 1, start_date=date(2020, 1, 1)))
    sp = _season_work(coll.id, 0)
    db_session.add(sp)
    await db_session.flush()
    await ms._update_series_from_entity(
        db_session, sp, {"content_type": "tv"},
        raw_source=None, canonical_id=None, granularity="series",
    )
    assert sp.start_date == date(2020, 1, 1)


async def test_update_series_never_overwrites_existing_start_date(db_session):
    coll = await _mk_collection(db_session)
    db_session.add(_season_work(coll.id, 1, start_date=date(2020, 1, 1)))
    sp = _season_work(coll.id, 0, start_date=date(2019, 6, 1))
    db_session.add(sp)
    await db_session.flush()
    await ms._update_series_from_entity(
        db_session, sp, {"content_type": "tv"},
        raw_source=None, canonical_id=None, granularity="series",
    )
    assert sp.start_date == date(2019, 6, 1)


async def test_update_series_respects_manually_edited_start_date(db_session):
    coll = await _mk_collection(db_session)
    db_session.add(_season_work(coll.id, 1, start_date=date(2020, 1, 1)))
    sp = _season_work(coll.id, 0, manually_edited_fields=["start_date"])
    db_session.add(sp)
    await db_session.flush()
    await ms._update_series_from_entity(
        db_session, sp, {"content_type": "tv"},
        raw_source=None, canonical_id=None, granularity="series",
    )
    assert sp.start_date is None


# ---------------------------------------------------------------------------
# Coverage boost: mark_manually_edited / sniff / poster edge branches
# ---------------------------------------------------------------------------


def test_mark_manually_edited_records_editable_fields():
    work = TVSeries(id=_uuid(), title_en="X", content_type="tv")
    ms.mark_manually_edited(work, {"title_cn": "新标题", "rating": 9.0, "collection_id": "x"})
    assert set(work.manually_edited_fields) == {"title_cn", "rating"}


def test_mark_manually_edited_ignores_system_fields():
    work = TVSeries(id=_uuid(), title_en="X", content_type="tv")
    ms.mark_manually_edited(work, {"collection_id": "x", "search_text": "s"})
    assert work.manually_edited_fields is None


def test_sniff_image_ext_webp_and_gif():
    assert ms._sniff_image_ext(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "webp"
    assert ms._sniff_image_ext(b"GIF89a") == "gif"


def test_extract_search_title_empty_raw_returns_raw():
    r = SimpleNamespace(title_cn=None, title_en=None, title_raw="   ")
    assert ms.extract_search_title(r) == "   "


async def test_download_and_cache_poster_no_cache_dir(tmp_path):
    saved = ms.settings.poster_cache_dir
    ms.settings.poster_cache_dir = ""
    try:
        assert await ms.download_and_cache_poster("https://x/poster.jpg") is None
    finally:
        ms.settings.poster_cache_dir = saved


async def test_download_and_cache_poster_write_failure(tmp_path, monkeypatch):
    ms.settings.poster_cache_dir = str(tmp_path)

    async def _fake_to_thread(fn, *a, **kw):
        return fn()

    import asyncio
    import hashlib

    monkeypatch.setattr(asyncio, "to_thread", _fake_to_thread)

    class _Resp:
        content = b"\xff\xd8\xff\xe0" + b"\x00" * 32

        def raise_for_status(self):
            pass

    class _Client:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, **kw):
            return _Resp()

    import httpx
    monkeypatch.setattr(httpx, "Client", _Client)
    digest = hashlib.sha256(b"https://x/fail.jpg").hexdigest()[:16]
    from pathlib import Path

    real_write = Path.write_bytes

    def _fail_write(self, data):
        if self.name == f"{digest}.jpg":
            raise OSError("disk full")
        return real_write(self, data)

    monkeypatch.setattr(Path, "write_bytes", _fail_write)
    assert await ms.download_and_cache_poster("https://x/fail.jpg") is None


# ---------------------------------------------------------------------------
# Coverage boost: _parse_date edge branches
# ---------------------------------------------------------------------------


async def test_parse_date_datetime_and_time_string():
    from datetime import datetime

    assert ms._parse_date(datetime(2024, 5, 1, 12, 30)) == date(2024, 5, 1)
    assert ms._parse_date("2024-05-01T12:00:00") == date(2024, 5, 1)
    # long string whose prefix fails both formats -> except -> continue
    assert ms._parse_date("2024-13-99 99:99:99 extra") is None
    # "0000" is a 4-digit year but year 0 is out of range -> ValueError -> None
    assert ms._parse_date("0000") is None


# ---------------------------------------------------------------------------
# Coverage boost: _sniff / _identity_granularity / season helpers
# ---------------------------------------------------------------------------


def test_identity_granularity_synthetic_and_season_source():
    assert ms._identity_granularity("wikipedia", "wikipedia:zh:123#s2") == (
        "season", "wikipedia:zh:123", 2,
    )
    assert ms._identity_granularity("bangumi", None) == ("season", None, None)


def test_season_entry_none_season():
    assert ms._season_entry({}, None) is None


def test_season_episode_subset_none_season():
    assert ms._season_episode_subset({}, None) == []


def test_work_episode_count_from_subset():
    data = {
        "episode_list": [
            {"season": 2, "episode": 1},
            {"season": 2, "episode": 2},
            {"season": 1, "episode": 1},
        ]
    }
    assert ms._work_episode_count(data, 2, "series") == 2


def test_work_start_end_date_from_episode_dates():
    data = {
        "episode_list": [
            {"season": 1, "episode": 1, "air_date": "2020-04-01"},
            {"season": 1, "episode": 2, "air_date": "2020-04-08"},
        ]
    }
    assert ms._work_start_date(data, 1, "series") == date(2020, 4, 1)
    assert ms._work_end_date(data, 1, "series") == date(2020, 4, 8)


def test_work_end_date_from_season_entry():
    data = {"seasons": [{"season_number": 1, "end_date": "2020-06-30"}]}
    assert ms._work_end_date(data, 1, "series") == date(2020, 6, 30)


def test_title_season_from_entity_number_of_seasons_range():
    data = {"title_en": "Show III", "number_of_seasons": 3}
    assert ms._title_season_from_entity(data) == 3


def test_merge_primary_external_id_synthetic_same_season():
    assert ms._merge_primary_external_id("tmdb:82684#s4", "tmdb:82684#s4") == "tmdb:82684#s4"


def test_year_mismatch_undetectable_string_date():
    assert ms._year_mismatch(2026, "not-a-date") is False


async def test_find_collection_by_titles_no_titles(db_session):
    assert await ms._find_collection_by_titles(db_session, []) is None
    assert await ms._find_collection_by_titles(db_session, [" ", None]) is None


async def test_merge_collection_aliases_manual_edit_skipped(db_session):
    coll = WorkCollection(
        id=_uuid(), title_cn="合集", external_source="series_group",
        manually_edited_fields=["aliases"],
    )
    db_session.add(coll)
    await db_session.flush()
    ms._merge_collection_aliases(coll, {"title_cn": "合集", "title_en": "New Name"})
    assert coll.aliases is None


# ---------------------------------------------------------------------------
# Coverage boost: find_series/movie_by_external_id
# ---------------------------------------------------------------------------


async def test_find_series_by_external_id(db_session):
    s = TVSeries(
        id=_uuid(), title_en="X", content_type="tv",
        external_id="tmdb:123", external_source="tmdb",
    )
    db_session.add(s)
    await db_session.flush()
    found = await ms.find_series_by_external_id(db_session, {
        "external_source": "tmdb", "external_id": "tmdb:123",
    })
    assert found.id == s.id
    assert await ms.find_series_by_external_id(db_session, {"external_source": "tmdb"}) is None


async def test_find_movie_by_external_id(db_session):
    m = Movie(
        id=_uuid(), title_en="M", content_type="movie",
        external_id="tmdb:456", external_source="tmdb",
    )
    db_session.add(m)
    await db_session.flush()
    found = await ms.find_movie_by_external_id(db_session, {
        "external_source": "tmdb", "external_id": "tmdb:456",
    })
    assert found.id == m.id
    assert await ms.find_movie_by_external_id(db_session, {"external_source": "tmdb"}) is None


# ---------------------------------------------------------------------------
# Coverage boost: match_*_by_title empty/edge + audio + movie fuzzy
# ---------------------------------------------------------------------------


async def test_match_series_by_title_empty(db_session):
    assert await ms.match_series_by_title(db_session, "") == (None, 0)
    assert await ms.match_series_by_title(db_session, "  ") == (None, 0)


async def test_match_movie_by_title_empty(db_session):
    assert await ms.match_movie_by_title(db_session, "") == (None, 0)
    assert await ms.match_movie_by_title(db_session, "  ") == (None, 0)


async def test_match_movie_by_title_exact_and_fuzzy(db_session):
    m = Movie(id=_uuid(), title_en="Demon Slayer", content_type="movie")
    db_session.add(m)
    await db_session.flush()
    exact, score = await ms.match_movie_by_title(db_session, "Demon Slayer")
    assert exact.id == m.id and score == 100
    fuzzy, fscore = await ms.match_movie_by_title(db_session, "Demon Slayerr")
    assert fuzzy.id == m.id and fscore >= 70
    miss, mscore = await ms.match_movie_by_title(db_session, "Quantum Physics Explained")
    assert miss is None and mscore == 0


async def test_match_audio_work_by_title(db_session):
    from app.models.audio_work import AudioWork

    assert await ms.match_audio_work_by_title(db_session, "") == (None, 0)
    assert await ms.match_audio_work_by_title(db_session, "  ") == (None, 0)
    aw = AudioWork(id=_uuid(), title_en="Audio Show", content_type="music")
    db_session.add(aw)
    await db_session.flush()
    exact, score = await ms.match_audio_work_by_title(db_session, "Audio Show")
    assert exact.id == aw.id and score == 100
    fuzzy, fscore = await ms.match_audio_work_by_title(db_session, "Audio Showw")
    assert fuzzy.id == aw.id and fscore >= 70
    miss, mscore = await ms.match_audio_work_by_title(db_session, "Something Else Entirely")
    assert miss is None and mscore == 0


# ---------------------------------------------------------------------------
# Coverage boost: search_metadata_via_llm (real body, patched agent)
# ---------------------------------------------------------------------------


def _make_agent(result):
    from types import SimpleNamespace as _SimpleNS

    return _SimpleNS(process_title_only=AsyncMock(return_value=result))


async def test_search_metadata_via_llm_success(monkeypatch):
    from app.services.metadata_resource_meta import ResourceMetadata

    result = ResourceMetadata(
        clean_title="X", found=True,
        matched_entity={"content_type": "tv", "title_en": "X", "external_id": "tmdb:1", "external_source": "tmdb"},
    )
    monkeypatch.setattr("app.services.metadata_agent.get_agent", lambda: _make_agent(result))
    out = await ms.search_metadata_via_llm("X", "tmdb")
    assert out == [{"content_type": "tv", "title_en": "X", "external_id": "tmdb:1", "external_source": "tmdb"}]


async def test_search_metadata_via_llm_found_false_ambiguous(monkeypatch):
    from app.services.metadata_resource_meta import ResourceMetadata

    result = ResourceMetadata(
        clean_title="X", found=False, ambiguous=True,
        ambiguous_candidates=[{"content_type": "tv", "title_en": "Amb", "external_id": "tmdb:2", "external_source": "tmdb"}],
    )
    monkeypatch.setattr("app.services.metadata_agent.get_agent", lambda: _make_agent(result))
    out = await ms.search_metadata_via_llm("X")
    assert out == [{"content_type": "tv", "title_en": "Amb", "external_id": "tmdb:2", "external_source": "tmdb"}]


async def test_search_metadata_via_llm_not_found(monkeypatch):
    from app.services.metadata_resource_meta import ResourceMetadata

    result = ResourceMetadata(clean_title="X", found=False)
    monkeypatch.setattr("app.services.metadata_agent.get_agent", lambda: _make_agent(result))
    assert await ms.search_metadata_via_llm("X") == []


async def test_search_metadata_via_llm_ambiguous_lead(monkeypatch):
    from app.services.metadata_resource_meta import ResourceMetadata

    result = ResourceMetadata(
        clean_title="X", found=True, ambiguous=True,
        matched_entity={"content_type": "tv", "title_en": "X", "external_id": "tmdb:1", "external_source": "tmdb"},
        ambiguous_candidates=[{"content_type": "tv", "title_en": "Y", "external_id": "tmdb:2", "external_source": "tmdb"}],
    )
    monkeypatch.setattr("app.services.metadata_agent.get_agent", lambda: _make_agent(result))
    out = await ms.search_metadata_via_llm("X")
    assert out[0]["ambiguous"] is True
    assert len(out) == 2


async def test_search_metadata_via_llm_agent_exception(monkeypatch):
    agent = SimpleNamespace(process_title_only=AsyncMock(side_effect=RuntimeError("boom")))
    monkeypatch.setattr("app.services.metadata_agent.get_agent", lambda: agent)
    assert await ms.search_metadata_via_llm("X") == []


# ---------------------------------------------------------------------------
# Coverage boost: _bag_matched_entity_ids alt_external_ids
# ---------------------------------------------------------------------------


async def test_create_or_update_movie_bags_alt_external_ids(db_session):
    from sqlalchemy import select

    from app.models.work_external_id import WorkExternalId

    with patch(
        "app.services.metadata_service.download_and_cache_poster",
        new_callable=AsyncMock, return_value=None,
    ):
        m = await ms.create_or_update_movie_from_external(db_session, {
            "content_type": "movie", "title_en": "Bag Movie",
            "external_id": "tmdb:777", "external_source": "tmdb",
            "alt_external_ids": [
                {"source": "imdb", "id": "tt777"},
                "not-a-dict",
                42,
            ],
        })
    await db_session.flush()
    rows = (await db_session.execute(
        select(WorkExternalId).where(WorkExternalId.work_id == m.id)
    )).scalars().all()
    ids = {(r.source, r.external_id) for r in rows}
    assert ("tmdb", "tmdb:777") in ids
    assert ("imdb", "imdb:tt777") in ids


# ---------------------------------------------------------------------------
# Coverage boost: upsert_episodes
# ---------------------------------------------------------------------------


async def test_upsert_episodes_empty(db_session):
    s = TVSeries(id=_uuid(), title_en="X", content_type="tv")
    db_session.add(s)
    await db_session.flush()
    assert await ms.upsert_episodes(db_session, s, None) == 0
    assert await ms.upsert_episodes(db_session, s, [{"season": 1}]) == 0
    assert await ms.upsert_episodes(db_session, s, [{"episode": 1}]) == 0


async def test_upsert_episodes_per_season_retags(db_session):
    from sqlalchemy import select

    from app.models.episode import Episode

    coll = WorkCollection(id=_uuid(), title_cn="系列", external_source="series_group")
    s = TVSeries(id=_uuid(), title_en="X", content_type="tv", season_number=1, collection_id=coll.id)
    db_session.add_all([coll, s])
    await db_session.flush()
    n = await ms.upsert_episodes(db_session, s, [
        {"season": 3, "episode": 25, "title": "EP25"},
        {"season": 3, "episode": 26, "title": "EP26"},
    ], entity_granularity="season")
    assert n == 2
    rows = (await db_session.execute(
        select(Episode).where(Episode.series_id == s.id)
    )).scalars().all()
    assert {(r.season, r.episode) for r in rows} == {(1, 25), (1, 26)}


async def test_upsert_episodes_series_granularity_filters(db_session):
    from sqlalchemy import select

    from app.models.episode import Episode

    coll = WorkCollection(id=_uuid(), title_cn="系列", external_source="series_group")
    s = TVSeries(id=_uuid(), title_en="X", content_type="tv", season_number=2, collection_id=coll.id)
    db_session.add_all([coll, s])
    await db_session.flush()
    n = await ms.upsert_episodes(db_session, s, [
        {"season": 2, "episode": 1, "title": "S2E1"},
        {"season": 1, "episode": 10, "title": "S1E10"},
        {"season": 3, "episode": 5},
    ], entity_granularity="series")
    assert n == 1
    rows = (await db_session.execute(select(Episode))).scalars().all()
    assert [(r.season, r.episode) for r in rows] == [(2, 1)]


async def test_upsert_episodes_continuation_guard(db_session, caplog):
    import logging

    coll = WorkCollection(id=_uuid(), title_cn="系列", external_source="series_group")
    s = TVSeries(
        id=_uuid(), title_en="X", content_type="tv", season_number=1,
        collection_id=coll.id, number_of_episodes=12,
    )
    db_session.add_all([coll, s])
    await db_session.flush()
    with caplog.at_level(logging.WARNING, logger="app.services.metadata_service"):
        n = await ms.upsert_episodes(db_session, s, [
            {"season": 1, "episode": 25},
            {"season": 1, "episode": 26},
        ], entity_granularity="series")
    assert n == 0
    assert any("out-of-range episode" in r.message for r in caplog.records)


async def test_upsert_episodes_legacy_absorbs_all(db_session):
    s = TVSeries(
        id=_uuid(), title_en="X", content_type="tv",
        seasons=[{"season_number": 1, "episode_count": 12}, {"season_number": 2, "episode_count": 12}],
        number_of_seasons=2,
    )
    db_session.add(s)
    await db_session.flush()
    n = await ms.upsert_episodes(db_session, s, [
        {"season": 1, "episode": 1}, {"season": 2, "episode": 1},
    ], entity_granularity="series")
    assert n == 2


async def test_upsert_episodes_updates_existing(db_session):
    from sqlalchemy import select

    from app.models.episode import Episode

    s = TVSeries(id=_uuid(), title_en="X", content_type="tv", season_number=1)
    db_session.add(s)
    await db_session.flush()
    await ms.upsert_episodes(db_session, s, [{"season": 1, "episode": 1, "title": "A"}])
    await ms.upsert_episodes(db_session, s, [{"season": 1, "episode": 1, "title": "B", "air_date": "2024-01-01"}])
    row = (await db_session.execute(
        select(Episode).where(Episode.series_id == s.id)
    )).scalars().first()
    assert row.title == "B"
    assert row.air_date == date(2024, 1, 1)


# ---------------------------------------------------------------------------
# Coverage boost: series upsert collection/season resolution paths
# ---------------------------------------------------------------------------


async def test_series_upsert_resolves_collection_member_from_bag(db_session):
    from app.services.external_ids import add_external_id

    coll = WorkCollection(id=_uuid(), title_cn="系列合集", external_source="series_group")
    s1 = TVSeries(id=_uuid(), title_cn="剧集", content_type="tv", season_number=1, collection_id=coll.id)
    db_session.add_all([coll, s1])
    await db_session.flush()
    await add_external_id(db_session, "collection", coll.id, "tmdb", "tmdb:555")
    with patch(
        "app.services.metadata_service.download_and_cache_poster",
        new_callable=AsyncMock, return_value=None,
    ):
        work = await ms.create_or_update_series_from_external(db_session, {
            "content_type": "tv", "title_cn": "剧集", "title_en": "Show",
            "external_id": "tmdb:555", "external_source": "tmdb",
            "number_of_seasons": 2,
            "seasons": [{"season_number": 1, "episode_count": 12}, {"season_number": 2, "episode_count": 12}],
        }, season_hint=1)
    assert work.id == s1.id
    assert work.title_en == "Show"


async def test_series_upsert_parking_on_multi_member_collection(db_session):
    from app.services.external_ids import add_external_id

    coll = WorkCollection(id=_uuid(), title_cn="多季合集", external_source="series_group")
    db_session.add(coll)
    await db_session.flush()
    await add_external_id(db_session, "collection", coll.id, "tmdb", "tmdb:666")
    db_session.add_all([
        TVSeries(id=_uuid(), title_cn="剧A", content_type="tv", season_number=1, collection_id=coll.id),
        TVSeries(id=_uuid(), title_cn="剧B", content_type="tv", season_number=2, collection_id=coll.id),
    ])
    await db_session.flush()
    with patch(
        "app.services.metadata_service.download_and_cache_poster",
        new_callable=AsyncMock, return_value=None,
    ):
        work = await ms.create_or_update_series_from_external(db_session, {
            "content_type": "tv", "title_cn": "剧A", "title_en": "Show",
            "external_id": "tmdb:666", "external_source": "tmdb",
            "number_of_seasons": 2,
        })
    assert work is None


async def test_series_upsert_creates_season_1_when_single_season_evidence(db_session):
    from app.services.external_ids import add_external_id

    coll = WorkCollection(id=_uuid(), title_cn="单季合集", external_source="series_group")
    db_session.add(coll)
    await db_session.flush()
    await add_external_id(db_session, "collection", coll.id, "tmdb", "tmdb:777")
    with patch(
        "app.services.metadata_service.download_and_cache_poster",
        new_callable=AsyncMock, return_value=None,
    ):
        work = await ms.create_or_update_series_from_external(db_session, {
            "content_type": "tv", "title_cn": "剧A", "title_en": "Show",
            "external_id": "tmdb:777", "external_source": "tmdb",
            "number_of_seasons": 1,
        })
    assert work is not None
    assert work.season_number == 1
    assert work.collection_id == coll.id


async def test_series_upsert_synthetic_season_bag_lookup(db_session):
    from app.services.external_ids import add_external_id

    coll = WorkCollection(id=_uuid(), title_cn="系列", external_source="series_group")
    s = TVSeries(
        id=_uuid(), title_en="Show", content_type="tv", season_number=1,
        collection_id=coll.id, external_id="tmdb:82684#s1", external_source="tmdb",
    )
    db_session.add_all([coll, s])
    await db_session.flush()
    await add_external_id(db_session, "series", s.id, "tmdb", "tmdb:82684#s1")
    with patch(
        "app.services.metadata_service.download_and_cache_poster",
        new_callable=AsyncMock, return_value=None,
    ):
        out = await ms.create_or_update_series_from_external(db_session, {
            "content_type": "tv", "title_en": "Show",
            "external_id": "tmdb:82684", "external_source": "tmdb",
        }, season_hint=1)
    assert out.id == s.id


async def test_series_upsert_title_candidate_exact_season(db_session):
    coll = WorkCollection(id=_uuid(), title_cn="系列", external_source="series_group")
    s1 = TVSeries(
        id=_uuid(), title_cn="剧集", content_type="tv", season_number=1,
        collection_id=coll.id, external_source="manual",
    )
    db_session.add_all([coll, s1])
    await db_session.flush()
    with patch(
        "app.services.metadata_service.download_and_cache_poster",
        new_callable=AsyncMock, return_value=None,
    ):
        work = await ms.create_or_update_series_from_external(db_session, {
            "content_type": "tv", "title_cn": "剧集", "title_en": "Show",
            "external_source": "llm_search", "external_id": "wikipedia:999",
            "number_of_seasons": 2,
            "seasons": [{"season_number": 1, "episode_count": 12}],
        }, season_hint=1)
    assert work.id == s1.id


async def test_series_upsert_title_candidate_creates_missing_season(db_session):
    coll = WorkCollection(id=_uuid(), title_cn="系列", external_source="series_group")
    s1 = TVSeries(
        id=_uuid(), title_cn="剧集", content_type="tv", season_number=1,
        collection_id=coll.id, external_source="manual",
    )
    db_session.add_all([coll, s1])
    await db_session.flush()
    with patch(
        "app.services.metadata_service.download_and_cache_poster",
        new_callable=AsyncMock, return_value=None,
    ):
        work = await ms.create_or_update_series_from_external(db_session, {
            "content_type": "tv", "title_cn": "剧集", "title_en": "Show",
            "external_source": "llm_search", "external_id": "wikipedia:999",
            "number_of_seasons": 2,
            "seasons": [
                {"season_number": 1, "episode_count": 12},
                {"season_number": 2, "episode_count": 12},
            ],
            "episode_list": [{"season": 2, "episode": 1, "title": "S2E1"}],
        }, season_hint=2)
    assert work is not None
    assert work.season_number == 2
    assert work.collection_id == coll.id


async def test_series_upsert_parks_on_shared_collection(db_session):
    coll = WorkCollection(id=_uuid(), title_cn="系列", external_source="series_group")
    db_session.add_all([
        coll,
        TVSeries(id=_uuid(), title_cn="剧集", content_type="tv", season_number=1, collection_id=coll.id, external_source="manual"),
        TVSeries(id=_uuid(), title_cn="剧集", content_type="tv", season_number=2, collection_id=coll.id, external_source="manual"),
    ])
    await db_session.flush()
    with patch(
        "app.services.metadata_service.download_and_cache_poster",
        new_callable=AsyncMock, return_value=None,
    ):
        work = await ms.create_or_update_series_from_external(db_session, {
            "content_type": "tv", "title_cn": "剧集", "title_en": "Show",
            "external_source": "llm_search", "external_id": "wikipedia:999",
            "number_of_seasons": 2,
        })
    assert work is None


async def test_series_upsert_multi_candidate_no_shared_collection(db_session):
    s1 = TVSeries(id=_uuid(), title_cn="剧集", content_type="tv", season_number=1, external_source="manual")
    db_session.add(s1)
    await db_session.flush()
    db_session.add(TVSeries(id=_uuid(), title_cn="剧集", content_type="tv", season_number=2, external_source="manual"))
    await db_session.flush()
    with patch(
        "app.services.metadata_service.download_and_cache_poster",
        new_callable=AsyncMock, return_value=None,
    ):
        work = await ms.create_or_update_series_from_external(db_session, {
            "content_type": "tv", "title_cn": "剧集",
            "external_source": "llm_search", "external_id": "wikipedia:999",
            "number_of_seasons": 2,
        })
    assert work is not None
    assert work.season_number == 1


# ---------------------------------------------------------------------------
# Coverage boost: find_collection_for_entity / locate_absolute_episode
# ---------------------------------------------------------------------------


async def test_find_collection_for_entity_via_bag(db_session):
    from app.services.external_ids import add_external_id

    coll = WorkCollection(id=_uuid(), title_cn="系列", external_source="series_group")
    db_session.add(coll)
    await db_session.flush()
    await add_external_id(db_session, "collection", coll.id, "tmdb", "tmdb:888")
    out = await ms.find_collection_for_entity(db_session, {
        "content_type": "tv", "title_en": "Show",
        "external_id": "tmdb:888", "external_source": "tmdb",
    })
    assert out.id == coll.id


async def test_locate_absolute_episode_in_collection(db_session):
    coll = WorkCollection(id=_uuid(), title_cn="系列", external_source="series_group")
    s1 = TVSeries(
        id=_uuid(), title_cn="A", content_type="tv", season_number=1,
        collection_id=coll.id, number_of_episodes=12,
    )
    s2 = TVSeries(
        id=_uuid(), title_cn="B", content_type="tv", season_number=2,
        collection_id=coll.id, number_of_episodes=12,
    )
    s3 = TVSeries(id=_uuid(), title_cn="C", content_type="tv", season_number=3, collection_id=coll.id)
    db_session.add_all([coll, s1, s2, s3])
    await db_session.flush()
    assert await ms.locate_absolute_episode_in_collection(db_session, coll.id, None) is None
    assert await ms.locate_absolute_episode_in_collection(db_session, coll.id, 0) is None
    member, ep = await ms.locate_absolute_episode_in_collection(db_session, coll.id, 15)
    assert (member.id, ep) == (s2.id, 3)
    # walking into the count-less season aborts the cumulative walk
    assert await ms.locate_absolute_episode_in_collection(db_session, coll.id, 30) is None
    assert await ms.locate_absolute_episode_in_collection(db_session, coll.id, 999) is None


async def test_locate_absolute_episode_tolerance_on_last_member(db_session):
    coll = WorkCollection(id=_uuid(), title_cn="系列", external_source="series_group")
    s1 = TVSeries(
        id=_uuid(), title_cn="A", content_type="tv", season_number=1,
        collection_id=coll.id, number_of_episodes=12,
    )
    s2 = TVSeries(
        id=_uuid(), title_cn="B", content_type="tv", season_number=2,
        collection_id=coll.id, number_of_episodes=12,
    )
    db_session.add_all([coll, s1, s2])
    await db_session.flush()
    # absolute 26 lands in the reconcile-tolerance headroom of the LAST member
    member, ep = await ms.locate_absolute_episode_in_collection(db_session, coll.id, 26)
    assert (member.id, ep) == (s2.id, 14)
    assert await ms.locate_absolute_episode_in_collection(db_session, coll.id, 27) is None


# ---------------------------------------------------------------------------
# Coverage boost: reconcile_linked_series_resource branches
# ---------------------------------------------------------------------------


async def test_reconcile_linked_series_resource_missing_series(db_session, channel):
    res = SimpleNamespace(
        id=_uuid(), channel_id=channel.id, series_id=_uuid(),
        season=None, absolute_episode=None, episode_confidence=None, is_batch=False,
    )
    await ms.reconcile_linked_series_resource(db_session, res)
    assert res.series_id is not None  # untouched


async def test_reconcile_linked_series_resource_locates_absolute(db_session, channel):
    coll = WorkCollection(id=_uuid(), title_cn="系列", external_source="series_group")
    s1 = TVSeries(
        id=_uuid(), title_cn="A", content_type="tv", season_number=1,
        collection_id=coll.id, number_of_episodes=12,
    )
    s2 = TVSeries(
        id=_uuid(), title_cn="B", content_type="tv", season_number=2,
        collection_id=coll.id, number_of_episodes=12,
    )
    db_session.add_all([coll, s1, s2])
    await db_session.flush()
    res = _resource(channel.id, series_id=s1.id, season=None, episode=None, absolute_episode=15)
    db_session.add(res)
    await db_session.flush()
    await ms.reconcile_linked_series_resource(db_session, res, series=s1)
    assert res.series_id == s2.id
    assert res.season == 2
    assert res.episode == 3
    assert res.episode_confidence == "reconciled"


async def test_reconcile_linked_series_resource_seasons_map(db_session, channel):
    s = TVSeries(id=_uuid(), title_cn="剧", content_type="tv", season_number=1, number_of_episodes=24)
    db_session.add(s)
    await db_session.flush()
    res = _resource(channel.id, series_id=s.id, season=1, episode=1, episode_confidence="raw")
    db_session.add(res)
    await db_session.flush()
    await ms.reconcile_linked_series_resource(db_session, res, series=s)
    assert res.episode_confidence == "raw"


# ---------------------------------------------------------------------------
# Coverage boost: _update_series_from_entity field population
# ---------------------------------------------------------------------------


async def test_update_series_from_entity_populates_fields(db_session):
    coll = WorkCollection(id=_uuid(), title_cn="系列", external_source="series_group")
    s = TVSeries(id=_uuid(), title_cn="剧", content_type="tv", season_number=1, collection_id=coll.id)
    db_session.add_all([coll, s])
    await db_session.flush()
    with patch(
        "app.services.metadata_service.download_and_cache_poster",
        new_callable=AsyncMock, return_value="/posters/x.jpg",
    ):
        await ms._update_series_from_entity(db_session, s, {
            "content_type": "tv",
            "title_cn": "剧",
            "wikipedia_url": "https://zh.wikipedia.org/wiki/X",
            "description": "desc",
            "rating": 8.5,
            "original_title": "OT",
            "status": "airing",
            "number_of_episodes": 12,
            "seasons": [{"season_number": 1, "episode_count": 12}],
            "start_date": "2024-01-05",
            "end_date": "2024-03-30",
            "genre": ["Animation", "Drama"],
            "poster_url": "https://x/poster.jpg",
            "episode_list": [{"season": 1, "episode": 1, "title": "EP1"}],
        }, raw_source="tmdb", canonical_id="tmdb:999", granularity="series")
    await db_session.flush()
    assert s.wikipedia_url == "https://zh.wikipedia.org/wiki/X"
    assert s.rating == 8.5
    assert s.number_of_episodes == 12
    assert s.start_date == date(2024, 1, 5)
    assert s.end_date == date(2024, 3, 30)
    assert s.genre == ["Animation", "Drama"]
    assert s.poster_url == "/posters/x.jpg"
    from sqlalchemy import select

    from app.models.episode import Episode

    ep = (await db_session.execute(select(Episode).where(Episode.series_id == s.id))).scalars().first()
    assert ep.episode == 1


async def test_update_series_from_entity_legacy_unsplit(db_session):
    s = TVSeries(
        id=_uuid(), title_cn="剧", content_type="tv",
        seasons=[{"season_number": 1, "episode_count": 12}, {"season_number": 2, "episode_count": 12}],
        number_of_seasons=2,
    )
    db_session.add(s)
    await db_session.flush()
    with patch(
        "app.services.metadata_service.download_and_cache_poster",
        new_callable=AsyncMock, return_value=None,
    ):
        await ms._update_series_from_entity(db_session, s, {
            "content_type": "tv", "title_cn": "剧",
            "number_of_episodes": 24, "start_date": "2023-01-01", "end_date": "2023-12-31",
        }, raw_source="tmdb", canonical_id="tmdb:1000", granularity="series")
    await db_session.flush()
    assert s.number_of_episodes == 24
    assert s.start_date == date(2023, 1, 1)
    assert s.end_date == date(2023, 12, 31)


# ---------------------------------------------------------------------------
# Coverage boost: movie / audio upsert update branches
# ---------------------------------------------------------------------------


async def test_movie_upsert_update_wikipedia_and_poster(db_session):
    m = Movie(
        id=_uuid(), title_en="M", content_type="movie",
        external_id="tmdb:300", external_source="tmdb",
    )
    db_session.add(m)
    await db_session.flush()
    with patch(
        "app.services.metadata_service.download_and_cache_poster",
        new_callable=AsyncMock, return_value="/posters/m.jpg",
    ):
        out = await ms.create_or_update_movie_from_external(db_session, {
            "content_type": "movie", "title_en": "M",
            "external_id": "tmdb:300", "external_source": "tmdb",
            "wikipedia_url": "https://zh.wikipedia.org/wiki/M",
            "poster_url": "https://x/m.jpg",
            "rating": 8.0, "original_title": "OT", "status": "released",
            "release_date": "2024-05-01", "runtime": 100,
            "genre": ["Drama"],
        })
    assert out.wikipedia_url == "https://zh.wikipedia.org/wiki/M"
    assert out.poster_url == "/posters/m.jpg"


async def test_audio_work_update_populates_fields(db_session):
    from app.models.audio_work import AudioWork

    aw = AudioWork(
        id=_uuid(), title_cn="音声", content_type="asmr",
        external_id="tmdb:400", external_source="tmdb",
    )
    db_session.add(aw)
    await db_session.flush()
    with patch(
        "app.services.metadata_service.download_and_cache_poster",
        new_callable=AsyncMock, return_value="/posters/a.jpg",
    ):
        out = await ms.create_or_update_audio_work_from_external(db_session, {
            "external_id": "tmdb:400", "external_source": "tmdb",
            "description": "d", "rating": 8.0, "original_title": "OT", "status": "s",
            "release_date": "2024-01-01", "runtime": 60, "genre": ["Music"],
            "title_cn": "新音声", "title_en": "Audio Title",
            "content_type": "music",
            "poster_url": "https://x/a.jpg",
        })
    assert out.id == aw.id
    assert out.title_cn == "音声"  # existing title preserved (only fills null)
    assert out.title_en == "Audio Title"
    assert out.rating == 8.0
    assert out.release_date == date(2024, 1, 1)
    assert out.runtime == 60
    assert out.genre == ["Music"]
    assert out.content_type == "music"
    assert out.poster_url == "/posters/a.jpg"


# ---------------------------------------------------------------------------
# Coverage boost: select_channel_works_for_refresh
# ---------------------------------------------------------------------------


async def test_select_channel_works_for_refresh_gates_gaps(db_session, channel):
    complete = TVSeries(
        id=_uuid(), title_cn="剧", title_en="Show", original_title="Show",
        description="d", rating=8.0, status="airing", genre=["Drama"],
        poster_url="/posters/x.jpg", number_of_episodes=12,
        start_date=date(2024, 1, 1), end_date=date(2024, 3, 1),
        external_id="tmdb:1", external_source="tmdb", content_type="tv",
    )
    gap = Movie(id=_uuid(), title_cn="影", content_type="movie")
    db_session.add_all([complete, gap])
    await db_session.flush()
    db_session.add_all([
        _resource(channel.id, series_id=complete.id),
        _resource(channel.id, movie_id=gap.id),
    ])
    await db_session.flush()
    gated = await ms.select_channel_works_for_refresh(db_session, channel.id, full_scope=False)
    assert gated == [{"id": gap.id, "content_type": "movie"}]
    full = await ms.select_channel_works_for_refresh(db_session, channel.id, full_scope=True)
    assert len(full) == 2


# ---------------------------------------------------------------------------
# Coverage boost: fetch_and_link_metadata remaining branches
# ---------------------------------------------------------------------------


async def test_fetch_and_link_no_search_title_records_not_found(db_session, channel):
    res = _resource(channel.id, title_raw="", search_title=None, title_cn=None, title_en=None)
    db_session.add(res)
    await db_session.flush()
    await ms.fetch_and_link_metadata(db_session, res, channel)
    assert res.metadata_failure_type == "not_found"
    assert res.metadata_attempts == 1


async def test_local_exact_match_autolinks_movie(db_session, channel):
    m = Movie(id=_uuid(), title_cn="电影名", content_type="movie")
    db_session.add(m)
    await db_session.flush()
    res = _resource(channel.id, search_title="电影名")
    db_session.add(res)
    await db_session.flush()
    await ms.fetch_and_link_metadata(db_session, res, channel)
    assert res.movie_id == m.id
    assert res.metadata_matched_at is not None


async def test_fetch_and_link_layer4_search_exception_transient(db_session, channel):
    channel.metadata_agent_enabled = True
    res = _resource(channel.id, search_title="unknown show abc", title_cn=None, title_en=None)
    db_session.add(res)
    await db_session.flush()
    with patch(
        "app.services.metadata_service.search_metadata_via_llm",
        side_effect=RuntimeError("boom"),
    ):
        await ms.fetch_and_link_metadata(db_session, res, channel)
    assert res.metadata_failure_type == "transient"


async def test_fetch_and_link_layer4_links_movie(db_session, channel):
    channel.metadata_agent_enabled = True
    res = _resource(channel.id, search_title="Movie ABC 2024", title_cn=None, title_en=None)
    db_session.add(res)
    await db_session.flush()
    fake = [{
        "content_type": "movie", "title_en": "Movie ABC",
        "external_id": "tmdb:55555", "external_source": "tmdb",
    }]
    with patch(
        "app.services.metadata_service.search_metadata_via_llm",
        new_callable=AsyncMock, return_value=fake,
    ), patch(
        "app.services.metadata_service.download_and_cache_poster",
        new_callable=AsyncMock, return_value=None,
    ), patch(
        "app.services.collection_service.link_movie_collection",
        new_callable=AsyncMock, return_value=None,
    ):
        await ms.fetch_and_link_metadata(db_session, res, channel)
    assert res.movie_id is not None
    assert res.metadata_matched_at is not None


async def test_fetch_and_link_layer4_parks_on_collection(db_session, channel):
    channel.metadata_agent_enabled = True
    res = _resource(channel.id, search_title="Multi Season Show", title_cn=None, title_en=None)
    db_session.add(res)
    await db_session.flush()
    fake = [{
        "content_type": "tv", "title_en": "Multi Season Show",
        "external_id": "tmdb:77777", "external_source": "tmdb",
        "number_of_seasons": 3,
    }]
    with patch(
        "app.services.metadata_service.search_metadata_via_llm",
        new_callable=AsyncMock, return_value=fake,
    ), patch(
        "app.services.metadata_service.download_and_cache_poster",
        new_callable=AsyncMock, return_value=None,
    ):
        await ms.fetch_and_link_metadata(db_session, res, channel)
    assert res.series_id is None
    assert res.collection_id is not None
    assert res.episode_confidence == "ambiguous"


async def test_fetch_and_link_layer4_park_collection_not_found(db_session, channel):
    channel.metadata_agent_enabled = True
    res = _resource(channel.id, search_title="Some Show", title_cn=None, title_en=None)
    db_session.add(res)
    await db_session.flush()
    fake = [{
        "content_type": "tv", "title_en": None,
        "external_source": "llm_search", "external_id": "abc-123",
        "number_of_seasons": 3,
    }]
    with patch(
        "app.services.metadata_service.search_metadata_via_llm",
        new_callable=AsyncMock, return_value=fake,
    ), patch(
        "app.services.metadata_service.download_and_cache_poster",
        new_callable=AsyncMock, return_value=None,
    ):
        await ms.fetch_and_link_metadata(db_session, res, channel)
    assert res.metadata_failure_type == "not_found"
    assert res.series_id is None


async def test_fetch_and_link_layer4_upsert_exception_transient(db_session, channel):
    channel.metadata_agent_enabled = True
    res = _resource(channel.id, search_title="Boom Show", title_cn=None, title_en=None)
    db_session.add(res)
    await db_session.flush()
    fake = [{
        "content_type": "tv", "title_en": "Boom Show",
        "external_source": "tmdb", "external_id": "tmdb:424242",
    }]
    with patch(
        "app.services.metadata_service.search_metadata_via_llm",
        new_callable=AsyncMock, return_value=fake,
    ), patch(
        "app.services.metadata_service.create_or_update_series_from_external",
        side_effect=RuntimeError("boom"),
    ):
        await ms.fetch_and_link_metadata(db_session, res, channel)
    assert res.metadata_failure_type == "transient"


# ---------------------------------------------------------------------------
# Coverage boost: manual_search_metadata local + normalization
# ---------------------------------------------------------------------------


async def test_manual_search_metadata_local_tv(db_session):
    s = TVSeries(id=_uuid(), title_en="Local Show", content_type="tv")
    db_session.add(s)
    await db_session.flush()
    out = await ms.manual_search_metadata(db_session, "Local Show", "tv", data_source_type="local")
    assert len(out) == 1
    assert out[0]["_local_id"] == s.id
    assert out[0]["content_type"] == "tv"


async def test_manual_search_metadata_local_movie(db_session):
    m = Movie(id=_uuid(), title_en="Local Film", content_type="movie")
    db_session.add(m)
    await db_session.flush()
    out = await ms.manual_search_metadata(db_session, "Local Film", "movie", data_source_type="local")
    assert len(out) == 1
    assert out[0]["content_type"] == "movie"


async def test_manual_search_metadata_llm_normalizes_and_prefers(db_session):
    fake = [
        {"content_type": "tv", "title_en": "A"},
        "not-a-dict",
        {"content_type": None, "title_en": "B"},
    ]
    with patch(
        "app.services.metadata_service.search_metadata_via_llm",
        new_callable=AsyncMock, return_value=fake,
    ):
        out = await ms.manual_search_metadata(db_session, "query", "tv")
    assert len(out) == 2
    assert out[0]["content_type"] == "tv"
    assert out[1]["content_type"] == "tv"


async def test_manual_search_metadata_llm_no_preference(db_session):
    fake = [{"content_type": "tv", "title_en": "A"}]
    with patch(
        "app.services.metadata_service.search_metadata_via_llm",
        new_callable=AsyncMock, return_value=fake,
    ):
        out = await ms.manual_search_metadata(db_session, "query", "audio")
    assert len(out) == 1
    assert out[0]["content_type"] == "tv"


# ---------------------------------------------------------------------------
# Coverage boost: invalidate_metadata_cache_for_external_id
# ---------------------------------------------------------------------------


async def test_invalidate_metadata_cache_for_external_id(db_session):
    from app.models.metadata_cache import MetadataCache

    assert await ms.invalidate_metadata_cache_for_external_id(db_session, None) == 0
    assert await ms.invalidate_metadata_cache_for_external_id(db_session, "") == 0
    db_session.add_all([
        MetadataCache(
            id=_uuid(), title="t1", source="metadata_agent:tmdb",
            metadata_json={"matched_entity": {"external_id": "tmdb:999"}},
        ),
        MetadataCache(
            id=_uuid(), title="t2", source="metadata_agent:tmdb",
            metadata_json={"matched_entity": {"external_id": "tmdb:888"}},
        ),
        MetadataCache(id=_uuid(), title="t3", source="metadata_agent:tmdb", metadata_json={}),
    ])
    await db_session.flush()
    assert await ms.invalidate_metadata_cache_for_external_id(db_session, "tmdb:000") == 0
    assert await ms.invalidate_metadata_cache_for_external_id(db_session, "tmdb:999") == 1


# ---------------------------------------------------------------------------
# Coverage boost: manual_link_metadata stale-ambiguous + raw_title key
# ---------------------------------------------------------------------------


async def test_manual_link_movie_clears_stale_ambiguous(db_session, channel):
    res = _resource(channel.id, title_raw="[G] Some Movie", episode_confidence="ambiguous")
    db_session.add(res)
    await db_session.flush()
    selected = {
        "content_type": "movie", "title_en": "Some Movie",
        "external_id": "tmdb:8888", "external_source": "tmdb",
    }
    with patch(
        "app.services.metadata_service.download_and_cache_poster",
        new_callable=AsyncMock, return_value=None,
    ), patch(
        "app.services.collection_service.link_movie_collection",
        new_callable=AsyncMock, return_value=None,
    ):
        await ms.manual_link_metadata(db_session, res, channel, selected)
    assert res.episode_confidence is None


async def test_manual_link_metadata_raw_title_key_update(db_session, channel):
    mapping = ChannelRawTitleMapping(
        id=_uuid(), channel_id=channel.id, raw_title="", search_title_key="",
        content_type="movie", movie_id=None,
    )
    db_session.add(mapping)
    await db_session.flush()
    res = _resource(channel.id, title_raw="", title_cn=None, title_en=None)
    db_session.add(res)
    await db_session.flush()
    selected = {
        "content_type": "movie", "title_en": "No Title",
        "external_id": "tmdb:7777", "external_source": "tmdb",
    }
    with patch(
        "app.services.metadata_service.download_and_cache_poster",
        new_callable=AsyncMock, return_value=None,
    ), patch(
        "app.services.collection_service.link_movie_collection",
        new_callable=AsyncMock, return_value=None,
    ):
        entity = await ms.manual_link_metadata(db_session, res, channel, selected)
    assert mapping.movie_id == entity.id
    assert mapping.content_type == "movie"


# ---------------------------------------------------------------------------
# Coverage boost: apply_channel_default_is_anime + bangumi verification
# ---------------------------------------------------------------------------


async def test_apply_channel_default_is_anime_series(db_session, channel):
    channel.default_is_anime = True
    s = TVSeries(id=_uuid(), title_en="X", content_type="tv")
    db_session.add(s)
    await db_session.flush()
    res = _resource(channel.id, series_id=s.id)
    db_session.add(res)
    await db_session.flush()
    await ms.apply_channel_default_is_anime(db_session, channel, res)
    assert s.is_anime is True


async def test_apply_channel_default_is_anime_movie(db_session, channel):
    channel.default_is_anime = True
    m = Movie(id=_uuid(), title_en="X", content_type="movie")
    db_session.add(m)
    await db_session.flush()
    res = _resource(channel.id, movie_id=m.id)
    db_session.add(res)
    await db_session.flush()
    await ms.apply_channel_default_is_anime(db_session, channel, res)
    assert m.is_anime is True


async def test_apply_channel_default_is_anime_sticky(db_session, channel):
    channel.default_is_anime = True
    s = TVSeries(id=_uuid(), title_en="X", content_type="tv", is_anime=True)
    db_session.add(s)
    await db_session.flush()
    res = _resource(channel.id, series_id=s.id)
    db_session.add(res)
    await db_session.flush()
    await ms.apply_channel_default_is_anime(db_session, channel, res)
    assert s.is_anime is True


async def test_verify_is_anime_via_bangumi_skipped_when_default_flag(db_session, channel):
    channel.default_is_anime = True
    s = TVSeries(id=_uuid(), title_en="Anime Show", content_type="tv")
    db_session.add(s)
    await db_session.flush()
    res = _resource(channel.id, series_id=s.id)
    db_session.add(res)
    await db_session.flush()
    await ms.maybe_verify_is_anime_via_bangumi(db_session, channel, res)
    assert s.is_anime is None


async def test_verify_is_anime_via_bangumi_sets_verdict(db_session, channel, monkeypatch):
    s = TVSeries(id=_uuid(), title_cn="某动画", content_type="tv")
    db_session.add(s)
    await db_session.flush()
    res = _resource(channel.id, series_id=s.id)
    db_session.add(res)
    await db_session.flush()
    monkeypatch.setattr("app.services.bangumi_client.bangumi_configured", lambda: True)

    async def fake_search(client, query):
        return [{"name": "某动画", "name_cn": "某动画", "date": "2024-04-01", "type": 2}]

    monkeypatch.setattr("app.services.bangumi_client.search_subjects", fake_search)
    await ms.maybe_verify_is_anime_via_bangumi(db_session, channel, res)
    assert s.is_anime is True


async def test_verify_is_anime_via_bangumi_movie_branch(db_session, channel, monkeypatch):
    m = Movie(
        id=_uuid(), title_en="Some Movie", content_type="movie",
        release_date=date(2023, 6, 1),
    )
    db_session.add(m)
    await db_session.flush()
    res = _resource(channel.id, movie_id=m.id)
    db_session.add(res)
    await db_session.flush()
    monkeypatch.setattr("app.services.bangumi_client.bangumi_configured", lambda: True)

    async def fake_search(client, query):
        return [{"name": "Some Movie", "name_cn": None, "date": "2023-06-01", "type": 6}]

    monkeypatch.setattr("app.services.bangumi_client.search_subjects", fake_search)
    await ms.maybe_verify_is_anime_via_bangumi(db_session, channel, res)
    assert m.is_anime is False


async def test_verify_is_anime_via_bangumi_skips_manually_edited(db_session, channel, monkeypatch):
    s = TVSeries(
        id=_uuid(), title_cn="某动画", content_type="tv",
        manually_edited_fields=["is_anime"],
    )
    db_session.add(s)
    await db_session.flush()
    res = _resource(channel.id, series_id=s.id)
    db_session.add(res)
    await db_session.flush()
    monkeypatch.setattr("app.services.bangumi_client.bangumi_configured", lambda: True)
    called = False

    async def fake_search(client, query):
        nonlocal called
        called = True
        return []

    monkeypatch.setattr("app.services.bangumi_client.search_subjects", fake_search)
    await ms.maybe_verify_is_anime_via_bangumi(db_session, channel, res)
    assert called is False
    assert s.is_anime is None


async def test_verify_is_anime_via_bangumi_no_match(db_session, channel, monkeypatch):
    s = TVSeries(id=_uuid(), title_cn="某动画", title_en="Anime", content_type="tv")
    db_session.add(s)
    await db_session.flush()
    res = _resource(channel.id, series_id=s.id)
    db_session.add(res)
    await db_session.flush()
    monkeypatch.setattr("app.services.bangumi_client.bangumi_configured", lambda: True)

    async def fake_search(client, query):
        return []

    monkeypatch.setattr("app.services.bangumi_client.search_subjects", fake_search)
    await ms.maybe_verify_is_anime_via_bangumi(db_session, channel, res)
    assert s.is_anime is None


async def test_verify_is_anime_via_bangumi_exception_handled(db_session, channel, monkeypatch):
    s = TVSeries(id=_uuid(), title_cn="某动画", content_type="tv")
    db_session.add(s)
    await db_session.flush()
    res = _resource(channel.id, series_id=s.id)
    db_session.add(res)
    await db_session.flush()
    monkeypatch.setattr("app.services.bangumi_client.bangumi_configured", lambda: True)

    async def fake_search(client, query):
        raise RuntimeError("network down")

    monkeypatch.setattr("app.services.bangumi_client.search_subjects", fake_search)
    await ms.maybe_verify_is_anime_via_bangumi(db_session, channel, res)
    assert s.is_anime is None


# ---------------------------------------------------------------------------
# Coverage boost: remaining defensive branches
# ---------------------------------------------------------------------------


async def test_find_same_title_works_blank(db_session):
    assert await ms._find_same_title_works(db_session, "   ") == []
    assert await ms._find_same_title_works(db_session, "") == []


async def test_series_upsert_bags_alt_ids_and_synthetic(db_session):
    from sqlalchemy import select

    from app.models.work_external_id import WorkExternalId

    with patch(
        "app.services.metadata_service.download_and_cache_poster",
        new_callable=AsyncMock, return_value=None,
    ):
        work = await ms.create_or_update_series_from_external(db_session, {
            "content_type": "tv", "title_cn": "剧", "title_en": "Show",
            "external_id": "tmdb:82684#s2", "external_source": "tmdb",
            "alt_external_ids": [{"source": "imdb", "id": "tt999"}],
        })
    await db_session.flush()
    assert work.season_number == 2
    rows = (await db_session.execute(select(WorkExternalId))).scalars().all()
    by = {(r.work_type, r.source, r.external_id) for r in rows}
    assert ("series", "tmdb", "tmdb:82684#s2") in by
    assert ("collection", "imdb", "imdb:tt999") in by


async def test_series_upsert_creates_season_1_unverified_empty_collection(db_session):
    from app.services.external_ids import add_external_id

    coll = WorkCollection(id=_uuid(), title_cn="空合集", external_source="series_group")
    db_session.add(coll)
    await db_session.flush()
    await add_external_id(db_session, "collection", coll.id, "tmdb", "tmdb:778")
    with patch(
        "app.services.metadata_service.download_and_cache_poster",
        new_callable=AsyncMock, return_value=None,
    ):
        work = await ms.create_or_update_series_from_external(db_session, {
            "content_type": "tv", "title_cn": "剧", "title_en": "Show",
            "external_id": "tmdb:778", "external_source": "tmdb",
        })
    assert work is not None
    assert work.season_number == 1
    assert work.collection_id == coll.id


async def test_verify_is_anime_via_bangumi_skips_determined_work(db_session, channel, monkeypatch):
    s = TVSeries(id=_uuid(), title_en="X", content_type="tv", is_anime=True)
    db_session.add(s)
    await db_session.flush()
    res = _resource(channel.id, series_id=s.id)
    db_session.add(res)
    await db_session.flush()
    monkeypatch.setattr("app.services.bangumi_client.bangumi_configured", lambda: True)
    called = False

    async def fake_search(client, query):
        nonlocal called
        called = True
        return []

    monkeypatch.setattr("app.services.bangumi_client.search_subjects", fake_search)
    await ms.maybe_verify_is_anime_via_bangumi(db_session, channel, res)
    assert called is False
    assert s.is_anime is True


async def test_manual_search_metadata_local_low_score_skipped(db_session, monkeypatch):
    s = TVSeries(id=_uuid(), title_en="Completely Different Name", content_type="tv")
    db_session.add(s)
    await db_session.flush()

    async def fake_series_fts(db, query, limit=20):
        return [s.id]

    async def fake_movie_fts(db, query, limit=20):
        return []

    monkeypatch.setattr("app.services.metadata_service.fts_service.search_series_fts", fake_series_fts)
    monkeypatch.setattr("app.services.metadata_service.fts_service.search_movie_fts", fake_movie_fts)
    out = await ms.manual_search_metadata(db_session, "Quantum Physics Explained", "tv", data_source_type="local")
    assert out == []


async def test_manual_search_metadata_local_movie_low_score_skipped(db_session, monkeypatch):
    m = Movie(id=_uuid(), title_en="Completely Different", content_type="movie")
    db_session.add(m)
    await db_session.flush()

    async def fake_series_fts(db, query, limit=20):
        return []

    async def fake_movie_fts(db, query, limit=20):
        return [m.id]

    monkeypatch.setattr("app.services.metadata_service.fts_service.search_series_fts", fake_series_fts)
    monkeypatch.setattr("app.services.metadata_service.fts_service.search_movie_fts", fake_movie_fts)
    out = await ms.manual_search_metadata(db_session, "Quantum Physics", "movie", data_source_type="local")
    assert out == []
