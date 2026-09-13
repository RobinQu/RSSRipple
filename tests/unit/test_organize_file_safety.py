"""Regressions for actual file loss, including races after the plan precheck."""

import errno
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import organize_executor as executor
from app.services import organize_file_safety as safety
from app.services.organize_executor import ExecOp, execute_ops, precheck, run_execution
from app.services.organize_planner import PlanError, _check_conflicts


def files(tmp_path, left=b"AAAA", right=b"BBBB"):
    src, dst = tmp_path / "src.mkv", tmp_path / "dst.mkv"
    src.write_bytes(left)
    dst.write_bytes(right)
    return src, dst, ExecOp("move", str(src), str(dst), len(left))


@pytest.mark.parametrize("mode", ["move", "copy", "hardlink"])
def test_same_size_different_contents_never_succeeds_or_deletes(tmp_path, mode):
    src, dst, op = files(tmp_path)
    with pytest.raises(PlanError):
        _check_conflicts([op], file_op=mode)
    assert precheck([op], file_op=mode)
    assert not run_execution([op], file_op=mode).ok
    assert execute_ops([op], file_op=mode)[0].status == "failed"
    assert src.read_bytes() == b"AAAA"
    assert dst.read_bytes() == b"BBBB"


@pytest.mark.parametrize("data", [b"", b"same", b"a" * (1024 * 1024 + 9)])
@pytest.mark.parametrize("mode", ["move", "copy"])
def test_full_equality_and_empty_files_allow_recovery(tmp_path, data, mode):
    src, dst, op = files(tmp_path, data, data)
    assert run_execution([op], file_op=mode).ok
    assert src.exists() == (mode == "copy")
    assert dst.read_bytes() == data


def test_difference_after_first_chunk_is_not_missed(tmp_path):
    src, dst, op = files(tmp_path, b"a" * 1048576 + b"X", b"a" * 1048576 + b"Y")
    assert execute_ops([op])[0].status == "failed"
    assert src.exists() and dst.read_bytes().endswith(b"Y")


def test_same_inode_move_only_removes_source_name(tmp_path):
    src, dst, op = files(tmp_path)
    dst.unlink()
    os.link(src, dst)
    assert run_execution([op]).ok
    assert not src.exists() and dst.read_bytes() == b"AAAA"


@pytest.mark.parametrize("exists", [False, True])
def test_same_path_requires_real_file_of_expected_size(tmp_path, exists):
    src = tmp_path / "same"
    if exists:
        src.write_bytes(b"a")
    op = ExecOp("move", str(src), str(src), 2)
    assert execute_ops([op])[0].status == "failed"
    if exists:
        assert src.read_bytes() == b"a"


@pytest.mark.parametrize("mode", ["copy", "hardlink"])
def test_missing_seeding_source_is_not_recovered_as_success(tmp_path, mode):
    src, dst, op = files(tmp_path)
    src.unlink()
    assert execute_ops([op], file_op=mode)[0].status == "failed"
    assert dst.read_bytes() == b"BBBB"


def test_read_error_preserves_both_files(tmp_path, monkeypatch):
    src, dst, op = files(tmp_path, b"same", b"same")
    original = Path.open

    def fail_source(path, *args, **kwargs):
        if path == src:
            raise PermissionError("unreadable source")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_source)
    assert execute_ops([op])[0].status == "failed"
    assert src.exists() and dst.exists()


def test_change_during_comparison_cannot_authorize_unlink(tmp_path, monkeypatch):
    src, dst, op = files(tmp_path, b"same", b"same")
    compare = safety._same_contents

    def mutate(left, right):
        equal = compare(left, right)
        src.write_bytes(b"evil")
        return equal

    monkeypatch.setattr(safety, "_same_contents", mutate)
    assert execute_ops([op])[0].status == "failed"
    assert src.read_bytes() == b"evil" and dst.read_bytes() == b"same"


@pytest.mark.parametrize("stage", [False, True])
def test_destination_created_after_precheck_is_never_overwritten(tmp_path, monkeypatch, stage):
    src, dst, op = files(tmp_path)
    dst.unlink()
    rename = executor.rename_noreplace

    def race(source, destination):
        destination.write_bytes(b"OTHER")
        rename(source, destination)

    monkeypatch.setattr(executor, "rename_noreplace", race)
    outcome = run_execution([op], file_op="copy" if stage else "move")
    assert not outcome.ok
    assert src.read_bytes() == b"AAAA" and dst.read_bytes() == b"OTHER"
    assert not list(tmp_path.glob(".rssripple-*.tmp"))


def test_missing_native_rename_uses_non_replacing_file_publication(tmp_path, monkeypatch):
    src, dst, op = files(tmp_path)
    dst.unlink()
    monkeypatch.setattr(safety.ctypes, "CDLL", lambda *a, **k: SimpleNamespace())
    assert execute_ops([op])[0].status == "done"
    assert not src.exists() and dst.read_bytes() == b"AAAA"


@pytest.mark.parametrize("code", [errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP])
@pytest.mark.parametrize("mode", ["move", "copy"])
def test_unsupported_native_rename_preserves_file_contract(tmp_path, monkeypatch, code, mode):
    src, dst, op = files(tmp_path)
    dst.unlink()

    def unsupported(*args):
        raise OSError(code, "unsupported")

    monkeypatch.setattr(safety, "_rename_noreplace_linux", unsupported)
    assert run_execution([op], file_op=mode).ok
    assert dst.read_bytes() == b"AAAA"
    assert src.exists() == (mode == "copy")


