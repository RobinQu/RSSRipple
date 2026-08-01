# 数据模型

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
    metadata_agent_enabled: bool         # 是否启用统一 metadata agent（默认 true）
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
    # BCP-47 language tags detected on the raw title. Sentinel ``["multi"]``
    # marks titles that only say "多语言"/"多国字幕" without spelling out
    # which ones. ``None`` = never parsed (legacy row); ``[]`` = parsed with
    # no subtitle marker present. Populated by pre-parser + MetadataAgent.
    subtitle_langs: list[str] | None     # e.g. ["zh-CN", "zh-TW", "ja", "en"]
    container: str | None                # 容器格式 (MKV, MP4)
    file_size: int | None                # 文件大小（bytes）
    torrent_url: str                     # 下载链接（magnet:?xt=... 或 .torrent URL）
    detail_url: str | None               # 详情页链接
    published_at: datetime | None        # RSS 发布时间
    # 合集（多集打包）标识 —— 由 pre-parser 与 MetadataAgent 联合判定
    is_batch: bool                       # 该资源是否为多集合集，默认 False
    episode_start: int | None            # 合集起始集，尽力而为（标题里可能没有）
    episode_end: int | None              # 合集结束集，尽力而为（标题里可能没有）
    # 跨季集号 reconciliation
    absolute_episode: int | None         # 当 episode 由绝对集号换算得到时，保存原始绝对集号
    episode_confidence: str | None       # "raw" | "reconciled" | "ambiguous" | "manual" | None
    # Metadata 关联（series/movie 用于定位作品；episode 字段用于定位剧集集数）
    series_id: str | None → TVSeries     # 关联剧集系列 FK
    movie_id: str | None → Movie         # 关联电影 FK
    parsed_at: datetime | None           # 字段映射解析完成时间
    metadata_matched_at: datetime | None # metadata 匹配完成时间
    created_at: datetime
    updated_at: datetime
