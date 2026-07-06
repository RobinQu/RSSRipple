# RSSRipple 架构文档

## 1. 项目简介

RSSRipple 是一个 RSS 订阅源聚合 + 智能筛选 + 自动推送到下载客户端（Transmission）的媒体资源自动下载工具。用于自动追番、追美剧、下载电影等场景。

## 2. 技术栈

| 层 | 技术选型 |
|---|---|
| Backend | Python 3.11+, FastAPI, SQLAlchemy 2.0 (async), APScheduler, aiosqlite |
| Frontend | React 18, TypeScript, Vite, Ant Design 5, react-router v6 |
| External | Transmission (RPC), LLM API (OpenAI-compatible chat/completions), Exa Agent API, TMDB API, Wikipedia Python library, feedparser |
| Task Queue | 内置 MemoryQueue / RedisQueue 双后端（基于 SETNX 做幂等） |

数据库默认使用 SQLite，可切换至 PostgreSQL。

## 3. 模块划分

```
┌─────────────────────────────────────────────┐
│              Frontend (React SPA)           │
│  Dashboard / Channels / Agents / Downloaders│
└─────────────────┬───────────────────────────┘
                  │ HTTP / SSE
┌─────────────────▼───────────────────────────┐
│            FastAPI Backend                  │
│  ┌─────────┐ ┌─────────┐ ┌────────────────┐ │
│  │ Channels│ │ Agents  │ │ Downloaders    │ │
│  │  API    │ │  API    │ │  API + Tasks   │ │
│  └────┬────┘ └────┬────┘ └───────┬────────┘ │
│       │           │              │          │
│  ┌────▼───────────▼──────────────▼────────┐ │
│  │          Service Layer                 │ │
│  │ fetch_service / agent_service /        │ │
│  │ metadata_service / feed_analyzer(LLM)  │ │
│  └────┬───────────┬──────────────┬────────┘ │
│       │           │              │          │
│  ┌────▼────┐ ┌────▼────┐ ┌──────▼───────┐  │
│  │Scheduler│ │TaskQueue│ │Clients Layer │  │
│  │(APSched)│ │(Mem/Redis│ │ RSS/Trans/LLM│  │
│  └─────────┘ └─────────┘ └──────────────┘  │
│       │                                    │
│  ┌────▼────────────────────────────────┐   │
│  │   SQLAlchemy ORM + SQLite/Postgres  │   │
│  └─────────────────────────────────────┘   │
└─────────────────────────────────────────────┘
```

各层职责：

- **API 层**：REST 端点 + SSE 流，Pydantic 校验，统一响应格式 `{success, data, error, meta}`
- **Service 层**：核心业务逻辑（抓取、解析、过滤、metadata 匹配、下载推送）
- **Client 层**：外部服务封装（RSS 解析、Transmission RPC、LLM 调用）。下载器通过工厂 `app.clients.downloader.get_downloader_client(downloader)` 分派：`type="transmission"` → `TransmissionWrapper`，`type="mock"` → `MockDownloaderWrapper`（本地内存模拟器，用于测试 Agent 流程）。二者共享同一异步接口。
- **Scheduler 层**：APScheduler 管理定时任务（频道抓取、进度同步、过期清理）
- **Task Queue 层**：异步后台任务队列（手动触发的 fetch/run），支持内存和 Redis 两种后端
- **数据层**：SQLAlchemy ORM，SQLite 默认

## 4. 核心数据实体

