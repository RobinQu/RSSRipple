# 核心业务逻辑

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
  │     │     # 用户自定义 regex 一律按 re.IGNORECASE 匹配（"1080P"/"1080p" 等价）；
  │     │     # 解析后 resolution 统一正则化为小写 "Np" 形式（"1080P"→"1080p"，
  │     │     # "1920x1080" 等原样保留），降低订阅条件的编写成本
  │     ├─ c. 兜底提取 torrent_url：从 enclosure/link 中找 magnet 或 .torrent
  │     ├─ d. 创建 FileResource 对象（parsed_at = now）
  │     ├─ e. 统一 Metadata Agent（通过 LangGraph ReAct 循环，单次调用完成标题清洗 + 单数据源 metadata 搜索）
  │     │     agent = UnifiedMetadataAgent()
  │     │     await agent.process(resource, channel, db)
  │     │     # Agent 内部完成：标题清洗 → episode/season 推断 → 选择唯一数据源搜索
  │     │     # 生产抓取默认使用 Exa Agent Search；评测/手动搜索可选择 exa/tmdb/wikipedia
  │     │     # 结果写入 resource.search_title, episode, season, series_id/movie_id
  │     │     # 并通过 MetadataCache(source="metadata_agent") 缓存
  │     ├─ f. fetch_and_link_metadata(resource, channel)  # 详见 Metadata 匹配流程
  │     ├─ g. 若 LLM 返回 poster_url 且为 http(s) URL → 下载到 POSTER_CACHE_DIR
  │     │     文件名: {sha256(url)[:16]}.{ext}，保存路径相对于 POSTER_CACHE_DIR
  │     │     更新 series.poster_url = /posters/xxx.jpg
  │     └─ h. db 批量提交
  │
  │     # 并发说明：e–h 的 metadata 处理按资源在独立短会话中并发执行（上限
  │     # MAX_METADATA_CONCURRENCY=4），但同一作品（规范化 search_title 相同）
  │     # 的资源在进程内串行锁下逐一处理并在锁内提交——否则多个同作品资源并发
  │     # 查不到彼此尚未提交的 series/movie 行，会重复创建同一作品的记录。
  │     # 跨进程（PostgreSQL 多副本）的重复仍由每日 04:00 dedup 兜底。
  │
  ├─ 4. 更新 channel.last_fetched_at = now, last_fetch_status="success",
  │        status = "active", last_fetch_error = null
  │
  └─ 5. 为该 channel 下所有 status="active" 的 Agent enqueue run_agent
