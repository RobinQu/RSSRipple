"""Real reviewed torrent filename -> notification -> persisted plan -> execution.

Media bytes are deterministic synthetic payloads, NOT captured media. The RPC
manifest retains the captured size; planning uses the actual temporary file's
size. Source provenance/semantics come from the immutable independent review.
Only downloader RPC and media-server refresh are replaced at the system edge.
"""

import hashlib
import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from app.models.download_task import DownloadTask
from app.models.episode import Episode
from app.models.organize_plan import OrganizePlan
from app.models.organize_plan_op import OrganizePlanOp
from app.models.organize_rule import OrganizeRule
from app.models.series import TVSeries
from app.models.work_collection import WorkCollection
from app.services.organize_service import execute_plan
from app.services.scheduler import _process_download_notifications
from app.services.torrent_inspect import parse_torrent_payload
from tests.integration.organize.test_organize_pipeline import (
    TV_TEMPLATE,
    _audit_actions,
    _library,
    _ops,
    _rule,
    _seed_chain,
)
from tests.metadata_corpus.dataset import ROOT, asset, digest, load_corpus, read_json

CASE_ID = "f79ef2eb-02d5-42d3-80dc-70dd3c1d733b"


@pytest.mark.parametrize("mode", ["move", "copy", "hardlink"])
@pytest.mark.parametrize("scenario", ["new", "identical_target", "different_tail", "legacy_collision", "config_changed"])
async def test_real_manifest_file_safety(
    db_session, session_factory, shared_volume, rpc_mocks, refresh_mock,
    mode, scenario, record_testsuite_property,
):
    manifest, corpus, reviews = load_corpus(ROOT)
    case = next(case for case in corpus["cases"] if case["id"] == CASE_ID)
    review = reviews[CASE_ID]
    assert review["status"] == "confirmed"
    torrent_path = asset(ROOT, case["evidence"]["torrent"])
    raw = torrent_path.read_bytes()
    assert digest(raw) == torrent_path.stem
    listing_path = asset(ROOT, manifest["file_list_file"])
    assert digest(listing_path.read_bytes()) == manifest["file_list_sha256"]
    listing = read_json(listing_path)[case["evidence"]["torrent"]]
    assert parse_torrent_payload(raw) == listing
    [entry] = listing
    [assignment] = review["expected"]["assignments"]
    assert assignment["file_path"] == entry["name"]
    record_testsuite_property("real_case", CASE_ID)
    record_testsuite_property("torrent_sha256", torrent_path.stem)
    record_testsuite_property("original_media_size", entry["size"])
    record_testsuite_property("media_bytes", "synthetic; original filenames and reviewed S1E10 retained")

    collection = WorkCollection(id=str(uuid.uuid4()), title_cn="猫与龙")
    db_session.add(collection)
    series = TVSeries(
        id=str(uuid.uuid4()), title_cn="猫与龙", original_title="猫と竜",
        external_source="bangumi", external_id="bangumi:538760",
        collection_id=collection.id, season_number=1, number_of_episodes=12,
        content_type="tv", is_anime=True,
    )
    db_session.add_all([
        Episode(id=str(uuid.uuid4()), series_id=series.id, season=1, episode=number)
        for number in range(1, 13)
    ])
    chain = await _seed_chain(
        db_session, work=series,
        resource_kw=dict(title_raw=case["input"]["title_raw"],
                         season=assignment["season"], episode=assignment["episode_start"],
                         is_batch=False, container="mkv"),
        download_dir=shared_volume.complete_daemon, volume=shared_volume.volume,
        downloader_dir=shared_volume.daemon,
    )
    folder = shared_volume.complete_process / "captured-torrent"
    folder.mkdir()
    src = folder / entry["name"]
    # Exceeds the comparison chunk: a difference only in the tail must fail.
    body = hashlib.sha256(raw).digest() * 32769
    src.write_bytes(body)
    record_testsuite_property("test_media_size", len(body))
    rpc_mocks.get_files.return_value = {"name": folder.name, "files": listing}
    library = await _library(db_session, shared_volume.media / "tv")
    await _rule(db_session, library.id, TV_TEMPLATE, file_op=mode)
    await _process_download_notifications()

    async with session_factory() as session:
        plan = (await session.execute(select(OrganizePlan))).scalar_one()
        assert plan.status == "pending", plan.error_message
        [op] = await _ops(session, plan.id)
        assert Path(op.src) == src and op.size == len(body)
        assert plan.file_op == mode
        effective_mode = mode
        dst = Path(op.dst)
        assert "s01e10" in dst.name.lower()
        if scenario in {"identical_target", "different_tail"}:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if scenario == "identical_target" and mode == "hardlink":
                os.link(src, dst)
            else:
                dst.write_bytes(body if scenario == "identical_target" else body[:-1] + b"!")
        if scenario == "legacy_collision":
            other = src.with_name("duplicate-" + src.name)
            other.write_bytes(body[:-1] + b"?")
            session.add(OrganizePlanOp(
                plan_id=plan.id, seq=op.seq + 1, op_type="move",
                src=str(other), dst=op.dst, size=len(body),
            ))
            await session.commit()  # imitate an old unsafe persisted plan
        if scenario == "config_changed":
            # Real reviewed manifest through the rule-commit-before-rebuild
            # window: execution must replace the old ops before switching mode.
            effective_mode = {"move": "copy", "copy": "hardlink", "hardlink": "move"}[mode]
            rule = await session.get(OrganizeRule, plan.rule_id)
            rule.file_op = effective_mode
            await session.commit()
            record_testsuite_property("initial_file_op", mode)
            record_testsuite_property("effective_file_op", effective_mode)
        result = await execute_plan(session, plan.id)
        if scenario == "config_changed":
            assert result.file_op == effective_mode
            assert result.revision >= 3
            assert op.id not in {current.id for current in await _ops(session, plan.id)}
        task = await session.get(DownloadTask, chain.task.id)
        actions = await _audit_actions(session, plan.id)
        if scenario in {"different_tail", "legacy_collision"}:
            assert result.status == "failed"
            assert "前置门禁" in result.error_message
            assert src.read_bytes() == body
            assert task.status == "completed"
            assert "cleanup" not in actions
            rpc_mocks.remove.assert_not_awaited()
            refresh_mock.assert_not_awaited()
            if scenario == "different_tail":
                assert dst.read_bytes() == body[:-1] + b"!"
            else:
                assert not dst.exists()
                assert other.read_bytes() == body[:-1] + b"?"
            # A failed plan remains safely retryable; no source is lost.
            assert (await execute_plan(session, plan.id)).status == "failed"
            assert src.read_bytes() == body
        else:
            assert result.status == "done", result.error_message
            assert dst.read_bytes() == body
            assert src.exists() == (effective_mode != "move")
            if effective_mode == "hardlink":
                assert src.stat().st_ino == dst.stat().st_ino
            assert task.status == ("cancelled" if effective_mode == "move" else "completed")
            if effective_mode == "move":
                rpc_mocks.remove.assert_awaited_once_with(42, delete_data=False)
            else:
                rpc_mocks.remove.assert_not_awaited()
            assert (await execute_plan(session, plan.id)).status == "done"
            assert dst.read_bytes() == body