```

资源的 FK 互斥规则：
- 若为剧集资源，`series_id` 非空，`movie_id` 必须为空；具体集数统一使用 `episode` 字段。
- 若为电影资源，`movie_id` 非空，`series_id` 必须为空。
- 未识别资源两个 FK 均为空。

**合集资源识别**：`is_batch=true` 标识多集打包资源（Season Pack / 全集 / `S01E01~13` / `[01-12 合集]` 等）。判定分两层：

1. **Pre-parser**（`app/services/resource_parser.detect_batch`）：抓取时用正则识别典型 pattern，直接写入 `is_batch / episode_start / episode_end`。
2. **MetadataAgent**（LLM）：finalize schema 输出 `is_batch / inferred_episode_start / inferred_episode_end`；LLM 输出的非空值覆盖 pre-parser 结果。

合集资源约束：`episode` 字段固定为空（避免与"单集集数"语义混淆）；`episode_start/end` 尽力而为，标题未标明时保留为空。

**跨季集号 reconciliation**：部分 RSS 标题使用**绝对集号**（跨全部季数累加），例如「关于我转生变成史莱姆这档事 第四季 S04 - 84」中的 `84` 实际是从第一季累计到第四季当前集的绝对数，而不是第四季的第 84 集。为了让 Agent 侧的 `(series_id, episode)` 去重语义稳定，在 `_apply_to_resource` 里根据 metadata 的 `seasons: [{season_number, episode_count}]` 证据做一次调整：

- **`NN(MM)` 双标记**（如 `13(85)`）——pre-parser 直接抽取，`episode=13`，`absolute_episode=85`，`episode_confidence="reconciled"`。
- **只标了 MM**（如 `S04 - 84`）——`reconcile_episode()` 检查 `raw_episode ≤ season_count + tolerance(2)`：符合就保留（`raw`）；否则减去前几季累计集数得到 candidate；candidate 落在 `[1, season_count + tolerance]` → 记为 `reconciled`（写回 `absolute_episode`），否则记为 `ambiguous`。
- `ambiguous` 的资源**不参与派发**。`agent_service` 在通过 work-scope + filter 之后，将其创建为一条 PendingDecision（reason 以 `"集号不确定，需要人工确认集号: {title}"` 标记、`candidates` 仅含该资源本身、跳过 LLM 候选选择），等待用户在前端手动修正集号；**不再**归入 `AgentSuggestion`。用户修正集号（`episode_confidence` 变为 `manual`）后，下一次运行会自动把这条过期决策标记为 `decided`，资源重新进入正常 filter→派发流程。
- `episode_confidence` 值：`raw` / `reconciled` / `ambiguous` / `manual` / `None`（老数据）。

### TVSeries（剧集系列 - Metadata 缓存）

```python
class TVSeries(Base):
    __tablename__ = "tv_series"

    id: str                              # UUID
    title_cn: str | None                 # 中文标题
    title_en: str | None                 # 英文标题
    original_title: str | None           # 原始标题（原名）
    aliases: list[str] | None            # 别名列表，自动积累合并（去重）
    external_id: str | None              # 外部 ID（MetadataAgent 返回的参考 ID，如 TMDB/MAL/IMDb/Wikipedia ID）
    external_source: str | None          # 枚举字符串: "exa" | "tmdb" | "wikipedia" | "manual" | "local_match" | "llm_search"（旧版遗留）
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
    raw_title_mappings: list[ChannelRawTitleMapping]
    pending_decisions: list[PendingDecision]
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
    external_source: str | None          # 枚举: "exa" | "tmdb" | "wikipedia" | "manual" | "local_match" | "llm_search"（旧版遗留）
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
    llm_enabled: bool                    # 是否启用 LLM 辅助决策（影响冲突自动解决建议），默认 true
    scope_channel_wide: bool             # true=订阅整个频道（仅靠 filter_config 过滤）
                                         # false=仅订阅 works 中的作品，默认 false
    conflict_resolution: str             # 冲突处理策略: "ask" | "auto"，默认 "auto"
                                         # "ask"=多候选时创建 PendingDecision 等待用户
                                         # "auto"=按启发式评分自动选择最优资源
    llm_prompt: str | None               # 可选：LLM 候选选择器的自定义指令（最多 4000 字符）
                                         # 为空时使用内置默认 prompt（metadata 字段最完整 >
                                         # 清晰度最高 > 带字幕 > 发布时间最新）。同时影响
                                         # "auto" 自动选择路径与 "ask" 模式下展示的 LLM 建议
    filter_config: dict | None           # 过滤规则 DSL 树（BoolCondition 根节点，详见 Filter DSL 章节）
    status: str                          # "active" | "paused" | "error"
    last_run_at: datetime | None         # 上次运行时间
    last_run_status: str | None          # 上次运行状态:
                                         # "success" | "failed"
                                         # | "pending_decisions"（当 dispatched=0
                                         #   且 pending_decisions>0 时使用；UI 据此
                                         #   显示"待决策"徽标而不是绿色 success）
    last_consumed_at: datetime | None    # 消费水位线：本 Agent 已处理过的最新
                                         # FileResource.created_at 时间戳。增量运行
                                         # （fetch 触发 / 手动 run）只处理 created_at >
                                         # last_consumed_at 的资源；规则变更保存时水位
                                         # 推进到频道当前最大值，使后续增量运行只看到真正
                                         # 的新资源。Null=从未运行（按"推进到 now、不处理
                                         # 任何资源"处理，避免静默自动派发历史回填——回填
                                         # 必须经 rules-preview 选择流程）
    created_at: datetime
    updated_at: datetime

    # Relationships
    channel: Channel
    downloader: DownloaderInstance
    works: list[AgentWork]               # 订阅作品列表（最多 10 个）
    suggestions: list[AgentSuggestion]   # 未识别资源建议分组（持久化）
    download_tasks: list[DownloadTask]
    pending_decisions: list[PendingDecision]
    runs: list[AgentRun]                 # 执行历史记录（每次 run 一条，cascade 删除）
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

