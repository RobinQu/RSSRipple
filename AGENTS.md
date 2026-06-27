# RSSRipple - 详细方案设计

本文档是 RSSRipple 项目实现的唯一权威设计来源。所有实现必须遵循本文档定义的数据模型、API、业务流程与交互规范。

---

## 数据模型

所有 ORM 模型使用 SQLAlchemy 2.0 风格声明，主键均为 UUID v4 字符串，时间字段均为 UTC 时区。

### Channel（订阅频道）

```python
class Channel(Base):
    __tablename__ = "channels"

    id: str                              # UUID 主键
    name: str                            # 频道名称
    type: str                            # 枚举值: "rss_feed"（当前唯一支持）
    url: str                             # RSS Feed URL
    fetch_interval: int                  # 定时抓取间隔（秒），默认 1800
    status: str                          # 枚举: "active" | "inactive" | "error"
    field_mapping: dict                  # LLM 生成或用户手动配置的字段映射规则（必填）
                                         # 格式: {list_locator: {source: "entries"},
                                         #        field_mappings: {field: {source, regex?, group?, transform?}}}
    title_extraction_method: str         # 标题清洗方式: "none" | "regex" | "llm"，默认 "none"
    title_extraction_regex: str | None   # 用户填入或 LLM 生成的标题清洗正则
    metadata_source: str                 # 元数据来源策略: "llm" | "none"，默认 "llm"
                                         # "llm" 表示本地匹配失败时回退到 LLM web-search
    last_fetched_at: datetime | None     # 上次抓取完成时间
    last_fetch_status: str | None        # 上次抓取状态: "success" | "failed"
    last_fetch_error: str | None         # 上次抓取错误信息
    created_at: datetime
    updated_at: datetime

    # Relationships
    file_resources: list[FileResource]
    raw_title_mappings: list[ChannelRawTitleMapping]
    agents: list[Agent]
```

### FileResource（RSS 资源条目）

```python
class FileResource(Base):
    __tablename__ = "file_resources"
    __table_args__ = (UniqueConstraint("channel_id", "guid"),)

    id: str                              # UUID
    channel_id: str → Channel            # 所属频道 FK
    guid: str                            # RSS entry 唯一标识，用于去重
    # RSS 原始数据
    title_raw: str                       # RSS 原始标题，未做任何清洗
    # 字段映射提取结果
    title_cn: str | None                 # 中文标题
    title_en: str | None                 # 英文标题
    search_title: str | None             # 清洗后用于搜索的标题
    subtitle_group: str | None           # 字幕组
    episode: int | None                  # 集数
    season: int | None                   # 季数
    resolution: str | None               # 分辨率 (1080p, 2160p, 720p...)
    source: str | None                   # 来源 (WebRip, WEB-DL, BDRip...)
    video_codec: str | None              # 视频编码 (HEVC, HEVC-10bit, AVC, H264...)
    audio_codec: str | None              # 音频编码 (AAC, FLAC, DTS, AC3...)
    subtitle_type: str | None            # 字幕类型 (CHS, CHT, 简繁内封, 外挂...)
    container: str | None                # 容器格式 (MKV, MP4)
    file_size: int | None                # 文件大小（bytes）
    torrent_url: str                     # 下载链接（magnet:?xt=... 或 .torrent URL）
    detail_url: str | None               # 详情页链接
    published_at: datetime | None        # RSS 发布时间
    # Metadata 关联（series/movie 用于定位作品；episode 字段用于定位剧集集数）
    series_id: str | None → TVSeries     # 关联剧集系列 FK
    movie_id: str | None → Movie         # 关联电影 FK
    parsed_at: datetime | None           # 字段映射解析完成时间
    metadata_matched_at: datetime | None # metadata 匹配完成时间
    created_at: datetime
```

资源的 FK 互斥规则：
- 若为剧集资源，`series_id` 非空，`movie_id` 必须为空；具体集数统一使用 `episode` 字段。
- 若为电影资源，`movie_id` 非空，`series_id` 必须为空。
- 未识别资源两个 FK 均为空。

### TVSeries（剧集系列 - Metadata 缓存）

```python
class TVSeries(Base):
    __tablename__ = "tv_series"

    id: str                              # UUID
    title_cn: str | None                 # 中文标题
    title_en: str | None                 # 英文标题
    original_title: str | None           # 原始标题（原名）
    aliases: list[str] | None            # 别名列表，自动积累合并（去重）
    external_id: str | None              # 外部 ID（LLM 搜索时返回的参考 ID，如 TMDB ID）
    external_source: str | None          # 枚举字符串: "llm_search" | "manual" | "local_match"
    description: str | None              # 简介
    poster_url: str | None               # 海报本地缓存路径，格式 /posters/{hash}.jpg
    rating: float | None                 # 评分（0-10）
    genre: list[str] | None              # 类型标签
    status: str | None                   # 剧集状态: "Ended" | "Returning Series" | "Canceled" 等
    number_of_episodes: int | None       # 总集数
    number_of_seasons: int | None        # 总季数
    start_date: date | None              # 首播日期
    end_date: date | None                # 完结日期
    content_type: str | None             # "tv" | "anime" | "mixed"
    created_at: datetime
    updated_at: datetime

    # Relationships
    episodes: list[Episode]
    file_resources: list[FileResource]
    agent_works: list[AgentWork]
```

### Movie（电影 - Metadata 缓存）

```python
class Movie(Base):
    __tablename__ = "movies"

    id: str                              # UUID
    title_cn: str | None
    title_en: str | None
    original_title: str | None
    aliases: list[str] | None
    external_id: str | None
    external_source: str | None          # 枚举: "llm_search" | "manual" | "local_match"
    description: str | None
    poster_url: str | None
    rating: float | None
    genre: list[str] | None
    status: str | None                   # "Released" | "Upcoming" 等
    release_date: date | None            # 上映日期（区别于 TVSeries 的 start_date）
    runtime: int | None                  # 片长（分钟）
    content_type: str | None             # "movie"
    created_at: datetime
    updated_at: datetime

    # Relationships
    file_resources: list[FileResource]
    pending_decisions: list[PendingDecision]
    agent_works: list[AgentWork]
```

### Episode（剧集单集 - Metadata 缓存）

