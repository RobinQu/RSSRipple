# 其他约定

- **时间格式**：API 中所有时间均为 ISO 8601 UTC 字符串（如 `2025-01-01T12:00:00Z`）。
- **下载目录格式**：
  - `DownloaderInstance.download_dir` 必填，必须是 Transmission 下载服务器 OS 可识别的绝对路径；路径语义以 Transmission daemon 为准，而不是 RSSRipple 后端进程所在主机为准。
  - 支持 POSIX absolute path、Windows drive absolute path、daemon 支持的 UNC path；后端校验时需要按路径风格识别根目录。
  - `Agent.download_subdir` 可空；非空时必须是相对路径，禁止以 `/`、`\`、`~`、Windows drive prefix（如 `C:\`）、UNC prefix（如 `\\server\share`）开头，禁止 `..` 段和控制字符。
  - 子目录 API 表达推荐使用 `/` 分隔；后端根据 Downloader 根目录风格拼接，标准化后必须保证最终路径仍在 `DownloaderInstance.download_dir` 下。
  - `DownloadTask.download_dir` 保存创建任务时解析出的最终绝对路径；任务重试沿用该字段。
  - 前端下载器表单的默认下载目录预填值为 `/downloads/complete`（新建表单初始值；编辑表单在已存值为空时回填；mock 类型除外，用 `/tmp/mock-downloads`）。
- **Transmission 目录 RPC 使用**：RSSRipple 不调用 `session_set(download_dir=...)` 修改 Transmission 全局默认目录；所有自动下载都通过 `torrent_add(..., download_dir=DownloadTask.download_dir)` 设置单个任务目录。
- **配置项**（环境变量，完整列表）：
  - `DATABASE_URL`：SQLAlchemy 数据库 URL（默认 `sqlite+aioturso:///data/rss_ripple_turso.db`）。支持两种后端：
    - `sqlite+aioturso:///`（默认）：嵌入式 Turso 引擎（SQLite 文件格式兼容）。启用 MVCC 并发写：`journal_mode='mvcc'` 为文件级持久属性（由 `create_tables` 或 `scripts/migrate_to_turso.py` 设置），连接级通过 URL 自动附加 `isolation_level=CONCURRENT`（隐式事务发 `BEGIN CONCURRENT`）。写-写冲突报 `Write-write conflict`，由统一的锁/冲突重试设施处理。旧 SQLite 库用 `uv run python scripts/migrate_to_turso.py --source <old.db> --target <new.db>` 迁移（备份复制 → 删除 FTS5 对象 → 开启 MVCC → 校验）。**单进程锁定**：同一 Turso 文件同时只允许一个进程打开（其实验性 `multiprocess_wal` 与 MVCC 不兼容），因此 app 与 eval 等并容器必须各用各的库文件；需要多实例共享数据库时用 PostgreSQL。**全文检索**：Turso 原生 FTS（`USING fts` 索引）与 MVCC 互斥，因此 FTS 影子表放在独立的边车数据库（`<主库名>_fts.db`，WAL 模式，ngram tokenizer，`experimental_features=index_method`）；索引仅缓存规范化标题、可随时从基表重建（`app/services/fts.py`），启动时若为空自动回填。
    - `postgresql+asyncpg://`：PostgreSQL（分布式部署）。
  - `QUEUE_BACKEND`：队列后端，`"memory"`（默认）或 `"redis"`。
  - `REDIS_URL`：可选 Redis 地址，`QUEUE_BACKEND=redis` 时必填。
  - `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL`：OpenAI 兼容 LLM，用于 feed 分析、统一 MetadataAgent、PendingDecision 建议。
  - `EXA_API_KEY` / `EXA_EFFORT_LEVEL`（默认 `low`，可选 `minimal`/`low`/`medium`/`high`/`xhigh`）：Exa Agent Search 数据源（频道源已废弃；仍用于手动搜索/评测与 wikipedia 未命中时的有序 Exa 回退）。
  - `JINA_API_KEY`：Jina Search + Reader 数据源。
  - `TMDB_API_KEY`：TMDB 数据源（可选）。
  - `EXA_ENABLED` / `JINA_ENABLED` / `TMDB_ENABLED` / `WIKIPEDIA_ENABLED`：各数据源启用开关，默认 `true`；设为 `false` 可在不清除凭证的情况下从 UI 隐藏该源。
  - `POSTER_CACHE_DIR`：海报缓存目录，挂载到 `/posters`（默认 `./data/posters`）。
  - `DEFAULT_FETCH_INTERVAL`：频道默认抓取间隔（秒，默认 `1800`）。
  - `TRANSMISSION_TIMEOUT`：Transmission RPC 超时。
  - `MAX_RETRY_COUNT`：失败下载最大重试次数（默认 `3`）。
  - `TASK_EXPIRE_DAYS`：已完成任务自动清理天数（默认 `30`）。
  - `DEV_MODE`：为 `true` 时内部错误响应含堆栈（默认 `false`）。
  - `DEBUG` / `LOG_LEVEL`（默认 `INFO`）：调试开关与日志级别。
  - Wikipedia Search 通过免费 `wikipedia` Python 库实现，无需额外 API key。
- **海报服务**：FastAPI 挂载 StaticFiles 到 `/posters`，物理目录为 `POSTER_CACHE_DIR`。
- **日志**：结构化 JSON 日志，含 `request_id`、`channel_id`、`agent_id`、`task_id` 等上下文字段。
- **幂等性**：Channel 抓取以 guid 去重；手动触发的 run/fetch 以分布式锁保证同一资源不会重复入队；Transmission add_torrent 以 torrent 哈希幂等（RPC 本身支持）。

---

