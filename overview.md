# RSSRipple — Channel & Metadata 本地作品库设计逻辑分析

## 1. 项目定位

RSSRipple 是一个 **RSS 订阅聚合 + 智能筛选 + 自动推送下载** 的媒体资源工具，核心场景是自动追番/追剧/下载电影。

技术栈：Python 3.11+ / FastAPI / SQLAlchemy 2.0 async / APScheduler / React SPA 前端 / Transmission RPC 下载器。

---

## 2. 核心数据模型关系

### 2.1 Channel（订阅频道）

**文件**: `app/models/channel.py`

Channel 是整个系统的入口实体，负责 RSS 源的抓取配置和字段映射。

| 字段 | 类型 | 说明 |
|------|------|------|
| `name` | str | 频道名称 |
| `type` | enum | 当前仅 `"rss_feed"` |
| `url` | str | RSS Feed URL |
| `fetch_interval` | int | 定时抓取间隔（秒），默认 1800 |
| `status` | enum | `active` / `inactive` / `error` |
| `field_mapping` | JSON | LLM 生成或手动配置的字段映射规则（必填） |
| `metadata_agent_enabled` | bool | 是否启用统一 MetadataAgent（默认 true） |
| `last_fetched_at` | datetime | 上次抓取完成时间 |
| `last_fetch_status` | str | `success` / `failed` / `running` |
| `last_fetch_error` | str | 错误信息 |

**Relationships**: `file_resources` (一对多), `agents` (一对多), `raw_title_mappings` (一对多)

**设计要点**：
- `field_mapping` 是 Channel 的核心配置，格式为 `{list_locator: {source: "entries"}, field_mappings: {field: {source, regex?, group?, transform?}}}`，由 LLM 分析 RSS 结构后自动生成或用户手动配置
- `metadata_agent_enabled` 控制是否启用 LLM 辅助 metadata 匹配（Layer 4）
- Channel 负责"解析 + metadata 识别"，Agent 负责"订阅 + 过滤 + 推送"

### 2.2 FileResource（RSS 资源条目）

**文件**: `app/models/file_resource.py`

每条 RSS entry 解析后生成一个 FileResource，是 Channel 和 Metadata 之间的桥梁。

**关键字段分三组**：
1. **RSS 原始数据**: `guid`（去重键）, `title_raw`, `torrent_url`, `detail_url`, `published_at`
2. **字段映射提取结果**: `title_cn`, `title_en`, `search_title`, `subtitle_group`, `episode`, `season`, `resolution`, `source`, `video_codec`, `audio_codec`, `subtitle_type`, `container`, `file_size`
3. **Metadata 关联**: `series_id` → TVSeries, `movie_id` → Movie, `parsed_at`, `metadata_matched_at`

**FK 互斥规则**：`series_id` 和 `movie_id` 互斥——剧集资源只能关联 series，电影资源只能关联 movie，未识别资源两者均为 null。

**唯一约束**: `(channel_id, guid)` 联合唯一，用于跨频道去重。

### 2.3 Metadata 本地作品库

这是项目的核心设计之一——**本地缓存**外部 metadata 搜索结果，避免重复调用 LLM/外部 API。

#### TVSeries（剧集系列缓存）

**文件**: `app/models/series.py`

| 字段 | 说明 |
|------|------|
| `title_cn` / `title_en` / `original_title` | 多语言标题 |
| `aliases` | JSON list，自动积累合并（去重） |
| `external_id` / `external_source` | 外部 ID（TMDB/MAL/IMDb/Wikipedia ID）和来源标识 |
| `description` / `poster_url` / `rating` / `genre` | 作品详情 |
| `status` | 剧集状态（Ended / Returning Series 等） |
| `number_of_episodes` / `number_of_seasons` | 总集数/季数 |
| `start_date` / `end_date` | 首播/完结日期 |
| `content_type` | `"tv"` / `"anime"` / `"mixed"` |
| `canonical_name` / `wikipedia_url` / `wikipedia_page_id` | Wikipedia 补充字段 |

**Relationships**: `episodes` (一对多), `file_resources`, `agent_works`, `raw_title_mappings`, `pending_decisions`

#### Movie（电影缓存）

**文件**: `app/models/movie.py`

与 TVSeries 结构高度相似，区别：
- 用 `release_date` 代替 `start_date`
- 用 `runtime` 代替 `number_of_episodes/seasons`
- `content_type` 固定 `"movie"`

#### Episode（剧集单集缓存）

**文件**: `app/models/episode.py`

唯一约束 `(series_id, season, episode)`，记录每集的标题和播出日期。

#### MetadataCache（元数据缓存）

**文件**: `app/models/metadata_cache.py`

| 字段 | 说明 |
|------|------|
| `title` | 缓存 key：原始（未清洗）标题 |
| `source` | `"metadata_agent"`（当前主要）/ `"llm_title"`（旧版遗留） |
| `content_type` | `"tv"` / `"movie"` |
| `metadata_json` | 完整结果 dict |