```

### Resource Parser（字段映射解析引擎）

入口：`parse_entry(entry, field_mapping)`（`app/services/resource_parser.py`），按 Channel 的 `field_mapping` 配置把 feedparser entry dict 解析为 FileResource 字段。

支持两种格式：
- 新格式：`{"list_locator": {...}, "field_mappings": {field: {source, regex?, group?, transform?}}}`
- 旧格式（兼容）：`{field_name: {source, regex?, ...}, ...}`，整个 dict 视作 field_mappings。

单字段提取流程（`_extract_value`）：
1. `_resolve_source(entry, source)`：支持点路径与数组索引（如 `enclosures[0].url`）；路径任一段解析失败返回 None。
2. 可选 `regex` + `group`（默认 0）提取；一律按 `re.IGNORECASE` 匹配；正则不命中返回 None。
3. 可选 `transform`：`int` / `float` / `iso_datetime` / `lowercase` / `uppercase`；数值/时间转换失败返回 None。

其他约定：
- 单字段提取异常只记 debug 日志并置该字段为 None，不影响其他字段。
- `_postprocess_parsed` 把 `resolution` 统一为正则化小写 `Np` 形式（`"1080P"`→`"1080p"`；`"1920x1080"` 等原样保留）。
- `normalize_parsed_fields(title_raw, parsed)` 对 LLM 生成的 field_mapping 常见 regex miss 做**保守修复**：仅当 `title_cn`/`title_en` 泄漏方括号（多 bracket 标题只剥离了首个 `[...]`）时，从 raw title 重切作品名分段回填（`search_title` 优先取 latin 段）；并只在 tech 字段（resolution/source/video_codec/audio_codec/container）为 None 时从 raw title 补齐。已干净解析的资源不受影响。
- 同模块的 `extract_episode_fallback` 提供 episode/season 通用回退（`normalize_parsed_fields` 在 episode/season 为 None 时调用）：覆盖方括号集号 `[NN]`（限 1-3 位数字，`[1080p]`/`[2026]` 不误判）、`SxxExx`、`Season N`、`S N`、`第N季`（含中文数字）。各频道 field_mapping 的 episode 正则通常只覆盖 `- NN` 形式，该回退保证 fansub 方括号编号在抓取期即可解析；存量修复见 `scripts/repair_episode_parse.py`。
- 同模块还提供抓取期预解析器：`detect_batch`（合集识别）、`detect_absolute_episode`（`NN(MM)` 双标记提取）、`detect_subtitle_langs`（字幕语言 BCP-47 标签）、`strip_season_from_title`（去尾部季后缀），语义详见 data-models.md 的合集与集号 reconciliation 章节。

### Metadata 匹配流程（metadata_service）

入口：`fetch_and_link_metadata(db, resource, channel)`。

```
fetch_and_link_metadata(resource, channel, db)
  │
  ├─ Layer 1: 已链接 → 直接返回
  │     if resource.series_id or resource.movie_id: return
  │
  ├─ Layer 2: ChannelRawTitleMapping（search_title_key 优先，raw_title fallback）
  │     search_key = normalize_title(extract_search_title(resource))
  │     mapping = db.query(ChannelRawTitleMapping).filter_by(
  │         channel_id=channel.id, search_title_key=search_key
  │     ).first()
  │     if not mapping:  # 兼容旧数据
  │         mapping = db.query(ChannelRawTitleMapping).filter_by(
  │             channel_id=channel.id, raw_title=resource.title_raw
  │         ).first()
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
  ├─ Layer 4: 统一 MetadataAgent（仅当 channel.metadata_agent_enabled == True 时执行）
  │     调用 UnifiedMetadataAgent.process() — ReAct 循环
  │     数据源：默认 Exa Agent Search；评测/手动搜索可显式选择 exa/tmdb/wikipedia
  │     单次执行只允许调用所选数据源的工具，不做 TMDB→Exa→Wikipedia 级联或 fallback
  │     结果直接写入 resource.series_id/movie_id 并写 MetadataCache
  │
  └─ 全部失败 → resource 保持未链接（series_id/movie_id 均为 null）
