"""整理执行器（organize_executor）单元测试。

逐条覆盖 vault-organizer 移植来的执行器不变量：幂等状态表四分支、
前置门禁违例整体放弃、EXDEV 跨文件系统退化、自底向上清目录保留下载根、
movedir 冲突绝不覆盖、审计明细产出。全部用 tmp_path 真实文件树。
"""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from app.services.organize_executor import (
    ExecOp,
    cleanup_empty_dirs,
    execute_movedir,
    execute_ops,
    precheck,
    run_execution,
    verify_done,
)


def _mkfile(path: Path, size: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


def _move(src: Path, dst: Path, size: int) -> ExecOp:
    return ExecOp(op_type="move", src=str(src), dst=str(dst), size=size)


# ---------------------------------------------------------------- 状态表四分支


def test_move_normal(tmp_path):
    src = _mkfile(tmp_path / "dl" / "ep01.mkv", 100)
    dst = tmp_path / "lib" / "Show" / "ep01.mkv"
    [r] = execute_ops([_move(src, dst, 100)])
    assert r.status == "done"
    assert dst.exists() and dst.stat().st_size == 100
    assert not src.exists()


def test_move_already_done_cleans_residual_src(tmp_path):
    """dst 存在且 size 匹配 = 已完成；src 残留且 size 相同 → 删 src 收敛。"""
    src = _mkfile(tmp_path / "dl" / "ep01.mkv", 100)
    dst = _mkfile(tmp_path / "lib" / "ep01.mkv", 100)
    [r] = execute_ops([_move(src, dst, 100)])
    assert r.status == "done"
    assert not src.exists()
    assert dst.exists()


def test_move_already_done_keeps_different_src(tmp_path):
    """dst size 匹配但 src 残留 size 不同 → 不删 src（dst 为权威）。"""
    src = _mkfile(tmp_path / "dl" / "ep01.mkv", 50)
    dst = _mkfile(tmp_path / "lib" / "ep01.mkv", 100)
    [r] = execute_ops([_move(src, dst, 100)])
    assert r.status == "done"
    assert src.exists()


def test_move_conflict_never_overwrites(tmp_path):
    """dst 存在且 size 不符 → 冲突 failed，双方文件都不动。"""
    src = _mkfile(tmp_path / "dl" / "ep01.mkv", 100)
    dst = _mkfile(tmp_path / "lib" / "ep01.mkv", 200)
    [r] = execute_ops([_move(src, dst, 100)])
    assert r.status == "failed"
    assert "拒绝覆盖" in r.error
    assert src.exists() and src.stat().st_size == 100
    assert dst.exists() and dst.stat().st_size == 200


def test_move_both_missing_is_data_loss(tmp_path):
    src = tmp_path / "dl" / "ep01.mkv"
    dst = tmp_path / "lib" / "ep01.mkv"
    [r] = execute_ops([_move(src, dst, 100)])
    assert r.status == "failed"
    assert "均不存在" in r.error


def test_move_src_equals_dst_is_done(tmp_path):
    src = _mkfile(tmp_path / "lib" / "ep01.mkv", 100)
    [r] = execute_ops([ExecOp(op_type="move", src=str(src), dst=str(src), size=100)])
    assert r.status == "done"
    assert src.exists()


def test_keep_untouched(tmp_path):
    src = _mkfile(tmp_path / "dl" / "note.txt", 10)
    [r] = execute_ops([ExecOp(op_type="keep", src=str(src), dst=None, size=10)])
    assert r.status == "kept"
    assert src.exists()


# ---------------------------------------------------------------- 前置门禁


def test_precheck_violation_aborts_whole_plan(tmp_path):
    """任一 op 违例 → 整体放弃，不触碰任何文件（第一个就绪 op 也不执行）。"""
    src1 = _mkfile(tmp_path / "dl" / "a.mkv", 100)
    src2 = _mkfile(tmp_path / "dl" / "b.mkv", 100)
    dst1 = tmp_path / "lib" / "a.mkv"
    dst2 = tmp_path / "lib" / "b.mkv"
    ops = [_move(src1, dst1, 100), _move(src2, dst2, 999)]  # src2 size 快照不符
    outcome = run_execution(ops)
    assert not outcome.ok
    assert "前置门禁" in outcome.error
    assert not dst1.exists()  # 第一个 op 也未执行
    assert not dst2.exists()
    assert src1.exists() and src2.exists()
    assert outcome.audits[0]["action"] == "precheck"


def test_precheck_passes_on_replay(tmp_path):
    """重放收敛：dst 已到位视为幂等满足，不报违例。"""
    dst = _mkfile(tmp_path / "lib" / "a.mkv", 100)
    violations = precheck([_move(tmp_path / "dl" / "a.mkv", dst, 100)])
    assert violations == []


def test_precheck_movedir_conflict(tmp_path):
    src = _mkfile(tmp_path / "dl" / "movie" / "x.nfo", 5).parent
    dst = _mkfile(tmp_path / "extras" / "movie" / "y.nfo", 5).parent
    violations = precheck(
        [ExecOp(op_type="movedir", src=str(src), dst=str(dst), size=0)]
    )
    assert violations and "拒绝覆盖" in violations[0]


# ---------------------------------------------------------------- EXDEV 退化


def test_exdev_falls_back_to_copy(tmp_path, monkeypatch):
    src = _mkfile(tmp_path / "dl" / "ep01.mkv", 100)
    dst = tmp_path / "lib" / "ep01.mkv"

    real_rename = os.rename

    def fake_rename(s, d):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(os, "rename", fake_rename)
    [r] = execute_ops([_move(src, dst, 100)])
    monkeypatch.setattr(os, "rename", real_rename)
    assert r.status == "done"
    assert dst.exists() and dst.stat().st_size == 100
    assert not src.exists()


def test_exdev_copy_verify_failure_removes_partial_dst(tmp_path, monkeypatch):
    src = _mkfile(tmp_path / "dl" / "ep01.mkv", 100)
    dst = tmp_path / "lib" / "ep01.mkv"

    def fake_rename(s, d):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    def fake_copyfile(s, d):
        Path(d).parent.mkdir(parents=True, exist_ok=True)
        Path(d).write_bytes(b"x" * 10)  # 截断的半成品

    monkeypatch.setattr(os, "rename", fake_rename)
    monkeypatch.setattr("app.services.organize_executor.shutil.copyfile", fake_copyfile)
    [r] = execute_ops([_move(src, dst, 100)])
    assert r.status == "failed"
    assert "校验失败" in r.error
    assert not dst.exists()  # 不完整 dst 已删
    assert src.exists()


# ---------------------------------------------------------------- 后置校验


def test_verify_done_detects_residual(tmp_path):
    src = _mkfile(tmp_path / "dl" / "a.mkv", 100)
    dst = _mkfile(tmp_path / "lib" / "a.mkv", 100)
    problems = verify_done([_move(src, dst, 100)])
    assert problems == ["源文件残留：" + str(src)]


# ---------------------------------------------------------------- movedir


def test_movedir_normal_and_conflict(tmp_path):
    src = _mkfile(tmp_path / "dl" / "movie" / "x.nfo", 5).parent
    dst = tmp_path / "extras" / "movie"
    assert execute_movedir(ExecOp(op_type="movedir", src=str(src), dst=str(dst), size=0)) is None
    assert (dst / "x.nfo").exists()
    assert not src.exists()

    # 目标已存在 = 冲突，绝不覆盖
    src2 = _mkfile(tmp_path / "dl" / "m2" / "x.nfo", 5).parent
    dst2 = _mkfile(tmp_path / "extras" / "m2" / "old.nfo", 5).parent
    error = execute_movedir(ExecOp(op_type="movedir", src=str(src2), dst=str(dst2), size=0))
    assert error and "拒绝覆盖" in error
    assert (src2 / "x.nfo").exists()
    assert (dst2 / "old.nfo").exists()
    assert not (dst2 / "x.nfo").exists()


# ---------------------------------------------------------------- 空目录清理


def test_cleanup_empty_dirs_bottom_up_preserves_root(tmp_path):
    base = tmp_path / "downloads"
    torrent = base / "Show.S01"
    _mkfile(torrent / "keep" / "f.txt", 1)  # 非空分支保留
    (torrent / "empty" / "nested").mkdir(parents=True)
    removed = cleanup_empty_dirs(torrent, preserve=base)
    assert str(torrent / "empty" / "nested") in removed
    assert str(torrent / "empty") in removed
    assert base.exists()  # 下载根保留
    assert (torrent / "keep").exists()  # 非空目录保留
    # torrent 目录本身因 keep/ 非空而保留
    assert torrent.exists()


def test_cleanup_never_touches_preserve_even_when_empty(tmp_path):
    base = tmp_path / "downloads"
    torrent = base / "gone"
    torrent.mkdir(parents=True)
    removed = cleanup_empty_dirs(torrent, preserve=base)
    assert str(torrent) in removed
    assert base.exists()


# ---------------------------------------------------------------- 整体编排与审计


def test_run_execution_full_flow_and_audits(tmp_path):
    src = _mkfile(tmp_path / "dl" / "Show" / "ep01.mkv", 100)
    keep = _mkfile(tmp_path / "dl" / "Show" / "note.txt", 10)
    dst = tmp_path / "lib" / "Show" / "ep01.mkv"
    ops = [
        _move(src, dst, 100),
        ExecOp(op_type="keep", src=str(keep), dst=None, size=10),
    ]
    outcome = run_execution(
        ops,
        cleanup_root=str(tmp_path / "dl" / "Show"),
        preserve=str(tmp_path / "dl"),
    )
    assert outcome.ok
    assert dst.exists()
    assert len(outcome.op_results) == 2
    actions = [a["action"] for a in outcome.audits]
    assert "move" in actions and "keep" in actions
    # keep 文件仍在 cleanup_root 下 → 目录非空 → 无目录可删、无 cleanup 审计
    assert "cleanup" not in actions
    assert (tmp_path / "dl" / "Show").exists()


# ---------------------------------------------------------------- hardlink


def test_hardlink_normal_preserves_src(tmp_path):
    src = _mkfile(tmp_path / "dl" / "ep01.mkv", 100)
    dst = tmp_path / "lib" / "Show" / "ep01.mkv"
    [r] = execute_ops([_move(src, dst, 100)], file_op="hardlink")
    assert r.status == "done"
    assert dst.exists() and dst.stat().st_size == 100
    assert src.exists()  # 保种：源文件保留
    assert os.path.samefile(src, dst)  # 同一 inode，不占双份存储


def test_hardlink_already_done_keeps_src(tmp_path):
    """dst 存在且 size 匹配 = 已完成（同 move 状态表语义），但保留 src。"""
    src = _mkfile(tmp_path / "dl" / "ep01.mkv", 100)
    dst = _mkfile(tmp_path / "lib" / "ep01.mkv", 100)
    [r] = execute_ops([_move(src, dst, 100)], file_op="hardlink")
    assert r.status == "done"
    assert src.exists() and dst.exists()


def test_hardlink_conflict_never_overwrites(tmp_path):
    src = _mkfile(tmp_path / "dl" / "ep01.mkv", 100)
    dst = _mkfile(tmp_path / "lib" / "ep01.mkv", 200)
    [r] = execute_ops([_move(src, dst, 100)], file_op="hardlink")
    assert r.status == "failed"
    assert "拒绝覆盖" in r.error
    assert src.stat().st_size == 100 and dst.stat().st_size == 200


def test_hardlink_exdev_fails_without_copy_fallback(tmp_path, monkeypatch):
    """跨文件系统/不支持 → op failed + 明确原因，绝不静默退化为 copy。"""
    src = _mkfile(tmp_path / "dl" / "ep01.mkv", 100)
    dst = tmp_path / "lib" / "ep01.mkv"

    def fake_link(s, d):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(os, "link", fake_link)
    [r] = execute_ops([_move(src, dst, 100)], file_op="hardlink")
    assert r.status == "failed"
    assert "硬链接失败" in r.error and "Invalid cross-device link" in r.error
    assert not dst.exists()  # 未偷偷复制
    assert src.exists()


def test_hardlink_both_missing_is_data_loss(tmp_path):
    src = tmp_path / "dl" / "ep01.mkv"
    dst = tmp_path / "lib" / "ep01.mkv"
    [r] = execute_ops([_move(src, dst, 100)], file_op="hardlink")
    assert r.status == "failed"
    assert "均不存在" in r.error


def test_run_execution_hardlink_skips_cleanup_and_src_check(tmp_path):
    """hardlink 计划：源文件保留 → 后置校验不报残留、空目录清理跳过。"""
    torrent_dir = tmp_path / "dl" / "Show"
    src = _mkfile(torrent_dir / "ep01.mkv", 100)
    dst = tmp_path / "lib" / "Show" / "ep01.mkv"
    outcome = run_execution(
        [_move(src, dst, 100)],
        file_op="hardlink",
        cleanup_root=str(torrent_dir),
        preserve=str(tmp_path / "dl"),
    )
    assert outcome.ok
    assert src.exists() and os.path.samefile(src, dst)
    actions = [a["action"] for a in outcome.audits]
    assert "hardlink" in actions  # 审计 action 反映实际文件操作
    assert "cleanup" not in actions


# ---------------------------------------------------------------- copy


def test_copy_normal_preserves_src(tmp_path):
    src = _mkfile(tmp_path / "dl" / "ep01.mkv", 100)
    dst = tmp_path / "lib" / "Show" / "ep01.mkv"
    [r] = execute_ops([_move(src, dst, 100)], file_op="copy")
    assert r.status == "done"
    assert dst.exists() and dst.stat().st_size == 100
    assert src.exists()  # 保种：源文件保留
    assert not os.path.samefile(src, dst)  # 真复制，两份 inode


def test_copy_verify_failure_removes_partial_dst(tmp_path, monkeypatch):
    src = _mkfile(tmp_path / "dl" / "ep01.mkv", 100)
    dst = tmp_path / "lib" / "ep01.mkv"

    def fake_copyfile(s, d):
        Path(d).parent.mkdir(parents=True, exist_ok=True)
        Path(d).write_bytes(b"x" * 10)  # 截断的半成品

    monkeypatch.setattr("app.services.organize_executor.shutil.copyfile", fake_copyfile)
    [r] = execute_ops([_move(src, dst, 100)], file_op="copy")
    assert r.status == "failed"
    assert "校验失败" in r.error
    assert not dst.exists()  # 不完整 dst 已删
    assert src.exists()


def test_copy_already_done_and_conflict(tmp_path):
    src = _mkfile(tmp_path / "dl" / "ep01.mkv", 100)
    dst = _mkfile(tmp_path / "lib" / "ep01.mkv", 100)
    [r] = execute_ops([_move(src, dst, 100)], file_op="copy")
    assert r.status == "done"
    assert src.exists()  # 已完成分支不收敛 src

    bad_dst = _mkfile(tmp_path / "lib" / "ep02.mkv", 200)
    src2 = _mkfile(tmp_path / "dl" / "ep02.mkv", 100)
    [r] = execute_ops([_move(src2, bad_dst, 100)], file_op="copy")
    assert r.status == "failed"
    assert "拒绝覆盖" in r.error
    assert bad_dst.stat().st_size == 200


def test_copy_both_missing_is_data_loss(tmp_path):
    src = tmp_path / "dl" / "ep01.mkv"
    dst = tmp_path / "lib" / "ep01.mkv"
    [r] = execute_ops([_move(src, dst, 100)], file_op="copy")
    assert r.status == "failed"
    assert "均不存在" in r.error


def test_verify_done_hardlink_ignores_residual_src(tmp_path):
    src = _mkfile(tmp_path / "dl" / "a.mkv", 100)
    dst = _mkfile(tmp_path / "lib" / "a.mkv", 100)
    assert verify_done([_move(src, dst, 100)], file_op="hardlink") == []
    assert verify_done([_move(src, dst, 100)], file_op="copy") == []


# ---------------------------------------------------------------- 补缺分支


def test_precheck_movedir_completed_when_src_missing(tmp_path):
    """movedir 已完成（src 已移走、dst 在位）= 幂等满足，不算违例。"""
    src = tmp_path / "dl" / "movie"
    dst = _mkfile(tmp_path / "extras" / "movie" / "x.nfo", 5).parent
    violations = precheck(
        [ExecOp(op_type="movedir", src=str(src), dst=str(dst), size=0)]
    )
    assert violations == []


def test_precheck_movedir_both_missing(tmp_path):
    src = tmp_path / "dl" / "movie"
    dst = tmp_path / "extras" / "movie"
    violations = precheck(
        [ExecOp(op_type="movedir", src=str(src), dst=str(dst), size=0)]
    )
    assert violations and "均不存在" in violations[0]


def test_precheck_move_both_missing(tmp_path):
    src = tmp_path / "dl" / "a.mkv"
    dst = tmp_path / "lib" / "a.mkv"
    violations = precheck([_move(src, dst, 100)])
    assert violations and "源文件与目标均不存在" in violations[0]


def test_state_table_src_equals_dst(tmp_path):
    """src == dst → done（hardlink/copy 共用状态表分支）。"""
    src = _mkfile(tmp_path / "lib" / "ep01.mkv", 100)
    op = ExecOp(op_type="move", src=str(src), dst=str(src), size=100)
    for file_op in ("hardlink", "copy"):
        [r] = execute_ops([op], file_op=file_op)
        assert r.status == "done"
        assert src.exists()


def test_move_non_exdev_oserror_propagates(tmp_path, monkeypatch):
    """跨设备之外的 OSError（如权限）原样抛出，不吞异常不删源。"""
    src = _mkfile(tmp_path / "dl" / "ep01.mkv", 100)
    dst = tmp_path / "lib" / "ep01.mkv"

    def fake_rename(s, d):
        raise OSError(errno.EPERM, "Permission denied")

    monkeypatch.setattr(os, "rename", fake_rename)
    with pytest.raises(OSError):
        execute_ops([_move(src, dst, 100)])
    assert src.exists()
    assert not dst.exists()


def test_movedir_src_missing(tmp_path):
    src = tmp_path / "dl" / "movie"
    dst = _mkfile(tmp_path / "extras" / "movie" / "x.nfo", 5).parent
    assert (
        execute_movedir(ExecOp(op_type="movedir", src=str(src), dst=str(dst), size=0))
        is None
    )
    missing = tmp_path / "extras" / "m2"
    error = execute_movedir(
        ExecOp(op_type="movedir", src=str(src), dst=str(missing), size=0)
    )
    assert error and "均不存在" in error


def test_movedir_empty_src_is_noop(tmp_path):
    """空源目录无需移动，交给空目录清理。"""
    src = tmp_path / "dl" / "movie"
    src.mkdir(parents=True)
    dst = tmp_path / "extras" / "movie"
    assert (
        execute_movedir(ExecOp(op_type="movedir", src=str(src), dst=str(dst), size=0))
        is None
    )
    assert src.exists()
    assert not dst.exists()


def test_movedir_exdev_falls_back_to_shutil_move(tmp_path, monkeypatch):
    src = _mkfile(tmp_path / "dl" / "movie" / "x.nfo", 5).parent
    dst = tmp_path / "extras" / "movie"

    def fake_rename(s, d):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(os, "rename", fake_rename)
    assert (
        execute_movedir(ExecOp(op_type="movedir", src=str(src), dst=str(dst), size=0))
        is None
    )
    assert (dst / "x.nfo").exists()
    assert not src.exists()


def test_movedir_non_exdev_oserror_propagates(tmp_path, monkeypatch):
    src = _mkfile(tmp_path / "dl" / "movie" / "x.nfo", 5).parent
    dst = tmp_path / "extras" / "movie"

    def fake_rename(s, d):
        raise OSError(errno.EPERM, "Permission denied")

    monkeypatch.setattr(os, "rename", fake_rename)
    with pytest.raises(OSError):
        execute_movedir(ExecOp(op_type="movedir", src=str(src), dst=str(dst), size=0))
    assert src.exists()
    assert not dst.exists()


def test_precheck_src_size_mismatch(tmp_path):
    """源文件大小与计划快照不符（规划后已被改动）→ 违例。"""
    src = _mkfile(tmp_path / "dl" / "a.mkv", 100)
    dst = tmp_path / "lib" / "a.mkv"
    violations = precheck([_move(src, dst, 999)])
    assert violations and "大小与计划快照不符" in violations[0]


def test_precheck_dst_conflict_size_mismatch(tmp_path):
    """目标已存在且大小不符 → 违例（绝不覆盖）。"""
    src = _mkfile(tmp_path / "dl" / "a.mkv", 100)
    dst = _mkfile(tmp_path / "lib" / "a.mkv", 200)
    violations = precheck([_move(src, dst, 100)])
    assert violations and "目标已存在且大小不符" in violations[0]


def test_run_execution_cleanup_audit(tmp_path):
    """move 后种子目录空 → 自底向上清空并产 cleanup 审计。"""
    src = _mkfile(tmp_path / "dl" / "Show" / "ep01.mkv", 100)
    dst = tmp_path / "lib" / "Show" / "ep01.mkv"
    outcome = run_execution(
        [_move(src, dst, 100)],
        cleanup_root=str(tmp_path / "dl" / "Show"),
        preserve=str(tmp_path / "dl"),
    )
    assert outcome.ok
    assert any(a["action"] == "cleanup" for a in outcome.audits)


def test_verify_done_missing_and_wrong_size(tmp_path):
    src = _mkfile(tmp_path / "dl" / "a.mkv", 100)
    dst = tmp_path / "lib" / "a.mkv"
    problems = verify_done([_move(src, dst, 100)])
    assert any("目标文件缺失" in p for p in problems)
    _mkfile(dst, 999)
    problems = verify_done([_move(src, dst, 100)])
    assert any("目标文件大小与计划不符" in p for p in problems)


def test_cleanup_empty_dirs_non_dir(tmp_path):
    f = _mkfile(tmp_path / "f.txt", 1)
    assert cleanup_empty_dirs(f) == []


def test_cleanup_empty_dirs_keeps_root_when_preserve_equals_root(tmp_path):
    base = tmp_path / "downloads"
    _mkfile(base / "a" / "b" / "f.txt", 1)
    (base / "empty").mkdir(parents=True)
    removed = cleanup_empty_dirs(base, preserve=base)
    assert str(base / "empty") in removed
    assert base.exists()


def test_run_execution_op_failure_reports_error(tmp_path, monkeypatch):
    """单 op 失败（如 hardlink EXDEV）→ 计划级 failed + 审计明细，源保留。"""
    src = _mkfile(tmp_path / "dl" / "a.mkv", 100)
    dst = tmp_path / "lib" / "a.mkv"

    def fake_link(s, d):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(os, "link", fake_link)
    outcome = run_execution([_move(src, dst, 100)], file_op="hardlink")
    assert not outcome.ok
    assert "文件操作失败" in outcome.error
    assert any(
        a["action"] == "hardlink" and a["detail"]["status"] == "failed"
        for a in outcome.audits
    )
    assert src.exists()
    assert not dst.exists()


def test_run_execution_verify_failure(tmp_path, monkeypatch):
    """文件 op 全过但后置校验失败 → failed + verify 审计。"""
    src = _mkfile(tmp_path / "dl" / "a.mkv", 100)
    dst = tmp_path / "lib" / "a.mkv"
    monkeypatch.setattr(
        "app.services.organize_executor.verify_done",
        lambda ops, file_op="move": ["目标文件缺失：fake"],
    )
    outcome = run_execution([_move(src, dst, 100)])
    assert not outcome.ok
    assert "后置校验未通过" in outcome.error
    assert any(a["action"] == "verify" for a in outcome.audits)


def test_run_execution_movedir_failure(tmp_path, monkeypatch):
    """movedir 执行失败 → op failed + 计划级 failed，绝不让 movedir 失败混过。"""
    src = _mkfile(tmp_path / "dl" / "movie" / "x.nfo", 5).parent
    dst = tmp_path / "extras" / "movie"
    monkeypatch.setattr(
        "app.services.organize_executor.execute_movedir",
        lambda op: "目标目录已存在，拒绝覆盖",
    )
    ops = [ExecOp(op_type="movedir", src=str(src), dst=str(dst), size=0)]
    outcome = run_execution(ops)
    assert not outcome.ok
    assert "拒绝覆盖" in outcome.error
    assert outcome.op_results[0].status == "failed"
    assert any(
        a["action"] == "movedir" and a["detail"]["status"] == "failed"
        for a in outcome.audits
    )
