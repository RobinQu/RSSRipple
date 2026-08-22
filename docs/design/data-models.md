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
    metadata_source: str | None          # 频道主元数据源："wikipedia" | "tmdb" | "bangumi"（三数据源架构，
                                         # 其他值被轻迁移归一为 "wikipedia"）；None = 运行时用默认值
    metadata_fallback_sources: list[str] | None  # Exa 回退有序站点白名单（注册表站点名）；
                                         # None = 默认顺序，[] = 禁用回退；仅补身份/链接
    required_metadata_fields: list[str] | None   # 频道声明的必填元数据字段（覆盖全部 Filter DSL 字段：
                                         # 资源级字段以 DSL 字段名为键，作品字段按 series./movie.
                                         # 成对归入语义键 rating/year/genre/is_anime/collection，
                                         # 资源级 franchise 合集展示名走 resource_collection 键；
                                         # 权威目录 app/services/required_fields.py，两级分组：
                                         # section 按作品形态（base/tv/pack 先行）+ 语义 group；
                                         # 每键带 lock 作用域与 applies_to 形态适用性）；驱动资源列表
                                         # 「必填字段」列展示与 Agent 过滤 DSL 门控。
                                         # 强制且创建后只增不删：代码强制基线 = 基础必选七件套
                                         # （title_cn/title_en/search_title/content_type/is_batch/
                                         # year/is_anime）+ 形态必填（tv→season、tv_single→episode、
                                         # tv_batch→episode_start/end、franchise→resource_collection）
                                         # 永不可清除，不存在"不限制"状态；
                                         # 存量 NULL/残缺行由启动轻迁移收敛为基线
    default_is_anime: bool               # 「默认标记为 Anime」：NOT NULL DEFAULT FALSE（轻迁移加列）；
                                         # 创建后不可改（PUT 提交不同值 422）；开启后该频道资源链接到的
                                         # 作品 is_anime 先置 True（详见 business-logic.md「is_anime 分层判定」）
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
    title_year: int | None               # 从原始标题解析的作品年份（"[2026]" 或独立年份 token，
                                         # 1950..2100 合理区间外丢弃）；驱动 Layer-3 本地匹配的年份守卫
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
    torrent_file: str | None             # 通道 A 缓存的 .torrent 本地相对路径（TORRENT_CACHE_DIR
                                         # 下 <resource_id>.torrent；字节不落库，任务创建时本地推送）
    detail_url: str | None               # 详情页链接
    published_at: datetime | None        # RSS 发布时间
    # 合集（多集打包）标识 —— 由 pre-parser、torrent 内容检测与 MetadataAgent 联合判定
    is_batch: bool                       # 该资源是否为多集合集，默认 False
    batch_scope: str | None              # 合集细分：NULL=非合集；"season"=单季包；
                                         # "multi_season"=跨季包；"franchise"=多作品大 IP 包
    batch_seasons: list[int] | None      # multi_season/franchise 包覆盖的季集合（torrent 内容
                                         # 检测持久化）；驱动合集内容覆盖度去重；NULL=覆盖度未知
    episode_start: int | None            # 合集起始集，尽力而为（标题里可能没有）
    episode_end: int | None              # 合集结束集，尽力而为（标题里可能没有）
    # 跨季集号 reconciliation
    absolute_episode: int | None         # 当 episode 由绝对集号换算得到时，保存原始绝对集号
    episode_confidence: str | None       # "raw" | "reconciled" | "ambiguous" | "manual" | None
    # Metadata 关联（series/movie 用于定位作品；episode 字段用于定位剧集集数）
    series_id: str | None → TVSeries     # 关联剧集系列 FK
    movie_id: str | None → Movie         # 关联电影 FK
    collection_id: str | None → WorkCollection  # franchise 包关联合集 FK（此时作品 FK 全空）
    parsed_at: datetime | None           # 字段映射解析完成时间
    metadata_matched_at: datetime | None # metadata 匹配完成时间
    created_at: datetime
    updated_at: datetime
