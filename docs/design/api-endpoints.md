# API 端点设计

所有端点前缀 `/api/v1`，请求/响应均为 JSON。统一响应结构：

```json
{
  "success": true,
  "data": { ... },
  "error": null,
  "meta": { "page": 1, "page_size": 20, "total": 100 }
}
```

分页端点使用查询参数 `page`（默认 1）和 `page_size`（默认 20，最大 100），在 `meta` 中返回分页信息。非分页端点 `meta` 可省略或返回空对象。

### Dashboard

| Method | Path | 说明 |
|--------|------|------|
| GET | `/dashboard` | 概览数据：活跃 Agent 数、活跃下载（按 TVSeries/Movie 分组，无 metadata 的归入"未识别"组；下载器中正在下载但无对应 DownloadTask 的种子归入"未跟踪"组）、前 10 条 pending_decisions |

`GET /dashboard` 响应 `data` 结构：
```json
{
  "active_agents": 3,
  "active_channels": 2,
  "active_download_count": 12,
  "active_download_groups": [
    {
      "type": "series" | "movie" | "unknown" | "untracked",
      "id": "uuid-or-null",
      "title": "作品名或未识别",
      "poster_url": "/posters/xxx.jpg",
      "tasks": [ { "task_id": "...", "resource_title": "...", "progress": 0.5, "agent_id": "...", "agent_name": "...", "channel_id": "...", "channel_name": "..." } ]
    }
  ],
  "pending_decisions": [ { ... } ]
}
```

`untracked` 组：对每个下载器调用 `list_torrents`，筛选 `status ∈ {downloading, download pending}` 且 `is_finished=false` 且 torrent id 不属于任何非终态（pending/queued/downloading/paused）DownloadTask 的种子；下载器不可达时跳过（不影响整体响应）。其 task 条目 `task_id` 为合成值（`untracked-{downloader_id}-{torrent_id}`），`agent_*`/`channel_*` 为 null，附带 `downloader_id`/`downloader_name`；计入 `active_download_count`。

### Channels

| Method | Path | 说明 |
|--------|------|------|
| GET | `/channels` | 频道列表（分页） |
| POST | `/channels` | 创建频道（服务端校验 RSS URL 可达与格式合法性） |
| GET | `/channels/form-token` | 获取表单防重复提交 Token（一次有效，存服务端 Cache） |
| GET | `/channels/metadata-sources` | 频道表单数据源目录（两数据源架构：仅 wikipedia/tmdb + 可用性标志 + 默认值） |
| GET | `/channels/{id}` | 频道详情（含最近 20 条 FileResource 预览） |
| PUT | `/channels/{id}` | 更新频道（含 field_mapping/metadata_agent_enabled 等所有字段，一次性保存） |
| DELETE | `/channels/{id}` | 删除频道（级联删除其 file_resources、agents、tasks、mappings） |
| POST | `/channels/{id}/fetch` | 手动触发抓取（入队，返回 task_id） |
| GET | `/channels/{id}/fetch-status` | 轮询抓取任务状态（running/success/failed + 进度信息） |
| POST | `/channels/{id}/analyze` | 非流式 LLM 分析 RSS，返回 field_mapping（阻塞等待直到完成或超时） |
| POST | `/channels/{id}/analyze-stream` | SSE 流式 LLM 分析（delta/done/error 事件） |
| POST | `/channels/{id}/summarize-filters` | 给定若干资源 ID，按 Agent 规则结构生成建议：作品订阅 + 全局共性条件 + 按作品差异化条件 |
| POST | `/channels/validate-url` | 验证 RSS URL 可达性与格式（创建前校验） |
| POST | `/channels/preview-feed` | 预览 RSS 源，可选附带 field_mapping 预览解析结果（不落库） |
| POST | `/channels/analyze-url-stream` | 基于 URL 的 SSE 流式分析（创建频道前使用，无需 channel_id） |