两种场景创建 PendingDecision：
1. 同一作品的同一剧集（或同一电影）出现多个符合条件的候选资源，且 `conflict_resolution="ask"` 时创建（候选选择类）。
2. `episode_confidence="ambiguous"` 的资源（集号无法判定是单季集号还是绝对集号）创建，等待用户手动确认集号——此时 `candidates` 只含该资源本身，reason 以 `"集号不确定"` 前缀标记，且**跳过 LLM 候选选择**（无"挑最优候选"语义）。

**幂等性保证**：同一 `(agent_id, series_id | movie_id, episode, status='pending')` 键值全局唯一——Agent 反复运行时，`create_pending_decision` 会 upsert 已有行、合并新 `candidates`（保序、去重）、刷新 `reason` 和 `expires_at`，不会像 v1 那样堆积重复记录。`reason_override` 参数支持非冲突类决策（如集号不确定）复用同一 upsert 路径，并通过 `skip_llm` 跳过 LLM 调用。

```python
class PendingDecision(Base):
    __tablename__ = "pending_decisions"

    id: str                              # UUID
    agent_id: str → Agent                # 所属 Agent FK
    series_id: str | None → TVSeries     # 剧集系列 FK（TV 作品非空）
    movie_id: str | None → Movie         # 电影 FK（电影非空）
    episode: int | None                  # 集数（TV 作品）
    candidates: list[str]                # 候选 FileResource ID 列表（按匹配度预排序）
    reason: str                          # 需要决策的原因（如："多个资源匹配第03集"；
                                         #   集号不确定类以 "集号不确定" 前缀标记）
    llm_suggestion: str | None           # LLM 对候选的推荐理由（llm_enabled=true 时填充；
                                         #   集号不确定类决策跳过 LLM，为 None）
    llm_picked_resource_id: str | None   # LLM 选中的候选资源 ID（llm_enabled=true 时填充）。
                                         # 驱动 "AI 自动处理" 动作与决策 UI 中的高亮行；
                                         # 集号不确定类决策为 None
    decided_resource_id: str | None      # 用户最终选择的资源 ID（或 AI 自动处理选中的资源）
    status: str                          # "pending" | "decided" | "expired" | "skipped"
    expires_at: datetime | None          # 过期时间（默认 7 天）
    created_at: datetime
    decided_at: datetime | None
    updated_at: datetime
```

### AgentRun（Agent 执行记录）

每次 Agent 运行（`run_agent`）持久化一条记录，用于运行历史展示与审计。运行开始时即插入 `status="running"` 行（即使 handler 崩溃也有迹可循），运行结束时回填计数与状态。

```python
class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: str                              # UUID
    agent_id: str → Agent                # 所属 Agent FK（cascade 删除）
    started_at: datetime                 # 运行开始时间
    finished_at: datetime | None         # 运行结束时间
    status: str                          # "running" | "success" | "failed" | "pending_decisions"
                                         #（与 Agent.last_run_status 同语义）
    total_resources: int                 # 本次处理的资源总数
    matched: int                         # 通过 work-scope + filter 的资源数
    dispatched: int                      # 实际派发下载数
    pending_decisions: int               # 本次创建/更新的待决策数
    filter_failed: int                   # 在订阅范围内但未通过 filter 的资源数
    duplicates_skipped: int              # 因去重跳过的资源数
    unrecognized: int                    # 未识别 metadata（含集号不确定）的资源数
    matched_resource_ids: list[str]      # 本次通过 work-scope + filter 的资源 ID 列表
                                         #（供运行历史抽屉展示"匹配资源"明细）
    errors: list[str]                    # 本次运行捕获的错误信息列表
    created_at: datetime

    # Relationships
    agent: Agent
```

### DownloaderInstance（下载器实例）

