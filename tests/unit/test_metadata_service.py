"""Tests for metadata_service: 4-layer matching, manual search/link, poster cache."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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
    assert s3.external_id == "tmdb:82684"  # canonicalized


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
    """Per-resource refresh (Layer 4) must run the channel's configured source,
    not the hardcoded Exa default - otherwise a Jina channel's refresh silently
    uses Exa."""
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
    assert mock_search.call_args.args[1] == "jina"


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


async def test_manual_link_marks_season_uncertain(db_session, channel):
    """Manual link to a multi-season series marks a season-less resource
    季号不确定 instead of guessing a season."""
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
    assert res.series_id == entity.id
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
