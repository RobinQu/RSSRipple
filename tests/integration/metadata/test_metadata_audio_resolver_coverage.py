"""Branch coverage for app.services.metadata_audio_resolver.

The Wikipedia HTTP layer is mocked at the module boundary
(``_execute_search_wikipedia`` / ``_execute_get_wikipedia_page``); the local
match and AudioWork upsert run for real against the test database.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

import app.services.metadata_audio_resolver as mar
from app.models.audio_work import AudioWork
from app.models.channel import Channel
from app.models.file_resource import FileResource


async def _make_resource(db_session, **overrides) -> FileResource:
    ch = Channel(
        id=str(uuid.uuid4()), name="ch", type="rss_feed",
        url="https://example.com/rss", fetch_interval=1800, status="active",
        field_mapping={}, metadata_agent_enabled=False,
    )
    db_session.add(ch)
    await db_session.flush()
    defaults = dict(
        id=str(uuid.uuid4()), channel_id=ch.id, guid=str(uuid.uuid4()),
        title_raw="[Group] Test ASMR Work - 01",
        torrent_url="magnet:?xt=urn:btih:" + "ab" * 20,
        search_title="Test ASMR Work",
    )
    defaults.update(overrides)
    res = FileResource(**defaults)
    db_session.add(res)
    await db_session.commit()
    return res


def _search_hit(title: str, page_id: int = 12345) -> dict:
    return {
        "success": True,
        "data": [{
            "title": title, "page_id": page_id,
            "url": f"https://en.wikipedia.org/?curid={page_id}",
            "summary": "search snippet",
        }],
    }


# ---------------------------------------------------------------------------
# _search_audio_wikipedia
# ---------------------------------------------------------------------------


class TestSearchAudioWikipedia:
    async def test_exact_candidate_returns_entity(self, monkeypatch):
        async def _search(q, lang):
            return _search_hit("Test ASMR Work")

        async def _page(title, lang):
            return {"data": {
                "summary": "rich page summary",
                "url": "https://en.wikipedia.org/wiki/Test_ASMR_Work",
                "page_id": 999,
            }}

        monkeypatch.setattr(mar, "_execute_search_wikipedia", _search)
        monkeypatch.setattr(mar, "_execute_get_wikipedia_page", _page)
        matched = await mar._search_audio_wikipedia(
            "[Group] Test ASMR Work - 01", "Test ASMR Work"
        )
        assert matched is not None
        # The full-page fetch replaces id/url/description with canonical ones.
        assert matched["external_id"] == "wikipedia:en:999"
        assert matched["external_source"] == "wikipedia"
        assert matched["description"] == "rich page summary"
        assert matched["wikipedia_url"].endswith("Test_ASMR_Work")

    async def test_page_fetch_failure_keeps_search_fields(self, monkeypatch):
        async def _search(q, lang):
            return _search_hit("Test ASMR Work", page_id=555)

        async def _page(title, lang):
            raise RuntimeError("page fetch down")

        monkeypatch.setattr(mar, "_execute_search_wikipedia", _search)
        monkeypatch.setattr(mar, "_execute_get_wikipedia_page", _page)
        with pytest.raises(RuntimeError):
            # The page fetch is NOT guarded inside _search_audio_wikipedia —
            # the resolver's caller catches it (see stub-fallback test below).
            await mar._search_audio_wikipedia("Test ASMR Work", "Test ASMR Work")

    async def test_page_without_data_keeps_search_snippet(self, monkeypatch):
        async def _search(q, lang):
            return _search_hit("Test ASMR Work", page_id=555)

        async def _page(title, lang):
            return {"data": None}

        monkeypatch.setattr(mar, "_execute_search_wikipedia", _search)
        monkeypatch.setattr(mar, "_execute_get_wikipedia_page", _page)
        matched = await mar._search_audio_wikipedia("Test ASMR Work", "Test ASMR Work")
        assert matched["external_id"] == "wikipedia:en:555"
        assert matched["description"] == "search snippet"

    async def test_below_threshold_returns_none(self, monkeypatch):
        async def _search(q, lang):
            return _search_hit("completely different title")

        monkeypatch.setattr(mar, "_execute_search_wikipedia", _search)
        assert await mar._search_audio_wikipedia("Test ASMR Work", "Test ASMR Work") is None

    async def test_failed_and_exception_results_skipped(self, monkeypatch):
        async def _search(q, lang):
            if lang == "zh":
                raise TimeoutError("zh wiki down")
            return {"success": False}

        monkeypatch.setattr(mar, "_execute_search_wikipedia", _search)
        assert await mar._search_audio_wikipedia("音声作品テスト", "音声作品テスト") is None

    async def test_fallback_query_from_search_title(self, monkeypatch):
        # A raw title that yields no candidate queries falls back to
        # (search_title, lang-by-CJK).
        seen: list[tuple[str, str]] = []

        async def _search(q, lang):
            seen.append((q, lang))
            return _search_hit("Test ASMR Work")

        async def _page(title, lang):
            return {"data": None}

        monkeypatch.setattr(mar, "_execute_search_wikipedia", _search)
        monkeypatch.setattr(mar, "_execute_get_wikipedia_page", _page)
        matched = await mar._search_audio_wikipedia("", "Test ASMR Work")
        assert matched is not None
        assert ("Test ASMR Work", "en") in seen


# ---------------------------------------------------------------------------
# _resolve_audio_work
# ---------------------------------------------------------------------------


class TestResolveAudioWork:
    async def test_empty_search_title_returns_none(self, db_session):
        res = await _make_resource(
            db_session, title_raw="", search_title=None, title_cn=None, title_en=None,
        )
        out = await mar._resolve_audio_work(res, None, db_session, "asmr", False)
        assert out is None
        assert res.audio_work_id is None

    async def test_local_match_links_without_search(self, db_session, monkeypatch):
        from app.models.movie import Movie
        from app.models.series import TVSeries

        audio = AudioWork(
            id=str(uuid.uuid4()), title_cn="Test ASMR Work",
            external_id="wikipedia:en:1", external_source="wikipedia",
            content_type="asmr",
        )
        series = TVSeries(id=str(uuid.uuid4()), title_cn="剧集", content_type="tv")
        movie = Movie(id=str(uuid.uuid4()), title_cn="电影", content_type="movie")
        db_session.add_all([audio, series, movie])
        res = await _make_resource(db_session, series_id=series.id, movie_id=movie.id)

        async def _search_boom(q, lang):
            raise AssertionError("local match must not hit Wikipedia")

        monkeypatch.setattr(mar, "_execute_search_wikipedia", _search_boom)
        out = await mar._resolve_audio_work(res, None, db_session, "asmr", False)
        assert out is not None and out.found is True
        assert out.content_type == "asmr"
        assert res.audio_work_id == audio.id
        assert res.series_id is None and res.movie_id is None
        assert res.search_title == "Test ASMR Work"
        assert res.metadata_matched_at is not None

    async def test_wikipedia_match_creates_and_links_work(self, db_session, monkeypatch):
        async def _search(q, lang):
            return _search_hit("Test ASMR Work", page_id=777)

        async def _page(title, lang):
            return {"data": {"summary": "desc", "url": "https://en.wikipedia.org/?curid=777",
                             "page_id": 777}}

        monkeypatch.setattr(mar, "_execute_search_wikipedia", _search)
        monkeypatch.setattr(mar, "_execute_get_wikipedia_page", _page)
        res = await _make_resource(db_session)
        out = await mar._resolve_audio_work(res, None, db_session, "asmr", False)
        assert out is not None and out.found is True
        assert res.audio_work_id is not None
        audio = (
            await db_session.execute(select(AudioWork).where(AudioWork.id == res.audio_work_id))
        ).scalar_one()
        assert audio.external_source == "wikipedia"
        assert audio.content_type == "asmr"

    async def test_no_external_match_creates_stub(self, db_session, monkeypatch):
        async def _search(q, lang):
            return {"success": False}

        monkeypatch.setattr(mar, "_execute_search_wikipedia", _search)
        res = await _make_resource(db_session)
        out = await mar._resolve_audio_work(res, None, db_session, "drama_cd", False)
        assert out is not None and out.found is True
        audio = (
            await db_session.execute(select(AudioWork).where(AudioWork.id == res.audio_work_id))
        ).scalar_one()
        assert audio.external_source == "stub"
        assert audio.title_cn == "Test ASMR Work"
        assert audio.content_type == "drama_cd"

    async def test_wikipedia_failure_falls_back_to_stub(self, db_session, monkeypatch):
        # A match that then explodes on the full-page fetch propagates out of
        # _search_audio_wikipedia; the resolver catches it and stubs.
        async def _search(q, lang):
            return _search_hit("Test ASMR Work", page_id=888)

        async def _page(title, lang):
            raise RuntimeError("wikipedia down")

        monkeypatch.setattr(mar, "_execute_search_wikipedia", _search)
        monkeypatch.setattr(mar, "_execute_get_wikipedia_page", _page)
        res = await _make_resource(db_session)
        out = await mar._resolve_audio_work(res, None, db_session, "music", False)
        assert out is not None and out.found is True
        audio = (
            await db_session.execute(select(AudioWork).where(AudioWork.id == res.audio_work_id))
        ).scalar_one()
        assert audio.external_source == "stub"
        assert audio.content_type == "music"
