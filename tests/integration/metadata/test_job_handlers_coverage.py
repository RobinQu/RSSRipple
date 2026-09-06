"""Branch coverage for app.job_handlers.

Runs the queue handlers in-process against the test DB. External work is
patched at its module boundary: ``metadata_search.refresh_work_by_source``,
the scheduler's periodic functions, ``notify_service`` regeneration, the
batch-analysis LLM stream, and magnet resolution enqueueing.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

import app.database as db_mod
import app.job_handlers as jh
from app.models.channel import Channel
from app.models.episode import Episode
from app.models.file_resource import FileResource
from app.models.movie import Movie
from app.models.series import TVSeries
from app.utils.time import utcnow


def _uuid() -> str:
    return str(uuid.uuid4())


async def _make_channel(db_session, **overrides) -> Channel:
    defaults = dict(
        id=_uuid(), name="ch", type="rss_feed", url="https://example.com/rss",
        fetch_interval=1800, status="active", field_mapping={},
        metadata_agent_enabled=False,
    )
    defaults.update(overrides)
    ch = Channel(**defaults)
    db_session.add(ch)
    await db_session.commit()
    return ch


def _make_resource(channel_id: str, **overrides) -> FileResource:
    defaults = dict(
        id=_uuid(), channel_id=channel_id, guid=_uuid(),
        title_raw="[G] Show - 01 [1080p]",
        torrent_url="magnet:?xt=urn:btih:" + "ab" * 20,
        search_title="Show",
    )
    defaults.update(overrides)
    return FileResource(**defaults)


# ---------------------------------------------------------------------------
# _refresh_runtime_config
# ---------------------------------------------------------------------------


async def test_refresh_runtime_config_failure_is_swallowed(db_session, monkeypatch, caplog):
    import logging

    async def _load_boom(sess):
        raise RuntimeError("settings table corrupted")

    monkeypatch.setattr(
        "app.services.runtime_config.load_runtime_config", _load_boom
    )
    monkeypatch.setattr(
        "app.services.scheduler._sync_download_progress", AsyncMock()
    )
    with caplog.at_level(logging.WARNING):
        result = await jh._handle_sync_progress({})
    assert result == {"status": "done"}
    assert "Failed to refresh runtime config" in caplog.text


# ---------------------------------------------------------------------------
# _handle_refresh_works_metadata
# ---------------------------------------------------------------------------


class TestRefreshWorksMetadata:
    async def test_missing_source_is_error(self, db_session):
        result = await jh._handle_refresh_works_metadata({"items": [], "source": ""})
        assert result["status"] == "error"
        assert result["processed"] == 0

    async def test_work_not_found(self, db_session):
        result = await jh._handle_refresh_works_metadata({
            "items": [{"id": _uuid(), "content_type": "tv"}],
            "source": "wikipedia",
        })
        assert result["status"] == "done"
        assert result["results"][0]["found"] is False
        assert result["results"][0]["message"] == "work/title not found"

    async def test_refresh_success(self, db_session, monkeypatch):
        movie = Movie(id=_uuid(), title_cn="电影", content_type="movie")
        db_session.add(movie)
        await db_session.commit()
        monkeypatch.setattr(
            "app.services.metadata_search.refresh_work_by_source",
            AsyncMock(return_value={"found": True, "applied": ["title"], "message": "ok"}),
        )
        result = await jh._handle_refresh_works_metadata({
            "items": [{"id": movie.id, "content_type": "movie"}],
            "source": "tmdb",
        })
        assert result["processed"] == 1
        assert result["results"][0]["found"] is True
        assert result["results"][0]["applied"] == ["title"]

    async def test_refresh_timeout_isolated(self, db_session, monkeypatch):
        movie = Movie(id=_uuid(), title_cn="电影", content_type="movie")
        db_session.add(movie)
        await db_session.commit()

        async def _wait_for_boom(coro, **kw):
            coro.close()  # abandon cleanly; the wait itself times out
            raise TimeoutError

        monkeypatch.setattr(jh.asyncio, "wait_for", _wait_for_boom)
        result = await jh._handle_refresh_works_metadata({
            "items": [{"id": movie.id, "content_type": "movie"}],
            "source": "tmdb",
        })
        assert result["results"][0]["found"] is False
        assert result["results"][0]["error"] == "timeout"

    async def test_refresh_exception_keeps_processing_rest(self, db_session, monkeypatch):
        movie = Movie(id=_uuid(), title_cn="电影", content_type="movie")
        db_session.add(movie)
        await db_session.commit()

        async def _refresh(session, work, content_type, source, **kw):
            if content_type == "movie":
                raise RuntimeError("source exploded")
            return {"found": True, "applied": [], "message": "ok"}

        monkeypatch.setattr(
            "app.services.metadata_search.refresh_work_by_source", _refresh
        )
        result = await jh._handle_refresh_works_metadata({
            "items": [
                {"id": movie.id, "content_type": "movie"},
                {"id": _uuid(), "content_type": "tv"},  # not found → message path
            ],
            "source": "tmdb",
        })
        assert result["processed"] == 2
        assert result["results"][0]["error"] == "source exploded"
        assert result["results"][1]["found"] is False


# ---------------------------------------------------------------------------
# _handle_refresh_channel_works
# ---------------------------------------------------------------------------


class TestRefreshChannelWorks:
    async def test_channel_not_found(self, db_session):
        result = await jh._handle_refresh_channel_works({"channel_id": _uuid()})
        assert result == {"status": "done", "processed": 0, "results": []}

    async def test_inactive_channel_skipped(self, db_session):
        channel = await _make_channel(db_session, status="inactive")
        result = await jh._handle_refresh_channel_works({"channel_id": channel.id})
        assert result == {"status": "done", "processed": 0, "results": []}

    async def test_active_channel_refreshes_gapped_works(self, db_session, monkeypatch):
        channel = await _make_channel(db_session, metadata_source="wikipedia")
        series = TVSeries(id=_uuid(), title_cn="剧集", content_type="tv",
                          description=None)
        db_session.add(series)
        res = _make_resource(channel.id, series_id=series.id)
        db_session.add(res)
        await db_session.commit()

        seen: list[dict] = []

        async def _refresh(session, work, content_type, source, **kw):
            seen.append({"id": work.id, "source": source,
                         "override": kw.get("override_manual_edits")})
            return {"found": True, "applied": ["description"], "message": "ok"}

        monkeypatch.setattr(
            "app.services.metadata_search.refresh_work_by_source", _refresh
        )
        result = await jh._handle_refresh_channel_works({"channel_id": channel.id})
        assert result["status"] == "done"
        assert result["processed"] == len(result["results"])
        if seen:  # the work has fillable fields, so it should be selected
            assert seen[0]["source"] == "wikipedia"
            assert seen[0]["override"] is False  # manual edits always protected


# ---------------------------------------------------------------------------
# Periodic thin wrappers
# ---------------------------------------------------------------------------


async def test_daily_cleanup_handler(db_session, monkeypatch):
    cleanup = AsyncMock()
    monkeypatch.setattr("app.services.scheduler._cleanup_expired", cleanup)
    assert await jh._handle_daily_cleanup({}) == {"status": "done"}
    cleanup.assert_awaited_once()


async def test_daily_dedup_handler(db_session, monkeypatch):
    dedup = AsyncMock()
    monkeypatch.setattr("app.services.scheduler._dedup_metadata", dedup)
    assert await jh._handle_daily_dedup({}) == {"status": "done"}
    dedup.assert_awaited_once()


async def test_refresh_resource_organize_handler(db_session, monkeypatch):
    regen = AsyncMock(return_value={"regenerated": 1})
    monkeypatch.setattr(
        "app.services.notify_service.regenerate_resource_notifications", regen
    )
    result = await jh._handle_refresh_resource_organize({"resource_id": "r1"})
    assert result == {"regenerated": 1}
    regen.assert_awaited_once()
    assert regen.await_args.args[1] == "r1"


# ---------------------------------------------------------------------------
# _handle_analyze_batch_files
# ---------------------------------------------------------------------------


class _ProgressQueue:
    def __init__(self):
        self.updates: list[dict] = []

    async def update_progress(self, key, data):
        self.updates.append({"key": key, **data})


class TestAnalyzeBatchFiles:
    async def test_resource_not_found_raises(self, db_session, monkeypatch):
        monkeypatch.setattr(
            "app.services.task_queue.task_queue", _ProgressQueue()
        )
        with pytest.raises(RuntimeError, match="not found"):
            await jh._handle_analyze_batch_files({
                "resource_id": _uuid(), "fingerprint": "fp", "job_key": "k",
            })

    async def test_no_listing_stores_empty_suggestion(self, db_session, monkeypatch):
        channel = await _make_channel(db_session)
        res = _make_resource(channel.id)
        db_session.add(res)
        await db_session.commit()
        monkeypatch.setattr("app.services.task_queue.task_queue", _ProgressQueue())
        monkeypatch.setattr(
            "app.api.v1.resources._resolve_resource_files",
            AsyncMock(return_value=([], "none")),
        )
        # resources.py captured the default session factory at import time;
        # redirect it to the test DB (same factory the fixture installed).
        monkeypatch.setattr(
            "app.api.v1.resources.async_session_factory", db_mod.async_session_factory
        )
        result = await jh._handle_analyze_batch_files({
            "resource_id": res.id, "fingerprint": "fp-empty", "job_key": "k",
        })
        assert result == {"suggestion": None, "listing_source": "none"}
        # Persisted for the wizard's polling endpoint.
        from app.api.v1.resources import _BATCH_ANALYSIS_SOURCE
        from app.models.metadata_cache import MetadataCache

        row = (await db_session.execute(
            select(MetadataCache).where(
                MetadataCache.title == "fp-empty",
                MetadataCache.source == _BATCH_ANALYSIS_SOURCE,
            )
        )).scalar_one()
        assert row.metadata_json["suggestion"] is None

    async def test_full_analysis_merges_llm_placements(self, db_session, monkeypatch):
        channel = await _make_channel(db_session)
        series = TVSeries(id=_uuid(), title_cn="剧集A", content_type="tv",
                          season_number=1)
        db_session.add(series)
        # A Season-0 row so the fractional "11.5" label maps deterministically.
        db_session.add(Episode(
            id=_uuid(), series_id=series.id, season=0, episode=7,
        ))
        res = _make_resource(channel.id, series_id=series.id, season=1,
                             is_batch=True, batch_scope="season",
                             episode=None, episode_start=1, episode_end=2)
        db_session.add(res)
        await db_session.commit()

        queue = _ProgressQueue()
        monkeypatch.setattr("app.services.task_queue.task_queue", queue)
        files = [
            {"name": "Show - 01.mkv", "size": 100 * 1024 * 1024},
            {"name": "Show - 02.mkv", "size": 100 * 1024 * 1024},
            {"name": "Show - 11.5 OVA.mkv", "size": 100 * 1024 * 1024},
            {"name": "Show.mkv", "size": 100 * 1024 * 1024},  # no episode parse
            {"name": "notes.txt", "size": 5},
        ]
        monkeypatch.setattr(
            "app.api.v1.resources._resolve_resource_files",
            AsyncMock(return_value=(files, "torrent_cache")),
        )
        monkeypatch.setattr(
            "app.api.v1.resources.async_session_factory", db_mod.async_session_factory
        )

        async def _stream(title, listing, anchors, candidate_works):
            yield "delta", "thinking..."
            yield "result", {
                "works": [{
                    "title": "剧集A", "content_type": "tv",
                    "files": [{
                        "path": "Show.mkv",
                        "season": 1, "episode_start": 3, "episode_end": 3,
                    }],
                }],
            }

        monkeypatch.setattr(
            "app.services.batch_content_analysis.analyze_listing_stream", _stream
        )
        result = await jh._handle_analyze_batch_files({
            "resource_id": res.id, "fingerprint": "fp-full", "job_key": "job-1",
        })
        assert result["listing_source"] == "torrent_cache"
        assert result["output"].endswith("thinking...")
        deterministic = result["suggestion"]["deterministic"]
        parsed = {f["path"]: f for f in deterministic["files"]}
        # Resource's season=1 filled the deterministic parses lacking one.
        assert parsed["Show - 01.mkv"]["season"] == 1
        assert parsed["Show - 01.mkv"]["episode"] == 1
        # The fractional "11.5" label mapped onto the Season-0 Episode row.
        assert parsed["Show - 11.5 OVA.mkv"]["season"] == 0
        assert parsed["Show - 11.5 OVA.mkv"]["episode"] == 7
        # The LLM placement filled the unparseable file.
        assert parsed["Show.mkv"]["season"] == 1
        assert parsed["Show.mkv"]["episode"] == 3
        works = result["suggestion"]["works"]
        assert works and works[0]["title"] == "剧集A"
        # Season ranges reflect the merged deterministic + LLM view.
        assert deterministic["seasons"] == [0, 1]
        assert deterministic["season_ranges"] == [
            {"season": 0, "episode_start": 7, "episode_end": 7},
            {"season": 1, "episode_start": 1, "episode_end": 3},
        ]
        # Progress was streamed under the job key.
        assert any(u["key"] == "job-1" for u in queue.updates)


# ---------------------------------------------------------------------------
# _handle_magnet_resolve_sweep
# ---------------------------------------------------------------------------


async def test_magnet_sweep_reclaims_stale_and_enqueues(db_session, monkeypatch):
    from app.config import settings

    channel = await _make_channel(db_session)
    stale_age = (
        (1 + settings.magnet_resolve_max_attempts)
        * settings.magnet_resolve_timeout_seconds
        + settings.magnet_resolve_max_attempts * 60
        + 600 + 60
    )
    stuck = _make_resource(
        channel.id, magnet_resolve_status="running", magnet_resolve_attempts=2,
        magnet_resolve_updated_at=utcnow() - timedelta(seconds=stale_age),
    )
    fresh_null = _make_resource(channel.id)
    db_session.add_all([stuck, fresh_null])
    await db_session.commit()

    enqueued: list[str] = []

    async def _enqueue(resource_id):
        if resource_id == fresh_null.id:
            raise RuntimeError("queue down")
        enqueued.append(resource_id)

    monkeypatch.setattr("app.services.magnet_resolve.enqueue_resolution", _enqueue)
    result = await jh._handle_magnet_resolve_sweep({})
    assert result["status"] == "done"
    assert result["reclaimed"] == 1
    # The reclaimed row landed on NULL and was picked up by the scan; the
    # failing row did not stop the sweep.
    assert enqueued == [stuck.id]
    assert result["enqueued"] == 1
    await db_session.refresh(stuck)
    assert stuck.magnet_resolve_attempts == 0
    assert "interrupted" in stuck.magnet_resolve_error


async def test_magnet_sweep_nothing_to_do(db_session, monkeypatch):
    monkeypatch.setattr(
        "app.services.magnet_resolve.enqueue_resolution", AsyncMock()
    )
    result = await jh._handle_magnet_resolve_sweep({})
    assert result == {"status": "done", "enqueued": 0, "reclaimed": 0}