频道创建/更新的元数据字段：`metadata_source` 仅接受 `wikipedia | tmdb`（其他值 422）；`metadata_fallback_sources` 为 Exa 回退的有序站点白名单（JSON 数组，元素必须是注册表站点名 wikipedia/tmdb/bangumi/mal/anilist/imdb/douban，未知值 422；`null`=默认顺序，`[]`=禁用回退）。

`POST /channels/{id}/summarize-filters` 请求体：`{ "resource_ids": ["...", "..."] }`；响应 `data`：

```json
{
  "works": [
    {
      "content_type": "tv",
      "series_id": "...", "movie_id": null,
      "title": "...", "poster_url": "/posters/x.jpg",
      "resource_count": 3,
      "filter_overrides": { "...BoolCondition 或 null..." },
      "override_explanation": "subtitle_group=ANi"
    }
  ],
  "global_filter_config": { "...BoolCondition 或 null..." },
  "unlinked_count": 1,
  "explanation": "resolution=1080p; unlinked=1"
}
```

规则拆分逻辑：字段（subtitle_group/resolution/video_codec/audio_codec/container/subtitle_type/source）在**全部选中资源**中 ≥80% 同值 → 全局条件；在**单个作品**内 ≥80% 同值但不满足全局 → 该作品的 `filter_overrides`。未链接作品的资源计入 `unlinked_count`，不产生订阅。

### Agents

| Method | Path | 说明 |
|--------|------|------|
| GET | `/agents` | Agent 列表（分页） |
| POST | `/agents` | 创建 Agent，body 含 `filter_config`、`works`（AgentWork 列表，最多 10 个）、可选 `llm_prompt`、可选 `dispatch_resource_ids`（规则预览回填提交） |
| GET | `/agents/{id}` | Agent 详情（含 works、统计信息；TV 类型 works 附带 `latest_completed_season/episode`——该剧集全库范围内最新已完成下载的季/集号，无完成记录或为电影时为 null） |
| PUT | `/agents/{id}` | 更新 Agent（整体替换，含 works 列表） |
| DELETE | `/agents/{id}` | 删除 Agent（级联删除其 works、pending_decisions、runs；tasks 标记 cancelled） |
| POST | `/agents/{id}/run` | 手动触发处理（入队处理该 Agent 频道下未处理资源）。可选 body `{"scan_since": "<ISO 8601>" \| null}`：指定本次运行的扫描起始时间（按资源入库时间，必须为过去时间，否则 422）；显式 `null` = 不限制（全量历史）；不带 body = 普通增量运行 |
| GET | `/agents/{id}/run-status` | 轮询处理状态 |
| POST | `/agents/{id}/test-filters` | 给定资源或全部资源测试 filter_config 匹配情况，返回匹配结果明细 |
| GET | `/agents/{id}/suggestions` | 读取持久化的未识别资源建议分组，供用户一键添加为订阅作品 |
| GET | `/agents/{id}/runs` | 运行历史（分页）：每次 run 一条记录，含计数、状态、匹配资源 ID 列表、`scan_since`（指定起始时间运行的扫描下界；null=增量/定向，`1970-01-01`=全量）；`non_empty=true` 时仅返回"非空"运行（dispatched>0 或 pending_decisions>0 或 status 为 running/failed），隐藏无产出的例行空跑 |
| POST | `/agents/rules-preview` | 提交拟变更的订阅规则，预览匹配差异（新增匹配 / 不再匹配 / 已在队列跳过），供用户选择回填资源 |

`POST /agents` 请求体示例：
```json
{
  "name": "新番自动下载",
  "channel_id": "...",
  "downloader_id": "...",
  "download_subdir": "Anime/新番",
  "scope_channel_wide": false,
  "conflict_resolution": "auto",
  "llm_enabled": true,
  "llm_prompt": "优先选择内封简繁日字幕的 2160p HEVC 资源",
  "filter_config": { "combinator": "and", "conditions": [ { "field": "resolution", "operator": "in", "value": ["1080p","2160p"] } ] },
  "works": [
    { "content_type": "tv", "series_id": "...", "enable_episode_dedup": true, "filter_overrides": null }
  ],
  "dispatch_resource_ids": null
}
```

