# RSSRipple - 方案设计索引

本文档是 RSSRipple 项目设计的**索引与核心约束速查**。详细设计已拆分为 `docs/design/` 下的子文档，均为权威设计来源，所有实现必须遵循：

| 子文档 | 内容 |
|--------|------|
| [docs/design/data-models.md](docs/design/data-models.md) | 全部 ORM 数据模型（字段、约束、关系） |
| [docs/design/filter-dsl.md](docs/design/filter-dsl.md) | Filter DSL 类型定义、求值语义、示例 |
| [docs/design/api-endpoints.md](docs/design/api-endpoints.md) | 全部 REST API 端点与请求/响应结构 |
| [docs/design/business-logic.md](docs/design/business-logic.md) | 抓取、metadata 匹配、Agent 运行/过滤、调度、下载同步、Mock Downloader |
| [docs/design/notifications.md](docs/design/notifications.md) | 下载完成通知：模型、快照契约、多 webhook 注册/fan-out 投递/退避、聚合状态机、重试、下游清理 API、重新生成、mock webhook |
| [docs/design/file-organization.md](docs/design/file-organization.md) | 内置文件整理（organize）：Library/OrganizeRule（DSL 路由+命名模板）、OrganizePlan 两阶段计划→执行、执行器不变量、逻辑存储卷（StorageVolume）与下载器卷绑定、媒体服务器实例（MediaServerInstance）扫描派生 Library 与刷新、共享卷部署 |
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
- 作品 `genre` 统一为封闭 TMDB 27 类英文 canonical 名（权威清单 `app/services/genre_registry.py`，`GenreName` Literal/前端常量手同步）：LLM prompt 注入枚举 + 尽力推测指令（有简介至少给一个）+ `_ensure_genre` 简介兜底 + `normalize_genres` 出口钳制（表外值丢弃、空=未提供不清空旧值）；Create/Update 表外值 422；存量走 `scripts/genre_backfill.py`；缓存代际 4。
- 作品 `is_anime` 三态动漫标记（True=日本动画 / False=确认实拍 / NULL=未判定），正交于 `content_type`（媒介 tv/movie，剧场版动画两者独立）；权威模块 `app/services/anime_signals.py`：确定性证据优先——①身份源/身份袋命中 bangumi/mal/anilist 必为 True ②Wikipedia `{{Infobox animanga/TVAnime}}` 块（`_attach_wikipedia_content` 处标记）③TMDB Animation(16)+日语/JP → True、genre 存在无 Animation → False、非日语 Animation → None ④LLM judge/ReAct `is_anime` 兜底；赋值走 `apply_is_anime`（series/movie upsert 四处分支）：True sticky 不降级、False 只填 NULL、身份证据直接覆盖；存量回填 `scripts/anime_backfill.py`（dry-run 默认 + `--apply`，离线身份阶段 + 可选 `--tmdb`/`--wikipedia` 联网阶段），轻迁移在 `_apply_light_migrations`（tv_series/movies 各一行可空 BOOLEAN）。
- 作品**人工编辑保护**：`TVSeries`/`Movie` 新增 `manually_edited_fields`（JSON 字段名列表，轻迁移加列）。作品详情页提供统一「编辑」入口（编辑表单，非单字段选项），可编辑 `MANUAL_EDITABLE_FIELDS`（标题/动漫判定/简介/评分/分类/状态/季集/日期等），数据源等系统托管字段（external_id/external_source/wikipedia_*/seasons/collection_id/content_type 等）不可编辑；PUT 时按显式发送的字段记录到 `manually_edited_fields`。自动扫描（upsert `create_or_update_*_from_external`、`apply_is_anime`、频道默认标记、bangumi 验证）**跳过** `manually_edited_fields` 中的字段；`refresh_work_metadata` 默认同样跳过，仅当作品模块「刷新元数据」对话框勾选「覆盖所有人工编辑字段」（`POST /works/refresh-metadata` 的 `override_manual_edits=true`）时才覆盖。
- `WorkCollection` 大 IP 合集分组（组织层而非消歧核心）：作品经可空 `collection_id` 至多属一个合集；TMDB 链接走确定性 `link_movie_collection`（`tmdb_collection` 源 + 原始数字 id，禁止 `canonicalize_external_id`）；DSL 新增 `series.collection`/`movie.collection`（显示名），所有过滤求值点须链式 selectinload 作品的 `collection` 关系。
- `WorkExternalId` 身份袋（P3）：一个作品可携带多个 `source:id`（wikipedia pageid、langlinks 各语言页 pageid、Exa 回退 id 等），袋反查是 upsert 第一查找步；主 id 规则 creator-wins——`external_id/external_source` 列保持创建时值，后来的 id 只入袋绝不抢占；`UniqueConstraint(source, external_id)` 一 id 至多一作品，冲突不抢（记 warning，成去重候选）；去重合并对袋取并集。
- `DownloadNotification` 下载完成通知：队列从属于 Agent（`agent_id`，每 Agent 单例 FIFO）；`download_task_id` 唯一（一条任务至多一条通知，补建幂等的基础）；**并发创建安全**：调度 tick 与手动"重新生成"可能竞争同一任务，插入走 SAVEPOINT，输掉唯一约束竞争时回读已存在行（不掉整个批次）；payload 是创建时冻结的完整快照，重建只能走**重新生成**（`POST /agents/{id}/notifications/regenerate`：对该 Agent 的 completed 任务重跑完整生成链路——无通知的补建、已有的原地重建 payload（保留行与 `notification_id`）并复位其非 pending delivery 重投；当次拿不到 torrent 文件清单则保留旧快照不降级）；本表**只保留快照锚点**（投递列已从 ORM 移除，存量库走重建/DROP NOT NULL 轻迁移）。投递走**多 webhook fan-out**：`agent_webhooks` 表按 Agent 注册任意多个 webhook（url/mock/enabled；旧 `agents.notify_webhook_*` 三列成孤儿，启动轻迁移复制到 agent_webhooks），每条通知对每启用 webhook 生成一条 `webhook_deliveries`（`(notification_id, webhook_id)` 唯一，独立状态/退避）；纯出站——POST `{"event":"download.completed","notification":<payload>}`，**180s 超时**，2xx 即 done，**无 token、无消费者回调**（start/ack/fail 已删除）。通知展示状态由 delivery 聚合：无 delivery 或有 pending → pending，全 done → done，否则 failed。RSSRipple 语义到通知为止；文件整理由**内置 organize 子系统**（docs/design/file-organization.md：Library/OrganizeRule/OrganizePlan 两阶段 + `/libraries`、`/organize-rules`、`/organize/plans`、`/organize/audit` API）与外部 webhook 消费者（vault-organizer 等）并存，两者消费同一份快照；消费者清理走任务 API（`GET /tasks`、`POST /tasks/{id}/pause`、`DELETE /tasks/{id}`——删除实现与 organize 执行后清理共用 `app/services/task_cleanup.py`）。

