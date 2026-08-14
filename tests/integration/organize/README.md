# Organize 集成测试（内置文件整理子系统）

覆盖 docs/design/file-organization.md 的全链路：下载完成通知 → 自动规划落计划
→ 执行 → 文件落到 Library → 源目录清理 → 任务清理。两层形态：

## 1. 进程内集成测试（`test_organize_pipeline.py`，CI 可跑、不依赖 docker）

```
uv run pytest tests/integration/organize -q
```

- DB 用 tests/unit/conftest.py 的每测试独立 Turso 文件库（`db_engine` 同时把
  引擎安装为 `app.database` 全局 factory，因此 scheduler 的 notify tick 与
  organize auto_execute 后台任务都打在测试库上）。
- 文件系统用 `tmp_path` 模拟共享卷：Transmission 容器视角 `/downloads/...` 与
  本进程视角 `tmp_path/mnt/shared/...` **刻意不同**，DownloaderInstance 经
  `volume_id` 绑定 StorageVolume（mount_path=进程视角根），验证卷绑定路径解析。
- 链路从真实通知生成开始（`create_notification_for_task` 或 scheduler 单次
  tick `_process_download_notifications`，含停种 + RPC 文件清单快照 + mock
  webhook fan-out），走 `plan_for_notifications` → `execute_plan`。
- 覆盖：单集剧集（tick 全链路 + 手动执行）、auto_execute 全自动、合集
  （batch 覆盖度校验 + 字幕随正片 + 特典 keep）、电影 category
  （needs_category → classify → 执行）、待分类 → classify → 执行、
  无卷绑定恒等。
- 下载器 RPC（pause/get_torrent_files/remove_torrent）与 Plex 刷新全部 mock；
  单集用例会断言 `remove_torrent(..., delete_data=False)` 与任务转
  `cancelled`、源目录自底向上清空且下载根保留。

该目录会被 `docker-compose.test.yml` 的 test-runner 一并收集（只按路径
`tests/integration/` 选择，无额外环境需求）。

## 2. 容器级半 E2E（`docker-compose.organize-e2e.yml` + `scripts/organize_e2e.py`）

真实两容器共享卷形态（设计文档「部署（共享卷）」的落地验证）：

- 同一 named volume `organize-e2e-shared` 挂到 Transmission（`/downloads`）
  与 RSSRipple（`/mnt/shared/downloads`，**不同挂载点**）；Library root 用
  `organize-e2e-media` 卷（app 视角 `/media`）。
- 驱动脚本在本地构造一个真实 torrent（pieces 按内容计算，添加后 Transmiss­ion
  校验即做种，不需要外部 peer），文件预置进共享卷；app 启动前用一次性
  `docker compose run` 容器 ORM seed「completed DownloadTask」（Turso 单进程
  文件锁，不能与运行中的 app 并行写库；API 也不暴露任务创建端点）。
- app 启动后（`SCHEDULER_ENABLED=true` + `ORGANIZE_ENABLED=true`）一切走真实
  代码路径：每分钟 tick 停种（真实 RPC）→ 建通知 → organize 规划（卷绑定
  解析）→ auto_execute 执行 → 任务清理（真实 RPC remove_torrent）。

运行（仓库根目录，需要 docker）：

```
uv run python scripts/organize_e2e.py           # 全链路，保留环境供检查
uv run python scripts/organize_e2e.py --down    # 跑完拆除（含 volumes）
```

脚本断言：计划 done、文件落在 `/media/movies/Hamnet (2025)/`、源种子目录
被清空、下载根保留、任务转 `cancelled`。
