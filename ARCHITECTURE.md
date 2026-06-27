# RSSRipple 架构文档

## 1. 项目简介

RSSRipple 是一个 RSS 订阅源聚合 + 智能筛选 + 自动推送到下载客户端（Transmission）的媒体资源自动下载工具。用于自动追番、追美剧、下载电影等场景。

## 2. 技术栈

| 层 | 技术选型 |
|---|---|
| Backend | Python 3.11+, FastAPI, SQLAlchemy 2.0 (async), APScheduler, aiosqlite |
| Frontend | React 18, TypeScript, Vite, Ant Design 5, react-router v6 |
| External | Transmission (RPC), LLM API (OpenAI-compatible chat/completions + web_search tool), feedparser |
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
- **Client 层**：外部服务封装（RSS 解析、Transmission RPC、LLM 调用）
- **Scheduler 层**：APScheduler 管理定时任务（频道抓取、进度同步、过期清理）
- **Task Queue 层**：异步后台任务队列（手动触发的 fetch/run），支持内存和 Redis 两种后端
- **数据层**：SQLAlchemy ORM，SQLite 默认

## 4. 核心数据实体

| 实体 | 职责 |
|---|---|
| Channel | RSS 订阅频道，含 field_mapping 与 metadata 搜索配置 |
| FileResource | RSS 解析后的单条资源（种子/磁力），关联到作品 |
| TVSeries / Movie | 作品 metadata 缓存（本地库），含海报缓存 |
| Episode | TVSeries 下的单集 |
| Agent | 订阅器，监听 Channel 更新，按过滤规则推送到下载器 |
| AgentWork | Agent 订阅的具体作品（从 metadata 库选取，最多 10 个） |
| DownloadTask | 下载任务记录 |
| PendingDecision | 多候选待决策项 |
| DownloaderInstance | Transmission 下载器实例配置 |
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
  → backfill_titles 提取 search_title
  → fetch_and_link_metadata (本地模糊 → LLM web-search)
  → 持久化 FileResource + 海报本地缓存
  → 通知所有订阅该 Channel 的 active Agent（enqueue agent run）
```

### 5.2 Agent 过滤 → 推送下载

```
Channel 通知 / 手动触发 → run_agent(agent)
  → 拉取该 Channel 下未处理的新 FileResource
  → 所有未链接 metadata 的资源一律归入 suggestions，不参与过滤/下载
  → scope_channel_wide=false 时：仅匹配 selected_works（series_id/movie_id）内的资源
  → scope_channel_wide=true 时：所有已链接 metadata 的资源都进入过滤
  → 执行 DSL 过滤树（combinator + conditions，支持 AND/OR/嵌套）
  → TV 作品：检查 episode 去重（Agent 维度）
  → 单候选 → 创建 DownloadTask → Transmission.add_torrent
  → 多候选 → 创建 PendingDecision（若 Agent 配置 ask）或自动选最优（若 auto）
```

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
  → 调用 LLM web-search 返回候选列表
  → 用户选择正确作品
  → 写入 FileResource.series_id/movie_id
  → 写入 Channel.raw_title_mappings（记忆映射）
  → 重新触发关联 Agent 的过滤
```

## 6. LLM 服务职责

| 能力 | 说明 |
|---|---|
| RSS 字段映射分析 (feed_analyzer) | 给定 RSS 原始 entries，输出 field_mapping JSON，支持 SSE 流式输出 |
| 标题清洗 (title_cleaner) | 从原始标题提取干净的 search_title，结果缓存到 MetadataCache |
| Title regex 生成 | 根据样本 titles 生成清洗正则 |
| Metadata 搜索 | web_search 工具搜索作品信息，返回结构化 metadata JSON |
| PendingDecision 建议（可选） | 多候选时 LLM 给出推荐选项及理由 |

## 7. 前端路由

| Route | 页面 |
|---|---|
| `/` | Dashboard（活跃下载按作品分组、待决策） |
| `/channels` | 频道列表 |
| `/channels/new`, `/channels/:id/edit` | 频道表单 |
| `/channels/:id` | 频道详情（FileResource 按作品分组，多选创建 Agent） |
| `/agents` | Agent 列表 |
| `/agents/new`, `/agents/:id/edit` | Agent 表单 |
| `/agents/:id` | Agent 详情（任务/待决策/过滤测试/订阅作品管理） |
| `/downloaders` | 下载器列表 |
| `/downloaders/new`, `/downloaders/:id/edit` | 下载器表单 |
| `/downloaders/:id` | 下载器详情（Transmission 实时种子、速度） |

## 8. 关键设计决策

1. **纯 LLM metadata 搜索**：不依赖 TMDB/TVDB，统一用带 web_search 的 LLM。简化配置，提升中文/小众资源识别率。
2. **Agent 直接订阅作品**：废弃 WatchEntry 模糊匹配，直接从 metadata 库选取作品订阅（最多 10 个），匹配更准确。未匹配的资源进入 suggestions，一键添加。
3. **树形 DSL 过滤器**：废弃扁平 ResourceFilter，用 bool/combinator 树支持 AND/OR/嵌套，参考 ES Query DSL 设计。
4. **职责分离**：Channel 负责"解析 + metadata 识别"；Agent 负责"订阅 + 过滤 + 推送"。metadata_source 配在 Channel 上。
5. **ChannelRawTitleMapping 单独表**：手动修正的标题映射独立存储，支持精确/模糊匹配，便于审计和批量管理。
6. **海报本地缓存**：LLM 返回的海报 URL 下载到本地 `/posters/` 目录持久化，避免外链失效。
7. **幂等后台任务**：基于 Redis SETNX（或内存 map）做 dedup key，避免同一 channel/agent 重复运行。