### Filter DSL（详见 filter-dsl.md）

- `filter_config` / `filter_overrides` 均为 BoolCondition 根节点的 JSON DSL 树；字符串比较忽略大小写；`filter_overrides` 与全局 `filter_config` 按 AND 合并。
- 空值语义：`eq/contains/fuzzy/regex/gt/...` 不通过；`ne` 通过。空值匹配必须用 `is_empty`/`is_not_empty`（所有字段类型可用、不需要 value）；取值操作符的 `value` 禁止为空，保存时 422（`eq ""` 会静默过滤掉全部资源）。
- bool 字段新增 `series.is_anime` / `movie.is_anime`（eq/ne + is_empty/is_not_empty，三态取值见数据模型）；bool 空值语义已修正为与上述规则一致：NULL 时 `eq` 不通过、`ne` 通过（此前 NULL 被当 False；`is_batch` 为 NOT NULL 不受影响），区分「确认非动漫」与「未判定」必须用 `is_empty`/`is_not_empty`。
- 作品类型字段 `content_type`（枚举字符串，UI 限 `tv/movie/audio`）：值由资源互斥作品 FK 派生（series_id→tv、movie_id→movie、audio_work_id→audio，三者皆空→空值/未识别）；只读 FK id，无需 eager-load 作品关系。前端字段下拉按**语义分组**（发布信息/集数信息/标题信息/作品类型/剧集作品/电影作品），`series.collection`/`movie.collection` 亦纳入对应作品分组。

### API（详见 api-endpoints.md）

