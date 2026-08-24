"""organize 深度分支的进程内集成测试。

三块互补面（均纳入 .coverage.test-runner 覆盖率）：

- 执行器（organize_executor）：纯文件 IO 模块的全部分支——前置门禁违例、
  幂等状态表、hardlink 失败绝不退化为 copy、copy 校验失败、move 的 EXDEV
  跨文件系统退化、movedir 冲突、后置校验、空目录清理边界。
- 规划器多作品包（_plan_same_target_multi_work）：合并成功 / 目标不一致
  拒绝 / 作品元数据缺失 / 未命中规则。
- manifest 回退链（_resolve_manifest）：torrent 缓存命中（多文件种子补
  info/name 根分量）→ torrent_url 拉取回写缓存 → 下载器 RPC，以及不安全
  路径分量的过滤。
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import bencodepy
import pytest
from sqlalchemy import select

from app.models.channel import Channel
from app.models.download_task import DownloadTask
from app.models.file_resource import FileResource
from app.models.organize_plan import OrganizePlan
from app.models.organize_plan_op import OrganizePlanOp
from app.services import organize_service as osvc
from app.services.organize_executor import (
    ExecOp,
    cleanup_empty_dirs,
    execute_movedir,
    execute_ops,
    precheck,
    run_execution,
    verify_done,
)
from app.services.organize_planner import (
    DiskFile,
    PlanError,
    build_plan,
)


def _uuid() -> str:
    return str(uuid.uuid4())


def _mk(path: Path, size: int = 10) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


# ---------------------------------------------------------------------------
# 执行器：前置门禁 / 幂等状态表 / 三种文件操作
# ---------------------------------------------------------------------------


class TestPrecheck:
    def test_violations(self, tmp_path):
        src = _mk(tmp_path / "a.mkv", 100)
        ops = [
            # dst 已存在但大小不符 → 冲突
            ExecOp("move", str(_mk(tmp_path / "b.mkv", 5)),
                   str(_mk(tmp_path / "dst1.mkv", 999)), 100),
            # src 与 dst 均不存在
            ExecOp("move", str(tmp_path / "gone.mkv"),
                   str(tmp_path / "dst2.mkv"), 10),
            # src 大小与快照不符
            ExecOp("move", str(src), str(tmp_path / "dst3.mkv"), 999),
        ]
        violations = precheck(ops)
        assert len(violations) == 3
        # movedir：dst 在且 src 也在 → 冲突；src 已被移走 → 幂等满足
        d1 = _mk(tmp_path / "d1" / "f.mkv", 3).parent
        d2 = _mk(tmp_path / "d2" / "f.mkv", 3).parent
        assert any("目标目录已存在" in v for v in precheck([
            ExecOp("movedir", str(d1), str(d2), 0),
        ]))
        assert precheck([ExecOp("movedir", str(d2),
                                str(tmp_path / "elsewhere"), 0)]) == []
        assert precheck([ExecOp("keep", "/x", None, 0)]) == []


class TestExecuteOps:
    def test_hardlink_success_keeps_src(self, tmp_path):
        src = _mk(tmp_path / "show.mkv", 50)
        dst = tmp_path / "lib" / "show.mkv"
        results = execute_ops(
            [ExecOp("move", str(src), str(dst), 50)], file_op="hardlink"
        )
        assert results[0].status == "done"
        assert dst.exists() and dst.stat().st_size == 50
        assert src.exists()  # 保种

    def test_hardlink_failure_never_degrades_to_copy(self, tmp_path, monkeypatch):
        src = _mk(tmp_path / "a.mkv", 10)
        dst = tmp_path / "sub" / "a.mkv"

        def eperm(link_src, link_dst):
            raise OSError(1, "Operation not permitted")

        monkeypatch.setattr(os, "link", eperm)
        results = execute_ops(
            [ExecOp("move", str(src), str(dst), 10)], file_op="hardlink"
        )
        assert results[0].status == "failed"
        assert "硬链接失败" in results[0].error
        assert not dst.exists()  # 绝不静默复制

    def test_hardlink_idempotent_states(self, tmp_path):
        src = _mk(tmp_path / "s.mkv", 7)
        done_dst = _mk(tmp_path / "d1.mkv", 7)
        conflict_dst = _mk(tmp_path / "d2.mkv", 9)
        results = execute_ops([
            ExecOp("move", str(src), str(done_dst), 7),
            ExecOp("move", str(src), str(conflict_dst), 7),
            ExecOp("move", str(src), str(src), 7),  # src == dst
            ExecOp("move", str(tmp_path / "nope.mkv"), str(tmp_path / "x.mkv"), 1),
        ], file_op="hardlink")
        assert [r.status for r in results] == [
            "done", "failed", "done", "failed",
        ]

    def test_copy_size_verify_failure_removes_partial_dst(self, tmp_path, monkeypatch):
        import shutil as _shutil

        src = _mk(tmp_path / "c.mkv", 20)
        dst = tmp_path / "out" / "c.mkv"

        real_copyfile = _shutil.copyfile

        def short_copy(s, d):
            real_copyfile(s, d)
            with open(d, "wb") as f:
                f.write(b"short")

        monkeypatch.setattr(_shutil, "copyfile", short_copy)
        results = execute_ops(
            [ExecOp("move", str(src), str(dst), 20)], file_op="copy"
        )
        assert results[0].status == "failed"
        assert "复制校验失败" in results[0].error
        assert not dst.exists()

    def test_copy_success_keeps_src(self, tmp_path):
        src = _mk(tmp_path / "k.mkv", 33)
        dst = tmp_path / "o" / "k.mkv"
        results = execute_ops(
            [ExecOp("move", str(src), str(dst), 33)], file_op="copy"
        )
        assert results[0].status == "done"
        assert src.exists() and dst.stat().st_size == 33

    def test_move_exdev_falls_back_to_copy_and_unlinks_src(self, tmp_path, monkeypatch):
        src = _mk(tmp_path / "ex.mkv", 12)
        dst = tmp_path / "n" / "ex.mkv"

        def exdev(rs, rd):
            raise OSError(18, "Invalid cross-device link")

        monkeypatch.setattr(os, "rename", exdev)
        results = execute_ops([ExecOp("move", str(src), str(dst), 12)])
        assert results[0].status == "done"
        assert dst.exists() and not src.exists()

    def test_move_exdev_copy_verify_failure(self, tmp_path, monkeypatch):
        src = _mk(tmp_path / "bad.mkv", 12)
        dst = tmp_path / "n2" / "bad.mkv"

        def exdev(rs, rd):
            raise OSError(18, "Invalid cross-device link")

        monkeypatch.setattr(os, "rename", exdev)

        import shutil as _shutil

        def short_copy(s, d):
            with open(d, "wb") as f:
                f.write(b"partial")

        monkeypatch.setattr(_shutil, "copyfile", short_copy)
        results = execute_ops([ExecOp("move", str(src), str(dst), 12)])
        assert results[0].status == "failed"
        assert "跨文件系统复制校验失败" in results[0].error
        assert not dst.exists()


class TestMovedirAndVerifyAndCleanup:
    def test_movedir_branches(self, tmp_path):
        src_full = _mk(tmp_path / "s1" / "f.mkv", 4).parent
        dst_taken = _mk(tmp_path / "s2" / "g.mkv", 4).parent
        assert "拒绝覆盖" in execute_movedir(
            ExecOp("movedir", str(src_full), str(dst_taken), 0))
        empty_src = tmp_path / "empty-src"
        empty_src.mkdir()
        assert execute_movedir(
            ExecOp("movedir", str(empty_src), str(tmp_path / "e-dst"), 0)
        ) is None
        missing_src = tmp_path / "nope-dir"
        assert "均不存在" in execute_movedir(
            ExecOp("movedir", str(missing_src), str(tmp_path / "x"), 0))
        ok_src = _mk(tmp_path / "mv" / "f.mkv", 4).parent
        assert execute_movedir(
            ExecOp("movedir", str(ok_src), str(tmp_path / "mv-dst"), 0)
        ) is None
        assert (tmp_path / "mv-dst" / "f.mkv").exists()

    def test_verify_done_problems(self, tmp_path):
        good_dst = _mk(tmp_path / "ok.mkv", 8)
        leftover_src = _mk(tmp_path / "leftover.mkv", 8)  # move 后应消失却还在
        bad_size = _mk(tmp_path / "wrong.mkv", 3)
        ops = [
            ExecOp("move", str(leftover_src), str(good_dst), 8),
            ExecOp("move", "/gone/x.mkv", str(bad_size), 8),
            ExecOp("move", "/gone/y.mkv", str(tmp_path / "missing-dst.mkv"), 1),
            ExecOp("keep", "/z", None, 0),
        ]
        problems = verify_done(ops, file_op="move")
        assert len(problems) == 3
        assert any("残留" in p for p in problems)
        # hardlink/copy 不校验 src 残留
        problems_hl = verify_done(ops[:1], file_op="hardlink")
        assert problems_hl == []

    def test_cleanup_empty_dirs_preserves_boundary(self, tmp_path):
        root = tmp_path / "dl"
        keep_dir = root / "complete" / "season"
        keep_dir.mkdir(parents=True)
        _mk(keep_dir / "f.mkv", 2)          # 非空目录保留
        empty_leaf = root / "complete" / "show"
        empty_leaf.mkdir(parents=True)
        removed = cleanup_empty_dirs(root / "complete", preserve=root / "complete")
        assert removed == [str(empty_leaf)]
        assert (root / "complete").exists()
        # root 不存在 → 空列表
        assert cleanup_empty_dirs(tmp_path / "ghost") == []


class TestRunExecution:
    def test_precheck_failure_aborts_without_touching_files(self, tmp_path):
        src = _mk(tmp_path / "a.mkv", 10)
        ops = [ExecOp("move", str(src), str(_mk(tmp_path / "taken.mkv", 99)), 10)]
        outcome = run_execution(ops)
        assert not outcome.ok and "前置门禁" in outcome.error
        assert outcome.audits[0]["action"] == "precheck"
        assert src.exists()

    def test_op_failure_short_circuits_before_verify(self, tmp_path, monkeypatch):
        def eperm(s, d):
            raise OSError(1, "denied")

        monkeypatch.setattr(os, "link", eperm)
        src = _mk(tmp_path / "h.mkv", 5)
        outcome = run_execution(
            [ExecOp("move", str(src), str(tmp_path / "o" / "h.mkv"), 5)],
            file_op="hardlink",
        )
        assert not outcome.ok and "文件操作失败" in outcome.error
        assert outcome.audits[0]["action"] == "hardlink"

    def test_success_move_with_cleanup_audit(self, tmp_path):
        torrent_dir = _mk(tmp_path / "dl" / "Show" / "a.mkv", 15).parent
        lib = tmp_path / "lib"
        ops = [
            ExecOp("move", str(torrent_dir / "a.mkv"), str(lib / "a.mkv"), 15),
            ExecOp("keep", str(torrent_dir / "extra.mkv"), None, 0),
        ]
        outcome = run_execution(
            ops, cleanup_root=str(torrent_dir), preserve=str(tmp_path / "dl")
        )
        assert outcome.ok
        actions = [a["action"] for a in outcome.audits]
        assert "move" in actions and "keep" in actions and "cleanup" in actions
        # a.mkv 移走后 Show 变空被删；extra.mkv 所在目录保留
        assert not torrent_dir.exists()


# ---------------------------------------------------------------------------
# 规划器：多作品包（_plan_same_target_multi_work）
# ---------------------------------------------------------------------------

_TV_WORK = {
    "type": "series", "series_id": "", "title_cn": "", "title_en": "",
    "original_title": "", "year": 2026, "content_type": "tv",
    "is_anime": True, "collection": None, "genre": ["Animation"],
    "seasons": None, "episodes": None,
}


def _multi_work_payload(second_anime=True):
    first = {**_TV_WORK, "series_id": "s-1", "title_cn": "作品甲"}
    second = {
        **_TV_WORK, "series_id": "s-2", "title_cn": "作品乙",
        "is_anime": second_anime,
    }
    items = [
        {"file_path": "A/e01.mkv", "file_size": 300, "work_type": "series",
         "work_id": "s-1", "season": 1, "episode_start": 1,
         "episode_end": 1, "source": "manual"},
        {"file_path": "B/e02.mkv", "file_size": 300, "work_type": "series",
         "work_id": "s-2", "season": 1, "episode_start": 2,
         "episode_end": 2, "source": "manual"},
    ]
    return {
        "notification_id": "n-multi",
        "agent": None,
        "task": {"download_task_id": "t-1", "download_dir": "/downloads",
                 "torrent_name": "Pack"},
        "resource": {"id": "r-1", "title_raw": "Pack", "season": None,
                     "episode": None, "is_batch": True,
                     "batch_scope": "franchise", "collection": None,
                     "episode_start": None, "episode_end": None,
                     "subtitle_langs": None, "resolution": None,
                     "container": None, "title_year": None},
        "work": {"type": None},
        "works": {"series:s-1": first, "series:s-2": second},
        "file_associations": {"version": 1, "status": "complete",
                              "items": items},
    }


def _lib(name="tv", root="/media/tv"):
    return SimpleNamespace(id=f"lib-{name}", name=name, root_path=root,
                           kind="tv", subtitle_lang_map=None,
                           recycle_path=None)


def _rule(name, priority, library_id, template, filter=None):
    return SimpleNamespace(id=f"rule-{name}", name=name, priority=priority,
                           enabled=True, filter=filter,
                           library_id=library_id, path_template=template,
                           file_op="move", auto_execute=False)


PRESET_TV_T = "{title}/Season {season:02d}/{title} - s{season:02d}e{episode:02d}{ext}"


class TestMultiWorkPlanner:
    def _files(self):
        return [
            DiskFile("/d/Pack/A/e01.mkv", 300, "A/e01.mkv"),
            DiskFile("/d/Pack/B/e02.mkv", 300, "B/e02.mkv"),
        ]

    def test_same_target_merges_into_single_plan(self):
        lib = _lib()
        rule = _rule("all-tv", 10, lib.id, PRESET_TV_T)
        result = build_plan(
            _multi_work_payload(), self._files(), [rule], [lib],
            category="Anime",
        )
        moves = [op for op in result.ops if op.op_type == "move"]
        dsts = " ".join(op.dst for op in moves)
        assert "作品甲" in dsts and "作品乙" in dsts
        assert result.rule is rule and result.library is lib

    def test_subtitle_routed_by_episode_range_within_groups(self):
        lib = _lib()
        rule = _rule("all-tv", 10, lib.id, PRESET_TV_T)
        files = self._files() + [
            DiskFile("/d/Pack/B/e02.chs.srt", 5, "B/e02.chs.srt"),
        ]
        result = build_plan(
            _multi_work_payload(), files, [rule], [lib], category="Anime",
        )
        subs = [op for op in result.ops
                if op.op_type == "move" and op.src.endswith(".srt")]
        assert len(subs) == 1 and "作品乙" in subs[0].dst

    def test_different_targets_rejected(self):
        anime, live = _lib("anime", "/anime"), _lib("live", "/live")
        rules = [
            _rule("anime", 1, anime.id, PRESET_TV_T,
                  {"field": "series.is_anime", "operator": "eq", "value": True}),
            _rule("live", 2, live.id, PRESET_TV_T,
                  {"field": "series.is_anime", "operator": "eq", "value": False}),
        ]
        with pytest.raises(PlanError, match="不同规则"):
            build_plan(
                _multi_work_payload(second_anime=False), self._files(),
                rules, [anime, live], category="Anime",
            )

    def test_missing_work_metadata_rejected(self):
        lib = _lib()
        rule = _rule("all-tv", 10, lib.id, PRESET_TV_T)
        payload = _multi_work_payload()
        payload["works"].pop("series:s-2")
        with pytest.raises(PlanError, match="缺少作品元数据"):
            build_plan(payload, self._files(), [rule], [lib])

    def test_leftover_video_without_association_is_kept(self):
        lib = _lib()
        rule = _rule("all-tv", 10, lib.id, PRESET_TV_T)
        files = self._files() + [
            DiskFile("/d/Pack/C/e09.mkv", 300, "C/e09.mkv"),
        ]
        result = build_plan(
            _multi_work_payload(), files, [rule], [lib], category="Anime",
        )
        keeps = [op for op in result.ops if op.op_type == "keep"]
        assert any(op.src.endswith("C/e09.mkv") for op in keeps)


# ---------------------------------------------------------------------------
# manifest 回退链（_resolve_manifest）
# ---------------------------------------------------------------------------


def _torrent_bytes(root_name: str, files: list[tuple[list[str], int]]) -> bytes:
    return bencodepy.encode({
        b"info": {
            b"name": root_name.encode(),
            b"files": [
                {b"length": size, b"path": [p.encode() for p in parts]}
                for parts, size in files
            ],
        },
    })


async def _seed_task_with_resource(db_session, *, torrent_file=None,
                                   torrent_url="magnet:?x"):
    from app.models.downloader import DownloaderInstance

    downloader = DownloaderInstance(
        id=_uuid(), name="dl-mf", type="mock", url="http://mock/rpc",
        download_dir="/downloads", status="connected",
    )
    db_session.add(downloader)
    await db_session.flush()
    channel = Channel(
        id=_uuid(), name="mf", type="rss_feed", url="https://x/rss",
        field_mapping={"list_locator": {"source": "entries"}},
        metadata_agent_enabled=False,
    )
    db_session.add(channel)
    await db_session.flush()
    resource = FileResource(
        id=_uuid(), channel_id=channel.id, guid=_uuid(),
        title_raw="t", torrent_url=torrent_url,
        torrent_file=torrent_file, parsed_at=datetime.now(UTC),
    )
    task = DownloadTask(
        id=_uuid(), agent_id=None, file_resource_id=resource.id,
        downloader_id=downloader.id, download_dir="/downloads",
        transmission_torrent_id=None, status="completed",
        completed_at=datetime.now(UTC),
    )
    db_session.add_all([resource, task])
    await db_session.commit()
    return task


def _payload_for(task_id: str):
    return {
        "notification_id": "n-mf",
        "task": {"download_task_id": task_id, "download_dir": "/downloads",
                 "torrent_name": None},
        "resource": None, "work": None,
    }


class TestResolveManifest:
    async def test_torrent_cache_hit_prefixes_root_component(self, db_session, tmp_path):
        tfile = tmp_path / "cached.torrent"
        tfile.write_bytes(_torrent_bytes(
            "Pack.Root", [(["A", "e01.mkv"], 100), (["e02.mkv"], 50)],
        ))
        task = await _seed_task_with_resource(db_session, torrent_file=str(tfile))
        manifest = await osvc._resolve_manifest(
            db_session, osvc.NotificationPayload.model_validate(_payload_for(task.id))
        )
        names = [e["name"] for e in manifest]
        assert names == ["Pack.Root/A/e01.mkv", "Pack.Root/e02.mkv"]

    async def test_unsafe_root_name_skips_prefixing(self, db_session, tmp_path):
        tfile = tmp_path / "weird.torrent"
        tfile.write_bytes(_torrent_bytes("../evil", [(["e.mkv"], 100)]))
        task = await _seed_task_with_resource(db_session, torrent_file=str(tfile))
        manifest = await osvc._resolve_manifest(
            db_session, osvc.NotificationPayload.model_validate(_payload_for(task.id))
        )
        assert [e["name"] for e in manifest] == ["e.mkv"]

    async def test_url_fetch_writes_back_cache(self, db_session, tmp_path):
        torrent_bytes = _torrent_bytes("", [(["flat.mkv"], 100)])

        async def fake_fetch(url, resource_id):
            path = tmp_path / f"{resource_id}.torrent"
            path.write_bytes(torrent_bytes)
            return str(path)

        task = await _seed_task_with_resource(
            db_session, torrent_url="https://example.com/a.torrent"
        )
        with patch(
            "app.services.torrent_inspect.fetch_torrent_file",
            side_effect=fake_fetch,
        ):
            manifest = await osvc._resolve_manifest(
                db_session,
                osvc.NotificationPayload.model_validate(_payload_for(task.id)),
            )
        assert [e["name"] for e in manifest] == ["flat.mkv"]

    async def test_downloader_rpc_last_resort(self, db_session):
        task = await _seed_task_with_resource(db_session)
        task.transmission_torrent_id = 7
        await db_session.commit()

        class _FakeClient:
            async def get_torrent_files(self, tid):
                return {"name": "RPC.Root", "files": [
                    {"name": "RPC.Root/f.mkv", "size": 100},
                    {"name": "/abs/path.mkv", "size": 5},     # 绝对路径剔除
                    {"name": "../escape.mkv", "size": 5},      # .. 分量剔除
                ]}

        with patch(
            "app.clients.downloader.get_downloader_client",
            return_value=_FakeClient(),
        ):
            manifest = await osvc._resolve_manifest(
                db_session,
                osvc.NotificationPayload.model_validate(_payload_for(task.id)),
            )
        assert [e["name"] for e in manifest] == ["RPC.Root/f.mkv"]

    async def test_no_task_or_no_sources_returns_none(self, db_session):
        assert await osvc._resolve_manifest(
            db_session,
            osvc.NotificationPayload.model_validate({"task": None}),
        ) is None
        task = await _seed_task_with_resource(db_session)
        assert await osvc._resolve_manifest(
            db_session,
            osvc.NotificationPayload.model_validate(_payload_for(task.id)),
        ) is None


# ---------------------------------------------------------------------------
# replan_open_plans：规则变更后的重路由 / 待分类回退
# ---------------------------------------------------------------------------


async def _seed_plan_chain(db_session, tmp_path, *, rule_filter=None,
                           template=PRESET_TV_T, file_op="move"):
    from app.models.download_notification import DownloadNotification
    from app.models.downloader import DownloaderInstance
    from app.models.storage_volume import StorageVolume

    downloader = DownloaderInstance(
        id=_uuid(), name="dl-rp", type="mock", url="http://mock/rpc",
        download_dir=str(tmp_path / "dl"), status="connected",
    )
    db_session.add(downloader)
    await db_session.flush()
    channel = Channel(
        id=_uuid(), name="rp", type="rss_feed", url="https://x/rss",
        field_mapping={"list_locator": {"source": "entries"}},
        metadata_agent_enabled=False,
    )
    volume = StorageVolume(id=_uuid(), name=f"vol-{_uuid()[:8]}",
                           mount_path=str(tmp_path))
    db_session.add_all([channel, volume])
    await db_session.flush()
    resource = FileResource(
        id=_uuid(), channel_id=channel.id, guid=_uuid(),
        title_raw="Show.S01E04", torrent_url="magnet:?x",
        series_id=None, parsed_at=datetime.now(UTC),
    )
    task = DownloadTask(
        id=_uuid(), agent_id=None, file_resource_id=resource.id,
        downloader_id=downloader.id, download_dir=str(tmp_path / "dl"),
        transmission_torrent_id=None, status="completed",
        completed_at=datetime.now(UTC),
    )
    payload = {
        "notification_id": _uuid(),
        "task": {"download_task_id": task.id,
                 "download_dir": str(tmp_path / "dl"),
                 "torrent_name": None},
        "resource": {"id": resource.id, "title_raw": "Show.S01E04",
                     "season": 1, "episode": 4, "is_batch": False,
                     "batch_scope": None, "collection": None,
                     "episode_start": None, "episode_end": None,
                     "subtitle_langs": None, "resolution": None,
                     "container": ".mkv", "title_year": None},
        "files": [{"name": "Show.S01E04.mkv", "size": 20}],
        "work": {"type": "series", "series_id": None, "title_cn": "攻壳机动队",
                 "title_en": "GITS", "original_title": "GITS", "year": 2026,
                 "content_type": "tv", "is_anime": True, "collection": None,
                 "genre": ["Animation"], "seasons": None, "episodes": None},
    }
    notification = DownloadNotification(
        id=payload["notification_id"], agent_id=None,
        download_task_id=task.id, payload=payload,
    )
    lib = SimpleNamespace(
        id=_uuid(), name="TV", root_path=str(tmp_path / "media"), kind="tv",
        subtitle_lang_map=None, recycle_path=None,
    )
    # 落真实 ORM Library/Rule 行（replan 从 DB 读规则与库）
    from app.models.library import Library as LibORM
    from app.models.organize_rule import OrganizeRule as RuleORM
    from app.models.series import TVSeries

    series = TVSeries(
        id=_uuid(), title_cn="攻壳机动队", title_en="GITS",
        original_title="GITS", content_type="tv", is_anime=True,
        start_date=datetime(2026, 4, 1).date(),
    )
    db_session.add(series)
    await db_session.flush()
    resource.series_id = series.id
    payload["work"]["series_id"] = series.id

    lib_row = LibORM(id=lib.id, name="TV", kind="tv", volume_id=volume.id)
    rule_row = RuleORM(
        id=_uuid(), name="rp-rule", priority=100, enabled=True,
        filter=rule_filter, library_id=lib.id, path_template=template,
        file_op=file_op, auto_execute=False,
    )
    plan = OrganizePlan(
        id=_uuid(), notification_id=notification.id, rule_id=rule_row.id,
        library_id=lib.id, status="pending", payload=payload,
    )
    db_session.add_all([lib_row, rule_row, resource, task, notification, plan])
    await db_session.commit()
    _mk(tmp_path / "dl" / "Show.S01E04.mkv", 20)
    return SimpleNamespace(plan=plan, rule=rule_row, lib=lib_row,
                           notification=notification)


async def test_replan_reroutes_and_resets_uncategorized(db_session, tmp_path):
    """规则被删 → 待分类（rule/library 清空）；模板变更 → op 重渲染。"""
    from app.services.organize_service import replan_open_plans

    chain = await _seed_plan_chain(db_session, tmp_path)
    stats = await replan_open_plans(db_session, reason="test")
    assert stats["rebuilt"] == 1
    await db_session.refresh(chain.plan)
    # 规则仍命中：op 重渲染到库根
    rows = (await db_session.execute(
        select(OrganizePlanOp).where(OrganizePlanOp.plan_id == chain.plan.id)
    )).scalars().all()
    # 库根 = volume.mount_path（tmp_path 本身）
    assert any(o.op_type == "move" and (o.dst or "").startswith(str(tmp_path))
               for o in rows)

    # 规则收紧为不再匹配（仍是唯一 enabled 规则）：退待分类，rule/library 清空
    chain.rule.filter = {"field": "is_batch", "operator": "eq", "value": True}
    await db_session.commit()
    stats2 = await replan_open_plans(db_session, reason="rule-tightened")
    assert stats2["rebuilt"] == 1
    await db_session.refresh(chain.plan)
    assert chain.plan.rule_id is None and chain.plan.library_id is None
