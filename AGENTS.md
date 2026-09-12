# RSSRipple - 方案设计索引

本文档是 RSSRipple 项目设计的**索引与核心速查**。所有实现必须遵循 `docs/design/` 下的权威子文档；完整的核心约束细节见 [docs/design/constraints.md](docs/design/constraints.md)。本文档只保留文档地图与最高频、最易违反的关键不变量。

## 文档地图

| 文档 | 内容 |
|------|------|
| [docs/README.md](docs/README.md) | 文档总索引（按读者分组：用户 / 贡献者 / Coding Agent / 测试） |
| [docs/design/data-models.md](docs/design/data-models.md) | 全部 ORM 数据模型（字段、约束、关系） |
| [docs/design/per-season-works.md](docs/design/per-season-works.md) | 作品单季化终态设计（**已实施 P2–P7**；P8 生产迁移 runbook 见 db-migration.md） |
| [docs/design/filter-dsl.md](docs/design/filter-dsl.md) | Filter DSL 类型定义、求值语义、示例 |
| [docs/design/api-endpoints.md](docs/design/api-endpoints.md) | 全部 REST API 端点与请求/响应结构 |
| [docs/design/business-logic.md](docs/design/business-logic.md) | 抓取、metadata 匹配、Agent 运行/过滤、调度、下载同步、Mock Downloader |
| [docs/design/notifications.md](docs/design/notifications.md) | 下载完成通知：模型、快照契约、多 webhook 注册/fan-out 投递/退避、聚合状态机、重试、重新生成 |
| [docs/design/file-organization.md](docs/design/file-organization.md) | 内置文件整理（organize）：Library/OrganizeRule/OrganizePlan 两阶段、执行器不变量、存储卷与媒体服务器 |
| [docs/design/frontend.md](docs/design/frontend.md) | 前端路由、页面与关键交互 |
| [docs/design/error-handling.md](docs/design/error-handling.md) | 统一响应结构、错误码、全局异常处理 |
| [docs/design/conventions.md](docs/design/conventions.md) | 时间格式、下载目录规则、环境变量、海报、日志、幂等性 |
| [docs/design/db-migration.md](docs/design/db-migration.md) | 数据库迁移：SQLite→Turso→PostgreSQL 矩阵、脚本用法、Docker 部署迁移 |
| [docs/design/branching.md](docs/design/branching.md) | 分支命名规范、CI/CD 与发布流程 |
| [docs/design/constraints.md](docs/design/constraints.md) | **核心约束完整速查**（从 AGENTS.md 拆出的细节） |
| [ARCHITECTURE.md](ARCHITECTURE.md) | 模块布局与运行时数据流 |
| [DESIGN.md](DESIGN.md) | 前端设计 token 与视觉指引 |
| [docs/sitemap.md](docs/sitemap.md) | 前端路由与页面标签标题 |
| [docs/testing/metadata-corpus.md](docs/testing/metadata-corpus.md) | Metadata 离线验收语料与录制/审核流程 |
| [docs/testing/integration-inventory.md](docs/testing/integration-inventory.md) | 集成测试清单与覆盖率门禁 |
| [docs/testing/organize-integration.md](docs/testing/organize-integration.md) | organize 集成测试与容器级半 E2E |
| [docs/testing/midscene-e2e.md](docs/testing/midscene-e2e.md) | Midscene.js 浏览器 E2E |
| [docs/testing/web-ui-functional-cases.md](docs/testing/web-ui-functional-cases.md) | 手工 UI 功能用例清单 |

修改任何上述领域的实现时，必须同步更新对应子文档。

---

## 核心约束

以下只列最易违反的要点；完整细节见 [docs/design/constraints.md](docs/design/constraints.md)，每条以「详见 …」指向权威子文档。

### 数据模型（详见 data-models.md / per-season-works.md）

