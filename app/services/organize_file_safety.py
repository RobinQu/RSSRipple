"""Read-only file/plan checks and atomic publication for organize.

All callers run these synchronous checks in their planning/execution thread.
Content equality is deliberately uncached: size and timestamps alone never
authorize deleting a residual source. This is not a cross-process plan lock.
"""

from __future__ import annotations

import ctypes
import errno
import os
import stat
from pathlib import Path
from typing import BinaryIO, Protocol

_CHUNK_SIZE = 1024 * 1024
FileStamp = tuple[int, int, int, int, int]


class FileSafetyError(ValueError):
    """A file cannot safely be treated as the planned/completed operation."""


class FileOp(Protocol):
    op_type: str
    src: str
    dst: str | None
    size: int


def _stamp(info: os.stat_result) -> FileStamp:
    if not stat.S_ISREG(info.st_mode):
        raise FileSafetyError("路径不是普通文件（不跟随文件符号链接）")
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def file_stamp(path: Path) -> FileStamp:
    return _stamp(path.stat(follow_symlinks=False))


def require_unchanged(path: Path, expected: FileStamp) -> None:
    if file_stamp(path) != expected:
        raise FileSafetyError(f"校验期间文件发生变化，保留源：{path}")


def _same_contents(left: BinaryIO, right: BinaryIO) -> bool:
    while True:
        chunk = left.read(_CHUNK_SIZE)
        other = right.read(_CHUNK_SIZE)
        if chunk != other:
            return False
        if not chunk:
            return True


def require_equal_files(src: Path, dst: Path, size: int, *, hardlink: bool = False) -> tuple[FileStamp, FileStamp]:
    """Return stable identities only after proving equality (or shared inode)."""
    before_src, before_dst = file_stamp(src), file_stamp(dst)
    if before_dst[2] != size:
        raise FileSafetyError(f"目标已存在且大小不符，拒绝覆盖：{dst}")
    if before_src[2] != size:
        raise FileSafetyError(f"源文件大小与计划快照不符：{src}")
    if before_src[:2] != before_dst[:2]:
        if hardlink:
            raise FileSafetyError(f"目标不是源文件的硬链接，拒绝覆盖：{dst}")
        with src.open("rb") as left, dst.open("rb") as right:
            if _stamp(os.fstat(left.fileno())) != before_src or _stamp(os.fstat(right.fileno())) != before_dst:
                raise FileSafetyError("打开文件时身份发生变化，保留源")
            equal = _same_contents(left, right)
            if _stamp(os.fstat(left.fileno())) != before_src or _stamp(os.fstat(right.fileno())) != before_dst:
                raise FileSafetyError("读取期间文件发生变化，保留源")
        if not equal:
            raise FileSafetyError(f"目标已存在但内容不同，拒绝覆盖：{dst}")
    require_unchanged(src, before_src)
    require_unchanged(dst, before_dst)
    return before_src, before_dst


def validate_file_state(src: Path, dst: Path, size: int, *, file_op: str = "move") -> bool:
    """True = destination verified; False = source ready, destination absent.

    A legacy move with no source retains the size-only recovery check. It
    authorizes no deletion and cannot establish content equality retroactively.
    """
    src_exists, dst_exists = os.path.lexists(src), os.path.lexists(dst)
    if dst_exists:
        if file_stamp(dst)[2] != size:
            raise FileSafetyError(f"目标已存在且大小不符，拒绝覆盖：{dst}")
        if src_exists:
            require_equal_files(src, dst, size, hardlink=file_op == "hardlink")
        elif file_op != "move":
            raise FileSafetyError(f"保种源文件缺失：{src}")
        return True
    if not src_exists:
        raise FileSafetyError(f"源文件与目标均不存在：{src}")
    if file_stamp(src)[2] != size:
        raise FileSafetyError(f"源文件大小与计划快照不符（规划后已被改动）：{src}")
    return False


