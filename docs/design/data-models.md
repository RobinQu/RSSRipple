# 数据模型

所有 ORM 模型使用 SQLAlchemy 2.0 风格声明，主键均为 UUID v4 字符串，时间字段语义均为 UTC。数据库继续使用无时区时间列并保存 naive UTC；数据库默认值及 ORM 自动更新时间使用 `UTCNow`：PostgreSQL 显式将当前事务时间转为 UTC，Turso 使用原生 UTC CURRENT_TIMESTAMP。该表达式不覆盖全局 func.now 编译，不依赖会话时区。Python 显式写入继续使用 UTC 时间；日期列不受此规则影响。

新增 `FileResource.magnet_resolve_attempt_id: VARCHAR(36), nullable`，用于独立 magnet 解析尝试身份；历史行保持 NULL。新领取生成 UUID，状态及重试计数写入匹配该标识；回收/人工重试清空。缓存路径含 attempt 标识，完成状态 CAS 接受后才被资源引用。

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
                                          # 现行新频道基线为五件套（不含 title_cn）；title_cn/title_en 可选。
                                          # 轻迁移移除旧基线遗留的 title_cn/title_en；此后用户可主动选为必填。
                                          # 强制且创建后只增不删：代码强制基线 = 基础必选五件套
                                          # （search_title/content_type/is_batch/year/is_anime）+ 形态必填（tv_single→episode、
                                          # tv_season_batch→episode_start/end；跨季批次由
                                          # batch_seasons（links-only 形态由关联季作品 links 派生校验）覆盖校验；franchise→resource_collection）
                                          # 永不可清除，不存在"不限制"状态；作品单季化后 season 键退役为可选、
                                          # absolute_episode/episode_confidence 两键退役出目录（存量频道声明
                                          # 由启动轻迁移一次性移除，哨兵 required_fields_per_season_v1）；
                                          # 存量 NULL/残缺行由启动轻迁移收敛为基线
    default_is_anime: bool               # 「默认标记为 Anime」：NOT NULL DEFAULT FALSE（轻迁移加列）；
                                         # 创建后不可改（PUT 提交不同值 422）；开启后该频道资源链接到的
                                         # 作品 is_anime 先置 True（详见 business-logic.md「is_anime 分层判定」）
    metadata_refresh_enabled: bool       # 频道级「定期刷新作品元数据」开关：NOT NULL DEFAULT FALSE
                                         # （轻迁移加列；原全局自动刷新已废除，存量行收敛为关闭）。
                                         # 开启后调度器按 metadata_refresh_interval_minutes 周期入队
                                         # refresh_channel_works，用本频道自身 metadata_source 补全
                                         # 本频道关联作品的空缺字段；只填空字段、绝不覆盖人工编辑
                                         # （override_manual_edits 恒 False）
    metadata_refresh_interval_minutes: int | None  # 刷新间隔（分钟），NULL = 默认 1440；
                                         # clamp 30..10080（schemas + 调度两侧）
    metadata_refresh_full_scope: bool    # 「刷新全量作品」：NOT NULL DEFAULT FALSE。False = 仅刷新
                                         # 确有待填空字段的作品（缺失门控，选集谓词与
                                         # metadata_search 刷新管线的 fill 字段表一致）；True = 每次刷
                                         # 全部关联作品
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

新建库使用命名 CHECK `ck_file_resources_work_fk`：`series_id/movie_id/audio_work_id` 至多一个非空，三个全空合法。`collection_id` 不参加该互斥约束，允许季资源同时关联所属合集；franchise 的三工作 FK 清空仍属于对应业务形态规则。旧表不会因 `create_all` 自动获得新约束，必须经过升级验证。

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
    subtitle_group: str | None           # 兼容旧字段；规范化后与 subtitle_groups 同步
    subtitle_groups: list[str] | None    # 字幕组列表，联合发布按成员保存
    subtitle_groups_source: str | None   # legacy|heuristic|llm|manual|unresolved
    episode: int | None                  # 集数
    season: int | None                   # 季数（解析证据 + DSL 字段；作品单季化后季号由季作品
                                         # 身份承载，本字段不再是派发/去重的判定分量）
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
                                         # "multi_season"=跨季包；"franchise"=多作品混合包；
                                         # "movies"=纯电影包（LLM 精判产出）
    batch_seasons: list[int] | None      # multi_season/franchise 包覆盖的季集合（torrent 内容
                                         # 检测持久化）；驱动合集内容覆盖度去重；NULL=覆盖度未知。
                                         # 作品单季化后为从关联季作品派生的冗余缓存（终态
                                         # multi_season 包清作品 FK、作品挂 resource_work_links，
                                         # links/assignments 变更处镜像重算）
    season_ranges: list[dict] | None     # 逐季集数范围 [{season, episode_start, episode_end}]；
                                         # 由 torrent 内容分析 / 向导保存时按 assignments 重算
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

### 合集富化表（资源↔作品多关联 / 文件级映射）

单作品资源继续使用上面的互斥 FK；合集资源的"多作品关联"与"文件↔作品映射"落在两张新表（`Base.metadata.create_all` 自动建表，无存量迁移）：

```
resource_work_links                    # 资源↔作品 多关联（合集场景 N 个作品）
    id: str                            # UUID pk
    resource_id → FileResource         # CASCADE
    series_id → TVSeries | None        # 与 movie_id 行级互斥（CheckConstraint）
    movie_id  → Movie | None
    source: str                        # "auto"|"llm"|"manual" —— provenance；自动层永不覆盖 manual
    created_at

resource_file_assignments              # 文件级映射：torrent 清单条目 → 作品/季/集
    id: str                            # UUID pk
    resource_id → FileResource         # CASCADE
    file_path: str                     # 清单相对路径，Unique(resource_id, file_path)
    file_size: int | None              # 冗余快照
    series_id/movie_id: str|None       # 双空=未指派（确定性层先于作品链接运行）
    work_title_hint: str | None        # 绑定具体作品前的簇标题提示（目录聚类/LLM 建议）
    season / episode_start / episode_end: int|None   # TV 专属；单集文件 start==end
    source: str                        # 同上；manual 永不被自动覆盖
    created_at / updated_at
```