```

资源的 FK 互斥规则：
- 若为剧集资源，`series_id` 非空，`movie_id` 必须为空；具体集数统一使用 `episode` 字段。
- 若为电影资源，`movie_id` 非空，`series_id` 必须为空。
- 若为 franchise（多作品包）资源，`collection_id` 非空，`series_id`/`movie_id`/`audio_work_id` 三者必须全空。
- 未识别资源两个 FK 均为空。

**合集资源识别**：`is_batch=true` 标识多集打包资源（Season Pack / 全集 / `S01E01~13` / `[01-12 合集]` 等）。判定分三层：

1. **Pre-parser**（`app/services/resource_parser.detect_batch`）：抓取时用正则识别典型 pattern，直接写入 `is_batch / episode_start / episode_end`。覆盖的范围形态：`SxxEyy~zz`、方括号内纯数字范围 `[01-12]`（后缀关键词可选）、**括号内尾部范围**（括号含标题文字但以范围结尾，如 `[青春猪头少年不会梦到圣诞服女郎 01-13]`）、**季标记上下文中的裸范围**（`S01 | 01-24`、`第2季 13-24`，季标记后 80 字符内；占有量词防 `S04 - 05` 单集回溯误判）、裸范围+强制关键词（`01-12 合集`）、`第01-第12话`；连接符含全角 `～`/`〜`，范围尾部容忍 `+SPx11` 类特典后缀。无边界关键词：Season Pack / Batch / BD-BOX / 全集|全季|合集|完整|完结 / Complete Series / **`TV fin`**（必须带 TV 前缀；裸 `Fin` 也是单集最终话用法，刻意不作关键词）/ 整理搬运 等。**整碟包规则**：标题含显式季标记（`S0x`/`Season N`/`Nst|nd|rd|th Season`/`第N季` 含中文数字）且解析不出任何集号且含整碟 token（`BD`/`BDRip`/`BDMV`/`BDRemux`/`Blu-ray`/`BD-BOX`，词边界）→ 判合集（`葬送的芙莉莲 第二季 (BD ...)` 形态）。sanity 过滤：`end-start>200` 或 `end>999` 判误报（挡 `[2020-2021]` 年份对）。命中时同时**清空 `resource.episode`**（field_mapping 可能把年份/分辨率/标题数字解析成单集号）并设 `batch_scope="season"`（标题层默认单季包，torrent 分析可修正）。
2. **Torrent 内容检测（通道 A，`app/services/torrent_inspect.maybe_inspect_torrent`）**：metadata 匹配前，对 `is_batch=false` 且 `torrent_url` 为 http(s) 直链的资源下载 .torrent 落盘（`TORRENT_CACHE_DIR`，记 `torrent_file`），bencode 解析文件清单 → `analyze_torrent_files` 纯函数按视频文件过滤、路径分量集号提取、顶层目录聚类判出 scope：`single`（≤1 视频文件，不改判）/`season`（单季多集，填 episode_start/end）/`multi_season`（≥2 季标记，清空 season 与 episode_start/end，并把覆盖季集合持久化到 `batch_seasons`）/`franchise`（≥2 作品簇，同写 `batch_seasons`，触发 `franchise_service.link_franchise_pack` 创建/复用 `franchise_pack` 来源的 WorkCollection、逐个匹配成员作品并挂 `collection_id`、资源改挂 collection）/`unknown`（不改判）。magnet 与下载/解析失败静默跳过。下载后 RPC 修正（通道 B）为保留优化项，未实现。
3. **MetadataAgent**（LLM）：finalize schema 输出 `is_batch / inferred_episode_start / inferred_episode_end` 与可选 `batch_scope`（白名单 season|multi_season|franchise，表外值丢弃）；LLM 输出的非空值覆盖 pre-parser 结果（`is_batch` 单向 OR 合并，只会补 True 不会改 False）；`batch_scope` 仅当现有值为 NULL/"season" 时写入（torrent 分析的 multi_season/franchise 不被降级），LLM 未输出时默认 `"season"`。

合集资源约束：`episode` 字段固定为空（避免与"单集集数"语义混淆）；`episode_start/end` 尽力而为，标题未标明时保留为空。**合集去重按内容覆盖度**（`agent_service._batch_coverage_key`）：电影包→`movie_id`；单季包→`(series_id, season)`；跨季包→`(series_id, batch_seasons)`。仅当覆盖度已知且完全相同的多个版本才进入与单集一致的冲突解决（ask → PendingDecision，episode 哨兵 -1；auto → LLM pick → 启发式），跨运行则按同 agent + 同覆盖度的 active 任务判重跳过；覆盖度不同（S1 包 vs S2 包）的合集不去重、各自派发。**覆盖度未知（season 包无季号、multi_season 无 batch_seasons）不再派发**——覆盖度是 organize 覆盖度校验的必填依据，落 PendingDecision（episode 哨兵 -2，reason 前缀「合集范围不确定」）待人工修订补齐后定向重跑。franchise 包作品 FK 全清，不进入派发。

**跨季集号 reconciliation**：部分 RSS 标题使用**绝对集号**（跨全部季数累加），例如「关于我转生变成史莱姆这档事 第四季 S04 - 84」中的 `84` 实际是从第一季累计到第四季当前集的绝对数，而不是第四季的第 84 集。为了让 Agent 侧的 `(series_id, season, episode)` 去重语义稳定，在 `_apply_to_resource` 里根据 metadata 的 `seasons: [{season_number, episode_count}]` 证据做一次调整：

- **`NN(MM)` 双标记**（如 `13(85)`）——pre-parser 直接抽取，`episode=13`，`absolute_episode=85`，`episode_confidence="reconciled"`。若标题未解析出季数（`season=None`），`apply_episode_reconcile()` 会用 `locate_absolute_episode()` 按各季集数累减反推 `(season, episode)` 并**同时写回两个字段**；超出总集数 + tolerance(2) 时记为 `ambiguous`。
- **只标了 MM**（如 `S04 - 84`）——`reconcile_episode()` 检查 `raw_episode ≤ season_count + tolerance(2)`：符合就保留（`raw`）；否则减去前几季累计集数得到 candidate；candidate 落在 `[1, season_count + tolerance]` → 记为 `reconciled`（写回 `absolute_episode`），否则记为 `ambiguous`。
- `apply_episode_reconcile()` 跳过条件：合集资源；`episode` 与 `absolute_episode` 均为空；`episode_confidence == "manual"`；以及 `season` 已知且已为 `reconciled` 的资源（不重算）。无判定依据（空 map / 未知季）时仅给无标记资源补上 `raw`。
- `ambiguous` 的资源**不参与派发**。`agent_service` 在通过 work-scope + filter 之后，将其创建为一条 PendingDecision（reason 以 `"集号不确定，需要人工确认集号: {title}"` 标记、`candidates` 仅含该资源本身、跳过 LLM 候选选择），等待用户在前端手动修正集号；**不再**归入 `AgentSuggestion`。用户修正集号（`episode_confidence` 变为 `manual`）后，下一次运行会自动把这条过期决策标记为 `decided`，资源重新进入正常 filter→派发流程。
- `ambiguous` 只对**单集 tv 资源**有意义：合集资源（`is_batch`，按内容覆盖度去重）与电影链接资源（无集号/季号问题）携带的 ambiguous 一律为残留标记——派发流程的 ambiguous 分支跳过这两类；Dashboard「待确认」列表（`pending_confirmations`）同样排除。各人工修订入口负责了结残留：标记为合集（PATCH `/resources/{id}`）置 `manual`；重新链接为电影（`/metadata/link`）或将作品 `content_type` 改为非 tv（PUT `/series/{id}`）置 null。存量遗留行由启动轻迁移 `ambiguous_stale_clear` 一次性清理（合集→`manual`，电影链接/非 tv 作品链接→null，app_settings 哨兵保证只跑一次）。
- `episode_confidence` 值：`raw` / `reconciled` / `ambiguous` / `manual` / `None`（老数据）。

**reconciliation 的触发路径**：早期只在 MetadataAgent 的 `_apply_to_resource` 里执行，导致免 Agent 的链接路径（已知作品短路 S1、ChannelRawTitleMapping、本地模糊 auto-link ≥85）完全绕过 reconcile——同一作品的新集恰恰都走这些路径。现在 `apply_episode_reconcile()` 作为统一的链接后步骤挂到全部四条路径：agent 完整路径优先用当次 `matched_entity.seasons`，其余路径（及 entity 缺 seasons 的兜底）用 `TVSeries.seasons` 持久化列。`NN(MM)` 预解析不受影响，仍在抓取期先行处理。

**Metadata prompt 的作品历史 few-shot**：MetadataAgent 构造生产 prompt 时，若标题与本地库中某 series 模糊匹配（≥70），注入该系列的集数编号约定（`seasons` 每季集数）和最近 5 条 sibling 解析示例（`episode_confidence` 为 `reconciled`/`manual` 的资源：title → S/E + absolute），引导模型与历史解析保持一致的季/集编号。

存量修复脚本：`scripts/series_seasons_backfill.py`——为缺 `seasons` 的 series 从 TMDB 补齐每季集数，并对 `episode_confidence` 为 NULL/`raw` 的单集资源重跑 reconcile（dry-run 默认，`--apply` 生效；需在 app 停止时运行，Turso 单进程文件锁）。

### TVSeries（剧集系列 - Metadata 缓存）

```python
class TVSeries(Base):
    __tablename__ = "tv_series"

    id: str                              # UUID
    title_cn: str | None                 # 中文标题
    title_en: str | None                 # 英文标题
    original_title: str | None           # 原始标题（原名）
    aliases: list[str] | None            # 别名列表，自动积累合并（去重）
    search_text: str | None              # 归一化搜索 haystack：title_cn+title_en+original_title+aliases
                                         # 过 normalize_title（NFKC+OpenCC t2s+小写）的拼接；由 ORM
                                         # before_flush 钩子同事务维护，启动时空值回填。Turso 镜像进 FTS
                                         # 边车（fts_outbox drain），PostgreSQL 上被 pg_trgm GIN 索引
    external_id: str | None              # 外部 ID（MetadataAgent 返回的参考 ID，如 TMDB/MAL/IMDb/Wikipedia ID）
    external_source: str | None          # 枚举字符串: "exa" | "tmdb" | "wikipedia" | "manual" | "local_match" | "llm_search"（旧版遗留）
    description: str | None              # 简介
    poster_url: str | None               # 海报本地缓存路径，格式 /posters/{hash}.jpg
    rating: float | None                 # 评分（0-10）
    genre: list[str] | None              # 类型标签（封闭 TMDB 27 类英文 canonical 名，取值约定见下文「genre 取值约定」）
    status: str | None                   # 剧集状态: "Ended" | "Returning Series" | "Canceled" 等
    number_of_episodes: int | None       # 总集数
    number_of_seasons: int | None        # 总季数
    seasons: list[dict] | None           # 每季集数 [{season_number, episode_count}, ...]，来自 TMDB/Exa 实体；驱动免 Agent 链接路径（短路/模糊匹配）的跨季集数 reconciliation
    start_date: date | None              # 首播日期
    end_date: date | None                # 完结日期
    content_type: str | None             # "tv" | "anime" | "mixed"
    is_anime: bool | None                # 三态动漫标记：True=日本动画 / False=确认实拍 / None=未判定；
                                         # 与 content_type（媒介）正交（剧场版动画两者独立），
                                         # 判定与赋值规则见下文「is_anime 判定约定」
    manually_edited_fields: list[str] | None  # 人工编辑保护：用户经作品详情页「编辑」表单改过的字段名列表
                                         # （JSON 数组，取值 ⊂ MANUAL_EDITABLE_FIELDS，见 metadata_service）；
                                         # 自动扫描（upsert / apply_is_anime / 频道默认标记 / bangumi 验证）
                                         # 跳过其中的字段；refresh_work_metadata 默认同样跳过，
                                         # 仅当刷新对话框勾选「覆盖所有人工编辑字段」时才覆盖。
    canonical_name: str | None           # 规范化名称（跨数据源消歧/搜索用的标准名）
    wikipedia_url: str | None            # 维基百科条目 URL
    wikipedia_page_id: int | None        # 维基百科 pageid（供维基数据源回填/海报抓取使用）
    collection_id: str | None → WorkCollection  # 所属合集 FK（组织层；一个作品至多属于一个合集）
    created_at: datetime
    updated_at: datetime

    # Relationships
    episodes: list[Episode]
    file_resources: list[FileResource]
    agent_works: list[AgentWork]
    raw_title_mappings: list[ChannelRawTitleMapping]
    pending_decisions: list[PendingDecision]
    collection: WorkCollection | None
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
    search_text: str | None              # 归一化搜索 haystack（同 TVSeries.search_text，见其注释）
    external_id: str | None
    external_source: str | None          # 枚举: "exa" | "tmdb" | "wikipedia" | "manual" | "local_match" | "llm_search"（旧版遗留）
    description: str | None
    poster_url: str | None
    rating: float | None
    genre: list[str] | None              # 同 TVSeries.genre 取值约定
    status: str | None                   # "Released" | "Upcoming" 等
    release_date: date | None            # 上映日期（区别于 TVSeries 的 start_date）
    runtime: int | None                  # 片长（分钟）
    content_type: str | None             # "movie"
    is_anime: bool | None                # 三态动漫标记（同 TVSeries.is_anime，见下文「is_anime 判定约定」）
    manually_edited_fields: list[str] | None  # 人工编辑保护（同 TVSeries.manually_edited_fields，见其注释）
    canonical_name: str | None           # 规范化名称（跨数据源消歧/搜索用的标准名）
    wikipedia_url: str | None            # 维基百科条目 URL
    wikipedia_page_id: int | None        # 维基百科 pageid（供维基数据源回填/海报抓取使用）
    collection_id: str | None → WorkCollection  # 所属合集 FK（组织层；一个作品至多属于一个合集）
    created_at: datetime
    updated_at: datetime

    # Relationships
    file_resources: list[FileResource]
    pending_decisions: list[PendingDecision]
    agent_works: list[AgentWork]
    collection: WorkCollection | None
