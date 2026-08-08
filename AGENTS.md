# RSSRipple - 方案设计索引

本文档是 RSSRipple 项目设计的**索引与核心约束速查**。详细设计已拆分为 `docs/design/` 下的子文档，均为权威设计来源，所有实现必须遵循：

| 子文档 | 内容 |
|--------|------|
| [docs/design/data-models.md](docs/design/data-models.md) | 全部 ORM 数据模型（字段、约束、关系） |
| [docs/design/filter-dsl.md](docs/design/filter-dsl.md) | Filter DSL 类型定义、求值语义、示例 |
| [docs/design/api-endpoints.md](docs/design/api-endpoints.md) | 全部 REST API 端点与请求/响应结构 |
| [docs/design/business-logic.md](docs/design/business-logic.md) | 抓取、metadata 匹配、Agent 运行/过滤、调度、下载同步、Mock Downloader |
| [docs/design/frontend.md](docs/design/frontend.md) | 前端路由、页面与关键交互 |
| [docs/design/error-handling.md](docs/design/error-handling.md) | 统一响应结构、错误码、全局异常处理 |
| [docs/design/conventions.md](docs/design/conventions.md) | 时间格式、下载目录规则、环境变量、海报、日志、幂等性 |
| [docs/design/branching.md](docs/design/branching.md) | 分支命名规范、CI/CD 与发布流程 |

修改任何上述领域的实现时，必须同步更新对应子文档。

---

## 核心约束速查

以下是最容易被违反的关键不变量；细节一律以子文档为准。

### 数据模型（详见 data-models.md）

- 所有 ORM 模型使用 SQLAlchemy 2.0 风格声明，主键为 UUID v4 字符串，时间字段均为 UTC。
- `FileResource` 的 FK 互斥：剧集资源 `series_id` 非空且 `movie_id` 为空；电影资源反之；未识别两者皆空。TV 集数统一用 `episode` 字段。
- 合集资源（`is_batch=true`）：`episode` 固定为空，`episode_start/end` 尽力而为；不参与 `(series_id, episode)` 聚合和 PendingDecision。
- `episode_confidence`：`raw | reconciled | ambiguous | manual | None`；`ambiguous` 资源不参与派发，按资源状态创建 PendingDecision（跳过 LLM）：`season` 非空为"集号不确定"，`season=None` 为"季号不确定"；用户修正为 `manual` 后自动回流。季号绝不猜测：无季标记时仅当作品可验证为单季（`number_of_seasons`/`seasons` 证据）才落 `season=1`，多季或未知一律 `season=None` + `ambiguous`。
- `Agent.last_consumed_at` 是消费水位线：增量运行只处理 `created_at > last_consumed_at` 的资源；null 表示从未运行（推进到 now、不处理任何资源，回填必须走 rules-preview 流程）。
- `PendingDecision` 幂等：同一 `(agent_id, series_id|movie_id, season, episode, status='pending')` 全局唯一，重复运行 upsert 合并 candidates。
- `AgentWork` 最多 10 个（`scope_channel_wide=false` 时生效）；CheckConstraint 保证 series/movie 二选一。
- `WorkCollection` 大 IP 合集分组（组织层而非消歧核心）：作品经可空 `collection_id` 至多属一个合集；TMDB 链接走确定性 `link_movie_collection`（`tmdb_collection` 源 + 原始数字 id，禁止 `canonicalize_external_id`）；DSL 新增 `series.collection`/`movie.collection`（显示名），所有过滤求值点须链式 selectinload 作品的 `collection` 关系。

### Filter DSL（详见 filter-dsl.md）

- `filter_config` / `filter_overrides` 均为 BoolCondition 根节点的 JSON DSL 树；字符串比较忽略大小写；`filter_overrides` 与全局 `filter_config` 按 AND 合并。
- 空值语义：`eq/contains/fuzzy/regex/gt/...` 不通过；`ne` 通过。空值匹配必须用 `is_empty`/`is_not_empty`（所有字段类型可用、不需要 value）；取值操作符的 `value` 禁止为空，保存时 422（`eq ""` 会静默过滤掉全部资源）。

### API（详见 api-endpoints.md）