序列化：`GET/PATCH/PUT` 单资源端点返回 `FileResourceDetailResponse`（在基础 schema 上附 `work_links[]` + `file_assignments[]`，需 `_DETAIL_LOAD_OPTIONS` selectinload）；列表端点保持精简基础 schema。

`FileResource.confirmation_ignored_at`：仅表示用户在 Dashboard 单个或批量永久忽略「文件资源元数据确认」的 UTC 时间。后台重解析不再写入或清除此列；人工忽略不会因任务成功、失败或重试消失。资源仍可在频道页检索和修订。临时隐藏由独立 `ResourceReparseRequest` 决定：请求存在且 `error_message IS NULL` 时暂不显示待确认；队列投递故障时显示待确认，人工忽略仍优先。

Dashboard 待确认扫描索引：`Index(confirmation_ignored_at, created_at, id)`，同时支撑未忽略资源过滤及稳定倒序分页扫描。

资源的 FK 互斥规则：
- 若为剧集资源，`series_id` 非空，`movie_id` 必须为空；具体集数统一使用 `episode` 字段。
- 若为电影资源，`movie_id` 非空，`series_id` 必须为空。
- 若为 franchise（多作品包）资源，`collection_id` 非空，`series_id`/`movie_id`/`audio_work_id` 三者必须全空。
- 若为 links-only multi_season 包（作品单季化终态形态），`series_id`/`movie_id` 同样全清，关联的多个季作品全部落在 `resource_work_links`；`collection_id` 可空。
- 系列级身份只解析到合集而季不可定的资源由 `park_resource_on_collection` 挂合集待确认：`collection_id` 非空、作品 FK 全清（非合集资源同时标 `episode_confidence="ambiguous"`）。
- 未识别资源两个 FK 均为空。

**合集资源识别**：`is_batch=true` 标识多集打包资源（Season Pack / 全集 / `S01E01~13` / `[01-12 合集]` 等）。判定分三层：

1. **Pre-parser**（`app/services/resource_parser.detect_batch`）：抓取时用正则识别典型 pattern，直接写入 `is_batch / episode_start / episode_end`。覆盖的范围形态：`SxxEyy~zz`、方括号内纯数字范围 `[01-12]`（后缀关键词可选）、**括号内尾部范围**（括号含标题文字但以范围结尾，如 `[青春猪头少年不会梦到圣诞服女郎 01-13]`）、**季标记上下文中的裸范围**（`S01 | 01-24`、`第2季 13-24`，季标记后 80 字符内；占有量词防 `S04 - 05` 单集回溯误判）、裸范围+强制关键词（`01-12 合集`）、`第01-第12话`；连接符含全角 `～`/`〜`，范围尾部容忍 `+SPx11` 类特典后缀。无边界关键词：Season Pack / Batch / BD-BOX / 全集|全季|合集|完整|完结 / Complete Series / **`TV fin`**（必须带 TV 前缀；裸 `Fin` 也是单集最终话用法，刻意不作关键词）/ 整理搬运 等。**整碟包规则**：标题含显式季标记（`S0x`/`Season N`/`Nst|nd|rd|th Season`/`第N季` 含中文数字）且解析不出任何集号且含整碟 token（`BD`/`BDRip`/`BDMV`/`BDRemux`/`Blu-ray`/`BD-BOX`，词边界）→ 判合集（`葬送的芙莉莲 第二季 (BD ...)` 形态）。sanity 过滤：`end-start>200` 或 `end>999` 判误报（挡 `[2020-2021]` 年份对）。命中时同时**清空 `resource.episode`**（field_mapping 可能把年份/分辨率/标题数字解析成单集号）并设 `batch_scope="season"`（标题层默认单季包，torrent 分析可修正）。
2. **Torrent 内容检测（通道 A，`app/services/torrent_inspect.maybe_inspect_torrent`）**：metadata 匹配前，对 `is_batch=false` 且 `torrent_url` 为 http(s) 直链的资源下载 .torrent 落盘（`TORRENT_CACHE_DIR`，记 `torrent_file`），bencode 解析文件清单 → `analyze_torrent_files` 纯函数按视频文件过滤、路径分量集号提取、顶层目录聚类判出 scope：`single`（≤1 视频文件，不改判）/`season`（单季多集，填 episode_start/end）/`multi_season`（≥2 季标记，清空 season 与 episode_start/end，并把覆盖季集合持久化到 `batch_seasons`）/`franchise`（≥2 作品簇，同写 `batch_seasons`，触发 `franchise_service.link_franchise_pack`；父 WorkCollection 仅由清洗后的资源标题确定并立即创建/复用，资源先挂 collection 且清除单作品 FK，TV/OVA/Film 等子目录成员解析只作尽力富化，失败/超时不能阻止父合集成立）/`unknown`（不改判）。文件映射富化同时在标题未解析出字幕组时尝试读取视频文件尾缀 `...-GROUP.ext`/`...[GROUP].ext`；仅唯一一致且非技术词的结果回填 `subtitle_group`。magnet 与下载/解析失败静默跳过。下载后 RPC 修正（通道 B）保留同一富化能力。
   **文件关联富化 pass（所有作品形态）**：① 确定性写回——按统一 `analyze_torrent_files` 的 `file_parses` 为单集 TV、单部电影及各类合集 upsert `resource_file_assignments`（source=auto，簇目录名作 `work_title_hint`）；作品链接完成后 `bind_single_work_assignments` 把单一作品绑定到这些行，TV 同时补季号/特别篇 S00；合集另重算 `season_ranges`。② LLM 精判只对合集门控（`app/services/batch_content_analysis.py`：scope=franchise，或 is_batch 且未解析集号占比 ≥0.5 且视频数 ≥2 且配置了 LLM key）——区分纯电影包与混合包、把电影簇经频道源 `process_title_only` → Movie 落库并绑定 links+assignments（source=llm）；失败/无 key 静默降级。Magnet 在抓取期无法取得清单时，由下载完成通知生成前用下载器清单运行同一确定性分析补齐。