```

### genre 取值约定（统一分类标签）

`TVSeries.genre` / `Movie.genre` / `AudioWork.genre` 取值被约束为 **TMDB 封闭分类集（movie 19 + TV 16，并集 27 类）的英文 canonical 名**，权威清单一处定义：`app/services/genre_registry.py`（含 tmdb_id、中文显示名、movie/tv 适用范围），`app/schemas/genre.py` 的 `GenreName` Literal 与前端 `constants/genres.ts` 与之手同步。

- **存储**：英文 canonical 名（如 `"Animation"`、`"Sci-Fi & Fantasy"`），按注册表顺序去重；中文显示归前端 i18n。
- **来源**：TMDB 直连 id 直译；wikipedia/Exa 路径由 LLM（judge/ReAct prompt 注入完整枚举）根据外部作品详情输出，prompt 要求**尽力推测**——源未显式列出标签时必须依据简介/categories 推断，有简介就至少给一个；judge/ReAct 仍为空时由 `_ensure_genre` 兜底（一次低成本 LLM 调用按简介分类，结果再次钳制）；所有产出统一经 `normalize_genres` 钳制——表外值一律丢弃（记录 debug 日志），空结果视为"未提供"。
- **写回规则**：新值非空才覆盖既有值；归一化后为空不清空旧值。手动 PATCH 的 genre 可能被下一次 metadata 更新覆盖（本期不做 manual 保护标记）。
- **API 校验**：作品 Create/Update 的 genre 为 `GenreName` 枚举，表外值 422；通知 payload 的 `work.genre` 快照同样落封闭集。
- **存量**：`scripts/genre_backfill.py` 模式 A 就地规范化既有值，模式 B `--refresh-empty` 对空值作品重跑 metadata 补齐；缓存代际 `METADATA_CACHE_GENERATION=3` 使旧缓存惰性失效。

### is_anime 判定约定（三态动漫标记）

`TVSeries.is_anime` / `Movie.is_anime` 是三态标记：**True=日本动画 / False=确认实拍 / NULL=未判定**。与 `content_type` 正交——`content_type` 是媒介（tv/movie），`is_anime` 是风格属性，剧场版动画两者独立。权威判定模块一处定义：`app/services/anime_signals.py`（`ANIME_IDENTITY_SOURCES` / `is_anime_from_tmdb` / `is_anime_identity` / `apply_is_anime`）。

- **证据优先级（确定性证据优先）**：
  1. **身份源/身份袋**（`is_anime_identity`）：主 `external_source` 或身份袋任一 `source:id` 命中 `bangumi | mal | anilist`（`ANIME_IDENTITY_SOURCES`，均为纯动漫站点）必为 anime。bangumi 频道源的搜索限动画分类 `type=[2]`，命中条目即以 `bangumi:{id}` 身份落库，天然走本条。
  2. **Wikipedia**：页面含 `{{Infobox animanga/TVAnime}}` 块（`wikipedia_episode_parser.has_tvanime_infobox`）或 `{{Infobox animanga/Movie|Film|OVA}}` 块（`has_animanga_film_infobox`，剧场版/OVA 信号），在 `_attach_wikipedia_content` 处向 matched_entity 标记 `is_anime=True`。
  3. **TMDB**（`is_anime_from_tmdb`）：genre 含 Animation(16) 且 `original_language == "ja"` 或 `origin_country` 含 JP → True；genre 存在但无 Animation → False（确认实拍）；非日语 Animation（西方动画）或无 genre 数据 → None。
  4. **LLM judge/ReAct finalize**：schema 新增可选 `is_anime` 输出，仅作无确定性证据时的兜底。
- **赋值规则**（`apply_is_anime`，在 series/movie upsert 的四处分支调用）：身份证据直接覆盖为 True；其余 **True sticky**——一旦 True 永不被后续弱证据降级；False 只填 NULL（既有 True 不降级，既有 False 不被 LLM 翻转）。
- **运行时分层判定**：频道默认标记（`default_is_anime`，链接作品先置 True）→ 第一层 Bangumi 验证（`maybe_verify_is_anime_via_bangumi` + `bangumi_verdict`：仅 NULL 作品、不带 type 过滤搜索，type 2 → True、type 6 三次元 → False）→ 第二层上下文推断（上述信号）→ 最终 NULL 待手动修正；统一入口 `classify_is_anime_post_link` 挂在资源链接作品的全部 5 处落点（详见 business-logic.md「is_anime 分层判定」）。
- **存量**：轻迁移在 `app/database.py` `_apply_light_migrations` additions 里（tv_series/movies 各一行可空 BOOLEAN）；回填走 `scripts/anime_backfill.py`（dry-run 默认 + `--apply`，离线身份阶段 + 可选 `--tmdb`/`--wikipedia` 联网阶段）。

### 人工编辑保护（manually_edited_fields）

`TVSeries` / `Movie` 新增 JSON 列 `manually_edited_fields`（字段名列表），记录用户经作品编辑页表单显式改过的字段。权威取值集合一处定义：`app/services/metadata_service.py` 的 `MANUAL_EDITABLE_FIELDS`（标题三字段、aliases、description、poster_url、rating、genre、status、is_anime、series 的季集/日期、movie 的 release_date/runtime、`content_type`、`external_id`、`external_source`）。

- **可编辑 vs 系统托管**：仅 `MANUAL_EDITABLE_FIELDS` 中的字段可在编辑页修改（`content_type` 限 `tv`/`movie`）；`canonical_name`/`wikipedia_url`/`wikipedia_page_id`/`seasons`/`collection_id`/`search_text`/时间戳为系统托管，不可编辑。
- **记录时机**：`PUT /series/{id}` / `PUT /movies/{id}` 按 `exclude_unset` 后的显式发送字段（含显式 null）与 `MANUAL_EDITABLE_FIELDS` 求交，并入 `manually_edited_fields`（`mark_manually_edited`，去重排序）。
- **身份变更入袋**：PUT 显式发送 `external_id`/`external_source` 且 `(external_source, external_id)` 实际变化时，先把旧身份对幂等写入 `WorkExternalId` 身份袋（`add_external_id`；id 已被其他作品占用则冲突不抢、仅记 warning），再覆盖主列——作品在旧身份下仍可反查。
- **自动扫描跳过**：`create_or_update_*_from_external` 更新分支、`apply_is_anime`、`apply_channel_default_is_anime`、`maybe_verify_is_anime_via_bangumi` 在写任一字段前检查 `field_manually_edited`，命中即不改写该字段（新建作品无该列表，不受影响）；`content_type`/`external_id`/`external_source` 三字段在 upsert 与 `refresh_work_metadata` 中同样受此守卫（`override_manual_edits=true` 时可覆盖）。
- **刷新元数据**：`refresh_work_metadata` 默认跳过 `manually_edited_fields` 中的字段；仅当请求带 `override_manual_edits=true`（作品模块刷新对话框「覆盖所有人工编辑字段」）时才覆盖。批量/周期刷新不传该 flag，恒为默认（不覆盖）。

### WorkExternalId（作品外部身份袋 - Phase P3）

"身份袋"反向索引：一个作品可携带**多个**外部身份（创建时的 wikipedia pageid、langlinks 各语言页的 pageid、Exa 回退命中的 tmdb/bangumi id …），任何一个已知 `(source, external_id)` 都能确定性地反查回作品行，使跨源/跨语言 upsert 收敛不再依赖标题运气。

```python
class WorkExternalId(Base):
    __tablename__ = "work_external_ids"
    __table_args__ = (UniqueConstraint("source", "external_id"),)  # 一个 id 至多映射一个作品

    id: str                              # UUID
    work_type: str                       # "series" | "movie" —— work_id 指向哪张作品表
    work_id: str                         # 跨表引用（tv_series.id 或 movies.id），故意不带 FK
    source: str                          # registry 源名（wikipedia/tmdb/bangumi/mal/anilist/imdb/douban）
    external_id: str                     # 完整 canonical "source:id" 字符串（镜像 TVSeries.external_id 约定）
    created_at: datetime
