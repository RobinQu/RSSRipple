"""Synchronous organize execution: validate, publish without overwrite, verify.

The planner and executor share file-content and operation-path checks.
Existing destinations authorize move source cleanup only after full content
comparison (or shared inode). Cross-device move/copy stage verified files in
an exclusive temporary destination before atomic publication. No DB access.
"""

from __future__ import annotations

import errno
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from app.services.organize_file_safety import (
    FileSafetyError,
    file_stamp,
    plan_path_conflicts,
    rename_noreplace,
    require_equal_files,
    require_unchanged,
    unlink_verified_source,
    validate_file_state,
)


@dataclass
class ExecOp:
    """执行器视角的单条 op（由 OrganizePlanOp ORM 行转换而来）。"""

    op_type: str  # "move" | "keep" | "movedir"
    src: str
    dst: str | None
    size: int
    reason: str = ""


@dataclass
class OpResult:
    op: ExecOp
    status: str  # "done" | "kept" | "failed"
    error: str | None = None


@dataclass
class ExecutionOutcome:
    """一次计划执行的完整结果（service 层据此落库）。"""

    op_results: list[OpResult] = field(default_factory=list)
    # 审计明细：{"action", "detail"}，action 如 move/keep/movedir/cleanup/
    # precheck/verify，detail 为自由结构 JSON。
    audits: list[dict] = field(default_factory=list)
    # 非 None = 计划级失败原因（前置门禁/文件操作/后置校验/movedir）。
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


# ---------------------------------------------------------------- 前置门禁


def precheck(ops: list[ExecOp], *, file_op: str = "move") -> list[str]:
    """Reject the whole plan before mutation, including persisted legacy ops."""
    violations = plan_path_conflicts(ops)
    if violations:
        return violations
    for op in ops:
        if op.op_type == "keep":
            continue
        assert op.dst is not None
        if op.op_type == "movedir":
            if os.path.lexists(op.dst):
                if not os.path.lexists(op.src) and Path(op.dst).is_dir():
                    continue
                violations.append(f"目标目录已存在，拒绝覆盖：{op.dst}")
            elif not os.path.exists(op.src):
                violations.append(f"源目录与目标目录均不存在：{op.src}")
            continue
        try:
            validate_file_state(Path(op.src), Path(op.dst), op.size, file_op=file_op)
        except (OSError, FileSafetyError) as exc:
            violations.append(str(exc))
    return violations


# ---------------------------------------------------------------- 幂等执行


def execute_ops(ops: list[ExecOp], *, file_op: str = "move") -> list[OpResult]:
    """Apply file operations; any plan-graph conflict prevents all mutations."""
    conflicts = plan_path_conflicts(ops)
    if conflicts:
        return [OpResult(op=op, status="failed", error="；".join(conflicts[:3])) for op in ops]
    execute = {"move": _execute_move, "hardlink": _execute_hardlink, "copy": _execute_copy}[file_op]
    results: list[OpResult] = []
    for op in ops:
        if op.op_type == "keep":
            results.append(OpResult(op=op, status="kept"))
        elif op.op_type == "move":
            try:
                results.append(execute(op))
            except (OSError, FileSafetyError) as exc:
                results.append(OpResult(op=op, status="failed", error=f"文件操作失败：{exc}"))
    return results


def _execute_move(op: ExecOp) -> OpResult:
    src, dst = Path(op.src), Path(op.dst) if op.dst else None
    assert dst is not None
    if validate_file_state(src, dst, op.size):
        if os.path.lexists(src):
            unlink_verified_source(src, dst, op.size)
        return OpResult(op=op, status="done")

    source_stamp = file_stamp(src)
    dst.parent.mkdir(parents=True, exist_ok=True)
    require_unchanged(src, source_stamp)
    try:
        rename_noreplace(src, dst)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        _copy_and_publish(src, dst, op.size)
        # A fresh comparison also protects recovery after publish-before-unlink.
        require_unchanged(src, source_stamp)
        unlink_verified_source(src, dst, op.size)
    return OpResult(op=op, status="done")