| 实体 | 职责 |
|---|---|
| Channel | RSS 订阅频道，含 field_mapping 与 metadata_agent_enabled 开关 |
| FileResource | RSS 解析后的单条资源（种子/磁力），关联到作品。合集资源（`is_batch=True`）额外携带 `episode_start/end` 尽力而为的集数区间 |
| TVSeries / Movie | 作品 metadata 缓存（本地库），含海报缓存 |
| Episode | TVSeries 下的单集 |
| Agent | 订阅器，监听 Channel 更新，按过滤规则推送到下载器；可选配置下载子目录。携带 `last_consumed_at` 消费水位线驱动增量运行 |
| AgentWork | Agent 订阅的具体作品（从 metadata 库选取，最多 10 个） |
| DownloadTask | 下载任务记录，持久化实际使用的下载目录 |
| AgentRun | 每次 Agent 运行的持久化记录（计数、状态、匹配资源 ID 列表），用于运行历史展示 |
| PendingDecision | 多候选待决策项；亦承载集号不确定（ambiguous）资源的人工确认 |
| AgentSuggestion | 未识别资源建议分组，持久化供用户后续关联 metadata |
| DownloaderInstance | Transmission 下载器实例配置，含默认下载目录 |
| ChannelRawTitleMapping | Channel 手动修正的标题映射记忆 |
| MetadataCache | LLM 标题清洗缓存 |

完整字段定义与 API 详见 `AGENTS.md`。

## 5. 核心数据流

### 5.1 RSS 抓取 → Metadata 匹配 → 通知 Agent

```
APScheduler/cron → fetch_channel_resources(channel)
  → feedparser 拉取 RSS
  → GUID 去重
  → parse_entry 用 field_mapping 解析结构化字段
  → detect_batch(title) 预填合集字段（is_batch / episode_start / episode_end）
  → UnifiedMetadataAgent.process() (single ReAct call: title clean + one selected metadata source, 可覆盖 pre-parser 的合集字段)
  → 持久化 FileResource + 海报本地缓存
  → 通知所有订阅该 Channel 的 active Agent（enqueue agent run）
```

### 5.2 Agent 过滤 → 推送下载

```
Channel 通知 / 手动触发 → run_agent(agent, resource_ids?)
  → 选择处理范围（三种运行模式）：
     · 增量运行（无 resource_ids）：只处理 created_at > agent.last_consumed_at 的资源，
       运行后推进水位线（替代旧的 limit(200)）；水位线为 null 时置为 now 且不处理任何资源
     · 定向运行（带 resource_ids，如 correct_episode）：只处理指定资源，绕过且不推进水位线
     · 回填提交（rules-preview 保存）：派发用户选中资源 + 推进水位线到频道 max
  → 持久化 AgentRun(status="running")，结束回填计数/状态/匹配资源 ID
  → 所有未链接 metadata 的资源一律归入持久化 suggestions，不参与过滤/下载
  → work-scope + filter 评估（_resource_matches_rules）；在范围内未通过 filter 计 filter_failed
  → episode_confidence=="ambiguous" 的资源 → 创建 PendingDecision（集号不确定类，跳过 LLM），
    等待用户手动修正集号；用户修正后下次运行自动把该决策标记为 decided
  → 合集资源（is_batch=True）绕过 (series_id, episode) 聚合，直接派发；
     不生成 PendingDecision，仅避免同一 FileResource 重复入队
  → TV 单集：检查 episode 去重（Agent 维度）
  → 单候选 → 解析下载目录 → 创建 DownloadTask → Transmission.add_torrent
  → 多候选 → conflict_resolution="ask" 创建 PendingDecision（llm_enabled 时填充
     llm_picked_resource_id 供 AI 自动处理）；"auto" 自动选最优（启发式 + LLM pick）
```

下载目录解析规则：

```
DownloaderInstance.download_dir              # 必填，Transmission 下载服务器视角的绝对目录
                                             # 支持该服务器 OS 的路径风格（POSIX/Windows/UNC）
Agent.download_subdir?                       # 可选，相对目录，不能是绝对路径，不能包含 ..
  → effective_download_dir
  → 写入 DownloadTask.download_dir
  → Transmission.add_torrent(download_dir=effective_download_dir)
```

`download_dir` 指向的是 Transmission 所在下载服务器本地可读写目录。RSSRipple 后端可能运行在另一个容器或主机上，因此路径语义必须以 Transmission daemon 为准，而不是以后端进程所在主机为准。