3. **MetadataAgent**（LLM）：finalize schema 输出 `is_batch / inferred_episode_start / inferred_episode_end` 与可选 `batch_scope`（白名单 season|multi_season|franchise|movies，表外值丢弃）；LLM 输出的非空值覆盖 pre-parser 结果（`is_batch` 单向 OR 合并，只会补 True 不会改 False）；`batch_scope` 仅当现有值为 NULL/"season" 时写入（torrent 分析的 multi_season/franchise/movies 不被降级），LLM 未输出时默认 `"season"`。

合集资源约束：`episode` 字段固定为空（避免与"单集集数"语义混淆）；`episode_start/end` 尽力而为，标题未标明时保留为空。**合集去重按内容覆盖度**（`agent_service._batch_coverage_key`）：电影包→`movie_id`；单季包→`("season", series_id)`——季作品身份即覆盖度（legacy 未拆分系列级行过渡态仍用解析季号 `("season", season)`）；跨季包→`("multi_season", links 关联季作品 id 集合)`（legacy 带 FK 行回退 `batch_seasons` 季集合）。仅当覆盖度已知且完全相同的多个版本才进入与单集一致的冲突解决（ask → PendingDecision；auto → LLM pick → 启发式），跨运行则按同 agent + 同覆盖度的已占用任务判重跳过；这里除活动态/`completed` 外，也包括 organize `move` 后的 `cancelled + completed_at 非空` 历史任务，从未完成即取消的任务不占用。覆盖度不同（S1 包 vs S2 包）的合集不去重、各自派发。**覆盖度未知（season 包无季号、multi_season 无 batch_seasons 且无关联季作品 links）不再派发**——它进入所属 Channel 的文件资源待确认，不创建 Agent PendingDecision；人工修订补齐后定向重跑。franchise 包作品 FK 全清，不进入派发。

**跨季集号 reconciliation**：部分 RSS 标题使用**绝对集号**（跨全部季数累加），例如「关于我转生变成史莱姆这档事 第四季 S04 - 84」中的 `84` 实际是从第一季累计到第四季当前集的绝对数，而不是第四季的第 84 集。为了让 Agent 侧的 `(series_id, episode)` 去重语义稳定（作品单季化后季已编码在季作品身份，冲突/去重槽位去 season 分量），在 `_apply_to_resource` 里根据 metadata 的 `seasons: [{season_number, episode_count}]` 证据做一次调整；链接后路径统一走共享 helper `reconcile_linked_series_resource`（metadata_service）：①历史惯例推断 → ②无季标记但有 `absolute_episode` 的资源沿链接作品的**合集成员**定位（`locate_absolute_episode_in_collection`：成员按 `season_number` 排序、各作品 `number_of_episodes` 累加）并**重指向**定位到的季作品 → ③季作品自身集数的单季算术（`seasons_map_for_work`：legacy 未拆分行回退惰性 `seasons` 列）→ ④验证季默认（`resolve_missing_work`，作品身份即最强季证据；系列级匹配但季不可定的资源由 `park_resource_on_collection` 挂合集待确认）。

- **`NN(MM)` 双标记**（如 `13(85)`）——pre-parser 直接抽取，`episode=13`，`absolute_episode=85`，`episode_confidence="reconciled"`。若标题未解析出季数（`season=None`），`apply_episode_reconcile()` 会用 `locate_absolute_episode()` 按各季集数累减反推 `(season, episode)` 并**同时写回两个字段**；超出总集数 + tolerance(2) 时记为 `ambiguous`。尚在更新时 metadata 少报的 tolerance 集数保留真实推导值（如已知 E7 后的 E8），不钳回旧集号。
- **只标了 MM**（如 `S04 - 84`）——`reconcile_episode()` 检查 `raw_episode ≤ season_count + tolerance(2)`：符合就保留（`raw`）；否则减去前几季累计集数得到 candidate；candidate 落在 `[1, season_count + tolerance]` → 记为 `reconciled`（写回 `absolute_episode`），否则记为 `ambiguous`。
- `apply_episode_reconcile()` 跳过条件：合集资源；`episode` 与 `absolute_episode` 均为空；`episode_confidence == "manual"`；以及 `season` 已知且已为 `reconciled` 的资源（不重算）。无判定依据（空 map / 未知季）时仅给无标记资源补上 `raw`。
- **人工历史推断**：已链接单集 TV 资源在上述算术前运行 `apply_episode_history_reconcile`。同频道+同作品+同发布组的相邻 `manual` 记录可确定 `(season, absolute_episode-episode)` 惯例；无同组证据时至少两个发布组或两个不同 absolute 样本完全一致才自动外推。只使用目标前 2 集内的已结构化历史；冲突/超界仍为 `ambiguous`，`manual` 永不覆盖。成功时原始数字写入 `absolute_episode`，结果标为 `reconciled`。
- `ambiguous` 的资源**不参与派发**。它仅进入所属 Channel 的文件资源待确认，不创建 AgentSuggestion 或 PendingDecision；用户修正为 `manual` 后，定向 Agent 运行重新进入正常 filter→派发流程。
- `ambiguous` 只对**单集 tv 资源**有意义：合集资源（`is_batch`，按内容覆盖度去重）与电影链接资源（无集号/季号问题）携带的 ambiguous 一律为残留标记——派发流程的 ambiguous 分支跳过这两类；Dashboard「待确认」列表（`pending_confirmations`）同样排除。各人工修订入口负责了结残留：标记为合集（PATCH `/resources/{id}`）置 `manual`；重新链接为电影（`/metadata/link`）或将作品 `content_type` 改为非 tv（PUT `/series/{id}`）置 null。存量遗留行由启动轻迁移 `ambiguous_stale_clear` 一次性清理（合集→`manual`，电影链接/非 tv 作品链接→null，app_settings 哨兵保证只跑一次）。
- `episode_confidence` 值：`raw` / `reconciled` / `ambiguous` / `manual` / `None`（老数据）。

