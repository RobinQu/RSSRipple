"""整理完成后的下载任务清理（内置整理子系统 organize 的执行后动作）。

按命中规则的 ``file_op`` 分流：

- ``move``：:func:`delete_task_after_organize` —— 与
  ``DELETE /api/v1/tasks/{id}?delete_data=false`` 同一逻辑的内部调用版本
  （不走 HTTP 回环）：移除下载器中的 torrent（保留数据——文件已由整理流程
  移走或原地保留），任务行置为 ``cancelled``。任务不存在视为已删除，幂等
  成功；下载器 RPC 失败只回报 False，由调用方记日志，不改写计划状态。
- ``hardlink`` / ``copy``：:func:`resume_task_after_organize` —— 保种，
  不删任务；快照生成时 best-effort 停过的做种经 RPC 恢复（与
  ``POST /api/v1/tasks/{id}/resume`` 同一个 ``resume_torrent`` RPC；对已
  在运行的 torrent 是幂等 no-op）。RPC 失败同样只回报 False。
- :func:`delete_task_with_data` —— ``delete_data=true`` 语义（torrent 与
  磁盘数据一并删除），供取消变更计划等人工入口选用，非执行后自动动作。
"""

from __future__ import annotations

import logging

from app.clients.downloader import get_downloader_client
from app.models.download_task import DownloadTask
from app.models.downloader import DownloaderInstance

logger = logging.getLogger(__name__)


async def delete_task_after_organize(db, download_task_id: str) -> bool:
    """清理下载任务（delete_data=false 语义）。返回是否完全成功。

    404（任务已删）= 幂等成功；RPC 失败 = False（任务仍置 cancelled 并记录
    error_message，残留 paused 条目人工删除即可）。
    """
    task = await db.get(DownloadTask, download_task_id)
    if task is None:
        return True  # 已删除，幂等成功
    ok = True
    if task.transmission_torrent_id and task.downloader_id:
        downloader = await db.get(DownloaderInstance, task.downloader_id)
        if downloader is not None:
            try:
                wrapper = get_downloader_client(downloader)
                await wrapper.remove_torrent(
                    task.transmission_torrent_id, delete_data=False
                )
            except Exception as e:  # noqa: BLE001 — best-effort，见模块 docstring
                logger.warning(
                    "[organize] 移除 torrent %s 失败：%s",
                    task.transmission_torrent_id, e,
                )
                task.error_message = str(e)[:2000]
                ok = False
    task.status = "cancelled"
    return ok


async def delete_task_with_data(db, download_task_id: str) -> bool:
    """删除下载任务及其磁盘数据（delete_data=true 语义）。返回是否完全成功。

    与 ``DELETE /api/v1/tasks/{id}?delete_data=true`` 同一逻辑的内部调用
    版本（不走 HTTP 回环）：经下载器 RPC 移除 torrent 并删除已下载数据，
    任务行置为 ``cancelled``。任务不存在视为已删除，幂等成功；RPC 失败
    只回报 False，由调用方记日志。
    """
    task = await db.get(DownloadTask, download_task_id)
    if task is None:
        return True  # 已删除，幂等成功
    ok = True
    if task.transmission_torrent_id and task.downloader_id:
        downloader = await db.get(DownloaderInstance, task.downloader_id)
        if downloader is not None:
            try:
                wrapper = get_downloader_client(downloader)
                await wrapper.remove_torrent(
                    task.transmission_torrent_id, delete_data=True
                )
            except Exception as e:  # noqa: BLE001 — best-effort，见模块 docstring
                logger.warning(
                    "[organize] 移除 torrent %s（含数据）失败：%s",
                    task.transmission_torrent_id, e,
                )
                task.error_message = str(e)[:2000]
                ok = False
    task.status = "cancelled"
    return ok


async def resume_task_after_organize(db, download_task_id: str) -> bool:
    """hardlink/copy 模式执行成功后恢复做种（保种）。返回是否完全成功。

    通知快照生成时 best-effort 停过种（notify_service ``_build_snapshot``
    的 ``pause_torrent``）；硬链/复制保留源文件继续保种，执行成功后经
    ``resume_torrent`` RPC 恢复（对已运行的 torrent 幂等 no-op）。任务行
    状态不改：完成态任务在下载同步循环里以 ``is_finished`` 为准，
    RPC 侧的 stopped 不会把 DB 状态回写成 paused。
    任务不存在 / 无挂载 torrent 视为无需恢复，幂等成功；RPC 失败只回报
    False，由调用方记日志，不改写计划状态。
    """
    task = await db.get(DownloadTask, download_task_id)
    if task is None:
        return True  # 已删除，幂等成功
    if not task.transmission_torrent_id or not task.downloader_id:
        return True  # 无 torrent 可恢复
    downloader = await db.get(DownloaderInstance, task.downloader_id)
    if downloader is None:
        return False
    try:
        wrapper = get_downloader_client(downloader)
        await wrapper.resume_torrent(task.transmission_torrent_id)
    except Exception as e:  # noqa: BLE001 — best-effort，见模块 docstring
        logger.warning(
            "[organize] 恢复 torrent %s 做种失败：%s",
            task.transmission_torrent_id, e,
        )
        return False
    return True