```

`extract_search_title(resource)`（同步、无 LLM；Layer 2/3 的匹配 key 来源）优先级：

1. `title_cn` 或 `title_en`（field_mapping 已解析），剥离尾部季后缀（`第三季` / `S04` / `Season 4` / `3期`）后返回。
2. `title_raw` 正则清洗：去除首个 `[字幕组]` 括号对、按 ` / ` 分隔只取第一个 alt-title 段、依次剥离 `- 集数` 尾部 / `SxxExx` / 游离季标记（`S04`）/ 尾部独立集数 / 尾部单个质量标签括号，最后去季后缀并修剪装饰分隔符。
3. 清洗结果为空时兜底返回原始 `title_raw`。

该函数设计上是保守的：多括号标题（`[Group][Work][S04][13]`）无法用正则可靠分离作品名与发布元数据，交由 LLM agent 处理；本函数只需"足够好"以支撑本地 DB/FTS 匹配和 agent 禁用/失败时的兜底。

`create_or_update_series_from_external(db, data)` 逻辑：
- 按 `external_id + external_source IN (data.external_source, 'llm_search')` 查询是否已存在。
- 不存在则按标题精确回退：`title_cn/title_en/original_title`（含去季后缀形式）**加上 `alt_titles`**（Wikipedia langlinks 的跨语言页面标题）匹配已有行的三个标题列。
- 存在 → 更新字段（合并 aliases：新别名 append 去重；poster_url 若本地缺失则下载）；若原 `external_source="llm_search"` 则迁移为新 source。
- 不存在 → 创建新实体（aliases 同样纳入 `alt_titles`）。
- 返回实体。

`create_or_update_movie_from_external` 同理。

### Wikipedia 跨语言收敛（langlinks）

Wikipedia 的 page id 按语言站点独立编号，同一作品的 zhwiki 页面（如 `wikipedia:7727654`"黃泉使者"）与 enwiki 页面（`wikipedia:70545449`"Daemons of the Shadow Realm"）external_id 不同、标题槽各填一个语言，标题回退无法命中 → 同一作品被建成多行。修复：`_execute_get_wikipedia_page` 抓取页面时一并取 **langlinks**（仅 en/zh/ja），search-then-judge 两条 finalize 路径据此：

- 补齐缺失语言的标题槽（auto-link 直接填；LLM judge 仅在对应槽缺失时回填）；
- 把全部 langlink 标题放入 `matched_entity.alt_titles`，upsert 的标题回退与 aliases 合并都纳入它们。

效果：第二种语言的匹配通过 alt_titles 精确命中已有行实现收敛，且 aliases 一旦带上跨语言标题，每日 04:00 的 metadata 去重（按共享标题/别名聚类）也能自动合并历史重复行。

### Metadata 一致性防护（缓存版本化 / 跨表守卫 / 修正联动 / 跨表去重）

针对"错误 verdict 被缓存复用 + Movie/Series 分表产生跨表重复"这一类问题（案例：franchise 页被旧分类器误判 movie 后，新频道的同标题资源命中陈旧缓存，在已有 Series 的情况下又建了 Movie）：

- **缓存逻辑版本化**：`MetadataCache.generation` 记录产生 verdict 的逻辑版本（`METADATA_CACHE_GENERATION`），读取时旧代条目视为未命中并懒删除。分类/判定逻辑变更时 bump 常量即全部失效（详见 data-models.md）。
- **落库前跨表守卫**：`metadata_repository` 的 upsert 分发处，movie verdict 先按 canonical external_id 查 TVSeries（tv verdict 对称查 Movie）；另一张表已有同 external_id 的行时，翻转到已有行的 upsert 路径并记 warning——即使漏过一个错误 verdict，也不会产生跨表重复行。
- **手动修正联动清缓存**：手动 link（`manual_link_metadata`）提交后，按 `external_id` 删除命中的 MetadataCache 行，让用户的类型纠正当即传播到后续资源。
- **每日去重的跨表合并**：`merge_cross_type_duplicates`（随 04:00 去重任务运行）检测共享 canonical external_id 或共享归一化标题（含别名、简繁折叠）的 (Movie, TVSeries) 对。存留规则：任一侧存在带集号的资源或 Episode 行 → 保留 Series；否则保留 Movie。失败方的 FileResource / AgentWork / ChannelRawTitleMapping / PendingDecision 引用全部改指存留方并合并标题/别名后删除。

### Metadata Search Agent 数据源策略

MetadataAgent 不再采用多级搜索或跨数据源 fallback。每次搜索必须选择且只选择一个数据源，由 LLM 基于该数据源返回的证据做标题理解、集数/季数推断和最终结构化输出。

支持的数据源（`SUPPORTED_METADATA_SOURCES = {"tmdb", "exa", "wikipedia", "jina", "local"}`）：
- `exa`：Exa Agent Search，代码默认数据源（`DEFAULT_METADATA_SOURCE = "exa"`）。通过 Exa Agent API 创建 run，传入结构化 `output_schema`，轮询完成后读取 `output.structured.candidates`。适合 Web 证据覆盖面更广的标题搜索与评测。
- `jina`：Jina Search + Reader。通过 `s.jina.ai` 搜索 + `r.jina.ai` Reader 抓取页面 markdown 作为证据；廉价 web 搜索，对中日韩标题覆盖较好。
- `tmdb`：TMDB Search。仅使用 TMDB API 的搜索/详情工具，适合结构化影视库匹配。
- `wikipedia`：Wikipedia Search。仅使用 Wikipedia 搜索与页面工具，适合以百科页面为唯一证据的评测。
- `local`：仅本地 DB 匹配，不调用任何外部源（关闭 MetadataAgent 外部搜索时使用）。

数据源选择规则：
- 一个数据源当且仅当"启用开关开启 **且** 凭证已配置"时才在 UI（频道表单 / 作品库元数据刷新）中可选；`wikipedia` 无需凭证，仅看启用开关。启用开关环境变量：`EXA_ENABLED` / `JINA_ENABLED` / `TMDB_ENABLED` / `WIKIPEDIA_ENABLED`（默认 `true`）。
- 作品库"刷新元数据"动作使用的默认数据源由运行时设置 `default_metadata_source`（`app_settings` 表）决定，需用户在 UI 主动选择一个可用数据源；未配置时刷新请求返回 400。频道级的 `metadata_source` 在频道表单中单独选择。

兼容规则：
- `combined` 仅作为旧评测数据集值保留；运行时归一化为默认 `exa`，不得作为新数据集或新搜索任务的数据源类型。
- 单次搜索必须保持"只使用一个数据源"的约束，不得跨数据源 fallback。
- eval 标注平台的新建 Dataset 必须人工选择 `exa` / `jina` / `tmdb` / `wikipedia`，数据集名称以前缀标明数据源（例如 `exa-eval-...`），并把 `data_source_type` 写入每条 entry、`resource_metadata.eval_data_source_type` 与 `agent_result.eval_data_source_type`。

### Wikipedia 匹配的 content_type 判定

Wikipedia 数据源没有 TMDB media_type 这类权威字段，tv/movie 由页面 **categories 关键词**推断（`_infer_content_type_from_categories`）：

- **TV 指示词优先**：页面分类含任何 TV 指示词（`television series`、`anime television`、`電視動畫`、`电视动画`、`電視劇`、`テレビアニメ` 等）即判 `tv`，即使同时含电影分类。原因：franchise/作品总页面（如轻小说系列）会同时携带各季 TV 动画分类与剧场版分类（如 `2021年日本電視動畫` 与 `2022年日本動畫電影` 并存），该页面代表的是剧集，电影有自己独立的页面；且电影关键词可能来自制作公司分类（如 `Liden Films` 含 `films`），不能单独作为判 movie 的依据。
- 仅含电影指示词（`film`、`movie`、`電影`、`映画` 等）且无 TV 指示词时判 `movie`；两者皆无默认 `tv`。
- LLM judge 提示词遵循同一规则：TV 分类存在时 franchise 总页面一律判 `tv`。

该判定发生在确定性 auto-link（标题相似度 ≥ `AUTO_LINK_THRESHOLD` 且页面判为 work）与 LLM judge 两条路径；Movie 落库后无自动重分类机制，修正需走手动 search + link（见下文手动匹配流程）。

### Agent 运行生命周期（run_agent）

入口：task queue worker `_handle_run_agent(payload)`（`app/main.py`），payload 为 `{"agent_id", "resource_ids"?, "scan_since"?}`（`scan_since` 为 naive UTC ISO 字符串或 null，键的存在即表示"指定起始时间运行"）。每次运行持久化一条 `AgentRun` 记录（开始即插入 `status="running"`，结束回填计数与状态）。

**事务边界**（避免长时间持有 SQLite 写锁阻塞前台写请求）：handler 分两个阶段——阶段 1 短事务内插入 AgentRun、选定本次资源并立即提交；阶段 2 处理阶段（含 LLM 调用与 Transmission RPC）以 `process_resources(..., autocommit=True)` 运行，每完成一次派发/决策即增量提交，写锁绝不跨越外部调用持有。各单元操作幂等（任务去重 / 决策 upsert），块级锁重试或中途崩溃后重跑会跳过已提交部分。四种运行模式：

| 模式 | 触发条件 | 处理范围 | 水位线 |
|------|----------|----------|--------|
| **增量运行**（scenario ①） | `resource_ids` 缺省（fetch 触发 / 手动 run） | `FileResource.created_at > agent.last_consumed_at` 的资源，按 `created_at` 升序 | 运行后推进到所处理资源的最大 `created_at`；水位线为 null 时置为 now 且不处理任何资源（避免静默回填） |
| **定向运行**（scenario ③） | `resource_ids` 非空（如 `correct_episode`） | 只处理指定的资源，按当前规则评估 | **绕过**水位线、**不推进**水位线（资源可能较旧，推进会跳过其邻居） |
| **回填提交**（scenario ②） | rules-preview 后保存 Agent，`dispatch_resource_ids` 非 null | 派发用户选中的资源，并把水位线推进到频道当前最大 `created_at` | 推进到频道 max（或 now） |
| **指定起始时间运行**（scenario ④） | 手动 run 且 payload 含 `scan_since` 键（过去的时间点；null = 不限制，即全量历史） | `FileResource.created_at > scan_since` 的资源（按**入库时间**过滤；null 时不加时间条件取全部） | 只影响本次扫描范围；运行后照常推进到所处理资源的最大 `created_at`，下次增量运行恢复正常 |

scenario ④ 的 AgentRun 记录 `scan_since` 字段：null 表示增量/定向运行，`1970-01-01` 表示显式"不限制"全量扫描，其余为实际起始时间。用途：补派"符合订阅条件但从未成功下载"的较早资源（episode 去重保证已有活动/完成任务的集数不会被重复派发，error/expired 的旧任务会被重新派发）；`scan_since` 为未来时间时 API 返回 422。

> 旧实现的 `limit(200)`（按 `published_at` 取最近 200 条）已废弃——它会在高频频道上静默丢弃更早的资源。增量水位线保证每条资源都被且只被处理一次。

### Agent 过滤流程（agent_service）

入口：`process_resources(agent: Agent, resources: list[FileResource], db, *, autocommit=False)`，对**已由 run_agent 选定**的资源列表执行过滤、去重、冲突处理与派发。`resources` 的筛选（增量/定向）在上层完成，本函数只关心单次处理逻辑。`autocommit=True`（仅后台 run_agent handler 使用）在每次派发/决策后增量 `commit`；请求路径保持默认，由请求结束统一提交。

```
process_resources(agent, resources, db)
  │
  ├─ 1. 构造规则快照 rule_set = _build_rule_set(agent)
  │     含 scope_channel_wide / filter_config / work_by_series_id / work_by_movie_id。
  │     独立于 Agent ORM 对象，使 rules-preview 能比较 old vs new 规则而不改动持久化数据。
  │
  ├─ 2. 初始化 RunResult:
  │     candidates_by_key: dict[(type, id, episode?), list[FileResource]] = defaultdict(list)
  │     suggestions: dict[search_title, 分组] = {}
  │     matched_resource_ids: list[str] = []   # 本次通过 work-scope + filter 的资源
  │
  ├─ 3. 对每个 resource:
  │     │
  │     ├─ a. Metadata 前置检查:
  │     │     若 resource 未链接 metadata（series_id 和 movie_id 均为 null），
  │     │     不参与过滤/下载，归入 suggestions bucket（unrecognized++），等待用户手动修正。
  │     │
  │     ├─ b. work-scope + filter 评估（_resource_matches_rules）:
  │     │     matched, work = _resource_matches_rules(resource, rule_set)
  │     │     # scope=false 时需命中订阅作品；再与 effective_filter（全局 filter_config
  │     │     #   AND work.filter_overrides）求值
  │     │     if not matched:
  │     │         # 区分"在订阅范围内但 filter 未通过"与"不在范围内"
  │     │         if 在订阅范围内（scope_channel_wide 或命中 work）: filter_failed++
  │     │         continue
  │     │
  │     ├─ c. 集号不确定分支（episode_confidence == "ambiguous"）:
  │     │     在通过 work-scope + filter 之后才判定——只对 Agent 真会下载的资源询问。
  │     │     创建 PendingDecision（reason_override="集号不确定，需要人工确认集号: {title}"、
  │     │     candidates=[该资源]、skip_llm=True），pending_decisions++ 且 unrecognized++，
  │     │     continue。绝不自动下载集号不确定的资源。
  │     │
  │     ├─ d. 合集分支（resource.is_batch=True）:
  │     │     合集资源不参与 (series_id, episode) 聚合、不参与 PendingDecision。
  │     │     检查是否已有该 FileResource 的 active/completed 下载任务；
  │     │     若无 → dispatch_download 派发本条资源（dispatched++、matched++、
  │     │     matched_resource_ids 记录），continue。
  │     │
  │     ├─ e. 去重检查（仅单集资源）:
  │     │     电影：按 movie_id 查询 active DownloadTask，存在则跳过；key=("movie", movie_id, None)
  │     │     TV 单集：dedup = work.enable_episode_dedup if work else True
  │     │             dedup 且 episode 非空时按 (series_id, episode) 查询 active 任务，存在则跳过
  │     │             key=("series", series_id, episode)
  │     │
  │     └─ f. candidates_by_key[key].append(resource)；matched++；
  │           matched_resource_ids.append(resource.id)
  │
  ├─ 4. 候选聚合处理:
  │     for key, candidates in candidates_by_key.items():
  │         if len(candidates) == 1:
  │             dispatch_download(agent, candidates[0])  # dispatched++
  │         else:
  │             if agent.conflict_resolution == "ask":
  │                 create_pending_decision(agent, key, candidates, db)
  │                 # upsert 同一 (agent, target, episode, pending) 行；合并 candidates；
  │                 # llm_enabled 时调用 _generate_llm_pick 填充 llm_picked_resource_id
  │                 # 与 llm_suggestion（用户可点 "AI 自动处理" 一键采纳）
  │             else:  # "auto"
  │                 picked_id, _ = await _generate_llm_pick(agent, candidates, key)
  │                 # LLM 优先：llm_enabled 且配置 API key 时由 LLM 选择
  │                 # （agent.llm_prompt 优先，否则内置默认 prompt）；
  │                 # 未启用 / 调用失败 / 无有效选择时回退启发式 score_and_pick
  │                 chosen = LLM 选中候选 or score_and_pick(candidates, agent, work)
  │                 # 启发式评分：分辨率高（2160p>1080p>720p）> 文件体积大 > 发布时间新
  │                 dispatch_download(agent, chosen)
  │
  ├─ 5. 清理过期集号不确定决策（_resolve_corrected_ambiguous_decisions）:
  │     遍历本 Agent 的 pending 决策，凡 reason 以 "集号不确定" 开头、且其候选资源
  │     已被用户修正（episode_confidence != "ambiguous"，通常为 "manual"）的，标记为
  │     "decided"——资源已在本次或上次运行中重新进入正常 filter→派发流程。
  │
  ├─ 6. Suggestions 聚合: 将未识别但标题有意义的资源按 search_title 模糊聚类，
  │     保存到 AgentSuggestion 表，供前端一键添加作品
  │
  └─ 7. 返回 RunResult（dispatched / pending_decisions / filter_failed /
        duplicates_skipped / unrecognized / suggestions / errors / matched_resource_ids）
        ——run_agent 据此回填 AgentRun 记录与 Agent.last_run_status