```python
class DownloaderInstance(Base):
    __tablename__ = "downloader_instances"

    id: str                              # UUID
    name: str                            # 下载器名称
    type: str                            # 枚举: "transmission" | "mock"
                                         #   transmission: 真实 Transmission RPC
                                         #   mock: 本地内存模拟器，用于测试 Agent 流程
                                         #        （所有连接测试通过；每个 add_torrent
                                         #         的任务在随机 1-10 秒后自动完成）
    url: str                             # Transmission RPC URL（如 http://127.0.0.1:9091/transmission/rpc）
                                         # mock 类型可省略，默认为 "mock://local"
    username: str | None                 # RPC 用户名
    password: str | None                 # RPC 密码
    download_dir: str                    # 默认下载目录（必填）
                                         # Transmission 下载服务器本地可读写的绝对路径
                                         # 支持该服务器 OS 的路径风格（POSIX/Windows/UNC）
                                         # mock 类型可省略，默认为 "/tmp/mock-downloads"
    status: str                          # "connected" | "disconnected" | "error"，默认 "disconnected"
    last_checked_at: datetime | None     # 上次连通性检查时间
    created_at: datetime
    updated_at: datetime
```

### ChannelRawTitleMapping（频道原始标题映射）

用户手动修正 metadata 后，将原始标题与作品的映射落库；后续抓取同一频道**同一作品不同集数/画质**的资源时自动匹配。
匹配 key 使用 `search_title_key`（`normalize_title(extract_search_title(raw_title))`），而非 raw_title 本身，
从而解决同一作品因集数/分辨率不同而无法匹配的问题。

```python
class ChannelRawTitleMapping(Base):
    __tablename__ = "channel_raw_title_mappings"
    __table_args__ = (UniqueConstraint("channel_id", "search_title_key"),)

    id: str                              # UUID
    channel_id: str → Channel            # 所属频道 FK
    raw_title: str                       # RSS 原始标题（留存审计用）
    search_title_key: str                # 匹配 key：normalize_title(extract_search_title(raw_title))
                                         # 剥离字幕组前缀、集数后缀、分辨率等可变部分
    content_type: str | None             # "tv" | "movie"（可空，空时以 series_id/movie_id 为准）
    search_title_override: str | None    # 可选：覆盖默认 search_title（用户自定义清洗结果）
    series_id: str | None → TVSeries     # 映射到的剧集 FK
    movie_id: str | None → Movie         # 映射到的电影 FK
    created_at: datetime
    updated_at: datetime
```

**兼容性**：旧数据使用 `raw_title` 精确匹配作为 fallback，保证已有 mapping 不失效。

### MetadataCache（元数据缓存）

缓存统一 MetadataAgent 的处理结果，避免对同一标题重复执行 LangGraph ReAct 循环。`source="llm_title"` 为旧版遗留标识。

```python
class MetadataCache(Base):
    __tablename__ = "metadata_cache"
    __table_args__ = (UniqueConstraint("title", "source"),)

    id: str                              # UUID
    title: str                           # 缓存 key：原始（未清洗）标题
    source: str                          # 来源标识: "metadata_agent:<source>"（当前主要） | "llm_title"（旧版遗留）
    content_type: str | None             # 判断的内容类型: "tv" | "movie"
    metadata_json: dict                  # 缓存内容，格式 {"clean_title": "...", "content_type": "...", "episode": ..., "season": ..., ...}
    generation: int                      # 产生该 verdict 的逻辑版本（见 METADATA_CACHE_GENERATION）；存量行迁移为 0
    created_at: datetime
    updated_at: datetime
```

**逻辑版本化**：`METADATA_CACHE_GENERATION`（当前 1）标记产生缓存 verdict 的分类/判定逻辑版本。读取时 `generation != 当前版本` 的行视为未命中并懒删除——任何分类器、judge 提示词、匹配规则的变更只需 bump 该常量，旧逻辑产生的 verdict 即全部作废，不会短路修复后的代码。

---