```python
class Episode(Base):
    __tablename__ = "episodes"
    __table_args__ = (UniqueConstraint("series_id", "season", "episode"),)

    id: str                              # UUID
    series_id: str → TVSeries            # 所属系列 FK
    season: int                          # 季号
    episode: int                         # 集号
    title: str | None                    # 单集标题
    air_date: date | None                # 播出日期
    created_at: datetime
    updated_at: datetime
```

### Agent（智能代理）

> **替代说明**：本模型取代旧版的 ResourceFilter 表和 WatchEntry 表。过滤规则以 JSON DSL 树存于 `filter_config` 字段；订阅作品以独立子表 `agent_works`（见下）管理。旧的 `mode`（global/watchlist）、`metadata_source`、`content_type` 字段全部废弃。

```python
class Agent(Base):
    __tablename__ = "agents"

    id: str                              # UUID
    name: str                            # Agent 名称
    channel_id: str → Channel            # 关联频道 FK（必选）
    downloader_id: str → DownloaderInstance  # 关联下载器 FK（必选）
    download_subdir: str | None          # 可选：相对 Downloader.download_dir 的子目录
                                         # 示例 "Anime/2026-01"，禁止绝对路径、..、空段逃逸
    task_expire_days: int                # completed 任务自动清理天数，默认 30
    llm_enabled: bool                    # 是否启用 LLM 辅助决策（影响冲突自动解决建议）
    scope_channel_wide: bool             # true=订阅整个频道（仅靠 filter_config 过滤）
                                         # false=仅订阅 works 中的作品
    conflict_resolution: str             # 冲突处理策略: "ask" | "auto"，默认 "ask"
                                         # "ask"=多候选时创建 PendingDecision 等待用户
                                         # "auto"=按启发式评分自动选择最优资源
    filter_config: dict | None           # 过滤规则 DSL 树（BoolCondition 根节点，详见 Filter DSL 章节）
    status: str                          # "active" | "paused" | "error"
    last_run_at: datetime | None         # 上次运行时间
    last_run_status: str | None          # 上次运行状态
    created_at: datetime
    updated_at: datetime

    # Relationships
    channel: Channel
    downloader: DownloaderInstance
    works: list[AgentWork]               # 订阅作品列表（最多 10 个）
    suggestions: list[AgentSuggestion]   # 未识别资源建议分组（持久化）
    download_tasks: list[DownloadTask]
    pending_decisions: list[PendingDecision]
```

### AgentWork（订阅作品）

> **替代说明**：本表取代旧版 WatchEntry。每个 AgentWork 代表 Agent 订阅的一个作品（关联 TVSeries 或 Movie），可携带作品级别的过滤覆盖选项。AgentWork 最多 10 个（当 `scope_channel_wide=false` 时生效）。

```python
class AgentWork(Base):
    __tablename__ = "agent_works"
    __table_args__ = (
        CheckConstraint(
            "(series_id IS NOT NULL AND movie_id IS NULL) OR (series_id IS NULL AND movie_id IS NOT NULL)",
            name="chk_work_single_target",
        ),
    )

    id: str                              # UUID
    agent_id: str → Agent                # 所属 Agent FK
    content_type: str                    # "tv" | "movie"
    series_id: str | None → TVSeries     # 订阅剧集 FK（content_type="tv" 时非空）
    movie_id: str | None → Movie         # 订阅电影 FK（content_type="movie" 时非空）
    enable_episode_dedup: bool           # 是否启用剧集集数维度去重，默认 true
                                         # 仅 TV 作品有效；电影固定按 movie_id 去重
    filter_overrides: dict | None        # 作品级别的过滤覆盖（FieldCondition 列表或 BoolCondition）
                                         # 与全局 filter_config 按 AND 合并
    display_name_override: str | None    # 用户自定义展示名（默认取作品标题）
    created_at: datetime
    updated_at: datetime
```

### DownloadTask（下载任务）

```python
class DownloadTask(Base):
    __tablename__ = "download_tasks"

    id: str                              # UUID
    agent_id: str → Agent                # 所属 Agent FK
    file_resource_id: str → FileResource # 对应资源 FK
    downloader_id: str → DownloaderInstance  # 使用的下载器 FK
    download_dir: str                       # 创建任务时解析出的最终下载目录（绝对路径）
                                            # = downloader.download_dir[/agent.download_subdir]
    transmission_torrent_id: int | None  # Transmission 返回的 torrent ID
    status: str                          # "pending" | "queued" | "downloading" | "paused"
                                         # | "completed" | "error" | "cancelled"
    progress: float                      # 下载进度，0.0 ~ 1.0
    download_speed: int                  # 下载速度，bytes/s
    upload_speed: int                    # 上传速度，bytes/s
    eta: int | None                      # 预计剩余秒数
    error_message: str | None            # 错误信息
    retry_count: int                     # 已重试次数
    max_retries: int                     # 最大重试次数，默认 3
    confirmed_at: datetime | None        # 任务确认时间（pending→downloading）
    completed_at: datetime | None        # 任务完成时间
    created_at: datetime
    updated_at: datetime

    # Relationships
    agent: Agent
    file_resource: FileResource
    downloader: DownloaderInstance
```

### AgentSuggestion（Agent 未识别资源建议）

当 Agent 运行时遇到未链接 metadata 的资源，系统将按 `search_title/title_raw` 模糊聚类并持久化到本表，供前端展示和后续手动关联作品。

```python
class AgentSuggestion(Base):
    __tablename__ = "agent_suggestions"
    __table_args__ = (UniqueConstraint("agent_id", "sample_title"),)

    id: str                              # UUID
    agent_id: str → Agent                # 所属 Agent FK
    sample_title: str                    # 分组代表标题
    resources: list[str]                 # FileResource ID 列表
    status: str                          # "active" | "ignored" | "resolved"
    created_at: datetime
    updated_at: datetime
```

### PendingDecision（待决策项）

当同一作品的同一剧集（或同一电影）出现多个符合条件的候选资源，且 `conflict_resolution="ask"` 时创建。