**reconciliation 的触发路径**：早期只在 MetadataAgent 的 `_apply_to_resource` 里执行，导致免 Agent 的链接路径（已知作品短路、ChannelRawTitleMapping、本地模糊 auto-link ≥85）完全绕过 reconcile——同一作品的新集恰恰都走这些路径。现在 `reconcile_linked_series_resource()` 作为统一的链接后步骤挂到全部四条路径：agent 完整路径优先用当次 `matched_entity.seasons` 做算术，其余路径（及 entity 缺 seasons 的兜底）用 `seasons_map_for_work`（季作品自身的 `season_number`→`number_of_episodes`；legacy 未拆分行回退惰性 `seasons` 列）。`NN(MM)` 预解析不受影响，仍在抓取期先行处理。

**Metadata prompt 的作品历史 few-shot**：MetadataAgent 构造生产 prompt 时，若标题与本地库中某 series 模糊匹配（≥70），注入该系列的集数编号约定（`seasons` 每季集数）和最近 5 条 sibling 解析示例（`episode_confidence` 为 `reconciled`/`manual` 的资源：title → S/E + absolute），引导模型与历史解析保持一致的季/集编号。

存量修复脚本：`scripts/series_seasons_backfill.py`——为缺 `seasons` 的 series 从 TMDB 补齐每季集数，并对 `episode_confidence` 为 NULL/`raw` 的单集资源重跑 reconcile（dry-run 默认，`--apply` 生效；需在 app 停止时运行，Turso 单进程文件锁）。

### TVSeries（剧集系列 - Metadata 缓存）

> **作品单季化（per-season works，已实施）**：一个 TVSeries 行 = 该 IP 的**恰好一季**；系列（IP）级关系由 `WorkCollection` 承载（每部剧集作品必属一个合集，单季作品套壳合集）。终态设计与迁移方案见 [per-season-works.md](per-season-works.md)。

```python
class TVSeries(Base):
    __tablename__ = "tv_series"

    id: str                              # UUID
    title_cn: str | None                 # 中文标题（基础剧名，剥季号；季限定变体进 aliases）
    title_en: str | None                 # 英文标题
    original_title: str | None           # 原始标题（原名）
    aliases: list[str] | None            # 别名列表，自动积累合并（去重；含季限定标题变体）
    search_text: str | None              # 归一化搜索 haystack：title_cn+title_en+original_title+aliases
                                         # 过 normalize_title（NFKC+OpenCC t2s+小写）的拼接；由 ORM
                                         # before_flush 钩子同事务维护，启动时空值回填。Turso 镜像进 FTS
                                         # 边车（fts_outbox drain），PostgreSQL 上被 pg_trgm GIN 索引
    external_id: str | None              # 外部 ID（MetadataAgent 返回的参考 ID，如 TMDB/MAL/IMDb/Wikipedia ID；
                                         # 季作品可袋合成季身份 {系列级id}#s{N}，见 WorkExternalId）
    external_source: str | None          # 枚举字符串: "exa" | "tmdb" | "wikipedia" | "manual" | "local_match" | "llm_search"（旧版遗留）
    description: str | None              # 简介
    poster_url: str | None               # 海报本地缓存路径，格式 /posters/{hash}.jpg
    rating: float | None                 # 评分（0-10）
    genre: list[str] | None              # 类型标签（封闭 TMDB 27 类英文 canonical 名，取值约定见下文「genre 取值约定」）
    status: str | None                   # 剧集状态: "Ended" | "Returning Series" | "Canceled" 等
    season_number: int                   # 本作品是 IP 的第几季：NOT NULL DEFAULT 1（轻迁移加列）；
                                         # 0 = 特典/SP（Plex Specials 约定）。身份属性，编辑页只读
    number_of_episodes: int | None       # 本季集数
    number_of_seasons: int | None        # 惰性孤儿列（作品单季化退役，永不写入；仅 legacy 未拆分行
                                         # 上可能残留系列级数据，season_split_migration 后不再被读取）
    seasons: list[dict] | None           # 惰性孤儿列（同上；每季集数 [{season_number, episode_count}, ...] 的
                                         # 旧系列级建模，读取侧仅剩 seasons_map_for_work 的 legacy 回退）
    start_date: date | None              # 本季首播日期（逐季源天然给出；系列级源取自该季数据或
                                         # NULL 待刷新——注意 Channel 必选字段 year 门禁依赖它）
    end_date: date | None                # 本季完结日期
    content_type: str | None             # "tv" | "anime" | "mixed"
    is_anime: bool | None                # 三态动漫标记：True=日本动画 / False=确认实拍 / None=未判定；
                                         # 与 content_type（媒介）正交（剧场版动画两者独立），
                                         # 判定与赋值规则见下文「is_anime 判定约定」
    manually_edited_fields: list[str] | None  # 人工编辑保护：用户经作品详情页「编辑」表单改过的字段名列表
                                         # （JSON 数组，取值 ⊂ MANUAL_EDITABLE_FIELDS，见 metadata_service）；
                                         # 自动扫描（upsert / apply_is_anime / 频道默认标记 / bangumi 验证）
                                         # 跳过其中的字段；刷新管线（apply_work_metadata）默认同样跳过，
                                         # 仅当刷新对话框勾选「覆盖所有人工编辑字段」时才覆盖。
    canonical_name: str | None           # 规范化名称（跨数据源消歧/搜索用的标准名）
    wikipedia_url: str | None            # 维基百科条目 URL
    wikipedia_page_id: int | None        # 维基百科 pageid（供维基数据源回填/海报抓取使用）
    collection_id: str | None → WorkCollection  # 所属合集 FK；单季化后剧集作品**必属一个合集**
                                         #（无自然合集时套 `series_group` 壳合集）；合集内
                                         # (collection_id, season_number) 部分唯一索引
                                         # uq_tv_series_collection_season（WHERE collection_id IS NOT NULL）
                                         # 新装与轻迁移均建立；冲突历史库须先预检/修复再升级
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
- **自动扫描跳过**：`create_or_update_*_from_external` 更新分支、`apply_is_anime`、`apply_channel_default_is_anime`、`maybe_verify_is_anime_via_bangumi` 在写任一字段前检查 `field_manually_edited`，命中即不改写该字段（新建作品无该列表，不受影响）；`content_type`/`external_id`/`external_source` 三字段在 upsert 与刷新管线（`apply_work_metadata`）中同样受此守卫（`override_manual_edits=true` 时可覆盖）。
- **刷新元数据**：刷新管线（`refresh_work_by_source` → `preview/apply_work_metadata`）默认跳过 `manually_edited_fields` 中的字段；仅当请求带 `override_manual_edits=true`（作品模块刷新对话框「覆盖所有人工编辑字段」，`POST /works/metadata/apply`）时才覆盖。批量/周期刷新不传该 flag，恒为默认（不覆盖）。

### WorkExternalId（作品外部身份袋 - Phase P3）

身份登记要求对应 series/movie/collection 拥有者存在；PG 登记事务以 KEY SHARE 保持拥有者存活。剧集/电影删除在同事务中清理身份袋，不留下阻断身份重新登记的孤儿。历史孤儿仅由显式审阅离线工具清理，见 db-migration.md。

"身份袋"反向索引：一个作品可携带**多个**外部身份（创建时的 wikipedia pageid、langlinks 各语言页的 pageid、Exa 回退命中的 tmdb/bangumi id …），任何一个已知 `(source, external_id)` 都能确定性地反查回作品行，使跨源/跨语言 upsert 收敛不再依赖标题运气。

```python
class WorkExternalId(Base):
    __tablename__ = "work_external_ids"
    __table_args__ = (UniqueConstraint("source", "external_id"),)  # 一个 id 至多映射一个作品

    id: str                              # UUID
    work_type: str                       # "series" | "movie" | "collection" —— work_id 指向哪张作品表
                                         #（"collection" 为作品单季化放开的第三值：系列级源 id 落合集袋；
                                         # 该表无 CheckConstraint，无 schema 变更）
    work_id: str                         # 跨表引用（tv_series.id / movies.id / work_collections.id），故意不带 FK
    source: str                          # registry 源名（wikipedia/tmdb/bangumi/mal/anilist/imdb/douban）
    external_id: str                     # 完整 canonical "source:id" 字符串（镜像 TVSeries.external_id 约定）
    created_at: datetime