def unlink_verified_source(src: Path, dst: Path, size: int) -> None:
    """Never remove a different source merely because its size matches."""
    if src.resolve() == dst.resolve():
        return
    src_stamp, dst_stamp = require_equal_files(src, dst, size)
    require_unchanged(src, src_stamp)
    require_unchanged(dst, dst_stamp)
    src.unlink()


def _rename_noreplace_linux(src: Path, dst: Path) -> None:
    """Attempt the native Linux no-replace rename."""
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, "renameat2", None)
    if rename is None:
        raise OSError(errno.ENOSYS, "renameat2(RENAME_NOREPLACE) is unavailable")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(src), -100, os.fsencode(dst), 1) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), str(dst))


def rename_noreplace(src: Path, dst: Path) -> None:
    """Publish without replacement, then remove the original file name.

    Filesystems without RENAME_NOREPLACE (including the tested Docker ZFS
    root) use an atomic hard-link publication for regular files. A crash may
    leave both names; normal equality-based recovery handles that state.
    Neither path falls back to an overwriting rename. EXDEV remains the
    caller's responsibility, and unsupported directory operations fail closed.
    """
    try:
        _rename_noreplace_linux(src, dst)
        return
    except OSError as exc:
        if exc.errno not in {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP}:
            raise
    before = file_stamp(src)  # excludes directories and symbolic links
    os.link(src, dst, follow_symlinks=False)  # atomic EEXIST, never replacement
    after = file_stamp(src)
    if after[:4] != before[:4] or file_stamp(dst) != after:
        # Hard-link creation itself changes ctime; identity/size/mtime must
        # still match, and both names must reference the same current inode.
        raise FileSafetyError("发布期间文件发生变化，保留源")
    unlink_verified_source(src, dst, before[2])


def plan_path_conflicts(ops: list[FileOp]) -> list[str]:
    """Check the final operation graph, including legacy database plans.

    File sources may live inside a movedir source: normal execution moves the
    main files out first, then relocates the remaining keep files. Destinations
    must not enter that directory or overlap another operation's destination.
    """
    errors: list[str] = []
    paths: list[tuple[int, FileOp, Path, Path | None]] = []
    for seq, op in enumerate(ops, 1):
        try:
            paths.append((seq, op, Path(op.src).resolve(), Path(op.dst).resolve() if op.dst else None))
        except (OSError, RuntimeError) as exc:
            errors.append(f"操作 {seq} 路径无法解析：{exc}")
    destinations = [(seq, op, src, dst) for seq, op, src, dst in paths if dst is not None]
    for i, (seq, op, src, dst) in enumerate(destinations):
        for other_seq, other, other_src, other_dst in destinations[i + 1:]:
            if dst == other_dst or dst.is_relative_to(other_dst) or other_dst.is_relative_to(dst):
                errors.append(f"操作 {seq}/{other_seq} 目标冲突，拒绝覆盖：{dst} / {other_dst}")
            if op.op_type == other.op_type == "movedir" and (
                src.is_relative_to(other_src) or other_src.is_relative_to(src)
            ):
                errors.append(f"操作 {seq}/{other_seq} 目录源重叠：{src} / {other_src}")
        if op.op_type == "movedir" and (dst.is_relative_to(src) or src.is_relative_to(dst)):
            errors.append(f"操作 {seq} 目录源目标嵌套：{src} / {dst}")
        for other_seq, other, other_src, _ in paths:
            if seq == other_seq:
                continue
            # Writing into a future movedir source would move the result away.
            if dst == other_src or (
                other.op_type == "movedir" and dst.is_relative_to(other_src)
            ) or (other.op_type != "movedir" and (
                dst.is_relative_to(other_src) or other_src.is_relative_to(dst)
            )):
                errors.append(f"操作 {seq} 目标影响操作 {other_seq} 的源：{dst} / {other_src}")
    return errors
