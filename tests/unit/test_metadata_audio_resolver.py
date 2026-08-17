"""Tests for metadata_audio_resolver: AudioWork resolution via local match,
Wikipedia/Exa search, and the title-stub fallback.

External boundaries (wikipedia client, exa search, poster download) are mocked.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

from app.models.audio_work import AudioWork
from app.services import metadata_audio_resolver as mar

_RESOLVER = "app.services.metadata_audio_resolver"


def _resource(**kw) -> SimpleNamespace:
    defaults = dict(
        title_raw="[Group] 深夜音声作品 - 01",
        title_cn=None,
        title_en=None,
        search_title="深夜音声作品",
        audio_work_id=None,
        series_id="keep-me-series",
        movie_id="keep-me-movie",
        metadata_matched_at=None,
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _channel(source="wikipedia") -> SimpleNamespace:
    return SimpleNamespace(metadata_source=source)


# ---------------------------------------------------------------------------
# _search_audio_wikipedia
# ---------------------------------------------------------------------------


async def test_search_audio_wikipedia_match():
    search = AsyncMock(return_value={
        "success": True,
        "data": [{"title": "深夜音声作品", "page_id": 7, "url": "http://w/7", "summary": "short"}],
    })
    page = AsyncMock(return_value={"data": {
        "summary": "a much longer description",
        "url": "http://w/canonical",
        "page_id": 7,
    }})
    with (
        patch(f"{_RESOLVER}._execute_search_wikipedia", search),
        patch(f"{_RESOLVER}._execute_get_wikipedia_page", page),
    ):
        out = await mar._search_audio_wikipedia("深夜音声作品 01", "深夜音声作品")

    assert out["external_id"] == "wikipedia:zh:7"  # language-qualified pageid
    assert out["external_source"] == "wikipedia"
    assert out["description"] == "a much longer description"
    assert out["wikipedia_url"] == "http://w/canonical"


async def test_search_audio_wikipedia_below_threshold_returns_none():
    search = AsyncMock(return_value={
        "success": True,
        "data": [{"title": "Completely Unrelated Thing", "page_id": 9}],
    })
    with patch(f"{_RESOLVER}._execute_search_wikipedia", search):
        out = await mar._search_audio_wikipedia("深夜音声作品 01", "深夜音声作品")
    assert out is None


async def test_search_audio_wikipedia_search_errors_are_skipped():
    search = AsyncMock(side_effect=RuntimeError("http down"))
    with patch(f"{_RESOLVER}._execute_search_wikipedia", search):
        out = await mar._search_audio_wikipedia("深夜音声作品 01", "深夜音声作品")
    assert out is None


async def test_search_audio_wikipedia_falls_back_to_search_summary():
    search = AsyncMock(return_value={
        "success": True,
        "data": [{"title": "深夜音声作品", "page_id": 7, "url": "http://w/7", "summary": "from search"}],
    })
    page = AsyncMock(return_value={"success": False, "error": "boom"})
    with (
        patch(f"{_RESOLVER}._execute_search_wikipedia", search),
        patch(f"{_RESOLVER}._execute_get_wikipedia_page", page),
    ):
        out = await mar._search_audio_wikipedia("深夜音声作品 01", "深夜音声作品")
    assert out["description"] == "from search"
    assert out["wikipedia_url"] == "http://w/7"


# ---------------------------------------------------------------------------
# _search_audio_exa
# ---------------------------------------------------------------------------


async def test_search_audio_exa_match():
    exa = AsyncMock(return_value={
        "success": True,
        "data": [{
            "title_cn": "音声作品",
            "title_en": "Audio Work",
            "external_id": "bangumi:1",
            "external_source": "bangumi",
            "description": "d",
            "poster_url": "http://p",
        }],
    })
    with patch(f"{_RESOLVER}._execute_search_exa_agent", exa):
        out = await mar._search_audio_exa("音声作品")
    assert out["title_cn"] == "音声作品"
    assert out["external_id"] == "bangumi:1"
    assert out["external_source"] == "bangumi"


async def test_search_audio_exa_no_result():
    with patch(f"{_RESOLVER}._execute_search_exa_agent", AsyncMock(return_value={"success": False})):
        assert await mar._search_audio_exa("x") is None
    with patch(
        f"{_RESOLVER}._execute_search_exa_agent",
        AsyncMock(return_value={"success": True, "data": []}),
    ):
        assert await mar._search_audio_exa("x") is None


# ---------------------------------------------------------------------------
# _resolve_audio_work
# ---------------------------------------------------------------------------


async def test_resolve_audio_work_local_match_skips_search(db_session):
    existing = AudioWork(
        title_cn="深夜音声作品", external_id="stub:abc", external_source="stub",
        content_type="asmr",
    )
    db_session.add(existing)
    await db_session.flush()

    wiki = AsyncMock()
    resource = _resource()
    with patch(f"{_RESOLVER}._search_audio_wikipedia", wiki):
        meta = await mar._resolve_audio_work(resource, _channel(), db_session, "asmr", False)

    assert meta is not None and meta.found is True
    assert meta.content_type == "asmr"
    assert meta.matched_entity == {"external_id": "stub:abc"}
    assert resource.audio_work_id == existing.id
    assert resource.series_id is None
    assert resource.movie_id is None
    assert resource.metadata_matched_at is not None
    wiki.assert_not_called()  # local hit -> no external search


async def test_resolve_audio_work_wikipedia_match(db_session):
    matched = {
        "title_cn": "深夜音声作品",
        "external_id": "wikipedia:7",
        "external_source": "wikipedia",
        "description": "d",
    }
    resource = _resource()
    with (
        patch(f"{_RESOLVER}._search_audio_wikipedia", AsyncMock(return_value=matched)),
        patch(
            "app.services.metadata_service.download_and_cache_poster",
            new_callable=AsyncMock, return_value=None,
        ),
    ):
        meta = await mar._resolve_audio_work(resource, _channel("wikipedia"), db_session, "asmr", False)

    assert meta.found is True
    assert meta.matched_entity["external_id"] == "wikipedia:7"
    aw = (await db_session.execute(select(AudioWork))).scalar_one()
    assert aw.external_id == "wikipedia:7"
    assert resource.audio_work_id == aw.id


async def test_resolve_audio_work_stub_fallback(db_session):
    resource = _resource()
    with (
        patch(f"{_RESOLVER}._search_audio_wikipedia", AsyncMock(return_value=None)),
        patch(
            "app.services.metadata_service.download_and_cache_poster",
            new_callable=AsyncMock, return_value=None,
        ),
    ):
        meta = await mar._resolve_audio_work(resource, _channel("wikipedia"), db_session, "asmr", False)

    assert meta.found is True
    assert meta.matched_entity["external_source"] == "stub"
    assert meta.matched_entity["title_cn"] == "深夜音声作品"
    aw = (await db_session.execute(select(AudioWork))).scalar_one()
    assert aw.external_source == "stub"
    assert resource.audio_work_id == aw.id


async def test_resolve_audio_work_search_exception_still_stubs(db_session):
    resource = _resource()
    with (
        patch(
            f"{_RESOLVER}._search_audio_wikipedia",
            AsyncMock(side_effect=RuntimeError("wiki down")),
        ),
        patch(
            "app.services.metadata_service.download_and_cache_poster",
            new_callable=AsyncMock, return_value=None,
        ),
    ):
        meta = await mar._resolve_audio_work(resource, _channel("wikipedia"), db_session, "drama_cd", False)
    assert meta.found is True
    assert meta.matched_entity["external_source"] == "stub"
    assert meta.content_type == "drama_cd"


async def test_resolve_audio_work_exa_channel_resolves_to_wikipedia(db_session):
    """Deprecated channel sources (exa) converge on wikipedia (Phase P1
    two-source channel resolution); the audio path follows the same rule."""
    matched = {"title_cn": "深夜音声作品", "external_id": "bangumi:1", "external_source": "bangumi"}
    wiki = AsyncMock(return_value=matched)
    exa = AsyncMock(return_value=None)
    resource = _resource()
    with (
        patch(f"{_RESOLVER}._search_audio_wikipedia", wiki),
        patch(f"{_RESOLVER}._search_audio_exa", exa),
        patch(
            "app.services.metadata_service.download_and_cache_poster",
            new_callable=AsyncMock, return_value=None,
        ),
    ):
        meta = await mar._resolve_audio_work(resource, _channel("exa"), db_session, "music", False)
    wiki.assert_awaited_once()
    exa.assert_not_called()
    assert meta.matched_entity["external_source"] == "bangumi"


async def test_resolve_audio_work_tmdb_channel_falls_back_to_wikipedia(db_session):
    wiki = AsyncMock(return_value=None)
    resource = _resource()
    with (
        patch(f"{_RESOLVER}._search_audio_wikipedia", wiki),
        patch(
            "app.services.metadata_service.download_and_cache_poster",
            new_callable=AsyncMock, return_value=None,
        ),
    ):
        await mar._resolve_audio_work(resource, _channel("tmdb"), db_session, "asmr", False)
    wiki.assert_awaited_once()


async def test_resolve_audio_work_empty_title_returns_none(db_session):
    resource = _resource(title_raw="", search_title=None)
    meta = await mar._resolve_audio_work(resource, _channel(), db_session, "asmr", False)
    assert meta is None