def test_link_publication_race_preserves_competing_destination(tmp_path, monkeypatch):
    src, dst, op = files(tmp_path)
    dst.unlink()

    def race(*args):
        dst.write_bytes(b"OTHER")
        raise OSError(errno.EINVAL, "unsupported")

    monkeypatch.setattr(safety, "_rename_noreplace_linux", race)
    assert not run_execution([op]).ok
    assert src.read_bytes() == b"AAAA" and dst.read_bytes() == b"OTHER"


def test_link_publication_unlink_failure_is_recoverable(tmp_path, monkeypatch):
    src, dst, op = files(tmp_path)
    dst.unlink()
    original_unlink = Path.unlink

    def unsupported(*args):
        raise OSError(errno.EINVAL, "unsupported")

    def fail_unlink(path, *args, **kwargs):
        if path == src:
            raise PermissionError("source unlink failed")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(safety, "_rename_noreplace_linux", unsupported)
    monkeypatch.setattr(Path, "unlink", fail_unlink)
    assert not run_execution([op]).ok
    assert src.read_bytes() == dst.read_bytes() == b"AAAA"
    monkeypatch.setattr(Path, "unlink", original_unlink)
    assert run_execution([op]).ok
    assert not src.exists() and dst.read_bytes() == b"AAAA"


def test_no_safe_publication_capability_preserves_source(tmp_path, monkeypatch):
    src, dst, op = files(tmp_path)
    dst.unlink()

    def unsupported(*args, **kwargs):
        raise OSError(errno.EOPNOTSUPP, "unsupported")

    monkeypatch.setattr(safety, "_rename_noreplace_linux", unsupported)
    monkeypatch.setattr(safety.os, "link", unsupported)
    assert not run_execution([op]).ok
    assert src.read_bytes() == b"AAAA" and not dst.exists()


def test_unsupported_directory_rename_does_not_use_file_fallback(tmp_path, monkeypatch):
    src, dst = tmp_path / "source", tmp_path / "target"
    src.mkdir()
    (src / "keep").write_bytes(b"keep")
    monkeypatch.setattr(safety.ctypes, "CDLL", lambda *a, **k: SimpleNamespace())
    with pytest.raises(safety.FileSafetyError):
        safety.rename_noreplace(src, dst)
    assert (src / "keep").read_bytes() == b"keep" and not dst.exists()


@pytest.mark.parametrize("cross_device", [False, True])
def test_copy_interrupted_cleans_only_own_temporary_file(tmp_path, monkeypatch, cross_device):
    src, dst, op = files(tmp_path)
    dst.unlink()
    unrelated = tmp_path / ".rssripple-another.tmp"
    unrelated.write_bytes(b"keep")
    rename = executor.rename_noreplace

    def exdev(source, destination):
        if source == src:
            raise OSError(errno.EXDEV, "cross device")
        rename(source, destination)

    def interrupted(source, target, **kwargs):
        target.write(b"A")
        raise OSError("disk full")

    monkeypatch.setattr(executor, "rename_noreplace", exdev)
    monkeypatch.setattr(executor.shutil, "copyfileobj", interrupted)
    assert not run_execution([op], file_op="move" if cross_device else "copy").ok
    assert src.read_bytes() == b"AAAA" and not dst.exists()
    assert list(tmp_path.glob(".rssripple-*.tmp")) == [unrelated]


def test_source_changes_during_copy_is_not_published(tmp_path, monkeypatch):
    src, dst, op = files(tmp_path)
    dst.unlink()
    copy = executor.shutil.copyfileobj

    def mutate(source, target, **kwargs):
        copy(source, target, **kwargs)
        src.write_bytes(b"NEW!")

    monkeypatch.setattr(executor.shutil, "copyfileobj", mutate)
    assert execute_ops([op], file_op="copy")[0].status == "failed"
    assert src.read_bytes() == b"NEW!" and not dst.exists()


def test_duplicate_destination_aborts_before_any_operation(tmp_path):
    src, dst, op = files(tmp_path)
    dst.unlink()
    second = tmp_path / "second.mkv"
    second.write_bytes(b"CCCC")
    ops = [op, ExecOp("move", str(second), str(dst), 4)]
    with pytest.raises(PlanError, match="操作 1/2"):
        _check_conflicts(ops)
    assert not run_execution(ops).ok
    assert all(r.status == "failed" for r in execute_ops(ops))
    assert src.read_bytes() == b"AAAA" and second.read_bytes() == b"CCCC"
    assert not dst.exists()


def test_parent_symlink_aliases_still_collide(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    ops = [
        ExecOp("move", str(tmp_path / "a"), str(real / "target"), 1),
        ExecOp("move", str(tmp_path / "b"), str(alias / "target"), 1),
    ]
    assert safety.plan_path_conflicts(ops)


@pytest.mark.parametrize("targets", [("b", "a"), ("b", "c"), ("out", "out/file")])
def test_cycles_chains_and_file_directory_conflicts(tmp_path, targets):
    ops = [ExecOp("move", str(tmp_path / src), str(tmp_path / dst), 1)
           for src, dst in zip(("a", "b"), targets)]
    assert safety.plan_path_conflicts(ops)


def test_valid_move_files_then_move_remaining_directory(tmp_path):
    directory = tmp_path / "downloads"
    directory.mkdir()
    src = directory / "main.mkv"
    src.write_bytes(b"video")
    keep = directory / "readme.txt"
    keep.write_bytes(b"keep")
    dst = tmp_path / "library" / "main.mkv"
    recycled = tmp_path / "recycle" / "downloads"
    ops = [ExecOp("move", str(src), str(dst), 5),
           ExecOp("keep", str(keep), None, 4),
           ExecOp("movedir", str(directory), str(recycled), 0)]
    assert not safety.plan_path_conflicts(ops)
    assert run_execution(ops).ok
    assert dst.read_bytes() == b"video"
    assert (recycled / "readme.txt").read_bytes() == b"keep"