def _execute_hardlink(op: ExecOp) -> OpResult:
    src, dst = Path(op.src), Path(op.dst) if op.dst else None
    assert dst is not None
    if validate_file_state(src, dst, op.size, file_op="hardlink"):
        return OpResult(op=op, status="done")
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dst, follow_symlinks=False)
    except OSError as exc:
        return OpResult(op=op, status="failed", error=f"硬链接失败（不静默退化为 copy）：{exc}")
    require_equal_files(src, dst, op.size, hardlink=True)
    return OpResult(op=op, status="done")


def _copy_and_publish(src: Path, dst: Path, size: int) -> None:
    """Only publish a verified, attempt-owned staging file; never overwrite."""
    source_stamp = file_stamp(src)
    dst.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix=".rssripple-", suffix=".tmp", dir=dst.parent, delete=False) as temp:
        staging = Path(temp.name)
        staging_identity = file_stamp(staging)[:2]
        try:
            # Copy through the exclusive descriptor, never reopen a temp path
            # for writing (a replacement path must not become an overwrite).
            with src.open("rb") as source:
                shutil.copyfileobj(source, temp, length=1024 * 1024)
            temp.flush()
            try:
                require_unchanged(src, source_stamp)
                _, verified = require_equal_files(src, staging, size)
                if verified[:2] != staging_identity:
                    raise FileSafetyError("临时文件身份变化")
                require_unchanged(staging, verified)
            except (OSError, FileSafetyError) as exc:
                raise FileSafetyError(f"复制校验失败：{exc}") from exc
            rename_noreplace(staging, dst)
        finally:
            # Only clean this attempt's inode, including on publication errors.
            if os.path.lexists(staging) and file_stamp(staging)[:2] == staging_identity:
                staging.unlink()


def _execute_copy(op: ExecOp) -> OpResult:
    src, dst = Path(op.src), Path(op.dst) if op.dst else None
    assert dst is not None
    if not validate_file_state(src, dst, op.size, file_op="copy"):
        _copy_and_publish(src, dst, op.size)
    return OpResult(op=op, status="done")


def execute_movedir(op: ExecOp) -> str | None:
    """目录级移动（如电影种子文件夹移入 Extras 库）；返回错误描述或 None。

    冲突绝不覆盖；源目录已为空视为无需移动（计划生成后剩余内容已被清空）。
    """
    src = Path(op.src)
    dst = Path(op.dst) if op.dst else None
    assert dst is not None
    if not src.exists():
        return None if dst.exists() else f"源目录与目标目录均不存在：{src}"
    if dst.exists():
        return f"目标目录已存在，拒绝覆盖：{dst}"
    if not any(src.iterdir()):
        return None  # 空目录无需移动，交给空目录清理
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.rename(src, dst)
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
        shutil.move(str(src), str(dst))  # 跨文件系统：copytree + 删除源
    return None


# ---------------------------------------------------------------- 后置校验


def verify_done(ops: list[ExecOp], *, file_op: str = "move") -> list[str]:
    """后置校验：每个 move op 的 dst 存在且 size 匹配；返回违例描述列表。

    src 已消失仅对 move 校验——hardlink/copy 源文件本应保留（保种）。
    """
    problems: list[str] = []
    for op in ops:
        if op.op_type != "move" or op.dst is None or op.dst == op.src:
            continue
        if not os.path.exists(op.dst):
            problems.append(f"目标文件缺失：{op.dst}")
        elif os.path.getsize(op.dst) != op.size:
            problems.append(f"目标文件大小与计划不符：{op.dst}")
        if file_op == "move" and os.path.exists(op.src):
            problems.append(f"源文件残留：{op.src}")
    return problems


# ---------------------------------------------------------------- 空目录清理