```python
class PendingDecision(Base):
    __tablename__ = "pending_decisions"

    id: str                              # UUID
    agent_id: str → Agent                # 所属 Agent FK
    series_id: str | None → TVSeries     # 剧集系列 FK（TV 作品非空）
    movie_id: str | None → Movie         # 电影 FK（电影非空）
    episode: int | None                  # 集数（TV 作品）
    candidates: list[str]                # 候选 FileResource ID 列表（按匹配度预排序）
    reason: str                          # 需要决策的原因（如："多个资源匹配第03集"）
    llm_suggestion: str | None           # LLM 对候选的推荐与理由（llm_enabled=true 时填充）
    decided_resource_id: str | None      # 用户最终选择的资源 ID
    status: str                          # "pending" | "decided" | "expired" | "skipped"
    expires_at: datetime | None          # 过期时间（默认 7 天）
    created_at: datetime
    decided_at: datetime | None
```

### DownloaderInstance（下载器实例）

```python
class DownloaderInstance(Base):
    __tablename__ = "downloader_instances"

    id: str                              # UUID
    name: str                            # 下载器名称
    type: str                            # 枚举: "transmission"（当前唯一支持）
    url: str                             # Transmission RPC URL（如 http://127.0.0.1:9091/transmission/rpc）
    username: str | None                 # RPC 用户名
    password: str | None                 # RPC 密码
    download_dir: str                    # 默认下载目录（必填）
                                         # Transmission 下载服务器本地可读写的绝对路径
                                         # 支持该服务器 OS 的路径风格（POSIX/Windows/UNC）
    status: str                          # "connected" | "disconnected" | "error"
    last_checked_at: datetime | None     # 上次连通性检查时间
    created_at: datetime
    updated_at: datetime
```

### ChannelRawTitleMapping（频道原始标题映射）

用户手动修正 metadata 后，将原始标题与作品的映射落库；后续抓取同一频道相同原始标题的资源时直接查表，避免重复匹配。

```python
class ChannelRawTitleMapping(Base):
    __tablename__ = "channel_raw_title_mappings"
    __table_args__ = (UniqueConstraint("channel_id", "raw_title"),)

    id: str                              # UUID
    channel_id: str → Channel            # 所属频道 FK
    raw_title: str                       # RSS 原始标题（精确匹配 key）
    content_type: str | None             # "tv" | "movie"（可空，空时以 series_id/movie_id 为准）
    search_title_override: str | None    # 可选：覆盖默认 search_title（用户自定义清洗结果）
    series_id: str | None → TVSeries     # 映射到的剧集 FK
    movie_id: str | None → Movie         # 映射到的电影 FK
    created_at: datetime
    updated_at: datetime
```

### MetadataCache（元数据缓存）

仅用于 LLM 标题清洗缓存，不缓存 metadata 搜索结果（搜索结果直接落到 TVSeries/Movie 表）。

```python
class MetadataCache(Base):
    __tablename__ = "metadata_cache"
    __table_args__ = (UniqueConstraint("title", "source"),)

    id: str                              # UUID
    title: str                           # 缓存 key：原始（未清洗）标题
    source: str                          # 来源标识，固定为 "llm_title"
    content_type: str | None             # LLM 判断的内容类型
    metadata_json: dict                  # 缓存内容，格式 {"clean_title": "...", "content_type": "..."}
    created_at: datetime
    updated_at: datetime
```

---

## Filter DSL 规范

Agent 的 `filter_config` 和 AgentWork 的 `filter_overrides` 均使用统一的布尔查询 DSL，类 Elasticsearch bool query 结构。

### 类型定义

```
FilterConfig = BoolCondition

BoolCondition = {
  "combinator": "and" | "or",
  "conditions": Array<BoolCondition | FieldCondition>,
  "is_not": bool?   // 可选，对整个条件组取反，默认 false
}

FieldCondition = {
  "field": "subtitle_group" | "resolution" | "source" | "video_codec" |
           "audio_codec" | "subtitle_type" | "container" | "file_size" |
           "episode" | "season" | "title_cn" | "title_en" | "search_title",
  "operator": "eq" | "ne" | "contains" | "fuzzy" | "in" | "regex" |
              "gt" | "gte" | "lt" | "lte",
  "value": string | number | string[]
}
```

### 求值语义

- **BoolCondition**：
  - `combinator="and"`：`conditions` 中所有子条件均通过时，本组通过。
  - `combinator="or"`：`conditions` 中任一子条件通过时，本组通过。
  - `is_not=true`：对最终结果取反。
- **字段类型与 operator 支持**：
  - 数字字段（`file_size`, `episode`, `season`）支持：`eq`, `ne`, `gt`, `gte`, `lt`, `lte`, `in`。
  - 字符串字段（其余全部）支持：`eq`, `ne`, `contains`, `fuzzy`, `in`, `regex`。
- **operator 语义**（字符串比较均忽略大小写）：
  - `eq`：字段值等于 value（字符串去首尾空格后比较）。
  - `ne`：字段值不等于 value。
  - `contains`：字段值包含 value 子串。
  - `fuzzy`：使用 thefuzz `fuzz.ratio` >= 70 判定为匹配。
  - `in`：value 为字符串数组（或逗号分隔字符串拆分为数组），字段值命中任一元素（子串匹配，等价于多值 OR contains）。
  - `regex`：用 `re.search(pattern, field_value, re.IGNORECASE)` 匹配。
  - `gt/gte/lt/lte`：数值大小比较。
- **空值处理**：若字段值为 None/空：
  - 对于 `is_required` 语义由 DSL 外层决定——即空值时 `eq/contains/fuzzy/regex/gt/...` 判定为不通过；`ne` 判定为通过。
- **合并规则**：AgentWork 的 `filter_overrides` 若存在，则与全局 `filter_config` 按 AND 包装：
  ```json
  { "combinator": "and", "conditions": [agent.filter_config, work.filter_overrides] }
  ```
  若其中任一为 null，则直接使用另一个；两者均为 null 视为全部通过。

### 示例

**示例 1**：字幕组必须是"XX字幕组"或"YY字幕组"，且分辨率为 1080p 或 2160p。

```json
{
  "combinator": "and",
  "conditions": [
    { "field": "subtitle_group", "operator": "in", "value": ["XX字幕组", "YY字幕组"] },
    { "field": "resolution", "operator": "in", "value": ["1080p", "2160p"] }
  ]
}
```

**示例 2**：字幕组包含"动漫"且文件大于 1GB，或字幕组等于"官方"且分辨率为 2160p。

