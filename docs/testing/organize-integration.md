# Organize 集成测试（内置文件整理子系统）

覆盖 [file-organization.md](../design/file-organization.md) 的全链路：下载完成通知 → 自动规划落计划
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
- app 启动后（`SCHEDULER_ENABLED=true`）一切走真实
  代码路径：每分钟 tick 停种（真实 RPC）→ 建通知 → organize 规划（卷绑定
  解析）→ auto_execute 执行 → 任务清理（真实 RPC remove_torrent）。

运行（仓库根目录，需要 docker）。`scripts/organize_e2e.py` 内部调用
`docker compose -f docker-compose.organize-e2e.yml`，因此用 `COMPOSE_PROJECT_NAME`
隔离项目名（**强制要求**，禁止默认的 `rssripple`，以免影响正在运行的 dev/生产栈）：

```
COMPOSE_PROJECT_NAME=rssripple-organize-e2e uv run python scripts/organize_e2e.py           # 全链路，保留环境供检查
COMPOSE_PROJECT_NAME=rssripple-organize-e2e uv run python scripts/organize_e2e.py --down    # 跑完拆除（含 volumes）
```

脚本断言：计划 done、文件落在 `/media/movies/Hamnet (2025)/`、源种子目录
被清空、下载根保留、任务转 `cancelled`。

## 文件丢失防回归（P0-7/P0-8）

`tests/unit/test_organize_file_safety.py` 使用真实临时文件覆盖：等大小异内容保源、完整相同/空文件重放、共享 inode、读取失败/比较期间变化、目标在预检后被占用、复制中断/EXDEV/不支持原子发布、重复目标/父目录链接别名/源目标循环、合法文件先移动后 movedir。planner 与 executor 共享门禁，旧持久计划也必须拒绝；service 测试验证失败不调用任务清理。

执行前完整内容比较会增加 IO；本轮不提供跨进程计划锁或断电后完整性保证，两者不等同于文件内容校验。


### P0 真实文件清单与队列消费补验

`test_p0_real_manifest.py` 从 v2 独立审核的猫与龙 S1E10 案例读取 torrent 哈希、真实文件名及原始大小，经 completed Task→通知→规划落库→执行→下载器清理走完整生产链。12 项覆盖 move/copy/hardlink 的新目标、相同目标、尾部异内容和旧计划重复目标，并验证重试。媒体字节明确为超过 1 MiB 的确定性合成内容；原始媒体未采集，不宣称验证真实媒体文件。JUnit suite properties 记录来源和尺寸差异，只有下载器 RPC 与媒体服务器刷新替换为边界 mock。

`test_p0_queue_consumption.py` 使用真实内存队列与生产 run_agent handler，验证修订端点导入后替换单例仍消费旧资源、事务可见、两次修订各落运行记录且不推进水位线；资源刻意保持未识别以验证门禁，不能当作 Redis 多 worker 派发验收。

### 2026-09-13 路径边界续验

新增 `test_source_path_boundary.py` 与 `test_media_scan_boundary.py`：真实审核文件名＋显式恶意路径注入、通知规划/执行、扫描 API 整批原子拒绝、历史库错误展示、旧计划源/目标和目录替换。路径错误必须保留源/卷外哨兵、无任务清理；合法与不相关坏库的对照仍成功。方案与当前门禁结果见 [V1-PATH-SAFETY.md](../plans/p0-and-backlog/V1-PATH-SAFETY.md) 和 [VALIDATION.md](../plans/p0-and-backlog/VALIDATION.md)。


## 计划版本与所有权回归

`test_p0_real_manifest.py` 增加配置变更窗口，使用已审核原始 torrent 清单与合成媒体字节，验证先替换 ops/版本再按冻结 move/copy/hardlink 执行，检查清理及保种副作用。`test_turso_snapshot_recovery.py` 以真实双连接和 SAVEPOINT 写锁冲突验证池中连接可见后续提交，无 SQL mock 或时序 sleep。

服务/API 回归覆盖 CAS 重建/分类、冻结字段、活动 owner 拒绝执行/取消及取消期间文件线程完成；`test_organize_migration.py` 从真实旧 schema 在 Turso 和独立 PG 上验两次升级。跨进程/容器竞争与故障恢复必须另跑 [V2 验收矩阵及工具](../plans/p0-and-backlog/V2-PLAN-OWNERSHIP.md)，不能以单进程锁测试或离线集成数量代替。各轮结果与剩余项见 [VALIDATION.md](../plans/p0-and-backlog/VALIDATION.md)。所有本地测试的整理锁目录必须独立 tmp_path。