- 所有 ORM 模型使用 SQLAlchemy 2.0 风格声明；主键为 UUID v4 字符串；时间字段均为 UTC。
- **作品单季化**：一个 `TVSeries` = 恰好一季（`season_number` NOT NULL DEFAULT 1，0=特典/SP）；每部剧集作品必属一个 `WorkCollection`；`seasons`/`number_of_seasons` 为退役孤儿列，永不写入；`Episode.season` 恒等于所属作品的 `season_number`；季号绝不猜测（不可定则 `season=None` + `ambiguous`，或挂合集待确认）。
- `FileResource` FK 互斥：剧集资源用 `series_id`、电影资源用 `movie_id`、未识别两者皆空；合集资源（`is_batch=true`）`episode` 为空。
- 合集去重按内容覆盖度；覆盖度未知不派发（进所属 Channel 待确认）；`batch_scope ∈ {NULL,season,multi_season,franchise,movies}`；`resource_work_links`/`resource_file_assignments` 为权威关联与文件映射。
- `episode_confidence ∈ {raw,reconciled,ambiguous,manual,None}`；`ambiguous` 不参与派发。
- `Agent.last_consumed_at` 是消费水位线（增量运行只处理其后的资源）；`PendingDecision` 仅用于 ≥2 合格下载候选；`AgentWork` 上限 10；订阅单位是季作品（多季需逐季订阅）。
- `genre` 统一为封闭 TMDB 27 类；`is_anime` 三态（True/False/NULL）；作品人工编辑保护 `manually_edited_fields`（自动扫描/刷新默认跳过）。
- `WorkExternalId` 身份袋（`work_type` 含 `collection`；wikipedia id 带语言版本）；`WorkCollection` 为系列级元数据载体（`aliases`/`search_text`/`manually_edited_fields`）。

### Filter DSL（详见 filter-dsl.md）

- `filter_config`/`filter_overrides` 均为 BoolCondition 根节点的 JSON DSL 树，按 AND 合并；字符串比较忽略大小写。
- 空值语义：`eq/contains/fuzzy/regex/gt/...` 不通过、`ne` 通过；空值匹配必须用 `is_empty`/`is_not_empty`；取值操作符 `value` 禁止为空（保存 422）。
- 频道 `required_metadata_fields` 强制且创建后只增不减（基础五件套 + 形态必填）；`season`/`absolute_episode`/`episode_confidence` 已退役为可选/移出目录。

### API（详见 api-endpoints.md）

- 前缀 `/api/v1`；统一响应结构 `{success,data,error,meta}`；分页 `page`/`page_size`（最大 100）。
- 认证默认开（`AUTH_ENABLED`）：Web 端 TOTP HttpOnly Cookie；程序端 API key（`rr_` 明文仅创建时返回一次；`Authorization: Bearer` 或 `X-API-Key`）。
- `POST /agents` 的 `dispatch_resource_ids`：`null`=普通保存不动水位线；数组（含空）=经 rules-preview 派发选中资源并推进水位线。
- 三个修订端点（`PATCH /resources/{id}/episode`、`PATCH /resources/{id}`、`PUT /resources/{id}/associations`）先 commit 再入队定向运行；`PUT .../associations` 为编辑向导统一提交。

### 核心业务逻辑（详见 business-logic.md）

- Agent 四种运行模式：增量（水位线之后）、定向（`resource_ids`，绕过水位线）、回填提交（rules-preview 后保存）、指定起始时间（`scan_since`）。
- Metadata 匹配四层：已链接 → `ChannelRawTitleMapping` → 本地 DB 精确/模糊 → 统一 MetadataAgent；单季化 upsert 后所有链接路径统一走 `reconcile_linked_series_resource`，绝不猜季。
- 频道源为三数据源架构 `wikipedia | tmdb | bangumi`（默认 `wikipedia`）；三主源未命中走有序网络搜索回退（仅补身份/链接，内容以主源为准）。
- 冲突解决优选链：`Agent.pick_preferences`（只排序不过滤）→ `_generate_llm_pick` → 启发式评分。
- 调度涵盖频道抓取、频道作品刷新、下载进度同步、每日清理与元数据去重、metadata 回填、通知 tick、magnet 解析；内置 organize 是通知流水线的消费者。
- 下载完成通知：payload 为创建时冻结的完整快照（`version: 2`）；多 webhook fan-out；纯出站无回调，消费者清理走任务 API。