```

**语义与规则**：

- **身份粒度与放置（作品单季化）**：注册表 `SourceSpec.granularity` 声明每个源的 TV 身份粒度（`"series"`：wikipedia/tmdb/imdb 一个 id 覆盖全季；`"season"`：bangumi/mal/anilist/douban 逐季条目；tmdb/douban 的电影形态为 `"movie"`，`granularity_of(source, content_type)` 判定）。逐季源 id 袋在季作品上（`work_type="series"`）；系列级源 id 袋在**合集**上（`work_type="collection"`）；季作品另袋**合成季身份** `{系列级id}#s{N}`（如 `tmdb:82684#s3`，`make_season_identity`/`split_season_identity`），使「同系列同季」的重复匹配直接袋命中。`canonicalize_external_id` 对 `#s{N}` 后缀先剥离、归一化后重附（透传），展示链接渲染时按系列级 id 出链。
- **主 id 规则（creator-wins）**：`TVSeries.external_id/external_source`（及 Movie）仍是创建时确定的展示/主 id；后续发现的 id 只进袋，绝不抢占主 id 列。wikipedia 主 id 特例外（见下）：不同 pageid 是同作品的不同语言版本页面，upsert 重匹配时主 id 不随来源语言翻转而保持稳定（`_merge_primary_external_id`）。
- **存储约定**：`source` 存 registry 源名，`external_id` 存完整 canonical `source:id`；裸 id（如纯 pageid）写入/查询时一律补 `source:` 前缀归一；非 registry 源（如 `llm_search`）跳过。
- **wikipedia id 带语言版本**：pageid 是每个语言版本各自编号的，canonical 形式为 `wikipedia:{lang}:{pageid}`（如 `wikipedia:zh:7301786`）；早期存量为无语言的 `wikipedia:{pageid}`。写入点（auto-link/judge/audio resolver/upsert 入口 `_qualify_incoming_wikipedia_id` 经 `wikipedia_url` 宿主推导）一律产出带语言形式；袋查询与作品列查询双形式兼容（`wikipedia_match_keys`：带语言精确匹配两种形式，裸 id 额外 LIKE 匹配任意语言）；同作品加带语言 id 命中裸行时原地升级为带语言形式；不同作品的同数字 pageid（跨语言撞号）不互抢（记 warning）。展示链接按语言渲染 `https://{lang}.wikipedia.org/?curid={pid}`，label 为 `Wikipedia (zh/en/ja)`；存量迁移走 `scripts/wikipedia_lang_backfill.py`（dry-run 默认 + `--apply`，标题锚定主页面语言 → langlinks 重解析各语言 pageid → 重写主 id/袋行并回填 `wikipedia_url`/`wikipedia_page_id`）。
- **no-steal**：`UniqueConstraint(source, external_id)` 保证一个 id 至多映射一个作品；把已属于作品 A 的 id 加给作品 B 时不改指、记 warning（该对成为去重候选）。
- **写入点**：upsert 成功时写入 matched_entity 的主 id + `alt_external_ids`（如 wikipedia langlinks pageids），按注册表粒度分别落季作品袋/合集袋（`_bag_entity_ids_by_granularity`）；去重合并（`_merge_series_group`/`_merge_movie_group`/跨表合并）对袋取并集（存留方继承重复方的主 id 与袋行，并幂等登记最终选定的有效注册来源主身份，保留 no-steal；wikipedia 行按 pageid 去重，与存储形式无关）。
- **回填**：`_apply_light_migrations` 启动时从存量 TVSeries/Movie 行的主 external_id 播种（幂等；仅 registry 源）。
- **读取**：`find_work_by_external_id` 只按同 `work_type` 反查——另一类型的袋命中在 upsert 中被忽略（跨表收敛由 metadata_repository 跨表守卫与每日去重负责）。