- 前缀 `/api/v1`，统一响应结构 `{ success, data, error, meta }`；分页参数 `page`/`page_size`（最大 100）。
- **认证**（`AUTH_ENABLED` 默认开）：`/api/v1/*` 与 `/posters/*` 需凭证（AuthMiddleware），`/api/v1/auth/*` 与 SPA/静态资源开放；无凭证 401 `UNAUTHORIZED`。两车道：Web 端 TOTP 登录（`POST /auth/otp` → HttpOnly Cookie `rssripple_auth`，30 天）；程序端全局 API key（`api_keys` 表，仅存 SHA-256 摘要，`rr_` 明文仅创建时返回一次；`Authorization: Bearer` 或 `X-API-Key`；环境变量 `API_KEY` 为可选静态引导 key）。
- `POST /agents` 的 `dispatch_resource_ids`：`null`=普通保存不动水位线；数组（含空 `[]`）= 经过 rules-preview，派发选中资源并推进水位线到频道 max `created_at`。
- `PATCH /resources/{id}/episode` 修正集号：未显式发送 `season` 且有 absolute 集号 + 剧集逐季数据时服务端推导 season（显式值优先）；**先 commit 再入队**定向运行（绕过且不推进水位线）。
- 任务重试必须使用 `DownloadTask.download_dir` 持久化值，不重读当前配置。

### 核心业务逻辑（详见 business-logic.md）

