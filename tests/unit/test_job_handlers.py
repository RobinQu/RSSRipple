"""Unit tests for background job handlers in app.job_handlers.

Each handler is invoked directly with crafted payloads; external services
(fetch, metadata agent, torrent inspect, LLM streaming, magnet resolve,
scheduler helpers) are stubbed with mocks. The ``db_engine`` fixture installs
the global session factory so ``committed_session()`` used by handlers hits a
real (throwaway) Turso DB.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.job_handlers import (
    _handle_analyze_batch_files,
    _handle_check_downloaders,
    _handle_daily_cleanup,
    _handle_daily_dedup,
    _handle_download_notifications,
    _handle_fts_drain,
    _handle_fts_reconcile,
    _handle_magnet_resolve_sweep,
    _handle_refresh_channel_works,
    _handle_refresh_resource_organize,
    _handle_refresh_works_metadata,
    _handle_resolve_magnet_torrent,
    _handle_sync_progress,
    _refresh_runtime_config,
    register_all_handlers,
)
from app.models.file_resource import FileResource
from app.services.torrent_inspect import TorrentReport, WorkCluster


def _uuid() -> str:
    return str(uuid.uuid4())


async def _make_resource(**overrides) -> tuple[FileResource, dict]:
    """Seed a Channel (and series when requested) plus a FileResource, then
    return the resource. Uses the global factory installed by db_engine."""
    from app.database import async_session_factory
    from app.models.channel import Channel
    from app.models.series import TVSeries

    channel_id = _uuid()
    series_id = None
    async with async_session_factory() as session:
        session.add(Channel(
            id=channel_id, name="ch", type="rss_feed", url="https://x/rss",
            field_mapping={"list_locator": {"source": "entries"}},
            metadata_agent_enabled=False,
        ))
        if overrides.get("series_id"):
            series_id = overrides["series_id"]
            session.add(TVSeries(
                id=series_id, title_cn="Show", title_en="Show",
                original_title="Show", content_type="tv", season_number=1,
            ))
        await session.commit()

    defaults = dict(
        id=_uuid(), channel_id=channel_id, guid=_uuid(), title_raw="[G] Show",
        search_title="Show", torrent_url="http://x/t.torrent",
    )
    defaults.update(overrides)
    resource = FileResource(**defaults)
    async with async_session_factory() as session:
        session.add(resource)
        await session.commit()
    return resource, defaults


def _make_report() -> TorrentReport:
    """A season-style report with two parsed episodes."""
    return TorrentReport(
        scope="season",
        is_batch=True,
        episode_start=1,
        episode_end=2,
        seasons=[1],
        season_ranges=[{"season": 1, "episode_start": 1, "episode_end": 2}],
        clusters=[WorkCluster(title="Show", files=["S01/Show - 01.mkv", "S01/Show - 02.mkv"])],
        file_parses=[
            {"path": "S01/Show - 01.mkv", "size": 100, "season": 1, "episode": 1},
            {"path": "S01/Show - 02.mkv", "size": 200, "season": 1, "episode": 2},
        ],
        video_file_count=2,
        unparsed_ratio=0.0,
    )


@pytest.mark.asyncio
async def test_refresh_runtime_config_logs_on_failure(monkeypatch, caplog):
    """The config reload failure path (except) must not raise — just log."""
    class _Ctx:
        async def __aenter__(self):
            raise RuntimeError("boom")

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(
        "app.database.async_session_factory", lambda: _Ctx()
    )
    with patch(
        "app.services.runtime_config.load_runtime_config",
        new=AsyncMock(side_effect=RuntimeError("load failed")),
    ):
        await _refresh_runtime_config()
    assert "Failed to refresh runtime config" in caplog.text


@pytest.mark.asyncio
async def test_analyze_batch_files_full_flow(db_engine):
    """The happy path with deterministic parse + streamed LLM result."""
    series_id = _uuid()
    resource, _ = await _make_resource(
        series_id=series_id, season=1,
        title_raw="[G] Show - 01 [1080p]",
    )

    report = _make_report()

    async def fake_stream(*args, **kwargs):
        yield "delta", "thinking"
        yield "result", {
            "works": [
                {
                    "files": [
                        {"path": "S01/Show - 01.mkv", "season": 1, "episode_start": 1, "episode_end": 1},
                        {"path": "S01/Show - 02.mkv", "season": 1, "episode_start": 2, "episode_end": 2},
                    ]
                }
            ]
        }

    with patch(
        "app.api.v1.resources._resolve_resource_files",
        new=AsyncMock(return_value=(
            [
                {"name": "S01/Show - 01.mkv", "size": 100},
                {"name": "S01/Show - 02.mkv", "size": 200},
            ],
            "torrent_cache",
        )),
    ), patch(
        "app.api.v1.resources._store_batch_analysis",
        new=AsyncMock(),
    ), patch(
        "app.services.batch_content_analysis.build_candidate_works",
        new=AsyncMock(return_value=[{"candidate_key": "series:x", "titles": ["Show"]}]),
    ), patch(
        "app.services.batch_content_analysis.resolve_fractional_specials",
        new=AsyncMock(return_value={}),
    ), patch(
        "app.services.batch_content_analysis.analyze_listing_stream",
        new=fake_stream,
    ), patch(
        "app.services.batch_content_analysis._valid_paths",
        return_value=[
            {
                "files": [
                    {"path": "S01/Show - 01.mkv", "season": 1, "episode_start": 1, "episode_end": 1},
                ]
            }
        ],
    ), patch(
        "app.services.torrent_inspect.analyze_torrent_files",
        return_value=report,
    ):
        result = await _handle_analyze_batch_files({
            "resource_id": resource.id,
            "fingerprint": "fp-1",
            "job_key": "job-1",
        })

    assert result["listing_source"] == "torrent_cache"
    assert result["suggestion"]["deterministic"]["scope_hint"] == "season"
    assert result["suggestion"]["works"]
    assert "output" in result


@pytest.mark.asyncio
async def test_analyze_batch_files_fractional_specials(db_engine):
    """When the resource is series-linked and fractional specials resolve, the
    per-file season is overridden to 0 and merged into the deterministic view."""
    from app.api.v1 import resources as res_mod
    from app.services import batch_content_analysis as bca
    from app.services import torrent_inspect as ti

    series_id = _uuid()
    resource, _ = await _make_resource(
        series_id=series_id, season=1, title_raw="[G] Show SP",
    )

    report = TorrentReport(
        scope="season", is_batch=True, season_ranges=[],
        file_parses=[
            {"path": "S00/Show - SP.mkv", "size": 100, "season": None, "episode": 1},
        ],
    )

    async def fake_stream(*args, **kwargs):
        yield "result", None

    with patch.object(
        res_mod, "_resolve_resource_files",
        new=AsyncMock(return_value=(
            [{"name": "S00/Show - SP.mkv", "size": 100}], "torrent_cache",
        )),
    ), patch.object(
        res_mod, "_store_batch_analysis", new=AsyncMock(),
    ), patch.object(
        bca, "build_candidate_works", new=AsyncMock(return_value=[]),
    ), patch.object(
        bca, "resolve_fractional_specials",
        new=AsyncMock(return_value={"S00/Show - SP.mkv": 1}),
    ), patch.object(
        bca, "analyze_listing_stream", new=fake_stream,
    ), patch.object(
        bca, "_valid_paths", return_value=[],
    ), patch.object(
        ti, "analyze_torrent_files",
        return_value=report,
    ):
        result = await _handle_analyze_batch_files({
            "resource_id": resource.id,
            "fingerprint": "fp-2",
            "job_key": "job-2",
        })

    files = result["suggestion"]["deterministic"]["files"]
    sp = next(f for f in files if f["path"] == "S00/Show - SP.mkv")
    assert sp["season"] == 0
    assert sp["episode"] == 1


@pytest.mark.asyncio
async def test_analyze_batch_files_no_listing(db_engine):
    """When no files resolve, store an empty suggestion and return."""
    from app.api.v1 import resources as res_mod
    from app.services import batch_content_analysis as bca

    resource, _ = await _make_resource()

    store = AsyncMock()
    with patch.object(
        res_mod, "_resolve_resource_files", new=AsyncMock(return_value=([], "none")),
    ), patch.object(
        res_mod, "_store_batch_analysis", new=store,
    ), patch.object(
        bca, "build_candidate_works", new=AsyncMock(return_value=[]),
    ):
        result = await _handle_analyze_batch_files({
            "resource_id": resource.id,
            "fingerprint": "fp-3",
            "job_key": "job-3",
        })

    assert result == {"suggestion": None, "listing_source": "none"}
    store.assert_awaited_once()


@pytest.mark.asyncio
async def test_analyze_batch_files_missing_resource(db_engine):
    with pytest.raises(RuntimeError, match="not found"):
        await _handle_analyze_batch_files({
            "resource_id": _uuid(), "fingerprint": "fp", "job_key": "jk",
        })


# ---------------------------------------------------------------------------
# Periodic scheduler jobs
# ---------------------------------------------------------------------------

async def _run_periodic(handler, func_name, monkeypatch):
    func = AsyncMock()
    monkeypatch.setattr(f"app.services.scheduler.{func_name}", func)
    return await handler({}), func


@pytest.mark.asyncio
async def test_sync_progress_handler(monkeypatch):
    result, func = await _run_periodic(
        _handle_sync_progress, "_sync_download_progress", monkeypatch
    )
    assert result == {"status": "done"}
    func.assert_awaited_once()


@pytest.mark.asyncio
async def test_daily_cleanup_handler(monkeypatch):
    result, func = await _run_periodic(
        _handle_daily_cleanup, "_cleanup_expired", monkeypatch
    )
    assert result == {"status": "done"}
    func.assert_awaited_once()


@pytest.mark.asyncio
async def test_daily_dedup_handler(monkeypatch):
    result, func = await _run_periodic(
        _handle_daily_dedup, "_dedup_metadata", monkeypatch
    )
    assert result == {"status": "done"}
    func.assert_awaited_once()


@pytest.mark.asyncio
async def test_check_downloaders_handler(monkeypatch):
    result, func = await _run_periodic(
        _handle_check_downloaders, "_check_downloader_connections", monkeypatch
    )
    assert result == {"status": "done"}
    func.assert_awaited_once()


@pytest.mark.asyncio
async def test_fts_drain_handler(monkeypatch):
    result, func = await _run_periodic(_handle_fts_drain, "_drain_fts_outbox", monkeypatch)
    assert result == {"status": "done"}
    func.assert_awaited_once()


@pytest.mark.asyncio
async def test_fts_reconcile_handler(monkeypatch):
    result, func = await _run_periodic(_handle_fts_reconcile, "_reconcile_fts", monkeypatch)
    assert result == {"status": "done"}
    func.assert_awaited_once()


@pytest.mark.asyncio
async def test_download_notifications_handler(monkeypatch):
    result, func = await _run_periodic(
        _handle_download_notifications, "_process_download_notifications", monkeypatch
    )
    assert result == {"status": "done"}
    func.assert_awaited_once()


@pytest.mark.asyncio
async def test_refresh_resource_organize_handler(db_engine, monkeypatch):
    regen = AsyncMock(return_value={"status": "done", "regenerated": 1})
    monkeypatch.setattr(
        "app.services.notify_service.regenerate_resource_notifications", regen
    )
    result = await _handle_refresh_resource_organize({"resource_id": "rid"})
    regen.assert_awaited_once()
    assert result == {"status": "done", "regenerated": 1}


@pytest.mark.asyncio
async def test_resolve_magnet_torrent_handler(monkeypatch):
    launch = AsyncMock(return_value=True)
    monkeypatch.setattr("app.services.magnet_resolve.launch_resolution", launch)
    result = await _handle_resolve_magnet_torrent({"resource_id": "mid"})
    assert result == {"accepted": True}
    launch.assert_awaited_once_with("mid")


@pytest.mark.asyncio
async def test_magnet_resolve_sweep_enqueues_and_reclaims(db_engine, monkeypatch):
    """A stale pending row is reclaimed; a fresh NULL row is enqueued."""
    from sqlalchemy import select

    from app.database import async_session_factory
    from app.utils.time import utcnow

    stale, _ = await _make_resource(
        title_raw="stale", torrent_url="magnet:?xt=urn:btih:aaa",
        magnet_resolve_status="pending",
        magnet_resolve_updated_at=utcnow() - timedelta(days=30),
    )
    fresh, _ = await _make_resource(
        title_raw="fresh", torrent_url="magnet:?xt=urn:btih:bbb",
        magnet_resolve_status=None,
    )

    enqueue = AsyncMock()
    monkeypatch.setattr("app.services.magnet_resolve.enqueue_resolution", enqueue)

    result = await _handle_magnet_resolve_sweep({})

    # The reclaimed stale row and the fresh row are both enqueued in the same
    # sweep (reclaimed rows land on status NULL and are picked up below).
    assert result["reclaimed"] == 1
    assert result["enqueued"] == 2
    enqueue.assert_any_await(stale.id)
    enqueue.assert_any_await(fresh.id)

    async with async_session_factory() as session:
        stale_row = (await session.execute(
            select(FileResource).where(FileResource.id == stale.id)
        )).scalar_one()
        assert stale_row.magnet_resolve_status is None


@pytest.mark.asyncio
async def test_magnet_resolve_sweep_enqueue_error_swallowed(db_engine, monkeypatch):
    """One failing enqueue must not stop the sweep."""
    r1, _ = await _make_resource(
        title_raw="one", torrent_url="magnet:?xt=urn:btih:ccc",
    )
    r2, _ = await _make_resource(
        title_raw="two", torrent_url="magnet:?xt=urn:btih:ddd",
    )

    def _side(resource_id):
        if resource_id == r1.id:
            raise RuntimeError("bad row")
        return None

    enqueue = AsyncMock(side_effect=_side)
    monkeypatch.setattr("app.services.magnet_resolve.enqueue_resolution", enqueue)

    result = await _handle_magnet_resolve_sweep({})

    assert result["enqueued"] == 1
    assert result["reclaimed"] == 0


# ---------------------------------------------------------------------------
# Metadata refresh handlers
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_refresh_works_metadata_missing_source():
    result = await _handle_refresh_works_metadata({"items": []})
    assert result["status"] == "error"
    assert result["results"][0]["error"] == "source is required"


@pytest.mark.asyncio
async def test_refresh_works_metadata_ok(monkeypatch):
    batch = AsyncMock(return_value=[{"id": "w1", "found": True}])
    monkeypatch.setattr("app.job_handlers._refresh_works_batch", batch)
    result = await _handle_refresh_works_metadata({
        "items": [{"id": "w1", "content_type": "tv"}],
        "source": "wikipedia",
        "trusted_sites": ["bangumi"],
        "strategy": "sync_non_manual",
    })
    assert result["status"] == "done"
    assert result["processed"] == 1
    batch.assert_awaited_once()


@pytest.mark.asyncio
async def test_refresh_works_batch_found_and_not_found(db_engine, monkeypatch):
    """The batch loop: found work and work-not-found branches."""
    from app.database import async_session_factory
    from app.models.series import TVSeries

    sid = _uuid()
    async with async_session_factory() as session:
        session.add(TVSeries(
            id=sid, title_cn="Show", title_en="Show", original_title="Show",
            content_type="tv", season_number=1,
        ))
        await session.commit()

    refresh = AsyncMock(return_value={"found": True, "applied": ["title_cn"]})
    monkeypatch.setattr("app.services.metadata_search.refresh_work_by_source", refresh)

    from app.job_handlers import _refresh_works_batch
    results = await _refresh_works_batch(
        [{"id": sid, "content_type": "tv"}, {"id": "missing", "content_type": "movie"}],
        "wikipedia",
    )

    assert len(results) == 2
    assert results[0]["found"] is True
    assert results[1]["message"] == "work/title not found"


@pytest.mark.asyncio
async def test_refresh_works_batch_timeout(db_engine, monkeypatch):
    """A work whose refresh times out is recorded with an error, not raised."""
    from app.database import async_session_factory
    from app.models.series import TVSeries

    sid = _uuid()
    async with async_session_factory() as session:
        session.add(TVSeries(
            id=sid, title_cn="Show", title_en="Show", original_title="Show",
            content_type="tv", season_number=1,
        ))
        await session.commit()

    def _slow(*a, **k):
        raise TimeoutError("hung")

    monkeypatch.setattr("app.services.metadata_search.refresh_work_by_source", _slow)
    monkeypatch.setattr(
        "app.job_handlers._REFRESH_WORK_TIMEOUT", 1,
    )

    from app.job_handlers import _refresh_works_batch
    results = await _refresh_works_batch(
        [{"id": sid, "content_type": "tv"}], "wikipedia",
    )

    assert results[0]["error"] == "timeout"


@pytest.mark.asyncio
async def test_refresh_works_batch_generic_error(db_engine, monkeypatch):
    """A generic refresh failure is swallowed into a per-work error result."""
    from app.database import async_session_factory
    from app.models.series import TVSeries

    sid = _uuid()
    async with async_session_factory() as session:
        session.add(TVSeries(
            id=sid, title_cn="Show", title_en="Show", original_title="Show",
            content_type="tv", season_number=1,
        ))
        await session.commit()

    def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr("app.services.metadata_search.refresh_work_by_source", _boom)

    from app.job_handlers import _refresh_works_batch
    results = await _refresh_works_batch(
        [{"id": sid, "content_type": "tv"}], "wikipedia",
    )

    assert results[0]["error"] == "boom"


@pytest.mark.asyncio
async def test_refresh_channel_works_missing_channel(db_engine):
    result = await _handle_refresh_channel_works({"channel_id": _uuid()})
    assert result == {"status": "done", "processed": 0, "results": []}


@pytest.mark.asyncio
async def test_refresh_channel_works_inactive(db_engine, monkeypatch):
    from app.database import async_session_factory
    from app.models.channel import Channel

    cid = _uuid()
    async with async_session_factory() as session:
        session.add(Channel(
            id=cid, name="ch", type="rss_feed", url="https://x/rss",
            field_mapping={"list_locator": {"source": "entries"}},
            metadata_agent_enabled=False, status="inactive",
        ))
        await session.commit()

    result = await _handle_refresh_channel_works({"channel_id": cid})
    assert result == {"status": "done", "processed": 0, "results": []}


@pytest.mark.asyncio
async def test_refresh_channel_works_runs_batch(db_engine, monkeypatch):
    from app.database import async_session_factory
    from app.models.channel import Channel

    cid = _uuid()
    async with async_session_factory() as session:
        session.add(Channel(
            id=cid, name="ch", type="rss_feed", url="https://x/rss",
            field_mapping={"list_locator": {"source": "entries"}},
            metadata_agent_enabled=False, status="active",
            metadata_source="bangumi", metadata_refresh_full_scope=True,
        ))
        await session.commit()

    monkeypatch.setattr(
        "app.services.metadata_service.select_channel_works_for_refresh",
        AsyncMock(return_value=[{"id": "w1", "content_type": "tv"}]),
    )
    monkeypatch.setattr(
        "app.services.metadata_sources.resolve_metadata_source",
        lambda source: "bangumi",
    )
    batch = AsyncMock(return_value=[{"id": "w1", "found": True}])
    monkeypatch.setattr("app.job_handlers._refresh_works_batch", batch)

    result = await _handle_refresh_channel_works({"channel_id": cid})

    assert result["status"] == "done"
    assert result["processed"] == 1
    batch.assert_awaited_once()


# ---------------------------------------------------------------------------
# Analyze-batch LLM merge branches
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_analyze_batch_llm_merge_branches(db_engine):
    """Covers the LLM-merge loop: missing placement (continue), season fill,
    and episode fill (start == end)."""
    from app.api.v1 import resources as res_mod
    from app.services import batch_content_analysis as bca
    from app.services import torrent_inspect as ti

    series_id = _uuid()
    resource, _ = await _make_resource(series_id=series_id, season=1)

    report = TorrentReport(
        scope="season", is_batch=True, season_ranges=[],
        file_parses=[
            {"path": "dir/a.mkv", "size": 100, "season": None, "episode": None},
            {"path": "dir/b.mkv", "size": 200, "season": 1, "episode": None},
        ],
    )

    async def fake_stream(*args, **kwargs):
        yield "result", {"works": []}

    # llm_works via _valid_paths: one placement for a path that does not exist
    # in the deterministic listing (continue), one that sets season, one that
    # sets episode.
    llm_works = [
        {
            "files": [
                {"path": "nonexistent.mkv", "season": 1, "episode_start": 1, "episode_end": 1},
                {"path": "dir/a.mkv", "season": 2, "episode_start": 5, "episode_end": 5},
                {"path": "dir/b.mkv", "season": 1, "episode_start": 3, "episode_end": 3},
            ]
        }
    ]

    with patch.object(
        res_mod, "_resolve_resource_files",
        new=AsyncMock(return_value=(
            [{"name": "dir/a.mkv", "size": 100}, {"name": "dir/b.mkv", "size": 200}],
            "torrent_cache",
        )),
    ), patch.object(
        res_mod, "_store_batch_analysis", new=AsyncMock(),
    ), patch.object(
        bca, "build_candidate_works", new=AsyncMock(return_value=[]),
    ), patch.object(
        bca, "resolve_fractional_specials", new=AsyncMock(return_value={}),
    ), patch.object(
        bca, "analyze_listing_stream", new=fake_stream,
    ), patch.object(
        bca, "_valid_paths", return_value=llm_works,
    ), patch.object(
        ti, "analyze_torrent_files", return_value=report,
    ):
        result = await _handle_analyze_batch_files({
            "resource_id": resource.id,
            "fingerprint": "fp-merge",
            "job_key": "job-merge",
        })

    deterministic = result["suggestion"]["deterministic"]
    files = {fp["path"]: fp for fp in deterministic["files"]}
    # a.mkv: season filled by LLM to 2, episode filled to 5.
    assert files["dir/a.mkv"]["season"] == 2
    assert files["dir/a.mkv"]["episode"] == 5
    # b.mkv: season stays 1, episode filled to 3.
    assert files["dir/b.mkv"]["season"] == 1
    assert files["dir/b.mkv"]["episode"] == 3


def test_register_all_handlers():
    """Every job type is registered on the queue."""
    queue = MagicMock()
    register_all_handlers(queue)
    registered = {call.args[0] for call in queue.register.call_args_list}
    expected = {
        "fetch_channel", "run_agent", "refresh_works_metadata",
        "refresh_channel_works", "backfill_metadata", "analyze_batch_files",
        "sync_progress", "daily_cleanup", "daily_dedup", "check_downloaders",
        "fts_drain", "fts_reconcile", "download_notifications",
        "refresh_resource_organize", "resolve_magnet_torrent",
        "magnet_resolve_sweep", "reprocess_resource_metadata",
    }
    assert registered == expected
