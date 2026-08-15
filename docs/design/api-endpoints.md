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

### 认证（Auth）

`AUTH_ENABLED=true`（默认）时，`/api/v1/*` 与 `/posters/*` 全部受 `AuthMiddleware` 保护，需携带有效凭证；`/api/v1/auth/*` 开放（登录与状态查询）；SPA 页面与静态资源开放（前端在收到 401 后自行跳转 `/login`）。无凭证或凭证无效一律返回 401 `{success:false, data:null, error:{code:"UNAUTHORIZED",...}, meta:{}}`。

两类凭证（任一有效即可）：
- **会话 Cookie**（Web 端）：`POST /auth/otp` 签发的 HttpOnly Cookie `rssripple_auth`。
- **API Key**（程序端）：`Authorization: Bearer <key>` 或 `X-API-Key: <key>` 头；接受环境变量 `API_KEY`（可选静态引导 key）或 `api_keys` 表中的 key。

| Method | Path | 说明 |
|--------|------|------|
| POST | `/auth/otp` | TOTP 登录：body `{"code": "123456"}`；校验通过（容忍 ±1 时间窗）后签发 HttpOnly Cookie `rssripple_auth`（30 天，SameSite=Lax），返回 `{authenticated: true}`；验证码错误 401 `UNAUTHORIZED` |
| POST | `/auth/logout` | 清除会话 Cookie，返回 `{authenticated: false}` |
| GET | `/auth/status` | 当前请求是否已认证 `{authenticated: bool}`，始终 200 |
| GET | `/api-keys` | API key 列表（`[{id, name, prefix, created_at}]`；不暴露 hash/明文） |
| POST | `/api-keys` | 创建 API key：body `{"name": "..."}` → 201，`data` 额外含 `key`（`rr_...` 明文，**仅本次响应返回一次**） |
| DELETE | `/api-keys/{id}` | 删除 API key；不存在 404 |

TOTP 秘钥与 Cookie 签名秘钥在首次启动时自动生成并持久化到 `app_settings`（`auth_totp_secret` / `auth_cookie_secret`）；provisioning URI（`otpauth://totp/RSSRipple:admin?...`）每次启动以 WARNING 级别打印，由运维手动添加到认证器。

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
| GET | `/channels/metadata-sources` | 频道表单数据源目录（三数据源架构：仅 wikipedia/tmdb/bangumi + 可用性标志 + 默认值） |
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

