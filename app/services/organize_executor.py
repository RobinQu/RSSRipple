"""整理执行器（内置整理子系统 organize 的执行层）。

逐条移植 vault-organizer ``executor.py`` 的安全不变量（见
docs/design/file-organization.md「执行器不变量」）：

- 前置门禁（:func:`precheck`）：执行前逐 op 复核磁盘现状与计划快照一致，
  任一违例整体放弃，不触碰任何文件。
- 幂等状态表（:func:`execute_ops` 逐 move op）：dst 存在且 size 匹配 =
  已完成（move 模式下 src 残留且 size 相同则删 src 收敛；hardlink/copy
  保留 src 保种）；src 在 dst 不在 = 执行文件操作；dst size 不符 = 冲突
  failed，绝不覆盖；src/dst 双不在 = 数据丢失 failed。
  ``src == dst`` 直接 done；keep 不触碰标 kept。
- 移动策略（``file_op="move"``）：同文件系统 ``os.rename``（原子）；跨
  文件系统（EXDEV）退化为 copy + size 校验 + 删源，校验失败删不完整 dst
  报 failed；dst 父目录 ``mkdir(parents=True, exist_ok=True)``。
- 硬链接（``file_op="hardlink"``）：``os.link``，源文件保留（保种）；
  EXDEV/EPERM 等 OSError → 该 op failed 且带明确 error_message，**不静默
  退化为 copy**（静默复制会偷偷翻倍存储并违背保种意图）。
- 复制（``file_op="copy"``）：copy + size 校验（失败删不完整 dst 报
  failed），源文件保留。
- 后置校验（:func:`verify_done`）：全部文件 op 后复核每个 dst 存在且
  size 一致；src 已消失仅对 move 校验（hardlink/copy 源文件本应保留）。
- movedir（:func:`execute_movedir`）：目标已存在 = 冲突，绝不覆盖。
- 空目录清理（:func:`cleanup_empty_dirs`）：``os.walk(topdown=False)``
  自底向上 ``os.rmdir``（只删空目录），preserve 边界 = 下载根；
  绝不 ``rm -rf``。hardlink/copy 计划跳过（源文件保留，目录本就不会空）。

本模块只做同步文件 IO 与结果汇报，不接触数据库：op 执行结果与审计明细
以 plain 数据返回，由 organize_service 在异步上下文里落库。串行化
（单 asyncio.Lock）与崩溃恢复（running 重放）同样在 organize_service。
"""

from __future__ import annotations

import errno
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path


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


def precheck(ops: list[ExecOp]) -> list[str]:
    """前置门禁：逐 op 复核磁盘现状与计划快照一致；返回违例描述列表（空 = 通过）。

    规划与执行之间是异步的，文件系统可能有其他改动：src 缺失/大小变化、dst
    冲突一律视为违例，调用方应整体放弃执行（不触碰任何文件）。已完成的 op
    （dst 在且 size 匹配 / 目录已整体移走）视为幂等满足（重放收敛）。
    """
    violations: list[str] = []
    for op in ops:
        if op.op_type == "keep":
            continue
        dst = op.dst
        assert dst is not None
        if op.op_type == "movedir":
            if os.path.exists(dst):
                if not os.path.exists(op.src):
                    continue  # 已完成：目录已整体移入
                violations.append(f"目标目录已存在，拒绝覆盖：{dst}")
            elif not os.path.exists(op.src):
                violations.append(f"源目录与目标目录均不存在：{op.src}")
            continue
        dst_exists = os.path.exists(dst)
        src_exists = os.path.exists(op.src)
        if dst_exists:
            if os.path.getsize(dst) == op.size:
                continue  # 已完成（重放收敛）
            violations.append(f"目标已存在且大小不符，拒绝覆盖：{dst}")
        elif not src_exists:
            violations.append(f"源文件与目标均不存在：{op.src}")
        elif os.path.getsize(op.src) != op.size:
            violations.append(f"源文件大小与计划快照不符（规划后已被改动）：{op.src}")
    return violations


# ---------------------------------------------------------------- 幂等执行