```json
{
  "combinator": "or",
  "conditions": [
    {
      "combinator": "and",
      "conditions": [
        { "field": "subtitle_group", "operator": "contains", "value": "动漫" },
        { "field": "file_size", "operator": "gte", "value": 1073741824 }
      ]
    },
    {
      "combinator": "and",
      "conditions": [
        { "field": "subtitle_group", "operator": "eq", "value": "官方" },
        { "field": "resolution", "operator": "eq", "value": "2160p" }
      ]
    }
  ]
}
```

**示例 3**：排除 MKV 以外的容器，且视频编码不是 AVC。

```json
{
  "combinator": "and",
  "conditions": [
    { "field": "container", "operator": "eq", "value": "mkv" },
    { "field": "video_codec", "operator": "ne", "value": "AVC" }
  ]
}
```

---

## API 端点设计

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
| GET | `/dashboard` | 概览数据：活跃 Agent 数、活跃下载（按 TVSeries/Movie 分组，无 metadata 的归入"未识别"组）、前 10 条 pending_decisions |

`GET /dashboard` 响应 `data` 结构：
```json
{
  "active_agents": 3,
  "active_channels": 2,
  "active_download_count": 12,
  "active_download_groups": [
    {
      "type": "series" | "movie" | "unknown",
      "id": "uuid-or-null",
      "title": "作品名或未识别",
      "poster_url": "/posters/xxx.jpg",
      "tasks": [ { "task_id": "...", "resource_title": "...", "progress": 0.5, "agent_id": "...", "agent_name": "...", "channel_id": "...", "channel_name": "..." } ]
    }
  ],
  "pending_decisions": [ { ... } ]
}
```

### Channels

| Method | Path | 说明 |
|--------|------|------|
| GET | `/channels` | 频道列表（分页） |
| POST | `/channels` | 创建频道（服务端校验 RSS URL 可达与格式合法性） |
| GET | `/channels/form-token` | 获取表单防重复提交 Token（一次有效，存服务端 Cache） |
| GET | `/channels/{id}` | 频道详情（含最近 20 条 FileResource 预览） |
| PUT | `/channels/{id}` | 更新频道（含 field_mapping/title_extraction/metadata_source 等所有字段，一次性保存） |
| DELETE | `/channels/{id}` | 删除频道（级联删除其 file_resources、agents、tasks、mappings） |
| POST | `/channels/{id}/fetch` | 手动触发抓取（入队，返回 task_id） |
| GET | `/channels/{id}/fetch-status` | 轮询抓取任务状态（running/success/failed + 进度信息） |
| POST | `/channels/{id}/analyze` | 非流式 LLM 分析 RSS，返回 field_mapping（阻塞等待直到完成或超时） |
| POST | `/channels/{id}/analyze-stream` | SSE 流式 LLM 分析（delta/done/error 事件） |
| POST | `/channels/{id}/generate-title-regex` | LLM 根据若干已解析资源样例生成标题清洗正则 |
| POST | `/channels/{id}/summarize-filters` | 给定若干资源 ID，统计特征并生成 FilterConfig 片段建议 |
| POST | `/channels/validate-url` | 验证 RSS URL 可达性与格式（创建前校验） |
| POST | `/channels/preview-feed` | 预览 RSS 源，可选附带 field_mapping 预览解析结果（不落库） |
| POST | `/channels/analyze-url-stream` | 基于 URL 的 SSE 流式分析（创建频道前使用，无需 channel_id） |

`POST /channels/{id}/summarize-filters` 请求体：`{ "resource_ids": ["...", "..."] }`；响应 `data`：`{ "filter_config": { ...BoolCondition... }, "explanation": "..." }`。

### Agents

| Method | Path | 说明 |
|--------|------|------|
| GET | `/agents` | Agent 列表（分页） |
| POST | `/agents` | 创建 Agent，body 含 `filter_config`、`works`（AgentWork 列表，最多 10 个） |
| GET | `/agents/{id}` | Agent 详情（含 works、统计信息） |
| PUT | `/agents/{id}` | 更新 Agent（整体替换，含 works 列表） |
| DELETE | `/agents/{id}` | 删除 Agent（级联删除其 works、pending_decisions；tasks 标记 cancelled） |
| POST | `/agents/{id}/run` | 手动触发处理（入队处理该 Agent 频道下未处理资源） |
| GET | `/agents/{id}/run-status` | 轮询处理状态 |
| POST | `/agents/{id}/test-filters` | 给定资源或全部资源测试 filter_config 匹配情况，返回匹配结果明细 |
| GET | `/agents/{id}/suggestions` | 读取持久化的未识别资源建议分组，供用户一键添加为订阅作品 |

`POST /agents` 请求体示例：
```json
{
  "name": "新番自动下载",
  "channel_id": "...",
  "downloader_id": "...",
  "download_subdir": "Anime/新番",
  "scope_channel_wide": false,
  "conflict_resolution": "ask",
  "llm_enabled": true,
  "filter_config": { "combinator": "and", "conditions": [ { "field": "resolution", "operator": "in", "value": ["1080p","2160p"] } ] },
  "works": [
    { "content_type": "tv", "series_id": "...", "enable_episode_dedup": true, "filter_overrides": null }
  ]
}
```

`POST /agents/{id}/test-filters` 请求体：`{ "resource_ids": ["..."]? }`（不传则测试最近 50 条资源）；响应返回每条资源是否通过及每个条件的命中情况。

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
| DELETE | `/downloaders/{id}` | 删除下载器（若仍有关联 Agent，则返回 409，需先修改/删除 Agent） |
| POST | `/downloaders/{id}/test` | 测试 Transmission RPC 连通性，并用 `free_space(download_dir)` 检查默认下载目录，更新 status |
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
| GET | `/agents/{agent_id}/tasks` | Agent 的下载任务（分页，可按 status 过滤） |
| GET | `/tasks/{id}` | 任务详情（含 file_resource、agent、channel 信息） |
| POST | `/tasks/{id}/pause` | 暂停（调用 Transmission RPC） |
| POST | `/tasks/{id}/resume` | 恢复 |
| POST | `/tasks/{id}/retry` | 重试（重置 retry_count，重新添加 torrent） |
| DELETE | `/tasks/{id}` | 删除任务；query 参数 `delete_data=false` 控制是否同时删除 Transmission 中已下载数据 |