- 前缀 `/api/v1`，统一响应结构 `{ success, data, error, meta }`；分页参数 `page`/`page_size`（最大 100）。
- `POST /agents` 的 `dispatch_resource_ids`：`null`=普通保存不动水位线；数组（含空 `[]`）= 经过 rules-preview，派发选中资源并推进水位线到频道 max `created_at`。
- `PATCH /resources/{id}/episode` 修正集号：未显式发送 `season` 且有 absolute 集号 + 剧集逐季数据时服务端推导 season（显式值优先）；**先 commit 再入队**定向运行（绕过且不推进水位线）。
- 任务重试必须使用 `DownloadTask.download_dir` 持久化值，不重读当前配置。

### 核心业务逻辑（详见 business-logic.md）

- Agent 四种运行模式：增量（水位线之后）、定向（`resource_ids`，绕过水位线）、回填提交（rules-preview 后保存，推进水位线到频道 max）、指定起始时间（手动 run 带 `scan_since`，按入库时间过滤，null=全量；只影响本次扫描范围，水位线照常推进）。旧的 `limit(200)` 已废弃。
- Metadata 匹配四层：已链接 → ChannelRawTitleMapping（`search_title_key` 优先，`raw_title` fallback）→ 本地 DB 精确/模糊（fuzzy ≥ 85 自动链接）→ 统一 MetadataAgent。
- MetadataAgent 单次搜索**只用一个数据源**（`exa` 默认 / `jina` / `tmdb` / `wikipedia` / `local`），禁止跨源 fallback；`combined` 仅为旧评测遗留，运行时归一化为 `exa`。
- LLM 候选选择器 `_generate_llm_pick` 共用逻辑：`auto` 自动选择、`ask` 建议、`ai-pick` 三处复用；失败时 `auto` 回退启发式评分。
- 调度：APScheduler 按频道 `fetch_interval` 定时抓取；全局每分钟同步下载进度、每小时检查下载器连通、每日清理过期任务/决策 + 04:00 metadata 去重。

### 前端（详见 frontend.md）

- 路由：`/` Dashboard、`/works`、`/channels*`、`/agents*`、`/downloaders*`、`/series*`、`/movies*`。
- Agent 保存前必须走 `/agents/rules-preview` + BackfillPreviewModal 回填流程。

### 错误处理（详见 error-handling.md）

- 错误码：`NOT_FOUND`(404)、`VALIDATION_ERROR`(422)、`INVALID_FEED`(422)、`DUPLICATE_SUBMISSION`(409)、`ALREADY_RUNNING`(409)、`TRANSMISSION_ERROR`(502)、`LLM_ERROR`(502)、`INTERNAL_SERVER_ERROR`(500，dev_mode 带 stack)。
- 未捕获异常统一转 `INTERNAL_SERVER_ERROR`；SSE 端点错误发 `event: error`。

### 其他约定（详见 conventions.md）

- API 时间一律 ISO 8601 UTC 字符串。
- 数据库后端二选一：`sqlite+aioturso:///`（默认，嵌入式 Turso，MVCC 并发写；**单进程独占文件锁**，多容器/多实例不得共享同一文件，需共享用 PostgreSQL；旧库迁移走 `scripts/migrate_to_turso.py`）、`postgresql+asyncpg://`。锁/冲突重试设施对两者语义一致。全文检索为 Turso 原生 FTS（ngram），因与 MVCC 互斥而放在边车库 `<主库名>_fts.db`（WAL），可随时重建、启动自动回填；同步 = 写入路径调用点 + 每 5 分钟对账任务兜底。
- `DownloaderInstance.download_dir` 以 Transmission daemon 视角为准（POSIX/Windows/UNC）；`Agent.download_subdir` 必须是相对路径，禁止 `..`/绝对路径/控制字符；不调用 `session_set` 改全局目录。
- 关键环境变量：`DATABASE_URL`、`QUEUE_BACKEND`、`LLM_API_KEY/BASE_URL/MODEL`、`EXA_API_KEY`、`JINA_API_KEY`、`TMDB_API_KEY`、`*_ENABLED` 数据源开关、`POSTER_CACHE_DIR` 等（完整列表见子文档）。

### 分支与 CI（详见 branching.md）

- 遵循 Conventional Branch v1.1.0：`<type>/<description>`，全小写 + 连字符；前缀 `feature|bugfix|hotfix|release|chore|ai|copilot|cursor|claude|codex`。
- CI：开发分支走 ci-fast（lint + 单元/API，覆盖率 ≥80%），`develop`/`release/**` 走 ci-strict（另含集成测试，覆盖率 ≥70%）；推送 `main` 或 `v*` 标签触发 GHCR 双架构镜像发布。
- 本地 pre-commit：`git config core.hooksPath githooks` 启用 `uv run ruff check .` 门禁。