唯一约束 `(title, source)`，避免对同一标题重复执行 LangGraph ReAct 循环。

#### ChannelRawTitleMapping（频道原始标题映射）

**文件**: `app/models/channel_raw_title_mapping.py`

用户手动修正 metadata 后，将 `raw_title` → 作品的映射落库。后续抓取同一频道相同原始标题的资源时直接查表，跳过所有匹配层。

唯一约束 `(channel_id, raw_title)`，支持 `search_title_override` 覆盖默认清洗结果。

---

## 3. 核心业务流程

### 3.1 RSS 抓取流程

**文件**: `app/services/fetch_service.py` → `fetch_channel_resources()`

```
APScheduler / 手动触发
  │
  ├─ 1. channel.last_fetch_status = "running"
  ├─ 2. feedparser 抓取 RSS（asyncio.to_thread, 30s 超时）
  │     失败 → channel.status="error", 记录错误
  ├─ 3. 遍历 entries:
  │     ├─ GUID 去重（查 existing_guids 集合）
  │     ├─ parse_entry(entry, channel.field_mapping) 解析结构化字段
  │     ├─ _extract_download_urls() 兜底提取 torrent_url
  │     ├─ 创建 FileResource, db.flush()
  │     ├─ 若 channel.metadata_agent_enabled:
  │     │     UnifiedMetadataAgent.process(resource, channel, db)
  │     │     → 标题清洗 + episode/season 推断 + 单数据源 metadata 搜索
  │     │     → 结果写入 resource.search_title/episode/season/series_id/movie_id
  │     │     → 通过 MetadataCache 缓存
  │     ├─ 否则: fetch_and_link_metadata(db, resource, channel) 本地匹配
  │     ├─ 海报下载: 若 poster_url 是 http(s) → 下载到 POSTER_CACHE_DIR
  │     └─ db.commit()
  ├─ 4. channel 状态更新: last_fetched_at, success, active
  └─ 5. 为该 channel 下所有 active Agent enqueue run_agent
```

**关键实现细节**：
- `torrent_url` 提取优先级：auto-detected (enclosures/magnets) > field_mapping
- MetadataAgent 失败时 fallback 到 `_simple_title_clean()` 做基础标题清洗
- 海报下载使用 sha256(url)[:16] 作为文件名，存储路径 `/posters/{hash}.{ext}`

### 3.2 Metadata 四层匹配流程

**文件**: `app/services/metadata_service.py` → `fetch_and_link_metadata()`

```
Layer 1: 已链接检查
  if resource.series_id or resource.movie_id: return

Layer 2: ChannelRawTitleMapping 精确匹配
  query by (channel_id, raw_title)
  → 写入 series_id/movie_id + search_title_override
  → return

Layer 3: 本地 DB 匹配
  search_title = resource.search_title or extract_search_title(resource)
  
  3a. 精确匹配: TVSeries.title_cn == search_title OR title_en == search_title
      → ratio = 100, auto-link
  
  3b. 模糊匹配: thefuzz fuzz.ratio(search_title, title) >= 70
      → ratio >= 85: auto-link
      → ratio 70-84: 跳过（太模糊，留 LLM 处理）

Layer 4: UnifiedMetadataAgent (仅当 channel.metadata_agent_enabled)
  → ReAct 循环: 标题清洗 → episode/season 推断 → 单数据源搜索
  → create_or_update_series_from_external / create_or_update_movie_from_external
  → 写入 resource FK
```

**`extract_search_title()` 策略**（同步，无 LLM）：
1. 优先 `title_cn` 或 `title_en`（field_mapping 已解析）
2. 正则清洗 `title_raw`：去除 `[字幕组]`、`- 集数`、`SxxExx`、尾部 `[质量标签]`

**`create_or_update_series_from_external()` upsert 逻辑**：
- 按 `external_id + external_source IN (data.source, 'llm_search')` 查询
- 存在 → 更新字段，合并 aliases（追加去重），迁移 `llm_search` → 新 source
- 不存在 → 创建新实体，下载海报

### 3.3 UnifiedMetadataAgent（统一 Metadata Agent）

**文件**: `app/services/metadata_agent.py`

基于 LangGraph ReAct 模式，单次调用完成：标题清洗 + episode/season 推断 + 单数据源 metadata 搜索。

**数据源策略**（单选，不级联）：
| 数据源 | 工具 | 适用场景 |
|--------|------|----------|
| `exa`（默认） | `search_exa_agent` | Web 证据覆盖面广，生产默认 |
| `tmdb` | `search_tmdb`, `get_tmdb_details` | 结构化影视库匹配 |
| `wikipedia` | `search_wikipedia`, `get_wikipedia_page` | 百科页面证据 |
| `combined` | 归一化为 `exa` | 旧版兼容 |

**核心类**: `UnifiedMetadataAgent`
- `process(resource, channel, db)` — 生产入口，写 DB
- `process_title_only(raw_title, data_source_type)` — 评测入口，无 DB
- `_agent_for_source(source)` — 按数据源构建 ReAct graph（工具受限）
- `_get_cache` / `_set_cache` — MetadataCache 读写
- `_apply_to_resource()` — 将 ResourceMetadata 写回 FileResource + upsert TVSeries/Movie