### WorkCollection（作品合集 - 大 IP 系列分组）

将同一 IP（攻壳机动队、蜘蛛侠、狮子王 …）的多个 TVSeries/Movie 归为一组。作品单季化后合集升级为**系列级元数据载体**：每部剧集作品**必属一个合集**（无自然合集时由 upsert 自动建 `external_source="series_group"` 的壳合集），系列级源身份（wikipedia/tmdb/imdb）落合集身份袋，季限定/跨语言标题变体收进合集 `aliases` 支撑两级标题兜底；Movie 的 `collection_id` 维持可空（电影合集逻辑不变）。

```python
class WorkCollection(Base):
    __tablename__ = "work_collections"
    __table_args__ = (
        UniqueConstraint("external_source", "external_id"),  # 幂等 upsert
    )

    id: str                              # UUID
    title_cn: str                        # 合集名（必填）
    title_en: str | None                 # 英文名（TMDB 电影详情不含，保持 NULL）
    aliases: list[str] | None            # 别名列表（季限定变体、跨语言标题；两级标题兜底的
                                         # 合集级匹配面，轻迁移加列）
    search_text: str | None              # 归一化搜索 haystack（title_cn+title_en+aliases 过
                                         # normalize_title；before_flush 钩子维护，**不进 FTS 边车**）
    manually_edited_fields: list[str] | None  # 人工编辑保护字段名列表（契约同作品的同名列）
    external_id: str | None              # 外部 ID（TMDB collection 为原始数字 id；Wikidata 为 franchise QID；
                                         # series_group 壳合集为 NULL）
    external_source: str | None          # "tmdb_collection" | "wikidata" | "series_group" | None；
                                         # 不用 canonicalize_external_id
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
    season: int                          # 季号；作品单季化后**恒等于所属作品的 season_number**
                                         #（去规范化，结构与唯一键不变，全部既有查询零改动）
    episode: int                         # 集号
    title: str | None                    # 单集标题
    air_date: date | None                # 播出日期
    created_at: datetime
    updated_at: datetime
```

填充来源（Phase P2）：由 wikipedia 主源的确定性剧集解析与 TMDB 逐季端点（P4）填充——季作品 upsert 在 `matched_entity.episode_list` 存在时调用 `upsert_episodes`（按该季子集）幂等 upsert（title/air_date；只增不删，`entity_granularity="season"` 的逐季源条目会把条目的集数重标到该季）；剧集详情 API selectinload 本关系。

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
    current_run_token: str | None        # 内部运行归属；只有最新启动的 attempt 可发布 Agent 摘要
    last_run_status: str | None          # 上次运行状态:
                                         # "success" | "failed"
                                         # | "pending_decisions"（当 dispatched=0
                                         #   且 pending_decisions>0 时使用；UI 据此
                                         #   显示"待决策"徽标而不是绿色 success）
    last_consumed_at: datetime | None    # 兼容时间展示，不作为增量准入游标。
                                         # NULL 且无发布进度时首次运行仅初始化；
                                         # 实际消费由 AgentPublicationProgress 控制。
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

活动任务与实时种子匹配索引：`Index(status, agent_id)`、`Index(downloader_id, transmission_torrent_id)`。

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

PendingDecision 只在一个场景创建：同一作品的同一剧集、同一电影或相同覆盖度合集出现**至少两个**已通过 Channel metadata/必选字段门禁及 Agent 规则的候选资源，且 `conflict_resolution="ask"`。单资源的 metadata 未识别、必选字段缺失、季集不确定或合集范围不确定一律属于 Channel 文件资源待确认，不进入本表。

**幂等性保证**：同一 `(agent_id, series_id | movie_id, season, episode, status='pending')` 键值全局唯一——Agent 反复运行时，`create_pending_decision` 会 upsert 已有行、合并新 `candidates`（保序、去重）、刷新 `reason` 和 `expires_at`。作品单季化后单集冲突的调用 key 为 3 元组 `(type, target_id, episode)`——季已编码在季作品身份，决策行的 `season` 分量由作品的 `season_number` 自动填充；合集冲突仍用 4 元组（episode 哨兵 -1）。已知限制：links-only multi_season 包无扁平作品 FK，其合集决策共享 `(series=NULL, season=NULL, episode=-1)` 幂等槽位（幂等列编不下作品 id 集合）。

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
    reason: str                          # 需要决策的原因（如："多个资源匹配第03集"）
    llm_suggestion: str | None           # LLM 对多候选的推荐理由
    llm_picked_resource_id: str | None   # LLM 选中的候选资源 ID（llm_enabled=true 时填充）。
                                         # 驱动 "AI 自动处理" 动作与决策 UI 中的高亮行
    decided_resource_id: str | None      # 用户最终选择的资源 ID（或 AI 自动处理选中的资源）
    status: str                          # "pending" | "decided" | "expired" | "skipped"
    expires_at: datetime | None          # 过期时间（默认 7 天）
    created_at: datetime
    decided_at: datetime | None
    updated_at: datetime