```

**语义与规则**：

- **主 id 规则（creator-wins）**：`TVSeries.external_id/external_source`（及 Movie）仍是创建时确定的展示/主 id；后续发现的 id 只进袋，绝不抢占主 id 列。wikipedia 主 id 特例外（见下）：不同 pageid 是同作品的不同语言版本页面，upsert 重匹配时主 id 不随来源语言翻转而保持稳定（`_merge_primary_external_id`）。
- **存储约定**：`source` 存 registry 源名，`external_id` 存完整 canonical `source:id`；裸 id（如纯 pageid）写入/查询时一律补 `source:` 前缀归一；非 registry 源（如 `llm_search`）跳过。
- **wikipedia id 带语言版本**：pageid 是每个语言版本各自编号的，canonical 形式为 `wikipedia:{lang}:{pageid}`（如 `wikipedia:zh:7301786`）；早期存量为无语言的 `wikipedia:{pageid}`。写入点（auto-link/judge/audio resolver/upsert 入口 `_qualify_incoming_wikipedia_id` 经 `wikipedia_url` 宿主推导）一律产出带语言形式；袋查询与作品列查询双形式兼容（`wikipedia_match_keys`：带语言精确匹配两种形式，裸 id 额外 LIKE 匹配任意语言）；同作品加带语言 id 命中裸行时原地升级为带语言形式；不同作品的同数字 pageid（跨语言撞号）不互抢（记 warning）。展示链接按语言渲染 `https://{lang}.wikipedia.org/?curid={pid}`，label 为 `Wikipedia (zh/en/ja)`；存量迁移走 `scripts/wikipedia_lang_backfill.py`（dry-run 默认 + `--apply`，标题锚定主页面语言 → langlinks 重解析各语言 pageid → 重写主 id/袋行并回填 `wikipedia_url`/`wikipedia_page_id`）。
- **no-steal**：`UniqueConstraint(source, external_id)` 保证一个 id 至多映射一个作品；把已属于作品 A 的 id 加给作品 B 时不改指、记 warning（该对成为去重候选）。
- **写入点**：upsert 成功时写入 matched_entity 的主 id + `alt_external_ids`（如 wikipedia langlinks pageids）；去重合并（`_merge_series_group`/`_merge_movie_group`/跨表合并）对袋取并集（存留方继承重复方的主 id 与袋行；wikipedia 行按 pageid 去重，与存储形式无关）。
- **回填**：`_apply_light_migrations` 启动时从存量 TVSeries/Movie 行的主 external_id 播种（幂等；仅 registry 源）。
- **读取**：`find_work_by_external_id` 只按同 `work_type` 反查——另一类型的袋命中在 upsert 中被忽略（跨表收敛由 metadata_repository 跨表守卫与每日去重负责）。