Downloader 连接测试应包含两步：先建立 Transmission RPC 会话，再调用 `free_space(download_dir)` 检查默认目录是否可被 Transmission 识别并返回剩余空间。Agent 子目录属于运行时拼接结果，可在保存 Agent 时做格式校验，在实际派发任务时由 `torrent_add(download_dir=...)` 暴露目录不存在、权限不足、磁盘不足等错误。

目录配置变更不回写历史任务：`DownloadTask.download_dir` 是任务创建时的快照，任务重试沿用该目录。

### 5.3 下载进度同步

```
APScheduler 每分钟 → sync_download_progress()
  → 查询 downloading/queued 状态的 DownloadTask
  → Transmission RPC 获取 torrent 状态
  → 更新 progress/speed/eta/status
  → 处理 torrent 被删/Transmission 不可达等异常
```

### 5.4 手动 metadata 修正

```
用户在 FileResource 详情点"修正 metadata"
  → 输入搜索词 + 选类型（tv/movie）
  → 调用 MetadataAgent，按所选数据源返回候选列表
  → 用户选择正确作品
  → 写入 FileResource.series_id/movie_id
  → 写入 Channel.raw_title_mappings（记忆映射）
  → 重新触发关联 Agent 的过滤
```

## 6. LLM 服务职责

| 能力 | 说明 |
|---|---|
| RSS 字段映射分析 (feed_analyzer) | 给定 RSS 原始 entries，输出 field_mapping JSON，支持 SSE 流式输出 |
| 统一 Metadata Agent (metadata_agent) | LangGraph ReAct 循环：标题清洗 + episode/season 推断 + 单数据源 metadata 搜索。每次只使用 `exa` / `tmdb` / `wikipedia` 之一，生产默认 `exa`，结果缓存到 MetadataCache。 |
| 标题清洗 (title_cleaner) | ~~已废弃~~ — 功能合并入 MetadataAgent |
| Title regex 生成 | ~~已废弃~~ — 功能合并入 MetadataAgent |
| Metadata 搜索 | ~~已废弃~~ — 功能合并入 MetadataAgent 的 Exa/TMDB/Wikipedia 单数据源工具 |
| PendingDecision 候选选择（可选） | 多候选时 LLM 选出最优候选（`llm_picked_resource_id`）并给出理由，驱动 `conflict_resolution="auto"` 自动选择、`"ask"` 模式的建议高亮，以及 `POST /decisions/{id}/ai-pick` 一键 AI 自动处理。可用 `agent.llm_prompt` 自定义选择指令 |

MetadataAgent 数据源约束：

- `exa`：默认搜索方式，调用 Exa Agent API，使用结构化 `output_schema` 获取 candidates。
- `tmdb`：仅调用 TMDB search/detail 工具。
- `wikipedia`：仅调用 Wikipedia search/page 工具。
- eval 标注平台的新 Dataset 必须显式选择上述三者之一，数据集名称以前缀标明数据源；`combined` 只用于兼容旧数据，不作为新评测目标。

## 7. 前端路由

| Route | 页面 |
|---|---|
| `/` | Dashboard（活跃下载按作品分组、待决策，统计卡可点击跳转） |
| `/works` | WorksPage（作品仓库海报墙，All/TV/Movie 筛选） |
| `/channels` | 频道列表 |
| `/channels/new`, `/channels/:id/edit` | 频道表单 |
| `/channels/:id` | 频道详情（FileResource 按作品分组，多选创建 Agent） |
| `/agents` | Agent 列表 |
| `/agents/new`, `/agents/:id/edit` | Agent 表单 |
| `/agents/:id` | Agent 详情（任务/待决策（含 AI 自动处理 + 批量）/过滤测试/订阅作品管理/运行历史） |
| `/downloaders` | 下载器列表 |
| `/downloaders/new`, `/downloaders/:id/edit` | 下载器表单（含 Transmission RPC 配置与默认下载目录） |
| `/downloaders/:id` | 下载器详情（Transmission 实时种子、速度） |
| `/series` | 剧集列表 |
| `/series/:id` | 剧集详情（含删除功能，Agent 引用检查） |
| `/movies` | 电影列表 |
| `/movies/:id` | 电影详情（含删除功能，Agent 引用检查） |