任务重试规则：`POST /tasks/{id}/retry` 必须优先使用该任务已持久化的 `download_dir` 重新添加 torrent，而不是重新读取当前 Agent/Downloader 配置；这样 Downloader 默认目录或 Agent 子目录后续变更不会改变历史任务的落点。

### Pending Decisions

| Method | Path | 说明 |
|--------|------|------|
| GET | `/agents/{agent_id}/decisions` | 待决策列表（分页，可按 status 查询） |
| POST | `/decisions/{id}/confirm` | 确认选择某个候选资源 → 推送下载 |
| POST | `/decisions/{id}/skip` | 跳过本次决策（标记 skipped） |

`POST /decisions/{id}/confirm` 请求体：`{ "resource_id": "uuid" }`。

### File Resources

| Method | Path | 说明 |
|--------|------|------|
| GET | `/channels/{channel_id}/resources` | 频道资源列表（分页；`?grouped=true` 时按 TVSeries/Movie 分组，无 metadata 的归入"未识别"组） |
| GET | `/resources/{id}` | 资源详情 |
| GET | `/resources/{id}/metadata` | 获取 metadata（若未链接则触发自动匹配流程，返回匹配结果；匹配中返回 status=processing 可轮询） |
| POST | `/resources/{id}/metadata/search` | 手动 LLM 搜索：`{ "search_title": "...", "content_type": "tv"|"movie" }` → 返回候选列表 |
| PUT | `/resources/{id}/metadata/link` | 手动确认关联：`{ "selected_result": { ... } }` → 创建/更新 TVSeries/Movie，写入 resource FK，写入 ChannelRawTitleMapping，重新触发 Agent 过滤 |

`POST /resources/{id}/metadata/search` 响应 `data`：
```json
{
  "results": [
    {
      "title_cn": "...", "title_en": "...", "original_title": "...",
      "description": "...", "poster_url": "https://...", "year": 2024,
      "external_id": "...", "content_type": "tv"
    }
  ]
}
```

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

---

## 核心业务逻辑

### RSS 抓取流程（fetch_service）

入口：`fetch_channel_resources(channel_id: str)`，由定时任务或手动触发入队。

```
fetch_channel_resources(channel, db)
  │
  ├─ 1. 更新 channel.last_fetch_status = "running"
  │
  ├─ 2. 使用 feedparser 在 asyncio.to_thread 中抓取 RSS（超时 30s）
  │     ├─ 抓取失败 → 标记 channel.status="error"、记录 last_fetch_error → 返回
  │
  ├─ 3. 遍历 entries：
  │     ├─ a. 计算 guid（缺则用 link 或 title_raw 兜底）；查询是否已存在 → 跳过
  │     ├─ b. parse_entry(entry, channel.field_mapping) → 解析出各字段
  │     ├─ c. 兜底提取 torrent_url：从 enclosure/link 中找 magnet 或 .torrent
  │     ├─ d. 创建 FileResource 对象（parsed_at = now）
  │     ├─ e. 标题清洗 backfill_titles(resource):
  │     │     search_title = extract_search_title(resource)  # 去字幕组/分辨率/编码等尾缀
  │     │     if method == "regex" and regex:
  │     │         search_title = clean_title_regex(search_title, regex)
  │     │     elif method == "llm":
  │     │         查 MetadataCache(source="llm_title", title=title_raw)
  │     │         命中 → 取 clean_title
  │     │         未命中 → 调 LLM → 写缓存 → 取 clean_title
  │     │     resource.search_title = search_title
  │     ├─ f. fetch_and_link_metadata(resource, channel)  # 详见 Metadata 匹配流程
  │     ├─ g. 若 LLM 返回 poster_url 且为 http(s) URL → 下载到 POSTER_CACHE_DIR
  │     │     文件名: {sha256(url)[:16]}.{ext}，保存路径相对于 POSTER_CACHE_DIR
  │     │     更新 series.poster_url = /posters/xxx.jpg
  │     └─ h. db 批量提交
  │
  ├─ 4. 更新 channel.last_fetched_at = now, last_fetch_status="success",
  │        status = "active", last_fetch_error = null
  │
  └─ 5. 为该 channel 下所有 status="active" 的 Agent enqueue run_agent
```

### Metadata 匹配流程（metadata_service）

入口：`fetch_and_link_metadata(db, resource, channel)`。

```
fetch_and_link_metadata(resource, channel, db)
  │
  ├─ Layer 1: 已链接 → 直接返回
  │     if resource.series_id or resource.movie_id: return
  │
  ├─ Layer 2: ChannelRawTitleMapping 精确匹配
  │     mapping = db.query(ChannelRawTitleMapping).filter_by(
  │         channel_id=channel.id, raw_title=resource.title_raw
  │     ).first()
  │     if mapping:
  │         写入 resource.series_id/movie_id
  │         若 mapping.search_title_override: resource.search_title = 覆盖值
  │         return
  │
  ├─ Layer 3: 本地 DB 精确/模糊匹配
  │     search_title = resource.search_title or resource.title_raw
  │     candidates_series = []
  │     candidates_movie = []
  │
  │     # 精确匹配 title_cn / title_en
  │     exact = query(TVSeries).filter(
  │         or_(TVSeries.title_cn == search_title, TVSeries.title_en == search_title)
  │     ).all()
  │     if exact: pick best → 写入 resource.series_id; return
  │     # Movie 同理
  │
  │     # 模糊匹配 title_cn/title_en/aliases，fuzz.ratio >= 70
  │     fuzzy_hits = []
  │     for series in all_series:
  │         titles = [series.title_cn, series.title_en, *(series.aliases or [])]
  │         best_ratio = max(fuzz.ratio(search_title, t) for t in titles if t)
  │         if best_ratio >= 70: fuzzy_hits.append( (best_ratio, series) )
  │     if fuzzy_hits:
  │         按 ratio 降序取 top1；若 top1 ratio >= 85 自动链接
  │         否则跳过（留 LLM 层处理，避免误匹配）
  │     # Movie 同理
  │
  ├─ Layer 4: LLM web-search（仅当 channel.metadata_source == "llm" 时执行）
  │     results = search_metadata_via_llm(search_title)
  │     if results 非空:
  │         best = results[0]  # LLM 已按相关性排序
  │         if best.content_type == "tv":
  │             series = create_or_update_series_from_external(db, best)
  │             resource.series_id = series.id
  │         else:
  │             movie = create_or_update_movie_from_external(db, best)
  │             resource.movie_id = movie.id
  │         return
  │
  └─ 全部失败 → resource 保持未链接（series_id/movie_id 均为 null）
```