```

**LLM 候选选择器**（`_generate_llm_pick`）：`conflict_resolution="auto"` 多候选自动选择、`"ask"` 模式下的 LLM 建议、以及 `POST /decisions/{id}/ai-pick` 共用同一逻辑。返回 `(picked_resource_id, reason)`：使用 `agent.llm_prompt`（若非空）否则内置默认 prompt（metadata 字段最完整 > 清晰度最高 > 带字幕 > 发布时间最新），要求 LLM 返回 JSON `{"pick": <候选编号>, "reason": "<一句话理由>"}`，`_parse_llm_pick` 兼容 markdown 包裹与裸数字兜底。发给 LLM 的候选摘要包含 `title`（资源原始标题）与关联作品的 `year`（电影 `release_date` / 剧集 `start_date` 的年份）、`rating`（0-10 分），无关联作品或字段为空时为 `null`；prompt 中附带字段说明。LLM 未启用 / 无 API key / 调用失败 / 未给出有效选择时返回 `(None, None)`，`"auto"` 回退到纯启发式评分（分辨率 > 体积 > 发布时间）。结果缓存在 `PendingDecision.llm_picked_resource_id`，AI 自动处理优先复用缓存值。

`dispatch_download(agent, resource)`：
1. 创建 `DownloadTask(status="pending")` 并 `db.add()`（**不立即 flush**：flush 会发出 INSERT 并持有 SQLite 写锁跨过整个 Transmission RPC）。
2. 解析下载目录：`effective_download_dir = join(downloader.download_dir, agent.download_subdir)`；若 `download_subdir` 为空则直接使用 `downloader.download_dir`。
3. 校验 `download_subdir`：必须是相对路径，禁止绝对路径、`..`、空段逃逸、控制字符；标准化后不得跳出 `downloader.download_dir`。
4. 将 `effective_download_dir` 写入 `DownloadTask.download_dir`，用于审计、重试与后续配置变更隔离。
5. 调用 `TransmissionWrapper.add_torrent(resource.torrent_url, download_dir=effective_download_dir)`。
6. 成功 → 更新 `task.status="downloading"`, `task.transmission_torrent_id=返回值`, `task.confirmed_at=now`。
7. 失败 → 更新 `task.status="error"`, `task.error_message=异常信息`；触发重试逻辑（若 retry_count < max_retries 则入队重试）。
8. RPC 结束后统一 `flush`，由调用方（请求路径）或 autocommit（后台路径）提交。

`dispatch_download` 的"创建任务 + 提交下载器"主体已抽取为 `create_and_submit_task(resource, downloader, db, agent_id, download_dir)`，供手动创建复用。手动创建下载任务（`POST /tasks`）完全绕过 Agent：用户从资源列表/资源详情选择 Downloader，后端直接以 `agent_id=None`、`download_dir=downloader.download_dir`（根目录，不加子目录）调用同一 `create_and_submit_task` 提交；不做去重检查（手动创建是显式用户意图，允许重复任务）。

### 手动 metadata 搜索与修正流程

```
用户在资源详情点击"修正 metadata"
  │
  ├─ 1. 用户输入 search_title、选择 content_type (tv/movie)
  │
  ├─ 2. 前端 POST /resources/{id}/metadata/search
  │     body = { search_title, content_type, data_source_type? }
  │     data_source_type 可选值: "exa"（默认）, "tmdb", "wikipedia"
  │     后端调用 MetadataAgent.process_title_only()，仅使用所选数据源 → 返回候选列表（不含本地落库）
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
  │       c. 写入 ChannelRawTitleMapping（upsert by channel_id+search_title_key）:
  │              raw_title = resource.title_raw
  │              search_title_key = normalize_title(extract_search_title(resource))
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
  ├─ 5. 每 5 分钟任务:
  │     metadata_backfill              # 重试可重试的未匹配资源
  │     fts_reconcile                  # FTS 影子表对账：全量 diff 基表 vs 影子表，
  │                                    # 修补调用点遗漏（脚本、去重合并、写入失败吞没）
  │
  └─ 6. 全局每日任务:
        cleanup_expired_tasks()  # 删除 completed 且 completed_at < now - task_expire_days 的任务
        expire_pending_decisions()  # 过期 pending decision → status="expired"
        _dedup_metadata()  # 04:00 运行：合并重复的 TVSeries/Movie 行（安全网，
                            # 防止 metadata agent 偶尔为同一作品新建第二行）。聚类 key 基于
                            # 共享的 title_cn/title_en/original_title **+ aliases**，
                            # 只折叠可证明为同一作品的行；幂等