```

Dashboard 待决策扫描索引：`Index(status, created_at, id)`。

### AgentRun（Agent 执行记录）

V34 独立候选新增 `AgentRunLease`（handler 已接入，尚待完整验收）：UUID 主键，唯一 `run_id` 外键指向 AgentRun 并级联删除，UUID `token` 表示本次执行权，`expires_at_epoch BIGINT` 为数据库 UTC epoch 秒截止时间；`(expires_at_epoch,id)` 索引用于有界回收。租约独立于历史行，续租不更新 AgentRun。存量无租约 running 行不会自动判死；上线前还必须完成旧库升级和离线审核策略。

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

**逻辑版本化**：`METADATA_CACHE_GENERATION`（当前 7；gen 7 = Wikipedia/TMDB 主来源身份与可信别名接地；gen 6 = web fallback 白名单前置、输出身份绑定可见候选及季集/身份袋字段出口保护）标记产生缓存 verdict 的分类/判定逻辑版本。读取时 `generation != 当前版本` 的行视为未命中并懒删除——任何分类器、judge 提示词、匹配规则的变更只需 bump 该常量，旧逻辑产生的 verdict 即全部作废，不会短路修复后的代码。缓存失效不等于修复已有作品身份，不触发存量作品批量重绑。

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


## 整理计划版本与配置单例

整理模型完整关系见 [file-organization.md](file-organization.md)。`OrganizePlan` 增加可空 String(16) `file_op`、非空 Boolean `needs_category`/`manual_destination`（服务端 false）、非空 BigInteger `revision`（服务端 0）、可空 BigInteger `config_revision`、可空 String(36) `owner_token`。历史未知模式保留 NULL，执行前重建或拒绝；不能迁移成 move。

`OrganizeConfiguration`（`organize_configuration`）是内部单例：UUID v4 主键 `b72934dd-04bd-4cc3-8a68-ab1957b78027`，非空 BigInteger `revision`（服务端 0），可空 String(36) `lock_domain`。配置与版本同事务写入；锁域首次使用注册，后续不能自动改指另一目录。两后端启动轻迁移幂等添加字段并确保单例存在，关键列失败中止启动。


### NotificationBuildFailure（通知生成失败）

表 `notification_build_failures`：UUID v4 字符串主键 `id`；`download_task_id` 非空、唯一、引用 `download_tasks.id ON DELETE CASCADE`；`attempt_count` 非空整数、server default 1；`next_attempt_at` 非空 UTC DateTime 并建索引；`error_message` 非空 String(2048)；`updated_at` 非空 UTC DateTime，默认当前时间、每次失败更新。它仅记录生成快照的失败，不是 DownloadNotification 或 WebhookDelivery 的替代品；成功后删除。


### AgentResourceRequest（持久化定向重跑请求）

`agent_resource_requests` 使用 UUID v4 字符串主键；`agent_id`、`resource_id` 非空并分别 FK CASCADE，二者联合唯一。`revision` 非空默认 1，每次新修订原子递增；`requested_at` 为 UTC；`attempt_count` 非空默认 0；`next_attempt_at` 可空 UTC 且有索引；`error_message` 可空、最长 2048。新修订保留行 id、重置错误与退避；消费确认必须同时匹配 id 与 revision，防止旧运行删除新修订或删除后重建的请求。


轻量迁移新增的七处外键（audio/collection/volume/media_server 关联）在新装与升级库必须具有相同目标及 ON DELETE；已有列不代表约束已存在。启动检查并补齐缺失约束，旧悬空关联必须由明确业务证据修复，不自动丢弃子行或猜测父项。只读预检、Turso 原子重建与 PG 事务补约束见 [数据库迁移](db-migration.md)「轻量迁移的升级外键对等」。

TVSeries.number_of_seasons / seasons 为退役孤儿列：创建/更新 API 拒绝显式输入，去重与人工字段登记不再扩散计数。只读兼容不表示允许新写入；存量清理必须保留未拆季证据，不能以整表置空替代单季迁移。

## DecisionMigration：审核迁移档案

`decision_migrations` 保留显式审核迁移或作品重指 rekey 的决策快照和变换结果，与 pending 行变换同事务提交。`id` 为 UUID v4；`review_fingerprint` 为唯一的 64 字符审核指纹；`original_review`、`result` 为非空 JSON；`created_at` 为 UTC。档案不对 Agent/资源建立级联删除外键，业务实体删除不应删除审核证据。历史非 pending 决策不修改；旧 pending 仅在显式允许 supersede 时变为 expired，其原状态、候选、理由和人工内容完整保存在档案中。

### PendingDecision 覆盖身份

`decision_key` 为可空 VARCHAR(80)，`decision_scope` 为可空 JSON。新 pending 必须有 key；`(agent_id, decision_key)` 仅 pending 部分唯一，离开 pending 后可重新创建同覆盖选择。scope 使用 version=1 的规范单集/电影身份或 batch 完整覆盖描述；key 为其排序紧凑 JSON 的 v1 SHA256。升级不仅检查非空，也验证版本化描述与摘要一致；历史非 pending 可保留空键。确认必须复验候选的当前覆盖与资格，不能把摘要一致当成当前资源仍合格。

自动作品重指档案的 original_review/result 标记 operation=work_rekey，记录重指资源后、改变 pending 状态前的快照；不得解释为人工批准的离线审核。


### 资源发布与 Agent 消费进度

以下模型主键均为 UUID v4 字符串；外键删除级联。

| 模型 | 字段与约束 |
|------|------------|
| `ChannelPublicationCounter` | `channel_id` 唯一 FK；`sequence` BigInteger 非空、默认 0、检查 >=0 |
| `ResourcePublication` | channel/resource FK；`sequence`、`origin_sequence` BigInteger，检查 `0 < origin_sequence <= sequence`；`kind ∈ {created,metadata}`；UTC `created_at`；唯一 `(channel_id,sequence)` 和 `(resource_id,kind)` |
| `AgentPublicationProgress` | `agent_id` 唯一 FK、`channel_id` FK；`generation` UUID 字符串；`baseline`、`cursor` BigInteger 非空且各自 >=0；nullable UTC `historical_created_after` |

每资源至多一条 created 和一条最新 metadata。创建序号永久充当 origin；metadata 替换时在同一事务删除旧事件并写入更高序号，不重编号。资源删除级联清理事件，频道计数器不回退。

baseline 和 cursor 分别表示历史准入与消费确认，不要求 cursor>=baseline。普通频道切换 cursor=0，baseline 取新频道当前前缀，历史时间下界沿用旧 last_consumed_at；显式回填清空历史下界。指定扫描可扩展历史准入并在处理前回退 cursor 以保留失败重试，确认只在同 generation 内前进。全历史以 datetime.min 表示下界。业务协议见 business-logic.md，升级步骤见 db-migration.md。

### DownloadDispatch（队列派发预留）

队列派发的稳定身份表 download_dispatches：id 为 UUID v4 主键；operation_key 为唯一 SHA256 字符串；task_id 为唯一预留 UUID v4；parameters 为冻结 JSON（资源、Agent、下载器、目录和 payload 摘要）；settled 为非空 Boolean，数据库默认 false；created_at 为 UTC。task_id 在任务创建之前预留，因此不指向 DownloadTask 外键；任务删除后保留记录以阻止旧执行重建。身份 JSON 的 ID 是不可变操作快照，不建立级联外键。新增可空 job_key（512）与 job_id（32）保存逻辑队列身份，created_at 建索引；身份缺失时保守保留。正常 queued 派发始终填入两字段。

`WebhookDelivery.attempt_token` 为 nullable String(36)，表示当前发送尝试/失效代次；旧行 NULL 可被首次条件领取。发送、人工重试及快照更新均更换 UUID，结果按 id/token/pending 条件写入。

## OTP 请求额度（auth_rate_limit_buckets）

AuthRateLimitBucket 使用 UUID v4 主键；bucket_key 为唯一 String(72)，global 为全局桶，peer: 后接服务器解析来源地址的 SHA-256。attempts 为正整数；resets_at 为 UTC DateTime 并建索引。表不保存验证码或密钥。全局及来源的条件 upsert 在同一短事务中完成，提交后才执行 OTP 验证；验证失败不得回滚额度。过期来源桶按 resets_at/id 排序每次最多删除 100 行。

Turso 单进程内，同一引擎的额度短事务串行执行，防止全局桶的 MVCC 写冲突耗尽重试。锁不保存计数或决定是否放行；持久化数据库始终是额度依据。PostgreSQL 继续由条件 upsert 和行锁协调跨进程并发，外部写冲突仍使用独立事务重试。


### Turso 资源父行并发保护

Turso CONCURRENT 的 child FK 写入不具备 PostgreSQL KEY SHARE 的父行保护。`resource_work_links`、`resource_file_assignments`、`download_tasks` 的 INSERT/UPDATE 各有一个 `trg_resource_parent_*` 触发器，对 NEW 指向的 FileResource 执行 `UPDATE file_resources SET id=id`。此等值写入只推进 MVCC 行版本，不改变 ID、updated_at 或其他业务值；它使并发旧快照删除发生写冲突，避免已提交 child 变成孤儿。三种表的 INSERT/UPDATE、SAVEPOINT/先前写入两种旧快照均须验收。PostgreSQL 不安装这些触发器，使用原生 FK 锁与清理服务的锁定/复查。

### ResourceReparseRequest（持久重解析请求）

`resource_reparse_requests` 每资源至多一行；`id` 为 UUID v4 主键，`resource_id` 为非空唯一 FK→file_resources（ON DELETE CASCADE）。`requested_at` 为 UTC 请求时间；`next_attempt_at` 为非空 UTC 下次补发时间（带索引）；`attempt_count` 为投递故障次数，默认 0；`error_message` 为可空 String(2048)，仅写固定脱敏错误类别。重复请求不更新时间或身份。

请求与入队分开提交，队列只是唤醒；handler payload 携带 `request_id`，成功或普通失败均仅删除自身 ID 对应请求。人工忽略字段永不被该生命周期改写。失权不确认；旧请求的迟到确认不能删除重建的新 UUID。表由统一迁移入口 create_all 创建，存量 confirmation_ignored_at 无法可靠区分人工忽略与旧任务标记，禁止批量清空。

重解析取消不是普通失败：CancelledError 等 BaseException 中断时保留持久请求，由 RedisQueue 重排描述符并交接消费者；仅成功或普通 Exception 终态才确认自身请求，且确认仍需有效所有权。真实 Redis worker.stop 取消回归覆盖原任务 ID 重放和最终确认。

### Channel / Agent 关系加载

Channel 的 `file_resources/agents/raw_title_mappings` 和 Agent 的 `works/download_tasks/pending_decisions/suggestions/notifications/webhooks` 使用 `lazy="raise"`。读取父实体不会默认实例化全部关联历史；业务需要的集合通过显式 selectin/refresh 或直接查询读取。缺失加载不能通过异步隐式 SELECT 兜底。此项仅改变 ORM 加载策略，不改变 FK、cascade 或数据库结构。

`works` 也不默认加载：仅限定范围下最多 10 条，频道全范围覆盖配置可能更多。规则处理在集合未加载时显式 refresh 已持久化 Agent 的 works；已经加载的本事务规则不被强制刷新。列表/详情按响应需求显式加载目标作品，任务/历史存在性校验只读取父实体或所需标量。


### 完整搜索文本的存储

`TVSeries`、`Movie`、`AudioWork`、`WorkCollection` 的派生 `search_text` 统一为 SQL `TEXT`，不使用 4096 字符上限。归一化后的全部标题及别名都必须保留；Unicode NFKC 可能扩大字符数，禁止按原始输入长度推断存储长度或截断尾部别名。原始 aliases 的 JSON 值保持原样。旧库列升级及空值回填见 db-migration.md。
