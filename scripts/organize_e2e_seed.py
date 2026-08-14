"""Organize 容器级半 E2E 的一次性 DB seed（在 app 容器启动前执行）。

由 ``scripts/organize_e2e.py`` 通过 ``docker compose run --rm --no-deps``
调用。Turso 库文件是单进程独占锁（conventions.md），不能与运行中的 app
同时写库，所以 seed 必须在 app 启动前完成；HTTP API 不暴露
FileResource/DownloadTask 的创建端点，这里直接用 ORM 造「已完成下载」
链路：Channel / StorageVolume + DownloaderInstance 卷绑定（daemon
``/downloads`` 根 == 卷 ``/mnt/shared/downloads`` + 子路径 ``complete``）/
Movie / FileResource / Agent / mock Webhook / completed DownloadTask
（指向 transmission 容器里真实添加的 torrent id）。app 启动后的每分钟
notify tick 会接手：停种 + 建通知 + organize 规划 + auto_execute 执行 +
任务清理。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from datetime import date
from pathlib import Path

# `docker compose run ... python /app/scripts/organize_e2e_seed.py` 的
# sys.path[0] 是 scripts/ 而非镜像 WORKDIR，显式补上 /app。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _uuid() -> str:
    return str(uuid.uuid4())


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--torrent-id", type=int, required=True)
    parser.add_argument("--title", default="Hamnet")
    parser.add_argument("--year", type=int, default=2025)
    parser.add_argument("--title-raw", default="Hamnet.2025.1080p")
    args = parser.parse_args()

    import app.models  # noqa: F401  (register models before create_tables)
    from app.database import async_session_factory, create_tables
    from app.models.agent import Agent
    from app.models.agent_webhook import AgentWebhook
    from app.models.channel import Channel
    from app.models.download_task import DownloadTask
    from app.models.downloader import DownloaderInstance
    from app.models.file_resource import FileResource
    from app.models.movie import Movie
    from app.models.storage_volume import StorageVolume
    from app.utils.time import utcnow

    await create_tables()
    async with async_session_factory() as db:
        channel = Channel(
            id=_uuid(), name="e2e-channel", type="rss_feed",
            url="https://example.com/rss", fetch_interval=1800,
            status="active",
            field_mapping={
                "list_locator": {"source": "entries"},
                "field_mappings": {"torrent_url": {"source": "link"}},
            },
            metadata_agent_enabled=False,
        )
        volume = StorageVolume(
            id=_uuid(), name="e2e-shared", mount_path="/mnt/shared/downloads",
        )
        downloader = DownloaderInstance(
            id=_uuid(), name="e2e-transmission", type="transmission",
            url="http://transmission:9091/transmission/rpc",
            download_dir="/downloads/complete",
            # 卷绑定：daemon 视角 /downloads/complete ==
            # 卷 /mnt/shared/downloads + 子路径 complete（两容器挂载点不同）
            volume_id=volume.id, volume_subpath="complete",
            status="disconnected",
        )
        movie = Movie(
            id=_uuid(), title_en=args.title, original_title=args.title,
            content_type="movie", is_anime=False, genre=["Drama"],
            release_date=date(args.year, 1, 1),
        )
        db.add_all([channel, volume, downloader, movie])
        await db.flush()
        agent = Agent(
            id=_uuid(), name="e2e-agent", channel_id=channel.id,
            downloader_id=downloader.id,
        )
        webhook = AgentWebhook(
            id=_uuid(), agent_id=agent.id,
            url="http://e2e-consumer.invalid/hook", mock=True, enabled=True,
        )
        resource = FileResource(
            id=_uuid(), channel_id=channel.id, guid=_uuid(),
            title_raw=args.title_raw,
            torrent_url="magnet:?xt=urn:btih:e2e",
            movie_id=movie.id, is_batch=False,
            resolution="1080p", container="mkv", title_year=args.year,
        )
        task = DownloadTask(
            id=_uuid(), agent_id=agent.id, file_resource_id=resource.id,
            downloader_id=downloader.id, download_dir="/downloads/complete",
            transmission_torrent_id=args.torrent_id,
            status="completed", completed_at=utcnow(),
        )
        db.add_all([agent, webhook, resource, task])
        await db.commit()
        # 最后一行输出 JSON 供宿主驱动脚本解析
        print(json.dumps({
            "channel_id": channel.id, "downloader_id": downloader.id,
            "movie_id": movie.id, "agent_id": agent.id,
            "resource_id": resource.id, "task_id": task.id,
        }))


if __name__ == "__main__":
    asyncio.run(main())