```

任务队列使用 MemoryQueue（默认）或 RedisQueue（配置时），用于承载手动触发的 fetch/run；APScheduler 定时任务也通过 enqueue 投递到同一队列，保证同一 Channel/Agent 的任务串行执行（分布式锁，避免重复运行）。

### 下载状态同步

每分钟由定时任务调用。注意：`TransmissionWrapper` 依赖 transmission-rpc **v7 的 snake_case 属性**（`t.percent_done`、`t.left_until_done`、`t.is_finished` 等；唯一例外是 camelCase 的 `t.hashString`），用 camelCase 访问会静默拿到 `getattr` 默认值（进度恒 0、`left_until_done` 恒 0），导致任务被误判 completed。

```
sync_download_progress():
  │
  ├─ 1. 查询所有 status in ("downloading","queued","pending") 的 DownloadTask；
  │     另带自愈条件：status="error" 且 error_message 以 "Transmission unreachable"
  │     开头（历史故障级联遗留）且 transmission_torrent_id 非空的任务也纳入同步，
  │     下载器恢复后自动回到正常跟踪（无 torrent id 的任务从未提交成功，须走重试）
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
  │             task.error_message = None  # 同步成功即清除历史故障遗留的错误信息
  │             if t.is_finished or (t.left_until_done == 0 and t.total_size > 0):
  │                 # left_until_done 需搭配 total_size>0：magnet 元数据未下载时两者皆为 0
  │                 task.status = "completed"
  │                 task.completed_at = now
  │             elif t.status == "stopped":
  │                 task.status = "paused"
  │             elif t.status in ("downloading","queued"):
  │                 task.status = "downloading" if t.rate_download > 0 else "queued"
  │     except TransmissionError:
  │         # RPC 失败不代表任务失败（种子在 daemon 中照常运行）：
  │         # 只标记 downloader.status = "error"，任务保持最后已知状态，
  │         # 下次同步成功后自动恢复；不再级联把任务标记为 error
  │         downloader.status = "error"
  │     # 每个 downloader 处理后立即 commit：否则下一轮迭代的查询会 autoflush
  │     # 这些 UPDATE，导致 SQLite 写锁在整个 list_torrents RPC 期间被持有
  │
  └─ 3. db.commit()