### WorkCollection（作品合集 - 大 IP 系列分组）

将同一 IP（攻壳机动队、蜘蛛侠、狮子王 …）的多个 TVSeries/Movie 归为一组。**合集是组织层而非消歧核心**：匹配/派发仍以单个作品行（TVSeries/Movie）为准。一个作品至多属于一个合集（由单个可空 `collection_id` FK 保证）。

```python
class WorkCollection(Base):
    __tablename__ = "work_collections"
    __table_args__ = (
        UniqueConstraint("external_source", "external_id"),  # 幂等 upsert
    )

    id: str                              # UUID
    title_cn: str                        # 合集名（必填）
    title_en: str | None                 # 英文名（TMDB 电影详情不含，保持 NULL）
    external_id: str | None              # 外部 ID（TMDB collection 为原始数字 id；Wikidata 为 franchise QID）
    external_source: str | None          # "tmdb_collection" | "wikidata" | None；不用 canonicalize_external_id
                                         # （其 TMDB 规则会把 tmdb-collection:131295 改写为
                                         # tmdb:131295，与电影 id 空间冲突）
    poster_url: str | None               # TMDB 远程图片 URL（phase 1 不做本地缓存）
    description: str | None              # 简介（TMDB 电影详情不含，保持 NULL，可手动编辑）
    created_at: datetime
    updated_at: datetime

    # Relationships
    series: list[TVSeries]
    movies: list[Movie]
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

填充来源（Phase P2）：仅由 wikipedia 主源的确定性剧集解析填充——`create_or_update_series_from_external` 在 `matched_entity.episode_list` 存在时调用 `upsert_episodes` 幂等 upsert（title/air_date；只增不删）；剧集详情 API selectinload 本关系。

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
    notifications: list[DownloadNotification]
    webhooks: list[AgentWebhook]         # 下载通知 webhook 注册（多 webhook fan-out）
```