`dispatch_resource_ids` 语义：
- `null`（默认）：普通保存，不回填、不改动水位线（用于非规则编辑）。
- 数组（可为空 `[]`）：表示本次保存经过 rules-preview 流程。后端会派发数组中选中的资源，并将 Agent 的 `last_consumed_at` 推进到频道当前最大 `created_at`，使后续增量运行只看到真正的新资源。空数组 = 不回填任何资源但推进水位线。

`POST /agents/{id}/test-filters` 请求体：`{ "resource_ids": ["..."]? }`（不传则测试最近 50 条资源）；响应返回每条资源是否通过及每个条件的命中情况。

`POST /agents/rules-preview` 请求体：
```json
{
  "agent_id": "uuid | null",      // 已有 Agent 时传，old rules 从 DB 读取；新建时不传
  "channel_id": "uuid | null",    // agent_id 缺省时必填，old rules 视为空（全部为新增匹配）
  "scope_channel_wide": false,
  "filter_config": { "...BoolCondition..." },
  "works": [ { "content_type": "tv", "series_id": "...", ... } ]
}
```
响应 `data`：
```json
{
  "newly_matching": [ { "...RulesPreviewResource..." } ],     // 新增匹配且无活跃 DownloadTask → 可选回填
  "no_longer_matching": [ { "...RulesPreviewResource..." } ], // 不再匹配（仅展示，不撤销已入队任务）
  "in_queue_skipped": 0                                        // 新增匹配但已有活跃任务的跳过数
}
```

### Agent Works（子资源）

独立 CRUD，用于在 Agent 详情页管理订阅作品。

| Method | Path | 说明 |
|--------|------|------|
| GET | `/agents/{agent_id}/works` | 列出 Agent 所有订阅作品 |
| POST | `/agents/{agent_id}/works` | 新增订阅作品 |
| PUT | `/agents/{agent_id}/works/{id}` | 更新作品的覆盖设置（去重开关、filter_overrides 等） |
| DELETE | `/agents/{agent_id}/works/{id}` | 移除订阅作品 |

`POST /agents/{agent_id}/works` 请求体：
```json
{
  "content_type": "tv",
  "series_id": "uuid",       // tv 时必填
  "movie_id": null,          // movie 时必填
  "enable_episode_dedup": true,
  "filter_overrides": null
}
```

### Downloaders

| Method | Path | 说明 |
|--------|------|------|
| GET | `/downloaders` | 下载器列表 |
| POST | `/downloaders` | 创建下载器 |
| GET | `/downloaders/{id}` | 下载器详情 |
| PUT | `/downloaders/{id}` | 更新下载器 |
| DELETE | `/downloaders/{id}` | 删除下载器；若仍有关联 Agent 返回 `409 CONFLICT` 并在 `error.details.agents` 中带出 `[{id, name}]` 列表，UI 可据此指引用户先解绑/删除这些 Agent |
| POST | `/downloaders/{id}/test` | 测试 Transmission RPC 连通性，并用 `free_space(download_dir)` 检查默认下载目录。请求体可选 `{url, username, password, download_dir}`：用于编辑表单按未保存的表单值探测（缺省字段回退到已存值，空密码 = 沿用已存密码）；带覆盖值的探测不更新 status，仅探测已存配置时才更新 status |
| GET | `/downloaders/{id}/tasks` | 本地 DownloadTask 分页列表 |
| GET | `/downloaders/{id}/torrents` | Transmission 实时种子列表（直连 RPC 返回） |

`POST /downloaders` 请求体示例：
```json
{
  "name": "家用 NAS Transmission",
  "type": "transmission",
  "url": "http://127.0.0.1:9091/transmission/rpc",
  "username": "user",
  "password": "pass",
  "download_dir": "/volume1/downloads/rssripple"
}
```