频道创建/更新的元数据字段：`metadata_source` 仅接受 `wikipedia | tmdb | bangumi`（其他值 422）；`metadata_fallback_sources` 为 Exa 回退的有序站点白名单（JSON 数组，元素必须是注册表站点名 wikipedia/tmdb/bangumi/mal/anilist/imdb/douban，未知值 422；`null`=默认顺序，`[]`=禁用回退）。`default_is_anime`（「默认标记为 Anime」，默认 false）：Create 接受、Response 透出，**创建后不可改**——PUT 提交不同值返回 422 VALIDATION_ERROR，同值幂等放行。

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
| POST | `/downloaders/{id}/test` | 测试 Transmission RPC 连通性，并用 `free_space(download_dir)` 检查默认下载目录；**同时校验卷绑定有效性**（存在且可读可写）。请求体可选 `{url, username, password, download_dir, volume_id, volume_subpath}`：用于编辑表单按未保存的表单值探测（缺省字段回退到已存值，空密码 = 沿用已存密码；`volume_id`/`volume_subpath` 缺省沿用已存值、显式 `null` = 解绑即恒等）；带覆盖值的探测不更新 status，仅探测已存配置时才更新 status。响应含 `volume_check`（`{exists, readable, writable}` 或 null），卷绑定无效时 `success=false` 且 message 说明原因 |
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
  "download_dir": "/volume1/downloads/rssripple",
  "volume_id": "b7f1…",
  "volume_subpath": "downloads/rssripple"
}
```

下载目录规则：
- `download_dir` 必填，必须是 Transmission 下载服务器视角的绝对路径；支持 POSIX（`/volume1/downloads`）、Windows drive path（`D:\Downloads`）和 daemon 支持的 UNC path。
- RSSRipple 后端可能无法访问该目录，因此路径语义以 Transmission daemon 为准。
- `volume_id` / `volume_subpath`（R1 卷绑定，均可空）：daemon 视角的 `download_dir` 根 == `volume.mount_path + volume_subpath`，用于把通知快照里的 daemon 路径解析为本进程视角（见 file-organization.md「统一路径解析：逻辑存储卷」）；两者皆 null = 两视角一致（恒等，默认）。`volume_id` 必须指向存在的 StorageVolume（404），`volume_subpath` 校验规则同 `Agent.download_subdir`（相对路径、禁 `..`/绝对路径/控制字符，空串归 null）且必须依附 `volume_id`（422）。P1 的 `path_map` 字段已移除。
- 创建/编辑时做路径格式校验；`POST /downloaders/{id}/test` 必须调用 Transmission `free_space(download_dir)`，返回目录可识别性与剩余空间。
- 真实写入能力、子目录存在性、磁盘不足等仍以 `torrent_add(download_dir=...)` 的结果为最终准据。
- 若多个 Agent 共用一个 Downloader，建议通过 Agent 的 `download_subdir` 分流目录。

### Storage Volumes

逻辑存储卷（file-organization.md「统一路径解析：逻辑存储卷」）：指向 RSSRipple 容器内一个挂载点；一切配置面路径引用存 `(volume_id, subpath)`，使用处动态解析 `volume.mount_path + subpath`。

| Method | Path | 说明 |
|--------|------|------|
| GET | `/volumes` | 逻辑卷列表（分页） |
| POST | `/volumes` | 创建 `{name, mount_path, remark?}` → 201；`mount_path` 必须绝对路径且**存在**（422）；`name` 全局唯一（重复 409 `DUPLICATE_SUBMISSION`） |
| GET | `/volumes/{id}` | 详情 |
| PUT | `/volumes/{id}` | 更新 `{name?, mount_path?, remark?}`（改 mount_path 全局生效——所有卷引用动态解析；同样过存在性校验 422 与名称唯一 409） |
| DELETE | `/volumes/{id}` | 删除；被下载器卷绑定 / 媒体服务器绑定 / Library 库根引用时 409 `DELETE_BLOCKED`（`error.details` 带出对应 `downloaders` / `media_server_bindings` / `libraries` 数组） |
| POST | `/volumes/{id}/check` | 探测挂载点存在性与写权限 → `{exists, writable}`（writable 仅展示提示，不拦截保存） |

### Media Servers

媒体服务器实例（file-organization.md「MediaServerInstance / MediaServerBinding」，R2）：多服务器、多类型（plex/emby/jellyfin）；Library 由扫描派生。token 不回显（对齐 Downloader password 惯例）。

| Method | Path | 说明 |
|--------|------|------|
| GET | `/media-servers` | 服务器列表（不分页；每项含 bindings 数组、派生 `library_count` 与 `unbound_library_count`） |
| POST | `/media-servers` | 创建 `{name, type, url, token?, enabled?, bindings?}` → **201**；`type` 限 `plex/emby/jellyfin`（422）；bindings 条目 `{server_path_prefix, volume_id, subpath?}`——prefix 非空（422）、volume 不存在（404）、subpath 校验同 `Agent.download_subdir` |
| GET | `/media-servers/{id}` | 详情（含 bindings 数组） |
| PUT | `/media-servers/{id}` | 部分更新；`bindings` 内嵌**整体替换**（缺省不动，数组含 `[]` 全量替换；同创建校验）；`token` 传 null/缺省 = 保持存储值 |
| DELETE | `/media-servers/{id}` | 删除（bindings 随 FK CASCADE；派生 Library `media_server_id` SET NULL 保留行） |
| POST | `/media-servers/{id}/test` | 连通性 + 凭证校验 → `{ok, server_version?, message?}` |
| POST | `/media-servers/{id}/scan` | 扫描 sections/虚拟目录，幂等 upsert Library（经 bindings 最长前缀匹配解析卷；未命中落 `volume_id=NULL` 待绑定）→ `{created, updated, unbound}`；服务器停用 → 409 `INVALID_STATE`；连接/接口失败 → 502 `MEDIA_SERVER_ERROR` |

### Download Tasks

| Method | Path | 说明 |
|--------|------|------|
| POST | `/tasks` | 手动创建下载任务（绕过 Agent，见下文） |
| GET | `/tasks` | **全局**下载任务列表（分页，`page_size`≤100；可选过滤 `downloader_id`/`agent_id`/`status`，status 非法值 422；`created_at` 倒序）。供外部消费者（如 vault-organizer）按通知 payload 的 `download_task_id` 寻址查询 |
| GET | `/agents/{agent_id}/tasks` | Agent 的下载任务（分页，可按 status 过滤） |
| GET | `/tasks/{id}` | 任务详情（含 file_resource、agent、channel 信息） |
| POST | `/tasks/{id}/pause` | 停止（暂停，调用 Transmission RPC） |
| POST | `/tasks/{id}/resume` | 恢复 |
| POST | `/tasks/{id}/retry` | 重试（重置 retry_count，重新添加 torrent） |
| POST | `/agents/{agent_id}/tasks/batch-retry` | 批量重试：对 Agent 下多个 error/paused 任务统一执行重试 |
| DELETE | `/tasks/{id}` | 删除任务（删除 Transmission 种子并将任务标记 `cancelled`）；query 参数 `delete_data=false` 控制是否同时删除已下载数据 |

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

### Download Notifications（下载完成通知）

完整语义（模型、聚合状态机、payload 契约、fan-out 与退避策略、下游清理 API）见 [notifications.md](notifications.md)。投递为纯出站：无 token、无消费者回调端点（旧版 start/ack/fail 已删除）。

| Method | Path | 说明 |
|--------|------|------|
| GET | `/agents/{agent_id}/notifications` | 该 Agent 的通知队列（分页；`status` 按**聚合状态**过滤 pending/done/failed，其他值 422；列表项不含 payload，带聚合 `status` 与 `delivery_summary {total,done,failed,pending}`） |
| GET | `/notifications/{id}` | 通知详情：完整 payload 快照 + `deliveries` 数组（每条含 webhook_url/status/attempt_count/error_message/delivered_at/next_attempt_at）；`payload.work.genre` 为封闭 TMDB 27 类枚举（完整取值见端点 /docs 描述与 data-models.md「genre 取值约定」） |
| GET | `/agents/{agent_id}/webhooks` | webhook 列表（`[{id, url, mock, enabled, created_at}]`） |
| POST | `/agents/{agent_id}/webhooks` | 注册 webhook：`{ "url": "...", "mock": false, "enabled": true }` → **201**；非 mock 必须 http(s) url（422）；注册后立即对积压通知 fan-out |
| PUT | `/agents/{agent_id}/webhooks/{webhook_id}` | 更新 webhook（部分更新 `{url?, mock?, enabled?}`；重新启用后同样立即 fan-out 恢复投递） |
| DELETE | `/agents/{agent_id}/webhooks/{webhook_id}` | 删除 webhook（delivery 历史随 CASCADE 删除；通知行保留） |
| POST | `/agents/{agent_id}/notifications/regenerate` | 重新生成：`{ "since": datetime \| null }`（null=从最早 completed 任务开始），对该 Agent 的 completed 任务重跑完整生成链路——无通知的补建、已有的重建 payload 并复位投递重投；当次拿不到 torrent 文件清单时保留旧快照；返回 `{ "created": n, "regenerated": m }` |
| POST | `/notifications/{id}/retry` | 单条手动重试：body `{ "mode": "failed" \| "all" }` → `{ "reset": n }`；`failed` 仅重置 failed delivery，`all` 重置全部非 pending（done + failed）delivery，重置后立即到期 |
| POST | `/notifications/retry` | 批量重试：body `{ "mode": "failed" \| "all", "since"?: datetime, "agent_id"?: uuid }` → `{ "reset": n }`；`since` 按通知 `created_at` 过滤，缺省为全库范围 |

### 内置文件整理（Organize）

完整语义（模型、规则与命名模板、两阶段规划/执行、触发链路）见 [file-organization.md](file-organization.md)。

##### Libraries

Library 为媒体服务器**扫描派生**（R2），收敛为只读 + 局部更新：无 POST（手工注册移除）；响应中 `root_path` 为派生展示字段（`volume.mount_path + root_subpath` 解析结果，未绑定为 null），`bound` 为绑定状态。

| Method | Path | 说明 |
|--------|------|------|
| GET | `/libraries` | 库列表（不分页，量小；每项含 `pending_plan_count`、`bound`、派生 `root_path`；`unbound=true` 过滤待绑定） |
| GET | `/libraries/{id}` | 详情（含来源服务器 `media_server_id`/`media_server_name`、`server_path`、解析后的 `root_path` 展示） |
| PUT | `/libraries/{id}` | 仅可更新 `subtitle_lang_map` 与 `volume_id`/`root_subpath`（待绑定就地修复；volume 不存在 404，root_subpath 非法 422）；其余字段由扫描派生，提交即 422 |
| DELETE | `/libraries/{id}` | 删除；存在关联计划或指向该库的规则时 **409 `DELETE_BLOCKED`** |

##### Organize Rules

| Method | Path | 说明 |
|--------|------|------|
| GET | `/organize-rules` | 规则列表（priority 升序、created_at 稳定排序；first-match-wins 的求值顺序） |
| POST | `/organize-rules` | 创建 → **201**；`filter` 经 `validate_filter_config`（非法结构/取值操作符空 value → 422）；`path_template` 经 `validate_template`（非法占位符/绝对路径/`..` 段 → 422）；`file_op` 限 `move`/`hardlink`/`copy`（其他 → 422；hardlink/copy 为保种模式，见 file-organization.md 清理策略分流）；`library_id` 不存在 → 404 |
| GET | `/organize-rules/{id}` | 详情 |
| PUT | `/organize-rules/{id}` | 部分更新（含 priority 调整；filter/模板/library 同上校验） |
| DELETE | `/organize-rules/{id}` | 删除；已有计划的 `rule_id` SET NULL 保留历史 |
| POST | `/organize-rules/preview` | dry-run 预览：body `{notification_id 或 resource_id（二选一，422/404）, rule?: <规则草稿>, category?}`；按草稿（缺省=当前规则列表 first-match）渲染，返回 `{matched_rule, library, category, needs_category, uncategorized, ops:[{op_type, src, dst, size, reason}]}`；resource_id 形态 best-effort 经下载器 RPC 取文件清单（只读不停种）；规划失败（PlanError）→ 422 带原因；**不落库不动磁盘** |

##### Plans / Audit

| Method | Path | 说明 |
|--------|------|------|
| GET | `/organize/plans` | 计划列表（分页；`status`（非法值 422）/`library_id` 过滤；created_at 倒序；列表项不含 payload，带 `rule_name`/`library_name`、`ops_summary {total,move,keep,movedir}` 与派生 `pending_reason: "unclassified" \| "unbound" \| null`——library 未定/缺 category → unclassified，目标库未绑定卷 → unbound，仅 pending 计划派生） |
| GET | `/organize/plans/{id}` | 详情：完整 payload 快照 + ops 数组 + audit_entries 时间线（同带 `pending_reason`） |
| POST | `/organize/plans/{id}/execute` | 后台执行（**202** + 当前状态）；仅 pending/failed 可执行，其余状态 / 待分类 / 缺 category / 待绑定 → **409 `INVALID_STATE`** |
| POST | `/organize/plans/execute-batch` | 批量执行 `{plan_ids: [...]}` → `{results: [{plan_id, status}]}`；锁内逐个，单个失败不影响其余 |
| POST | `/organize/plans/{id}/classify` | 待分类计划人工指定 `{library_id, category?}`：重渲染全部 op 的 dst 并复位 pending；非 pending/failed → 409；library 不存在 → 404；重渲染失败（如模板含 `{category}` 但未指定）→ 422 |
| POST | `/organize/plans/{id}/cancel` | 取消 pending/failed 计划 → cancelled（记 audit）；done/running/cancelled → **409 `INVALID_STATE`** |
| GET | `/organize/audit` | 审计条目分页（`plan_id` 过滤；最新在前） |

### File Resources

| Method | Path | 说明 |
|--------|------|------|
| GET | `/channels/{channel_id}/resources` | 频道资源列表。默认（`grouped=false`）按 `published_at` 倒序、按行分页返回。`grouped=true` 时按 **作品分组**分页——每一页返回若干个 group（TVSeries / Movie / 未识别），每个 group 内包含该作品全部资源（不跨页拆分），group 顺序按各自最新资源的 `published_at` 倒序；`meta.total` 表示 group 总数。 |
| GET | `/channels/{channel_id}/field-values` | Filter DSL 编辑器的自动补全数据源。Query 参数 `field`（必填，仅支持字符串字段与 `subtitle_langs`）、`q`（可选，忽略大小写的前缀匹配）、`limit`（默认 10，最大 50）。返回该频道下 top-N 出现频率最高的候选值数组。数值型字段被拒绝（422）。|
| GET | `/resources/{id}` | 资源详情 |
| GET | `/resources/{id}/metadata` | 获取 metadata（若未链接则触发自动匹配流程，返回匹配结果；匹配中返回 status=processing 可轮询）；链接为剧集时 `linked.entity` 额外携带 `seasons`（每季 `season_number`/`episode_count`），供集号修正 UI 从绝对集号前端预填季号 |
| POST | `/resources/{id}/metadata/search` | 手动 MetadataAgent 搜索：`{ "search_title": "...", "content_type": "tv"|"movie", "data_source_type": "exa"|"tmdb"|"wikipedia"|"bangumi"? }` → 返回候选列表 |
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
| GET | `/works` | 作品列表（分页，支持 search 模糊搜索和 content_type 过滤：all/tv/movie；`collection_id` 参数：合集 UUID=仅该合集成员（音频作品被排除），字面量 `none`=仅未分组作品，缺省=不过滤；tv/movie 条目带 `collection_id`/`collection_name`（显示名 = 合集 title_cn 或 title_en，未分组为 null）与 `is_anime`（可空布尔三态，见下），音频条目无合集恒为 null） |
| POST | `/works/refresh-metadata` | 刷新单个作品元数据：body `{id, content_type: "tv"\|"movie", source?, override_manual_edits?}` → 用现有标题对所选源重新搜索并补全缺失字段；`override_manual_edits`（默认 false）勾选「覆盖所有人工编辑字段」时才覆盖 `manually_edited_fields` 中的字段（见 data-models.md「人工编辑保护」） |

### TVSeries

`genre` 字段（Create/Update/Response）为封闭 TMDB 27 类枚举（/docs 中 `GenreName` 渲染完整取值，约定见 data-models.md「genre 取值约定」）；Create/Update 提交表外值返回 422。`is_anime` 字段（Create/Update/Response）为可空布尔三态（True=日本动画 / False=确认实拍 / null=未判定，判定与赋值规则见 data-models.md「is_anime 判定约定」）；Create/Update 接受手动指定。`manually_edited_fields`（Response 透出，不可直接提交）为人工编辑保护字段名列表：PUT 时按显式发送的 `MANUAL_EDITABLE_FIELDS` 字段（含显式 null）记录，自动扫描不再覆盖。

| Method | Path | 说明 |
|--------|------|------|
| GET | `/series` | 列表（分页，支持 title 模糊搜索） |
| POST | `/series` | 手动创建剧集元数据（极少使用） |
| GET | `/series/{id}` | 剧集详情（含 episodes、资源数、任务数） |
| PUT | `/series/{id}` | 更新剧集元数据（别名合并策略：追加不去重）；显式发送的可编辑字段记入 `manually_edited_fields` |
| DELETE | `/series/{id}` | 删除剧集（关联 FileResource 的 series_id 置空，不删资源） |

### Movies

`is_anime` 字段（Create/Update/Response）同 TVSeries：可空布尔三态，Create/Update 接受手动指定。`manually_edited_fields` 同 TVSeries：PUT 记录显式编辑字段，自动扫描跳过。

| Method | Path | 说明 |
|--------|------|------|
| GET | `/movies` | 列表（分页，支持 title 模糊搜索） |
| POST | `/movies` | 手动创建电影元数据 |
| GET | `/movies/{id}` | 电影详情 |
| PUT | `/movies/{id}` | 更新电影元数据；显式发送的可编辑字段记入 `manually_edited_fields` |
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