**ReAct 执行流程**：
1. 构建 user message（含 source mode 指引 + raw title + pre-parsed hints）
2. `create_react_agent(model, tools_for_source, prompt).ainvoke()`
3. 从 messages 中提取 `finalize` tool call 的 JSON 结果
4. 解析为 `ResourceMetadata` dataclass
5. 持久化到 DB + 缓存

**`ResourceMetadata` dataclass** 包含：clean_title, content_type, episode, season, 所有资源字段, matched_entity dict, confidence, ambiguous 标记, 搜索追踪信息。

### 3.4 手动 Metadata 修正流程

**文件**: `app/api/v1/resources.py`

```
用户点击"修正 metadata"
  │
  ├─ POST /resources/{id}/metadata/search
  │     body: { search_title, content_type, data_source_type? }
  │     → manual_search_metadata() → 返回候选列表（不落库）
  │
  ├─ 用户选择候选
  │
  └─ POST /resources/{id}/metadata/link
        body: { selected_result: {...} }
        → manual_link_metadata(db, resource, channel, selected_result)
          ├─ create_or_update_series/movie_from_external()
          ├─ resource.series_id/movie_id = entity.id
          ├─ resource.metadata_matched_at = now
          ├─ upsert ChannelRawTitleMapping (channel_id + raw_title)
          └─ db.commit()
        → enqueue run_agent for all active agents on this channel
```

### 3.5 Resource Parser（字段映射引擎）

**文件**: `app/services/resource_parser.py`

使用 Channel 的 `field_mapping` 配置，将 RSS entry dict 解析为 FileResource 字段。

**支持两种格式**：
- 新格式: `{list_locator: {...}, field_mappings: {field: {source, regex?, group?, transform?}}}`
- 旧格式（兼容）: `{field_name: {source, regex?, ...}, ...}`

**提取流程**：
1. `_resolve_source(entry, source_path)` — 支持点路径和数组索引（如 `enclosures[0].url`）
2. 可选 `regex` + `group` 正则提取
3. 可选 `transform`（`int` / `float` / `iso_datetime` / `lowercase` / `uppercase`）

---

## 4. 架构层次总结

```
┌─────────────────────────────────────────────────────┐
│                    API Layer                         │
│  channels.py / resources.py / agents.py / works.py  │
│  series.py / movies.py / decisions.py / tasks.py    │
└──────────────────────┬──────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────┐
│                 Service Layer                        │
│  fetch_service     — RSS 抓取 + 解析 + metadata 管线 │
│  metadata_service  — 4层匹配 + 实体 upsert + 海报    │
│  metadata_agent    — LangGraph ReAct 统一 agent     │
│  metadata_search_agent — Exa/TMDB/Wikipedia 工具后端  │
│  agent_service     — Agent 过滤 + 去重 + 下载派发    │
│  filter_engine     — BoolCondition DSL 求值          │
│  feed_analyzer     — LLM 分析 RSS 生成 field_mapping │
│  resource_parser   — 字段映射解析引擎                │
│  scheduler         — APScheduler 定时任务            │
│  task_queue        — Memory/Redis 异步队列           │
└──────────────────────┬──────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────┐
│                  Data Layer                          │
│  Channel / FileResource / TVSeries / Movie / Episode │
│  Agent / AgentWork / DownloadTask / PendingDecision  │
│  ChannelRawTitleMapping / MetadataCache              │
│  AgentSuggestion / DownloaderInstance                │
│  SQLAlchemy 2.0 async + SQLite/PostgreSQL            │
└─────────────────────────────────────────────────────┘
```

---

## 5. 关键设计决策

1. **职责分离**: Channel 负责"解析 + metadata 识别"；Agent 负责"订阅 + 过滤 + 推送"
2. **单数据源搜索**: MetadataAgent 每次只用一个数据源（exa/tmdb/wikipedia），不级联 fallback
3. **本地作品库缓存**: TVSeries/Movie 是外部 metadata 的本地缓存，通过 `external_id + external_source` 做 upsert
4. **四层匹配策略**: 已链接 → 原始标题映射 → 本地 DB（精确+模糊）→ LLM agent，逐层降级
5. **ChannelRawTitleMapping 记忆**: 手动修正后写入映射表，后续相同标题直接查表跳过匹配
6. **MetadataCache 缓存**: LLM 处理结果按 `(title, source)` 缓存，避免重复 ReAct 循环
7. **海报本地缓存**: LLM 返回的海报 URL 下载到 `/posters/` 目录，避免外链失效
8. **树形 DSL 过滤器**: 类 Elasticsearch bool query，支持 AND/OR/NOT 嵌套
9. **GUID 去重**: 跨频道以 `(channel_id, guid)` 唯一约束保证幂等
10. **AgentWork 直接订阅**: 废弃 WatchEntry 模糊匹配，直接从 metadata 库选取作品（最多 10 个）