## 8. 关键设计决策

1. **单数据源 metadata 搜索**：MetadataAgent 不再执行 TMDB→Exa→Wikipedia 多级调用或 fallback。每次搜索必须选择唯一数据源：`exa`（默认 Exa Agent Search）、`tmdb`（TMDB API）、`wikipedia`（Wikipedia Python 库）。LLM 只基于所选数据源返回的证据做标题理解和结构化输出。`combined` 仅作为旧评测数据兼容值，运行时映射到默认 `exa`。
2. **Agent 直接订阅作品**：废弃 WatchEntry 模糊匹配，直接从 metadata 库选取作品订阅（最多 10 个），匹配更准确。未匹配的资源进入 suggestions，一键添加。
3. **树形 DSL 过滤器**：废弃扁平 ResourceFilter，用 bool/combinator 树支持 AND/OR/嵌套，参考 ES Query DSL 设计。
4. **职责分离**：Channel 负责"解析 + metadata 识别"；Agent 负责"订阅 + 过滤 + 推送"。metadata_agent_enabled 配在 Channel 上。
5. **ChannelRawTitleMapping 单独表**：手动修正的标题映射独立存储，支持精确/模糊匹配，便于审计和批量管理。
6. **海报本地缓存**：LLM 返回的海报 URL 下载到本地 `/posters/` 目录持久化，避免外链失效。
7. **幂等后台任务**：基于 Redis SETNX（或内存 map）做 dedup key，避免同一 channel/agent 重复运行。
8. **下载目录分层配置**：Downloader 定义下载服务器上的绝对根目录，Agent 可选定义相对子目录；DownloadTask 持久化创建时解析出的最终目录，避免后续配置变更影响历史任务重试和审计。RSSRipple 不修改 Transmission 的全局 session 下载目录，而是在每次 `torrent_add` 时传入任务级 `download_dir`。
9. **合集资源不参与去重与冲突**：`is_batch=True` 的资源绕过 `(series_id, episode)` 聚合，直接派发到下载器；不产生 PendingDecision。用户如需过滤，请通过 Filter DSL 的 `is_batch` / `episode_start` / `episode_end` 字段控制。此设计有意让用户可以同时保留单集与合集，避免因隐式 dedup 造成"漏下"。
10. **Channel 详情按作品分组分页**：`grouped=true` 时后端按作品分组分页而非按行分页，保证同一作品的所有资源始终出现在同一页，避免更新出现在不同分页里。`meta.total` 为 group 总数。
11. **Mock downloader 类型**：`DownloaderInstance.type = "mock"` 提供纯内存的下载器模拟器（连接测试恒真、`add_torrent` 立即返回、每个任务 1-10 秒随机完成），配合工厂 `get_downloader_client` 供本地开发和自动化测试使用；生产环境仍使用 `transmission` 类型。
12. **增量水位线 + 规则预览回填**：Agent 以 `last_consumed_at` 水位线做增量运行（只处理 `created_at > 水位线` 的资源，替代旧的 `limit(200)`），保证每条资源被且只被处理一次。规则变更（scope/filter/works）不经 rules-preview 决不静默回填：`POST /agents/rules-preview` 先算出新增匹配/不再匹配差异，用户在 BackfillPreviewModal 勾选回填资源，保存时以 `dispatch_resource_ids` 提交并推进水位线。历史资源回填始终是用户显式选择的结果。
13. **集号不确定走 PendingDecision**：`episode_confidence="ambiguous"` 的资源不再归入 AgentSuggestion，而是创建一条 PendingDecision（reason 以"集号不确定"前缀标记、跳过 LLM 候选选择），引导用户手动修正集号；修正后（`manual`）下一次运行自动把该过期决策标记为 `decided`，资源重新进入正常派发流程。