- Agent 四种运行模式：增量（水位线之后）、定向（`resource_ids`，绕过水位线）、回填提交（rules-preview 后保存，推进水位线到频道 max）、指定起始时间（手动 run 带 `scan_since`，按入库时间过滤，null=全量；只影响本次扫描范围，水位线照常推进）。旧的 `limit(200)` 已废弃。
- Metadata 匹配四层：已链接 → ChannelRawTitleMapping（`search_title_key` 优先，`raw_title` fallback）→ 本地 DB 精确/模糊（fuzzy ≥ 85 自动链接）→ 统一 MetadataAgent。
- MetadataAgent 单次搜索**只用一个数据源**；频道源为三数据源架构 `wikipedia | tmdb | bangumi`（默认 `wikipedia`；exa/jina/local/combined 已废弃为频道源，频道解析归一为 `wikipedia`，存量由轻迁移改写；其 ReAct 路径仅手动搜索/评测保留）。三主源未命中（judge/ReAct found=False；transient 不触发）统一走**有序 Exa 回退**：频道 `metadata_fallback_sources`（JSON 白名单，NULL=默认顺序 bangumi→mal→anilist→tmdb→wikipedia→imdb→douban，`[]`=禁用）硬过滤候选、靠前站点优先，**仅补身份/链接**（剥离 seasons/集数，内容以主源为准）。身份体系为 7 站注册表 `metadata_source_registry`（wikipedia/tmdb/bangumi/mal/anilist/imdb/douban；baidu_baike/eiga 已移除）。
- `bangumi` 频道源（`metadata_bangumi`，镜像 wikipedia search-then-judge）：query 清洗复用 → 搜索限 `type=[2]` 动画分类（命中即 `is_anime=True`，身份 `bangumi:{id}`）→ 归一化标题相等 + 年份守卫(±1) 唯一命中免 LLM auto-link → 否则单次 judge → 详情+剧集端点展开 matched_entity；**刻意不设 `seasons`/`number_of_seasons`**（一个 bangumi 条目只是一季，季号绝不猜测不变量）；缓存命名空间 `metadata_agent:bangumi`；token 即启用（`BANGUMI_API_KEY`/设置页），无独立开关；found=False 走统一 Exa 回退。
- is_anime 分层判定：频道 `default_is_anime`（NOT NULL DEFAULT FALSE 轻迁移；**创建后不可改**，PUT 不同值 422）；资源链接作品的全部 5 处落点统一调 `classify_is_anime_post_link`：默认标记先置 True → 未开默认的频道走第一层 Bangumi 验证（`maybe_verify_is_anime_via_bangumi` + `bangumi_verdict`，仅 NULL 作品、不带 type 过滤：type 2 → True、type 6 三次元 → False）→ 上下文推断（Wikipedia TVAnime/Movie|Film|OVA infobox、TMDB、LLM）→ 最终 NULL 待作品详情页编辑表单手动修正。
- LLM 候选选择器 `_generate_llm_pick` 共用逻辑：`auto` 自动选择、`ask` 建议、`ai-pick` 三处复用；失败时 `auto` 回退启发式评分。
- Wikipedia 季/集内容提取（P2）：页面选中后（auto-link 与 judge 两路径）取 wikitext 走**确定性解析**（`wikipedia_episode_parser`，无 LLM；zh `{{劇集列表/base}}` + ja `{{エピソードリスト/base}}`，infobox **仅取 `{{Infobox animanga/TVAnime}}` 块**的 話數/集數，Novel/Manga 块诱饵一律拒绝、无 TV 块返回 None + `各話列表` 章节），合并 `seasons`/`number_of_seasons`/`number_of_episodes`/`episode_list` 进 matched_entity（随 MetadataCache 往返）；wikipedia 来源 `seasons` **覆盖** series 字段但带**防退化 guard**（`seasons_overwrite_allowed`：现有为空或解析季数 ≥ 现有才覆盖，更少则拒绝，service 与 `--apply` 回填共用），`upsert_episodes` 按 `(series_id, season, episode)` 幂等落 Episode 行（只增不删）。LLM judge schema 不变、不猜季数；Exa 回退仍仅补身份。TMDB 对称填充（P4）：tmdb ReAct finalize 后 `_attach_tmdb_episode_list` 用 `fetch_tmdb_episode_list`（逐季 `GET /tv/{id}/season/{n}`，并发 4、跳 season 0、单季失败容忍、>30 季跳过）填 `episode_list` 复用同一落库路径，仅对 tmdb 身份实体触发；存量回填走 `scripts/wikipedia_seasons_eval.py --apply`（wikipedia，覆盖 seasons）与 `scripts/tmdb_episodes_backfill.py --apply`（tmdb），均 dry-run 默认 + 批量提交。
- 调度：APScheduler 按频道 `fetch_interval` 定时抓取；全局每分钟同步下载进度、每小时检查下载器连通、每日清理过期任务/决策 + 04:00 metadata 去重；每分钟处理下载通知（tick 顺序：为 completed 任务停种 + 补建通知 → **organize 规划步**（常开无开关，存在 enabled 规则即激活，消费本 tick 新建/重建的通知，异常不中断 tick）→ `ensure_deliveries` fan-out → `deliver_due_deliveries` 并发投递到期 delivery，指数退避；`NOTIFY_ENABLED` 默认开，仅作熔断开关）。
- 内置文件整理 organize（详见 file-organization.md）：notification 流水线的**内置消费者**，与外部 webhook 消费者并列消费同一份冻结快照。`OrganizeRule` 全局有序 first-match-wins（filter 复用 Filter DSL 求值，null=匹配全部；vault-organizer 的 is_anime/content_type/genre 硬分流全部改由 DSL 表达）；`OrganizePlan.notification_id` 唯一（幂等键，regenerate 时 pending/failed 重建且保留人工 library/category，done/running 短路）；两阶段计划→执行不合并（auto_execute 只是省人工点击）；执行器不变量逐条保留：前置门禁（磁盘 vs 快照 size）、幂等状态表、冲突绝不覆盖、绝不扫共享下载根（只扫 `download_dir/torrent_name`）、`os.rmdir` 自底向上保留下载根、合集缺集拒绝整理；`DownloaderInstance` 卷绑定（`volume_id`+`volume_subpath` → `StorageVolume.mount_path`，两者皆 null=恒等；解析走 `app/services/volume_service.resolve_downloader_path`，P1 `path_map` 列已废弃为惰性孤儿）；Library 由媒体服务器**扫描派生**（R2：`MediaServerInstance`+`MediaServerBinding` 最长前缀匹配解析 `(volume_id, root_subpath)`，未命中=待绑定计划 pending_reason=unbound、执行门禁 409；`root_path`/`plex_section` 列废弃为惰性孤儿，库根使用处动态解析 `resolve_library_root`；全局 `PLEX_*` 配置已删，存量环境变量启动轻迁移为一条 Plex 实例）；执行后清理按 `file_op` 分流（R3 起 `move|hardlink|copy` 三值放开）：`move` 走 `app/services/task_cleanup.py` 删任务（与 `DELETE /tasks/{id}` 共用）；`hardlink`/`copy` 保种——不删任务、不清源目录、恢复快照时停过的做种（`resume_torrent` RPC；hardlink 跨设备/EPERM failed **不静默退化为 copy**；两者跳过空目录清理与"src 消失"后置校验）；媒体服务器刷新 best-effort 三态一致（Library→MediaServerInstance→adapter，Plex 优先 `?path=` partial 失败退整库）。
- 下载完成通知（详见 notifications.md）：completed → 停种 + 建通知（**仅当 Agent 有启用 webhook**，否则不生成）→ fan-out 到每启用 webhook 各一条 delivery → webhook 投递（180s 超时，退避超限该 delivery 转 `failed`，界面重试可重置 failed 或全部非 pending）；纯出站无回调，消费者清理走任务 API。通知（含 delivery）超过 `NOTIFY_RETENTION_DAYS`（默认 30 天）删除；`_cleanup_expired` 跳过有任一非 `done` delivery 的任务。投递只走每分钟循环这一条路径（新建/退避/手动重试/重新生成统一；webhook 注册/更新 API 额外触发一次即时 fan-out）。