> **webhook 注册迁移说明**：下载通知的 webhook 订阅已从 Agent 的三列（`notify_webhook_url`/`notify_webhook_mock`/`notify_webhook_token`）迁移到独立 `agent_webhooks` 表（见下）。旧列留在物理表中成为**惰性孤儿列**（无 DROP migration）；启动时 light migration 把存量注册一次性复制为 `agent_webhooks` 行。回调 token 机制已随消费者回调（start/ack/fail）一起删除。

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
    file_resource_id: str → FileResource # 对应资源 FK（资源删除时级联删除任务）
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
    notification: DownloadNotification | None   # 一对一（download_task_id 唯一）
```

### DownloadNotification（下载完成通知）

下载任务驱动的通知队列，从属于下载 Agent（每 Agent 单例 FIFO，按 `created_at` 升序）。payload 为创建时冻结的完整快照（任务 + 资源 + 作品 + torrent 文件清单）。fan-out 重构后本表**只保留快照锚点**；投递状态全部下放到 `WebhookDelivery`（通知的展示状态由 delivery 聚合计算，不落库）。完整语义见 [notifications.md](notifications.md)。

```python
class DownloadNotification(Base):
    __tablename__ = "download_notifications"

    id: str                              # UUID
    agent_id: str | None → Agent         # 队列归属（ON DELETE SET NULL 保留历史）
    download_task_id: str → DownloadTask # Unique：一个任务至多一条通知（幂等基础；
                                         # 并发创建走 SAVEPOINT，输掉唯一约束竞争回读）
    payload: dict                        # 完整快照 JSON
    created_at: datetime
    updated_at: datetime

    # Relationships
    agent: Agent
    download_task: DownloadTask
    deliveries: list[WebhookDelivery]    # cascade delete-orphan