def execute_ops(ops: list[ExecOp], *, file_op: str = "move") -> list[OpResult]:
    """逐条执行文件 op；movedir 由 :func:`execute_movedir` 单独处理。

    ``file_op``（来自命中规则）决定 plan op ``move`` 的实际文件操作：
    ``move`` / ``hardlink`` / ``copy``；keep 不受影响。
    """
    results: list[OpResult] = []
    for op in ops:
        if op.op_type == "keep":
            results.append(OpResult(op=op, status="kept"))
        elif op.op_type == "move":
            if file_op == "hardlink":
                results.append(_execute_hardlink(op))
            elif file_op == "copy":
                results.append(_execute_copy(op))
            else:
                results.append(_execute_move(op))
    return results


def _state_table(op: ExecOp, src: Path, dst: Path) -> OpResult | None:
    """三态共用的幂等状态表前置分支；返回 None = 就绪（src 在 dst 不在）。

    调用方负责 ready 分支的实际文件操作。``src == dst`` 与冲突/数据丢失
    三分支语义与 file_op 无关；hardlink/copy 的「已完成」不收敛 src
    （保种），由调用方不再触达。
    """
    if src == dst:
        return OpResult(op=op, status="done")
    if dst.exists():
        if dst.stat().st_size == op.size:
            return OpResult(op=op, status="done")  # 已完成（重放收敛）
        return OpResult(op=op, status="failed", error=f"目标已存在且大小不符，拒绝覆盖：{dst}")
    if not src.exists():
        return OpResult(op=op, status="failed", error=f"源文件与目标均不存在：{src}")
    return None


def _execute_move(op: ExecOp) -> OpResult:
    src, dst = Path(op.src), Path(op.dst) if op.dst else None
    assert dst is not None
    if src == dst:
        return OpResult(op=op, status="done")

    dst_exists = dst.exists()
    src_exists = src.exists()

    if dst_exists:
        dst_size = dst.stat().st_size
        if dst_size == op.size:
            # 已完成（重放收敛）：src 残留且确为同一文件则删除
            if src_exists and src.stat().st_size == dst_size:
                src.unlink()
            return OpResult(op=op, status="done")
        return OpResult(op=op, status="failed", error=f"目标已存在且大小不符，拒绝覆盖：{dst}")

    if not src_exists:
        return OpResult(op=op, status="failed", error=f"源文件与目标均不存在：{src}")

    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.rename(src, dst)
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
        # 跨文件系统：copy + size 校验 + 删 src
        shutil.copyfile(src, dst)
        if dst.stat().st_size != op.size:
            dst.unlink(missing_ok=True)
            return OpResult(op=op, status="failed", error=f"跨文件系统复制校验失败：{dst}")
        src.unlink()
    return OpResult(op=op, status="done")


def _execute_hardlink(op: ExecOp) -> OpResult:
    """硬链接：``os.link``，源文件保留（保种）。

    跨文件系统/不支持（EXDEV/EPERM 等）→ 该 op failed 且带明确原因，
    **不静默退化为 copy**（静默复制会偷偷翻倍存储并违背保种意图）。
    """
    src, dst = Path(op.src), Path(op.dst) if op.dst else None
    assert dst is not None
    early = _state_table(op, src, dst)
    if early is not None:
        return early

    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dst)
    except OSError as e:
        return OpResult(
            op=op, status="failed",
            error=f"硬链接失败（{e.strerror or e}；不跨文件系统静默退化为 copy）：{dst}",
        )
    return OpResult(op=op, status="done")


def _execute_copy(op: ExecOp) -> OpResult:
    """复制：copy + size 校验（失败删不完整 dst），源文件保留（保种）。"""
    src, dst = Path(op.src), Path(op.dst) if op.dst else None
    assert dst is not None
    early = _state_table(op, src, dst)
    if early is not None:
        return early

    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    if dst.stat().st_size != op.size:
        dst.unlink(missing_ok=True)
        return OpResult(op=op, status="failed", error=f"复制校验失败：{dst}")
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
    violations = precheck(ops)
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