下载目录规则：
- `download_dir` 必填，必须是 Transmission 下载服务器视角的绝对路径；支持 POSIX（`/volume1/downloads`）、Windows drive path（`D:\Downloads`）和 daemon 支持的 UNC path。
- RSSRipple 后端可能无法访问该目录，因此路径语义以 Transmission daemon 为准。
- 创建/编辑时做路径格式校验；`POST /downloaders/{id}/test` 必须调用 Transmission `free_space(download_dir)`，返回目录可识别性与剩余空间。
- 真实写入能力、子目录存在性、磁盘不足等仍以 `torrent_add(download_dir=...)` 的结果为最终准据。
- 若多个 Agent 共用一个 Downloader，建议通过 Agent 的 `download_subdir` 分流目录。

### Download Tasks

| Method | Path | 说明 |
|--------|------|------|
| POST | `/tasks` | 手动创建下载任务（绕过 Agent，见下文） |
| GET | `/agents/{agent_id}/tasks` | Agent 的下载任务（分页，可按 status 过滤） |
| GET | `/tasks/{id}` | 任务详情（含 file_resource、agent、channel 信息） |
| POST | `/tasks/{id}/pause` | 暂停（调用 Transmission RPC） |
| POST | `/tasks/{id}/resume` | 恢复 |
| POST | `/tasks/{id}/retry` | 重试（重置 retry_count，重新添加 torrent） |
| POST | `/agents/{agent_id}/tasks/batch-retry` | 批量重试：对 Agent 下多个 error/paused 任务统一执行重试 |
| DELETE | `/tasks/{id}` | 删除任务；query 参数 `delete_data=false` 控制是否同时删除 Transmission 中已下载数据 |

`POST /tasks` 手动创建下载任务：

- 请求体：`{ "resource_id": "uuid", "downloader_id": "uuid" }`。
- 成功返回 **201**，`data` 为 `DownloadTaskResponse`；提交下载器成功时 `status="downloading"` 且带 `transmission_torrent_id`/`confirmed_at`，失败时任务仍创建但 `status="error"` 并带 `error_message`。
- 错误码：`NOT_FOUND`（404，resource 或 downloader 不存在）、`VALIDATION_ERROR`（422，请求体缺字段）。
- 手动任务的 `agent_id` 固定为 `null`；`download_dir` 直接使用 Downloader 根目录（不加子目录）；不做去重检查（手动创建是显式用户意图，允许重复）。

任务重试规则：`POST /tasks/{id}/retry` 必须优先使用该任务已持久化的 `download_dir` 重新添加 torrent，而不是重新读取当前 Agent/Downloader 配置；这样 Downloader 默认目录或 Agent 子目录后续变更不会改变历史任务的落点。

`POST /agents/{agent_id}/tasks/batch-retry` 请求体：`{ "task_ids": ["..."] }`；`task_ids` 为 `null`/缺省表示重试该 Agent 全部可重试任务。仅 `status` 为 `error`/`paused` 的任务参与（与行级重试按钮条件一致），其余状态跳过；每个任务复用单任务重试语义（使用任务持久化的 `download_dir`），逐条捕获错误，最后统一 commit。响应 `data`：
```json
{ "processed": 10, "retried": 9, "failed": 1, "errors": ["<task_id>: <原因>"] }
```

### Pending Decisions

| Method | Path | 说明 |
|--------|------|------|
| GET | `/agents/{agent_id}/decisions` | 待决策列表（分页，可按 status 查询） |
| POST | `/decisions/{id}/confirm` | 确认选择某个候选资源 → 推送下载 |
| POST | `/decisions/{id}/skip` | 跳过本次决策（标记 skipped） |
| POST | `/decisions/{id}/ai-pick` | AI 自动处理：让 LLM 选中最优候选（优先复用缓存的 `llm_picked_resource_id`，否则即时调用 LLM）并派发下载 |
| POST | `/agents/{agent_id}/decisions/batch` | 批量处理：对多条 pending 决策统一执行 `skip` 或 `ai` 动作 |