def cleanup_empty_dirs(root: str | Path, preserve: str | Path | None = None) -> list[str]:
    """自底向上删除空目录（os.rmdir，绝不递归强删）；preserve 指定的目录本身保留。

    返回实际删除的目录列表（供审计）。
    """
    root_p = Path(root)
    keep = Path(preserve) if preserve is not None else root_p
    removed: list[str] = []
    if not root_p.is_dir():
        return removed
    for dirpath, _dirnames, _filenames in os.walk(root_p, topdown=False):
        p = Path(dirpath)
        if p == keep:
            continue
        try:
            p.rmdir()
            removed.append(str(p))
        except OSError:
            pass  # 非空目录自然失败跳过
    return removed


# ---------------------------------------------------------------- 整体编排


def run_execution(
    ops: list[ExecOp], *, file_op: str = "move",
    cleanup_root: str | None = None, preserve: str | None = None,
) -> ExecutionOutcome:
    """一个计划的完整同步执行段：前置门禁 → 文件 op 幂等执行 → 后置校验
    → movedir → 空目录清理。任一阶段失败整体 failed（已完成 op 不回滚，
    重放时由幂等状态表收敛）。

    ``file_op``：命中规则的 ``move`` / ``hardlink`` / ``copy``，决定 plan op
    ``move`` 的实际文件操作与审计 action 名。``cleanup_root`` / ``preserve``：
    空目录清理范围与保留边界（通常为种子独立目录与卷绑定解析后的下载根）；
    None = 跳过清理。hardlink/copy 计划恒跳过清理（源文件保留保种，目录
    本就不会空）。
    """
    outcome = ExecutionOutcome()
    file_ops = [op for op in ops if op.op_type in ("move", "keep")]
    dir_ops = [op for op in ops if op.op_type == "movedir"]

    # 前置门禁：任一违例整体放弃，不触碰任何文件
    violations = precheck(ops, file_op=file_op)
    if violations:
        outcome.error = f"前置门禁未通过（文件系统与计划快照不一致）：{'；'.join(violations[:3])}"
        outcome.audits.append(
            {
                "action": "precheck",
                "detail": {"status": "failed", "violations": violations[:5]},
            }
        )
        return outcome

    # 文件 op 幂等执行
    results = execute_ops(file_ops, file_op=file_op)
    outcome.op_results.extend(results)
    for r in results:
        outcome.audits.append(
            {
                # 审计 action 反映实际文件操作（plan op 恒为 move/keep 路由语义）
                "action": file_op if r.op.op_type == "move" else r.op.op_type,
                "detail": {
                    "src": r.op.src,
                    "dst": r.op.dst,
                    "size": r.op.size,
                    "status": r.status,
                    "error": r.error,
                    "reason": r.op.reason or None,
                },
            }
        )
    op_failures = [r for r in results if r.status == "failed"]
    if op_failures:
        detail = "；".join(
            f"{os.path.basename(r.op.src)}: {r.error}" for r in op_failures[:3]
        )
        outcome.error = f"文件操作失败：{detail}"
        return outcome

    # 后置校验：目标到位（src 消失仅 move 校验）
    problems = verify_done(file_ops, file_op=file_op)
    if problems:
        outcome.error = f"后置校验未通过：{'；'.join(problems[:3])}"
        outcome.audits.append(
            {
                "action": "verify",
                "detail": {"status": "failed", "problems": problems[:5]},
            }
        )
        return outcome

    # movedir（目录级移动，如电影种子文件夹移入 Extras 库）
    for op in dir_ops:
        error = execute_movedir(op)
        outcome.op_results.append(
            OpResult(op=op, status="failed" if error else "done", error=error)
        )
        outcome.audits.append(
            {
                "action": "movedir",
                "detail": {
                    "src": op.src,
                    "dst": op.dst,
                    "status": "failed" if error else "done",
                    "error": error,
                    "reason": op.reason or None,
                },
            }
        )
        if error:
            outcome.error = error
            return outcome

    # 空目录清理（只删空目录，保留下载根）；hardlink/copy 源文件保留保种，
    # 目录本就不会空，恒跳过。
    if cleanup_root and file_op == "move":
        removed = cleanup_empty_dirs(cleanup_root, preserve=preserve)
        if removed:
            outcome.audits.append(
                {"action": "cleanup", "detail": {"removed_dirs": removed}}
            )
    return outcome