`create_or_update_series_from_external(db, data)` 逻辑：
- 按 `external_id + external_source="llm_search"` 查询是否已存在。
- 存在 → 更新字段（合并 aliases：新别名 append 去重；poster_url 若本地缺失则下载）。
- 不存在 → 创建新 TVSeries，`external_source="llm_search"`。
- 返回实体。

`create_or_update_movie_from_external` 同理。

### Agent 过滤流程（agent_service）

入口：`process_resources(agent: Agent, channel: Channel, db)`，处理该 channel 下最近未处理或全部 pending 的新资源。

```
process_resources(agent, resources, db)
  │
  ├─ 1. 加载 agent.works → active_works
  │     work_by_series_id = { w.series_id: w for w in active_works if w.series_id }
  │     work_by_movie_id = { w.movie_id: w for w in active_works if w.movie_id }
  │
  ├─ 2. 初始化:
  │     candidates_by_key: dict[(type, id, episode?), list[FileResource]] = defaultdict(list)
  │     suggestions: list[未识别资源分组] = []
  │
  ├─ 3. 对每个 resource:
    │     │
    │     ├─ a. Metadata 前置检查:
    │     │     无论 scope_channel_wide 是否为 true，若 resource 未链接 metadata
    │     │     （series_id 和 movie_id 均为 null），则不参与过滤/下载，
    │     │     归入 suggestions bucket，等待用户手动修正。
    │     │
    │     ├─ b. Work 范围判定:
    │     │     if not agent.scope_channel_wide:
    │     │         if resource.series_id in work_by_series_id:
    │     │             work = work_by_series_id[resource.series_id]
    │     │         elif resource.movie_id in work_by_movie_id:
    │     │             work = work_by_movie_id[resource.movie_id]
    │     │         else:
    │     │             continue  # 不在订阅作品列表内，跳过
    │     │     else:
    │     │         work = None  # channel-wide 模式，所有已链接 metadata 的资源都进入下一步过滤
  │     │
  │     ├─ b. 构造有效 filter_config:
  │     │     effective_filter = merge_filter(agent.filter_config, work.filter_overrides if work else None)
  │     │
  │     ├─ c. 评估 effective_filter（evaluate_filter_config）
  │     │     if not passes → continue
  │     │
  │     ├─ d. 去重检查:
  │     │     if resource.movie_id:  # 电影
  │     │         exists = db.query(DownloadTask).filter(
  │     │             DownloadTask.agent_id == agent.id,
  │     │             DownloadTask.status.in_(["pending","queued","downloading","paused","completed"]),
  │     │             DownloadTask.file_resource.has(movie_id=resource.movie_id)
  │     │         ).first()
  │     │         if exists → continue
  │     │         key = ("movie", resource.movie_id, None)
  │     │     else:  # TV
  │     │         dedup = work.enable_episode_dedup if work else True
  │     │         if dedup and resource.episode is not None:
  │     │             exists = db.query(DownloadTask).filter(
  │     │                 DownloadTask.agent_id == agent.id,
  │     │                 DownloadTask.status.in_(["pending","queued","downloading","paused","completed"]),
  │     │                 DownloadTask.file_resource.has(
  │     │                     series_id=resource.series_id,
  │     │                     episode=resource.episode
  │     │                 )
  │     │             ).first()
  │     │             if exists → continue
  │     │         key = ("series", resource.series_id, resource.episode)
  │     │
  │     └─ e. candidates_by_key[key].append(resource)
  │
  ├─ 4. 候选聚合处理:
  │     for key, candidates in candidates_by_key.items():
  │         if len(candidates) == 1:
  │             dispatch_download(agent, candidates[0])
  │         else:
  │             if agent.conflict_resolution == "ask":
  │                 创建 PendingDecision(candidates=[...], reason="...")
  │             else:  # "auto"
  │                 chosen = score_and_pick(candidates, agent, work)
  │                 # 启发式评分：命中 filter_overrides 字段多的 > 分辨率高（2160p>1080p>720p）
  │                 #         > 文件体积大 > 发布时间新；llm_enabled 时可请 LLM 给出建议
  │                 dispatch_download(agent, chosen)
  │
  ├─ 5. Suggestions 聚合:
  │     将未识别但标题有意义的资源按 search_title 模糊聚类，
  │     保存到 AgentSuggestion 表，供前端一键添加作品
  │
  └─ 6. 返回 RunResult（新下载数、待决策数、跳过数、建议数）
```

`dispatch_download(agent, resource)`：
1. 创建 `DownloadTask(status="pending")`，写入 db。
2. 解析下载目录：`effective_download_dir = join(downloader.download_dir, agent.download_subdir)`；若 `download_subdir` 为空则直接使用 `downloader.download_dir`。
3. 校验 `download_subdir`：必须是相对路径，禁止绝对路径、`..`、空段逃逸、控制字符；标准化后不得跳出 `downloader.download_dir`。
4. 将 `effective_download_dir` 写入 `DownloadTask.download_dir`，用于审计、重试与后续配置变更隔离。
5. 调用 `TransmissionWrapper.add_torrent(resource.torrent_url, download_dir=effective_download_dir)`。
6. 成功 → 更新 `task.status="downloading"`, `task.transmission_torrent_id=返回值`, `task.confirmed_at=now`。
7. 失败 → 更新 `task.status="error"`, `task.error_message=异常信息`；触发重试逻辑（若 retry_count < max_retries 则入队重试）。

### 手动 metadata 搜索与修正流程