`POST /decisions/{id}/confirm` 请求体：`{ "resource_id": "uuid" }`。

`POST /decisions/{id}/ai-pick` 无请求体。决策非 `pending` 状态返回 `400 NOT_PENDING`；LLM 未能给出选择返回 `400 LLM_NO_PICK`（需手动确认）。响应 `data`：`{ "id", "status", "decided_resource_id", "decided_at" }`。

`POST /agents/{agent_id}/decisions/batch` 请求体：`{ "decision_ids": ["..."], "action": "skip" | "ai" }`。仅处理 `status="pending"` 的决策；响应 `data`：
```json
{ "processed": 10, "dispatched": 7, "skipped": 2, "failed": 1, "errors": ["<decision_id>: <原因>"] }
```

### File Resources

| Method | Path | 说明 |
|--------|------|------|
| GET | `/channels/{channel_id}/resources` | 频道资源列表。默认（`grouped=false`）按 `published_at` 倒序、按行分页返回。`grouped=true` 时按 **作品分组**分页——每一页返回若干个 group（TVSeries / Movie / 未识别），每个 group 内包含该作品全部资源（不跨页拆分），group 顺序按各自最新资源的 `published_at` 倒序；`meta.total` 表示 group 总数。 |
| GET | `/channels/{channel_id}/field-values` | Filter DSL 编辑器的自动补全数据源。Query 参数 `field`（必填，仅支持字符串字段与 `subtitle_langs`）、`q`（可选，忽略大小写的前缀匹配）、`limit`（默认 10，最大 50）。返回该频道下 top-N 出现频率最高的候选值数组。数值型字段被拒绝（422）。|
| GET | `/resources/{id}` | 资源详情 |
| GET | `/resources/{id}/metadata` | 获取 metadata（若未链接则触发自动匹配流程，返回匹配结果；匹配中返回 status=processing 可轮询）；链接为剧集时 `linked.entity` 额外携带 `seasons`（每季 `season_number`/`episode_count`），供集号修正 UI 从绝对集号前端预填季号 |
| POST | `/resources/{id}/metadata/search` | 手动 MetadataAgent 搜索：`{ "search_title": "...", "content_type": "tv"|"movie", "data_source_type": "exa"|"tmdb"|"wikipedia"? }` → 返回候选列表 |
| PUT | `/resources/{id}/metadata/link` | 手动确认关联：`{ "selected_result": { ... } }` → 创建/更新 TVSeries/Movie，写入 resource FK，写入 ChannelRawTitleMapping，重新触发 Agent 过滤 |
| PATCH | `/resources/{id}/episode` | 手动修正集号：`{ "episode": int|null, "season": int?, "absolute_episode": int|null?, "note": string? }` → 写入 per-season episode（可选保留 absolute_episode），设置 `episode_confidence="manual"`。未显式发送 `season` 且已知 absolute 集号、资源已链接剧集且该剧集有逐季集数数据时，服务端用 `locate_absolute_episode` 推导 season（episode 也未显式发送时一并推导）——显式值永远优先。**先 commit 再入队**（worker 只读已提交数据，避免读到修正前的 `ambiguous` 而重建过期决策），然后对该 channel 下所有 active Agent 入队一次**定向运行**（`resource_ids=[该资源]`）：按 Agent 当前规则只处理该资源，**绕过消费水位线**（资源可能较旧）、**不推进水位线**。省略 `absolute_episode` 时保留原值。 |

`POST /resources/{id}/metadata/search` 请求体示例：
```json
{
  "search_title": "Ascendance of a Bookworm",
  "content_type": "tv",
  "data_source_type": "exa"
}
```

响应 `data`：
```json
{
  "results": [
    {
      "title_cn": "...", "title_en": "...", "original_title": "...",
      "description": "...", "poster_url": "https://...", "year": 2024,
      "external_id": "...", "external_source": "exa", "content_type": "tv"
    }
  ]
}
```