```

> **存量库迁移**：fan-out 之前的投递列（`status`/`error_message`/`attempt_count`/`next_attempt_at`/`notified_at`/`processed_at`）已从 ORM 移除。SQLite/Turso 上 light migration **重建整张表**为当前模型列（无法原地 DROP NOT NULL）；PostgreSQL 仅对 `status`/`attempt_count` 执行 `DROP NOT NULL`，孤儿列保留。

### AgentWebhook（webhook 注册）

一个 Agent 可注册任意多个 webhook；每条通知 fan-out 到其 Agent 全部**启用**的 webhook，每个 webhook 一条 `WebhookDelivery`。

```python
class AgentWebhook(Base):
    __tablename__ = "agent_webhooks"

    id: str                              # UUID
    agent_id: str → Agent                # FK CASCADE
    url: str                             # 投递目标（非 mock 必须 http(s)）
    mock: bool                           # mock webhook：投递直接记成功、不发 HTTP（测试通道）
    enabled: bool                        # 停用保留行与投递历史但不再接收新 delivery；
                                         # 重新启用后从积压 backlog 恢复
    created_at: datetime
    updated_at: datetime

    # Relationships
    agent: Agent
    deliveries: list[WebhookDelivery]
```

### WebhookDelivery（投递执行记录）

每对 `(notification, webhook)` 一行——通知管道的 fan-out 单元。每条 delivery 携带自己的状态与重试簿记，单个 webhook 失败绝不阻塞其他 webhook。

```python
class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"
    __table_args__ = (UniqueConstraint("notification_id", "webhook_id"),)  # fan-out 幂等

    id: str                              # UUID
    notification_id: str → DownloadNotification  # FK CASCADE
    webhook_id: str → AgentWebhook       # FK CASCADE
    status: str                          # "pending" | "done" | "failed"
    attempt_count: int                   # 已失败次数
    next_attempt_at: datetime | None     # 下次投递时间（指数退避）；pending 且 null = 立即到期
    error_message: str | None            # 最近失败原因
    delivered_at: datetime | None        # 投递成功时间
    created_at: datetime
    updated_at: datetime

    # Relationships
    notification: DownloadNotification
    webhook: AgentWebhook
