"""Tests for the explicitly scoped resource listing repair script."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from scripts import repair_resource_listings as repair


def _resource(**overrides):
    values = {
        "id": str(uuid.uuid4()),
        "title_raw": "[LoliHouse] Example Show S01E01 [1080p]",
        "is_batch": False,
        "batch_scope": None,
        "batch_seasons": None,
        "subtitle_group": None,
        "subtitle_groups": None,
        "subtitle_groups_source": None,
        "channel_default_fields": [],
        "episode_confidence": None,
        "series_id": None,
        "movie_id": None,
        "audio_work_id": None,
        "collection_id": None,
        "series": None,
        "work_links": [],
        "file_assignments": [],
        "channel": SimpleNamespace(required_metadata_fields=[]),
        "torrent_file": None,
        "torrent_url": "",
        "magnet_url": None,
        "listing_reconcile_status": None,
        "listing_reconcile_error": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _Session:
    def __init__(self):
        self.commit = AsyncMock()
        self.rollback = AsyncMock()


class _SessionContext:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *_args):
        return None


def _install_run_mocks(monkeypatch, resources, source="torrent_cache"):
    session = _Session()
    by_id = {resource.id: resource for resource in resources}

    async def load(_db, resource_id):
        return by_id.get(resource_id)

    async def available(_db, _resource):
        return source

    monkeypatch.setattr(repair, "_load_resource", load)
    monkeypatch.setattr(repair, "available_listing_source", available)
    monkeypatch.setattr(
        "app.database.async_session_factory", lambda: _SessionContext(session)
    )
    return session


def test_no_arguments_prints_help_without_opening_database(monkeypatch, capsys):
    factory = AsyncMock(side_effect=AssertionError("database must not be opened"))
    monkeypatch.setattr("app.database.async_session_factory", factory)

    assert repair.main([]) == 2
    assert repair.main(["--apply"]) == 2
    out = capsys.readouterr().out
    assert "--resource-id" in out and "--known-issues" in out
    factory.assert_not_called()


def test_repeated_resource_ids_are_processed_once(monkeypatch, capsys):
    resource = _resource()
    session = _install_run_mocks(monkeypatch, [resource], source="none")

    summary = repair.asyncio.run(repair.run([resource.id, resource.id]))

    assert summary.planned == 1 and summary.failed == 0
    assert "selected_resources=1" in capsys.readouterr().out
    session.rollback.assert_awaited_once()


def test_main_known_issues_uses_explicit_reviewed_set(monkeypatch):
    ids = [str(uuid.uuid4()) for _ in range(6)]
    run = AsyncMock(return_value=repair.RunSummary(planned=6))
    monkeypatch.setattr(repair, "KNOWN_ISSUE_RESOURCE_IDS", tuple(ids))
    monkeypatch.setattr(repair, "run", run)

    assert repair.main(["--known-issues"]) == 0
    run.assert_awaited_once_with(ids, apply=False)


def test_reviewed_infohash_adds_only_idempotent_magnet_fallback():
    resource_id, infohash = next(iter(repair.KNOWN_ISSUE_MAGNETS.items()))
    resource = _resource(id=resource_id)

    plan = repair.plan_repairs(resource)
    assert plan.changes["magnet_url"] == f"magnet:?xt=urn:btih:{infohash}"
    repair.apply_plan(resource, plan)
    assert "magnet_url" not in repair.plan_repairs(resource).changes


def test_compilation_recovers_declared_multi_season_coverage():
    resource = _resource(
        title_raw="[整理搬运] 某作品 全三季：TV动画+剧场版",
        is_batch=True,
        batch_scope="season",
        batch_seasons=None,
    )

    plan = repair.plan_repairs(resource)

    assert plan.changes == {
        "batch_scope": "multi_season",
        "batch_seasons": [1, 2, 3],
    }
    assert resource.batch_scope == "season"  # planning never mutates the ORM row
    repair.apply_plan(resource, plan)
    assert repair.plan_repairs(resource).changes == {}


def test_compilation_never_overwrites_manual_shape():
    resource = _resource(
        title_raw="[整理搬运] 某作品 1-3季：TV动画",
        batch_scope="season",
        file_assignments=[SimpleNamespace(source="manual")],
    )

    plan = repair.plan_repairs(resource)

    assert not plan.changes
    assert "manual listing shape preserved" in plan.notes


def test_technical_group_is_cleared_and_conservatively_backfilled():
    resource = _resource(
        subtitle_group="AVC_FLAC",
        subtitle_groups=["AVC_FLAC"],
        subtitle_groups_source="llm",
        channel_default_fields=["subtitle_group", "resolution"],
    )

    plan = repair.plan_repairs(resource)

    assert plan.changes == {
        "subtitle_group": "LoliHouse",
        "subtitle_groups": ["LoliHouse"],
        "subtitle_groups_source": "heuristic",
        "channel_default_fields": ["resolution"],
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"subtitle_groups_source": "manual"},
        {"subtitle_group": "RealGroup", "subtitle_groups": ["RealGroup"]},
        {"subtitle_group": "RealGroup", "subtitle_groups": ["AVC_FLAC"]},
    ],
)
def test_real_or_manual_group_is_never_overwritten(overrides):
    base = {
        "subtitle_group": "AVC_FLAC",
        "subtitle_groups": ["AVC_FLAC"],
        "subtitle_groups_source": "llm",
    }
    resource = _resource(**(base | overrides))

    assert "subtitle_group" not in repair.plan_repairs(resource).changes


def test_technical_group_without_real_title_group_is_left_empty_and_idempotent():
    resource = _resource(
        title_raw="Example Show S01E01 [1080p AVC FLAC]",
        subtitle_group="x265.OPUS",
        subtitle_groups=["x265.OPUS"],
        subtitle_groups_source="heuristic",
    )

    plan = repair.plan_repairs(resource)
    assert plan.changes == {
        "subtitle_group": None,
        "subtitle_groups": None,
        "subtitle_groups_source": None,
    }
    repair.apply_plan(resource, plan)
    assert repair.plan_repairs(resource).changes == {}


def test_compilation_tag_is_not_backfilled_as_release_group():
    resource = _resource(
        title_raw="[整理搬运] 某作品 全三季：TV动画",
        subtitle_group="AVC_FLAC",
        subtitle_groups=["AVC_FLAC"],
    )

    plan = repair.plan_repairs(resource)

    assert plan.changes["subtitle_group"] is None
    assert plan.changes["subtitle_groups"] is None


async def test_dry_run_has_no_network_llm_reconcile_dispatch_or_writes(monkeypatch, capsys):
    resource = _resource(
        title_raw="[整理搬运] 某作品 全三季：TV动画",
        subtitle_group="AVC_FLAC",
        subtitle_groups=["AVC_FLAC"],
    )
    session = _install_run_mocks(monkeypatch, [resource])
    reconcile = AsyncMock(side_effect=AssertionError("reconcile called"))
    retry = AsyncMock(side_effect=AssertionError("retry called"))
    monkeypatch.setattr("app.services.resource_listing.reconcile_resource", reconcile)
    monkeypatch.setattr("app.services.resource_listing.retry_resource_torrent", retry)

    summary = await repair.run([resource.id])

    assert summary.planned == 1 and summary.failed == 0
    assert resource.batch_scope is None
    assert resource.subtitle_group == "AVC_FLAC"
    session.commit.assert_not_awaited()
    reconcile.assert_not_awaited()
    retry.assert_not_awaited()
    out = capsys.readouterr().out
    assert "coverage=" in out and "missing_fields=" in out
    assert "roles=" in out and "linked_seasons=" in out
    assert "某作品" not in out and "no network, LLM" in out


async def test_apply_commits_fields_then_uses_shared_reconcile(monkeypatch):
    resource = _resource(subtitle_group="AVC_FLAC", subtitle_groups=["AVC_FLAC"])
    session = _install_run_mocks(monkeypatch, [resource])
    reconcile = AsyncMock(return_value=SimpleNamespace(error=None, files=[{"name": "a"}]))
    retry = AsyncMock()
    monkeypatch.setattr("app.services.resource_listing.reconcile_resource", reconcile)
    monkeypatch.setattr("app.services.resource_listing.retry_resource_torrent", retry)

    summary = await repair.run([resource.id], apply=True)

    assert summary.applied == 1 and summary.failed == 0
    assert resource.subtitle_groups == ["LoliHouse"]
    session.commit.assert_awaited_once()
    reconcile.assert_awaited_once_with(resource.id, force=True)
    retry.assert_not_awaited()


async def test_reconcile_failure_is_not_reported_as_success(monkeypatch, capsys):
    resource = _resource()
    _install_run_mocks(monkeypatch, [resource])
    reconcile = AsyncMock(return_value=SimpleNamespace(
        error="https://private.example/secret", files=[]
    ))
    monkeypatch.setattr("app.services.resource_listing.reconcile_resource", reconcile)

    summary = await repair.run([resource.id], apply=True)

    assert summary.applied == 0 and summary.failed == 1
    out = capsys.readouterr().out
    assert "result=failed" in out
    assert "private.example" not in out and "secret" not in out


async def test_competing_successful_reconcile_is_idempotent_success(monkeypatch):
    resource = _resource(listing_reconcile_status="done")
    _install_run_mocks(monkeypatch, [resource])
    reconcile = AsyncMock(return_value=SimpleNamespace(error=None, files=[]))
    monkeypatch.setattr("app.services.resource_listing.reconcile_resource", reconcile)

    summary = await repair.run([resource.id], apply=True)

    assert summary.applied == 1 and summary.failed == 0


async def test_http_without_listing_uses_retry_and_verifies_reconcile(monkeypatch):
    resource = _resource(
        torrent_url="https://private.example/a.torrent",
        listing_reconcile_status="done",
    )
    _install_run_mocks(monkeypatch, [resource], source="http")
    retry = AsyncMock(return_value={"fetched": True, "error": None})
    reconcile = AsyncMock()
    monkeypatch.setattr("app.services.resource_listing.retry_resource_torrent", retry)
    monkeypatch.setattr("app.services.resource_listing.reconcile_resource", reconcile)

    summary = await repair.run([resource.id], apply=True)

    assert summary.applied == 1 and summary.failed == 0
    retry.assert_awaited_once_with(resource.id)
    reconcile.assert_not_awaited()


async def test_http_fetch_with_failed_reconcile_is_a_failure(monkeypatch):
    resource = _resource(
        torrent_url="https://private.example/a.torrent",
        listing_reconcile_status="failed",
        listing_reconcile_error="sensitive details",
    )
    _install_run_mocks(monkeypatch, [resource], source="http")
    retry = AsyncMock(return_value={"fetched": True, "error": None})
    monkeypatch.setattr("app.services.resource_listing.retry_resource_torrent", retry)

    summary = await repair.run([resource.id], apply=True)

    assert summary.applied == 0 and summary.failed == 1


async def test_retry_magnet_fallback_is_pending_not_success(monkeypatch, capsys):
    resource = _resource(
        torrent_url="https://private.example/a.torrent",
        magnet_url="magnet:?xt=urn:btih:private",
    )
    _install_run_mocks(monkeypatch, [resource], source="http")
    retry = AsyncMock(return_value={"fetched": False, "error": "fetch failed"})
    monkeypatch.setattr("app.services.resource_listing.retry_resource_torrent", retry)

    summary = await repair.run([resource.id], apply=True)

    assert summary.pending == 1 and summary.applied == 0 and summary.failed == 0
    out = capsys.readouterr().out
    assert "result=magnet-pending" in out
    assert "private" not in out and "fetch failed" not in out


async def test_missing_id_is_a_failure_and_does_not_stop_other_ids(monkeypatch):
    resource = _resource()
    _install_run_mocks(monkeypatch, [resource], source="none")

    summary = await repair.run([str(uuid.uuid4()), resource.id], apply=True)

    assert summary.failed == 1 and summary.skipped == 1


async def test_one_shot_apply_uses_configured_persistent_queue(monkeypatch):
    from app.services import task_queue as queue_module

    previous = queue_module.task_queue
    queue = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
    monkeypatch.setattr(
        queue_module, "create_queue", lambda **_kwargs: queue
    )
    run = AsyncMock(return_value=repair.RunSummary(applied=1))
    monkeypatch.setattr(repair, "run", run)

    summary = await repair._run_apply_with_persistent_queue(["resource-1"])

    assert summary.applied == 1
    queue.start.assert_awaited_once_with(consume=False)
    queue.stop.assert_awaited_once()
    run.assert_awaited_once_with(["resource-1"], apply=True)
    assert queue_module.task_queue is previous


async def test_multi_season_repair_links_declared_seasons_before_retry(monkeypatch):
    resource = _resource(
        is_batch=True,
        batch_scope="multi_season",
        batch_seasons=[1, 2],
        magnet_url="magnet:?xt=urn:btih:private",
    )
    session = _install_run_mocks(monkeypatch, [resource], source="http")
    link = AsyncMock()
    monkeypatch.setattr(
        "app.services.declared_season_links.link_declared_seasons", link
    )
    retry = AsyncMock(return_value={"fetched": False, "error": "offline"})
    monkeypatch.setattr("app.services.resource_listing.retry_resource_torrent", retry)

    summary = await repair.run([resource.id], apply=True)

    assert summary.pending == 1 and summary.failed == 0
    link.assert_awaited_once()
    session.commit.assert_awaited_once()
    retry.assert_awaited_once_with(resource.id)