### 作品仓库（Works）

统一的海报墙 API，合并 TVSeries 和 Movie 两种实体。

| Method | Path | 说明 |
|--------|------|------|
| GET | `/works` | 作品列表（分页，支持 search 模糊搜索和 content_type 过滤：all/tv/movie；`collection_id` 参数：合集 UUID=仅该合集成员（音频作品被排除），字面量 `none`=仅未分组作品，缺省=不过滤；tv/movie 条目带 `collection_id`/`collection_name`（显示名 = 合集 title_cn 或 title_en，未分组为 null），音频条目无合集恒为 null） |

### TVSeries

| Method | Path | 说明 |
|--------|------|------|
| GET | `/series` | 列表（分页，支持 title 模糊搜索） |
| POST | `/series` | 手动创建剧集元数据（极少使用） |
| GET | `/series/{id}` | 剧集详情（含 episodes、资源数、任务数） |
| PUT | `/series/{id}` | 更新剧集元数据（别名合并策略：追加不去重） |
| DELETE | `/series/{id}` | 删除剧集（关联 FileResource 的 series_id 置空，不删资源） |

### Movies

| Method | Path | 说明 |
|--------|------|------|
| GET | `/movies` | 列表（分页，支持 title 模糊搜索） |
| POST | `/movies` | 手动创建电影元数据 |
| GET | `/movies/{id}` | 电影详情 |
| PUT | `/movies/{id}` | 更新电影元数据 |
| DELETE | `/movies/{id}` | 删除电影 |

系列/电影详情响应额外包含 `collection`（`{id, name}` 或 null）与 `collection_siblings`（同合集其他作品 `[{id, title, year, type}]`，来自本地库共享 collection_id 查询）。响应顶层同时暴露 `external_id`/`external_source`、`canonical_name`/`wikipedia_url`（Wikipedia 实际 URL，优先于 curid 回退），以及服务端计算的 `source_links`（`[{source, label, url}]`，由站点注册表 `metadata_source_registry.build_source_links` 生成，支持旧复合形 `TMDB:NNN; IMDb:ttNNN` 拆分，TMDB 链接按 tv/movie 区分路径）——前端详情页直接渲染，不再自行解析。

### WorkCollections（作品合集）

大 IP 系列分组（CRUD-lite）。一个作品至多属于一个合集；挂载已属其他合集的作品返回 409 DUPLICATE_SUBMISSION。

| Method | Path | 说明 |
|--------|------|------|
| GET | `/collections` | 合集列表（分页，支持 search 名称模糊搜索；每项额外带 `work_count` 成员作品数） |
| POST | `/collections` | 创建合集（body: title_cn 必填，title_en/description/poster_url 可选） |
| GET | `/collections/{id}` | 合集详情（含 `works`: `[{id, title, year, type}]`；`?include_parts=true` 且合集为 `tmdb_collection` 源时额外返回 `untracked_parts`: `[{tmdb_id, title, year, poster_url}]` —— TMDB collection parts 中本地库未收录的作品，按需拉取、进程内 10 分钟缓存、不落库；拉取失败返回空数组） |
| GET | `/collections/{id}/works` | 合集成员作品分页列表（`page`/`page_size`；每项与 `GET /works` 相同的归一化结构，content_type 为 `tv`/`movie`，按 created_at 倒序合并剧集+电影） |
| PATCH | `/collections/{id}` | 更新合集（title_cn/title_en/description/poster_url） |
| DELETE | `/collections/{id}` | 删除合集（成员作品的 collection_id 置空，不删作品） |
| POST | `/collections/{id}/works` | 挂载作品（body: `{work_type: "series"\|"movie", work_id}`；重复挂载同合集幂等） |
| DELETE | `/collections/{id}/works/{work_id}?work_type=` | 从合集移除作品（collection_id 置空） |

---