```
用户在资源详情点击"修正 metadata"
  │
  ├─ 1. 用户输入 search_title、选择 content_type (tv/movie)
  │
  ├─ 2. 前端 POST /resources/{id}/metadata/search
  │     后端调 LLM web-search → 返回候选列表（不含本地落库）
  │
  ├─ 3. 用户选择一个候选并确认
  │
  ├─ 4. 前端 PUT /resources/{id}/metadata/link { selected_result: {...} }
  │     后端:
  │       a. if content_type == "tv":
  │              series = create_or_update_series_from_external(db, selected_result)
  │              resource.series_id = series.id; resource.movie_id = null
  │          else:
  │              movie = create_or_update_movie_from_external(db, selected_result)
  │              resource.movie_id = movie.id; resource.series_id = null
  │       b. 若有海报 URL → 异步下载海报到本地
  │       c. 写入 ChannelRawTitleMapping（upsert by channel_id+raw_title）:
  │              raw_title = resource.title_raw
  │              content_type = selected_result.content_type
  │              series_id / movie_id = 对应实体 id
  │       d. resource.metadata_matched_at = now
  │       e. db.commit()
  │       f. enqueue run_agent 为该 channel 下所有 active agent（重新触发过滤）
  │
  └─ 5. 返回更新后的 resource 详情
```

### Schedule 调度

APScheduler 在 FastAPI lifespan 启动时初始化（使用 AsyncIOScheduler）。

```
startup:
  │
  ├─ 1. 查询所有 status="active" 的 Channel:
  │     for ch in channels:
  │         scheduler.add_job(
  │             func=enqueue_fetch,
  │             trigger=IntervalTrigger(seconds=ch.fetch_interval),
  │             id=f"channel:{ch.id}",
  │             args=[ch.id],
  │             next_run_time=datetime.utcnow() + 5,  # 启动 5s 后首次执行
  │             replace_existing=True,
  │         )
  │
  ├─ 2. Channel CRUD 时动态调整:
  │     创建/更新 → active? add_job/reschedule_job : remove_job
  │     删除/paused → remove_job
  │
  ├─ 3. 全局每分钟任务:
  │     sync_download_progress()
  │
  ├─ 4. 全局每小时任务:
  │     check_downloader_connections()  # 调用 POST /downloaders/{id}/test
  │
  └─ 5. 全局每日任务:
        cleanup_expired_tasks()  # 删除 completed 且 completed_at < now - task_expire_days 的任务
        expire_pending_decisions()  # 过期 pending decision → status="expired"
```

任务队列使用 MemoryQueue（默认）或 RedisQueue（配置时），用于承载手动触发的 fetch/run；APScheduler 定时任务也通过 enqueue 投递到同一队列，保证同一 Channel/Agent 的任务串行执行（分布式锁，避免重复运行）。

### 下载状态同步

每分钟由定时任务调用：

```
sync_download_progress():
  │
  ├─ 1. 查询所有 status in ("downloading","queued","pending") 的 DownloadTask
  │     按 downloader_id 分组，减少 RPC 调用
  │
  ├─ 2. 对每个 downloader:
  │     try:
  │         torrents = TransmissionWrapper(downloader).get_all_torrents()
  │         torrent_map = {t.id: t for t in torrents}
  │         for task in downloader.tasks:
  │             t = torrent_map.get(task.transmission_torrent_id)
  │             if t is None:
  │                 task.status = "cancelled"
  │                 continue
  │             task.progress = t.percent_done
  │             task.download_speed = t.rate_download
  │             task.upload_speed = t.rate_upload
  │             task.eta = t.eta
  │             if t.is_finished or t.left_until_done == 0:
  │                 task.status = "completed"
  │                 task.completed_at = now
  │             elif t.status == "stopped":
  │                 task.status = "paused"
  │             elif t.status in ("downloading","queued"):
  │                 task.status = "downloading" if t.rate_download > 0 else "queued"
  │     except TransmissionError as e:
  │         for task in downloader.tasks:
  │             task.status = "error"
  │             task.error_message = f"Transmission unreachable: {e}"
  │
  └─ 3. db.commit()
```

---

## 前端路由与页面设计

| Route | Page | 内容说明 |
|-------|------|----------|
| `/` | Dashboard | 顶部统计卡（活跃 Agent/活跃频道/下载中/待决策）；活跃下载按作品分组卡片（卡片含 poster、作品名、该作品下任务列表；任务行显示资源标题、进度、速度、Agent 与 Channel 链接可点击跳转）；待决策列表，支持快速 confirm/skip |
| `/channels` | ChannelList | 频道列表表格（名称/状态/抓取间隔/上次抓取/资源数/Agent 数）；支持新建、编辑、删除、手动抓取 |
| `/channels/new` | ChannelForm | 创建频道表单（URL 验证、自动 LLM 分析）；右侧 RSS 预览 |
| `/channels/:id` | ChannelDetail | 顶部频道信息+抓取控制按钮；主体资源按作品分组展示（每组可折叠，含 poster、作品名、剧集数、最新更新时间）；"未识别"组单独展示，点资源可唤起 metadata 修正抽屉；表格多选 → "生成过滤规则"弹窗（后端调用 summarize-filters，返回建议 FilterConfig，可编辑）→ 可选"新建 Agent"或"应用到已有 Agent" |
| `/channels/:id/edit` | ChannelForm | 编辑频道表单，包含 field_mapping 可视化编辑器、标题清洗正则测试器、metadata_source 开关 |
| `/agents` | AgentList | Agent 列表（名称/频道/下载器/状态/作品数/任务数） |
| `/agents/new` | AgentForm | 创建 Agent：选择 Channel + Downloader；可选填写下载子目录；scope_channel_wide 开关；可视化 Filter DSL 编辑器；订阅作品选择器（从频道的已识别作品中多选，最多 10 个） |
| `/agents/:id` | AgentDetail | Tab 布局：订阅作品管理 Tab（列表/新增/移除/编辑 per-work 覆盖）；下载任务 Tab（按状态过滤、操作按钮 pause/resume/retry/delete）；待决策 Tab（confirm/skip 操作）；过滤器编辑器 Tab（可视化树形 bool-query 构建器 + 测试面板）；运行控制 Tab（手动 run、状态轮询） |
| `/downloaders` | DownloaderList | 下载器列表 |
| `/downloaders/new` | DownloaderForm | 创建 Transmission 实例，填写默认下载目录，含测试连接按钮 |
| `/downloaders/:id` | DownloaderDetail | 连接状态；实时速度与总量统计；Transmission 种子列表（直连 RPC 实时刷新）；本地 DownloadTask 分页 |
| `/downloaders/:id/edit` | DownloaderForm | 编辑下载器与默认下载目录 |
| `/series` | SeriesList | 剧集列表，支持模糊搜索 |
| `/series/:id` | SeriesDetail | 剧集详情，资源列表、任务列表、相关 Agent 列表 |
| `/movies` | MovieList | 电影列表 |
| `/movies/:id` | MovieDetail | 电影详情 |