### 前端（详见 frontend.md / sitemap.md）

- 关键路由：`/` Dashboard、`/works`（合集走 `?view=collections`）、`/collections/:id`、`/channels*`、`/agents*`、`/downloaders*`、`/series/:id/edit`、`/movies/:id/edit`、`/volumes`、`/media-library`（三 Tab）、`/settings`。
- Agent 保存前必须走 `/agents/rules-preview` 回填流程；编辑向导第二步为簇级指派。

### 错误处理（详见 error-handling.md）

- 错误码：`UNAUTHORIZED`(401)、`NOT_FOUND`(404)、`VALIDATION_ERROR`/`INVALID_FEED`(422)、`DUPLICATE_SUBMISSION`/`ALREADY_RUNNING`/`INVALID_STATE`/`DELETE_BLOCKED`(409)、`TRANSMISSION_ERROR`/`LLM_ERROR`(502)、`INTERNAL_SERVER_ERROR`(500，dev_mode 带 stack)。
- 未捕获异常统一转 `INTERNAL_SERVER_ERROR`；SSE 端点错误发 `event: error`。

### 其他约定（详见 conventions.md）

- API 时间一律 ISO 8601 UTC 字符串。
- 数据库后端二选一：`sqlite+aioturso:///`（嵌入式 Turso，单进程独占文件锁）或 `postgresql+asyncpg://`；默认 `docker-compose.yml` 为分布式（PostgreSQL+Redis，`APP_ROLE` 分 web/worker），Turso 单节点版在 `docker-compose.standalone.yml`。
- `DownloaderInstance.download_dir` 以 Transmission daemon 视角为准；`Agent.download_subdir` 必须是相对路径，禁止 `..`/绝对路径/控制字符。
- 关键环境变量：`DATABASE_URL`、`QUEUE_BACKEND`、`APP_ROLE`、`LLM_API_KEY/BASE_URL/MODEL`、`WIGOLO_BASE_URL/WIGOLO_API_TOKEN`、`TMDB_API_KEY`、`BANGUMI_API_KEY`、`*_ENABLED`、`POSTER_CACHE_DIR`、`AUTH_ENABLED`、`API_KEY`（完整列表见 conventions.md）。

### 分支与 CI（详见 branching.md）

- 遵循 Conventional Branch v1.1.0：`<type>/<description>`，全小写 + 连字符；前缀 `feature|bugfix|hotfix|release|chore|ai|copilot|cursor|claude|codex`。
- CI：开发分支走 ci-fast（lint + 单元/API，覆盖率 ≥95%），`develop`/`release/**` 走 ci-strict（另含集成测试，覆盖率 ≥85%），推送 `main` 或 `v*` 触发 GHCR 双架构镜像发布；生产 Metadata 离线验收见 [docs/testing/metadata-corpus.md](docs/testing/metadata-corpus.md)。
- 本地 pre-commit：`git config core.hooksPath githooks` 启用 `uv run ruff check .` 门禁。
- **本地测试 Compose 隔离（强制）**：dev/生产栈默认项目名为 `rssripple`；本地跑单元/API 或集成测试所用的任何 `docker compose` 必须用 `-p <唯一项目名>`（脚本内部调用则用 `COMPOSE_PROJECT_NAME`）隔离，禁止默认项目名，否则会重建/停止运行中的栈。详见 [CONTRIBUTION.md](CONTRIBUTION.md)「测试」与 [docs/design/branching.md](docs/design/branching.md)。