```

### ApiKey（全局 API key）

程序端访问凭证。仅存储 SHA-256 摘要；明文（`rr_` 前缀）只在创建响应中返回一次。

```python
class ApiKey(Base):
    __tablename__ = "api_keys"

    id: str                              # UUID
    name: str                            # 用户自定义名称
    prefix: str                          # 明文前 10 个字符（仅展示用，不参与匹配）
    key_hash: str                        # 明文的 SHA-256 hex 摘要（unique）
    created_at: datetime
```

接受方式：`Authorization: Bearer <key>` 或 `X-API-Key: <key>` 头；暂无过期机制。TOTP 秘钥与 Cookie 签名秘钥不建表——首次启动生成后持久化在 `app_settings`（`auth_totp_secret` / `auth_cookie_secret`）。

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

**幂等性保证**：同一 `(agent_id, series_id | movie_id, season, episode, status='pending')` 键值全局唯一——Agent 反复运行时，`create_pending_decision` 会 upsert 已有行、合并新 `candidates`（保序、去重）、刷新 `reason` 和 `expires_at`，不会像 v1 那样堆积重复记录。`season` 计入键值（S1E3 与 S4E3 不再互相合并）；调用方传入的 key 为 4 元组 `(type, target_id, season, episode)`，旧 3 元组 `(type, target_id, episode)` 仍兼容（season=None）。`reason_override` 参数支持非冲突类决策（如集号不确定）复用同一 upsert 路径，并通过 `skip_llm` 跳过 LLM 调用。

```python
class PendingDecision(Base):
    __tablename__ = "pending_decisions"

    id: str                              # UUID
    agent_id: str → Agent                # 所属 Agent FK
    series_id: str | None → TVSeries     # 剧集系列 FK（TV 作品非空）
    movie_id: str | None → Movie         # 电影 FK（电影非空）
    episode: int | None                  # 集数（TV 作品）
    season: int | None                   # 季数（TV 作品；幂等键的一部分，NULL=电影/无季资源）
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
                                         #（含集号/季号不确定转待决策的资源，
                                         # 供运行历史抽屉展示"匹配资源"明细）
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

**逻辑版本化**：`METADATA_CACHE_GENERATION`（当前 2；gen 2 = P1 回退身份语义 + P2 wikipedia seasons 附着 + P3 身份袋/`alt_external_ids`）标记产生缓存 verdict 的分类/判定逻辑版本。读取时 `generation != 当前版本` 的行视为未命中并懒删除——任何分类器、judge 提示词、匹配规则的变更只需 bump 该常量，旧逻辑产生的 verdict 即全部作废，不会短路修复后的代码。

### FtsOutbox（全文检索变更日志 - 仅 Turso 使用）

驱动 FTS 边车同步的**持久变更记录**（`app/models/fts_outbox.py`）。边车影子表（`<主库名>_fts.db`）与 MVCC 主库互斥，因此由独立任务投递；本表是该投递的 durable record：ORM `before_flush`/`after_flush` 钩子（`app/services/work_search_events.py`）在**作品行同一事务**内入队，commit 成功必留痕、rollback 随事务消失。PostgreSQL 后端不写本表（走 `search_text` + pg_trgm），表结构仍统一建出、恒为空。

```python
class FtsOutbox(Base):
    __tablename__ = "fts_outbox"

    id: str                              # UUID
    entity_type: str                     # "series" | "movie" | "audio" —— 指向的作品表
    entity_id: str                       # 作品主键（FK-less 跨表引用）
    op: str                              # "upsert" | "delete"
    created_at: datetime
```

无 `(entity_type, entity_id)` 唯一约束：同实体多次变更产生多行无妨——drain 每批清空、边车写入是幂等 DELETE+INSERT 全量替换，最终态 = 最后一个 op。

### FTS 边车影子表（`<主库名>_fts.db`，Turso 专属）

三张普通表 `tv_series_fts` / `movie_fts` / `audio_work_fts`（`entity_id` PK + 规范化标题列），其上建 `CREATE INDEX ... USING fts (…) WITH (tokenizer='ngram')` 由引擎自动跟踪 DML。仅缓存 `search_text` 同源的规范化内容，可随时从基表重建（`rebuild_*_fts` / `backfill_fts_if_empty`）；同步 = `search_*_fts` **读前先 drain outbox**（read-your-writes：刚提交的作品立即可搜）+ `_drain_fts_outbox`（每 30 秒批量化兜底，覆盖脚本/非 API 写入）+ `_reconcile_fts`（每小时全量对账兜底，修补绕过 outbox 的路径）。PostgreSQL 无此表。

---