### 前端（详见 frontend.md）

- 路由：`/login`（登录页，独立于侧边栏布局；API client 401 自动跳转）、`/` Dashboard、`/works`（合集列表作为浏览模式整合进作品仓库：`?view=collections`）、`/collections/:id`（合集详情页，无独立列表路由）、`/channels*`、`/agents*`、`/downloaders*`、`/series*`、`/movies*`、`/volumes`（存储卷）、`/media-library`（「媒体库管理」统一模块，合并原 `/media-servers` 与 `/organize`，三 Tab：变更计划 / 操作审计 / 媒体服务器配置；变更计划状态用下拉筛选默认 pending；媒体服务器配置为服务器+派生媒体库分组表格，库行「设置」打开媒体库设置 Drawer（媒体库规则 + 其他设置两 Tab）；`/media-servers`、`/organize` 重定向至此）、`/settings`（含 API Keys 卡片）。
- Agent 保存前必须走 `/agents/rules-preview` + BackfillPreviewModal 回填流程。

### 错误处理（详见 error-handling.md）

- 错误码：`UNAUTHORIZED`(401)、`NOT_FOUND`(404)、`VALIDATION_ERROR`(422)、`INVALID_FEED`(422)、`DUPLICATE_SUBMISSION`(409)、`ALREADY_RUNNING`(409)、`INVALID_STATE`(409)、`DELETE_BLOCKED`(409)、`TRANSMISSION_ERROR`(502)、`LLM_ERROR`(502)、`INTERNAL_SERVER_ERROR`(500，dev_mode 带 stack)。
- 未捕获异常统一转 `INTERNAL_SERVER_ERROR`；SSE 端点错误发 `event: error`。

### 其他约定（详见 conventions.md）

- API 时间一律 ISO 8601 UTC 字符串。
- 数据库后端二选一：`sqlite+aioturso:///`（默认，嵌入式 Turso，MVCC 并发写；**单进程独占文件锁**，多容器/多实例不得共享同一文件，需共享用 PostgreSQL；旧库迁移走 `scripts/migrate_to_turso.py`）、`postgresql+asyncpg://`。锁/冲突重试设施对两者语义一致。全文检索双后端统一基于 `search_text`（作品表上 `normalize_title` 归一化标题拼接列，由 ORM `before_flush` 钩子同事务维护，启动空值回填）：**Turso** 走原生 FTS（ngram）边车库 `<主库名>_fts.db`（WAL，可随时重建、启动自动回填），同步 = `fts_outbox` 表（ORM 钩子同事务入队 upsert/delete）+ 每 30 秒 drain 任务定向投递 + 每小时全量对账兜底（脚本/直连 SQL）；**PostgreSQL** 无 sidecar，直接 `search_text` + `pg_trgm` GIN 索引做子串匹配（需 `CREATE EXTENSION pg_trgm` 权限，无权限回退 Python 扫描）。ngram 单字查询不命中，`search_*_fts` 对归一化后 <2 字符的查询回退 Python 扫描。
- `DownloaderInstance.download_dir` 以 Transmission daemon 视角为准（POSIX/Windows/UNC）；`Agent.download_subdir` 必须是相对路径，禁止 `..`/绝对路径/控制字符；不调用 `session_set` 改全局目录。
- 关键环境变量：`DATABASE_URL`、`QUEUE_BACKEND`、`LLM_API_KEY/BASE_URL/MODEL`、`EXA_API_KEY`、`JINA_API_KEY`、`TMDB_API_KEY`、`BANGUMI_API_KEY`、`*_ENABLED` 数据源开关、`POSTER_CACHE_DIR`、`AUTH_ENABLED`（认证总开关，默认开）、`API_KEY`（可选静态引导 key）等（完整列表见子文档；`PLEX_URL`/`PLEX_TOKEN` 已移除，媒体服务器配置入库；内置整理常开，无 `ORGANIZE_ENABLED`）。

### 分支与 CI（详见 branching.md）

- 遵循 Conventional Branch v1.1.0：`<type>/<description>`，全小写 + 连字符；前缀 `feature|bugfix|hotfix|release|chore|ai|copilot|cursor|claude|codex`。
- CI：开发分支走 ci-fast（lint + 单元/API，覆盖率 ≥80%），`develop`/`release/**` 走 ci-strict（另含集成测试，覆盖率 ≥75%）；推送 `main` 或 `v*` 标签触发 GHCR 双架构镜像发布。
- 本地 pre-commit：`git config core.hooksPath githooks` 启用 `uv run ruff check .` 门禁。