### 关键交互说明

- **Filter DSL 编辑器**：前端使用树形 UI，支持 AND/OR 节点嵌套、添加/删除/拖拽条件节点；每个字段条件提供字段名下拉、operator 下拉（根据字段类型动态展示可用 operator）、value 输入（in 模式下多标签输入）；提供"测试"按钮调用 `/agents/{id}/test-filters` 实时预览当前频道资源匹配情况。
- **Channel 详情多选生成规则**：用户在资源表格勾选若干符合预期的资源，点击"生成过滤规则"，前端将选中 resource_ids 发送到后端 `summarize-filters`，后端统计这些资源的字幕组/分辨率/编码/来源等字段的共同特征，生成建议 FilterConfig 返回；前端展示 JSON/可视化两种视图供用户微调，然后选择新建 Agent 或追加到现有 Agent 的 filter_config（追加时用 and 包装）。
- **资源详情抽屉**：Channel 详情点击资源行打开右侧抽屉，展示 poster（若有）、metadata（作品名、集数）、解析字段明细、磁力链接复制按钮、"修正 metadata"按钮；点击修正进入手动 metadata 流程：输入搜索词+选择类型→查看 LLM 候选→确认→自动刷新该资源及其相关分组。
- **待决策卡片**：Dashboard 和 Agent 详情的待决策项展示候选资源的核心字段对比（字幕组/分辨率/编码/体积/发布时间），llm_enabled 时展示 LLM 推荐理由；点击候选选中，点击"确认"提交。

---

## 错误处理规范

### 响应示例

```json
// 成功
{
  "success": true,
  "data": { "id": "...", "name": "..." },
  "error": null,
  "meta": {}
}

// 失败
{
  "success": false,
  "data": null,
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "downloader_id is required",
    "details": { "field": "downloader_id" }
  },
  "meta": {}
}

// 500 内部错误（dev_mode=true 时带 stack trace）
{
  "success": false,
  "data": null,
  "error": {
    "code": "INTERNAL_SERVER_ERROR",
    "message": "Unexpected error",
    "stack": "Traceback (most recent call last): ..."
  },
  "meta": {}
}
```

### 错误码清单

| 错误码 | HTTP 状态码 | 说明 |
|--------|-------------|------|
| `NOT_FOUND` | 404 | 请求的资源不存在 |
| `VALIDATION_ERROR` | 422 | 请求参数验证失败（字段缺失/格式错误/枚举非法） |
| `INVALID_FEED` | 422 | RSS URL 无效、不可达或解析失败 |
| `DUPLICATE_SUBMISSION` | 409 | 表单 Token 已被使用或重复提交 |
| `ALREADY_RUNNING` | 409 | 该 Channel/Agent 的后台任务已在执行中 |
| `TRANSMISSION_ERROR` | 502 | Transmission RPC 连接失败或操作失败（含认证失败、磁盘不足等） |
| `LLM_ERROR` | 502 | LLM 调用失败（未配置 Key、超时、响应解析失败） |
| `INTERNAL_SERVER_ERROR` | 500 | 未预期错误；dev_mode 下附 stack trace，生产环境隐藏 |

### 全局异常处理

- 所有 HTTP 异常（RequestValidationError、HTTPException）由全局 exception handler 转换为统一响应格式。
- 未捕获异常统一转换为 `INTERNAL_SERVER_ERROR`，并记录日志（含 request_id、用户、URL、堆栈）。
- SSE 流式端点（`analyze-stream`、`analyze-url-stream`）发生错误时发送 `event: error` 事件：`data: {"code": "...", "message": "..."}`。
- Task queue 中的任务异常被捕获并记录到对应 Channel/Agent 的 `last_fetch_error`/`last_run_status` 字段，不抛出到全局。

---

## 其他约定

- **时间格式**：API 中所有时间均为 ISO 8601 UTC 字符串（如 `2025-01-01T12:00:00Z`）。
- **下载目录格式**：
  - `DownloaderInstance.download_dir` 必填，必须是 Transmission 下载服务器 OS 可识别的绝对路径；路径语义以 Transmission daemon 为准，而不是 RSSRipple 后端进程所在主机为准。
  - 支持 POSIX absolute path、Windows drive absolute path、daemon 支持的 UNC path；后端校验时需要按路径风格识别根目录。
  - `Agent.download_subdir` 可空；非空时必须是相对路径，禁止以 `/`、`\`、`~`、Windows drive prefix（如 `C:\`）、UNC prefix（如 `\\server\share`）开头，禁止 `..` 段和控制字符。
  - 子目录 API 表达推荐使用 `/` 分隔；后端根据 Downloader 根目录风格拼接，标准化后必须保证最终路径仍在 `DownloaderInstance.download_dir` 下。
  - `DownloadTask.download_dir` 保存创建任务时解析出的最终绝对路径；任务重试沿用该字段。
- **Transmission 目录 RPC 使用**：RSSRipple 不调用 `session_set(download_dir=...)` 修改 Transmission 全局默认目录；所有自动下载都通过 `torrent_add(..., download_dir=DownloadTask.download_dir)` 设置单个任务目录。
- **配置项**（环境变量）：`DATABASE_URL`、`REDIS_URL`（可选）、`LLM_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL`（用于 feed 分析、标题清洗、正则生成、PendingDecision 建议）、`LLM_SEARCH_MODEL`（用于 metadata web-search，需支持 web_search 工具，如 perplexity/sonar-pro；与 LLM_MODEL 相同时填同一值）、`POSTER_CACHE_DIR`（默认 `./data/posters`）、`TRANSMISSION_TIMEOUT`、`DEV_MODE`（默认 false）。
- **海报服务**：FastAPI 挂载 StaticFiles 到 `/posters`，物理目录为 `POSTER_CACHE_DIR`。
- **日志**：结构化 JSON 日志，含 `request_id`、`channel_id`、`agent_id`、`task_id` 等上下文字段。
- **幂等性**：Channel 抓取以 guid 去重；手动触发的 run/fetch 以分布式锁保证同一资源不会重复入队；Transmission add_torrent 以 torrent 哈希幂等（RPC 本身支持）。