```

### Mock Downloader（用于测试）

`DownloaderInstance.type = "mock"` 提供一个纯内存模拟器，行为如下：

- **连接测试**：`test_connection()` 总是返回成功（`"Mock Downloader 1.0"`），`free_space` 返回 1 TB。
- **add_torrent**：立即返回 `torrent_id`，同时把该"下载"记入内存 registry。
- **进度模拟**：每个 torrent 分配 `random.uniform(1, 10)` 秒的完成周期；`list_torrents/get_torrent` 按 wall-clock 计算 `percent_done`，到期后 `is_finished=True`；scheduler 的 `sync_download_progress` 因此能观测到进度增长并把 DownloadTask 标记为 `completed`。
- **pause/resume/remove** 均支持；pause 冻结 elapsed 计时，resume 继续。
- **状态存储**：模块级 `_STATE` dict，进程重启即清空——这正是测试所需的行为。

通过统一工厂 `app.clients.downloader.get_downloader_client(downloader)` 根据 `downloader.type` 分派到 `TransmissionWrapper` 或 `MockDownloaderWrapper`；两者共享同一异步接口（`test_connection` / `add_torrent` / `list_torrents` / `get_torrent` / `pause_torrent` / `resume_torrent` / `remove_torrent` / `free_space`），所有 Agent / scheduler / API 调用点均无需感知具体类型。

Mock downloader 面向本地开发和自动化测试；生产环境应使用 `transmission` 类型。

---

