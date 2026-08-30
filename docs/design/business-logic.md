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
  │     │     # 预解析同步内联：detect_batch / extract_compilation_work_title /
  │     │     # detect_subtitle_langs / detect_absolute_episode；合集命中即置
  │     │     # is_batch + batch_scope="season"（标题层默认，torrent 分析可修正）
  │     │     # 并清空 episode
  │     ├─ d2. torrent 缓存与通道 A 内容检测（metadata 匹配前，工作锁之外）：
  │     │     # ① 一律缓存：所有 http(s) 直链资源先 ensure_torrent_cached
  │     │     #   （先 bencode 校验完整 payload，只有有效清单才落盘
  │     │     #   TORRENT_CACHE_DIR 并写回 torrent_file；已有缓存先校验，
  │     │     #   无效缓存删除后再尝试下载；magnet/下载失败静默），保证后续
  │     │     #   文件清单查询（GET /resources/{id}/files）无需重新下载；
  │     │     # ② 合集分析门控：maybe_inspect_torrent 对 is_batch=false 的
  │     │     #   资源、以及已判合集但信息不完整（batch_scope 为 NULL，或
  │     │     #   scope="season" 缺 episode_start/end）的资源跑 bencode 解析
  │     │     #   文件清单 → analyze_torrent_files 判
  │     │     #   scope（season/multi_season/franchise）并补齐集数范围
  │     │     #   （single/unknown 不降级既有合集判定），franchise 触发
  │     │     #   franchise_service.link_franchise_pack（成员作品逐个走
  │     │     #   process_title_only 匹配落库 → get-or-create franchise_pack
  │     │     #   来源 WorkCollection → 资源挂 collection_id、作品 FK 全清）。
  │     │     #   通道 B（下载后 RPC 修正）为保留优化项未实现。
  │     ├─ e. 统一 Metadata Agent（通过 LangGraph ReAct 循环，单次调用完成标题清洗 + 单数据源 metadata 搜索）
  │     │     agent = UnifiedMetadataAgent()
  │     │     await agent.process(resource, channel, db)
  │     │     # Agent 内部完成：标题清洗 → episode/season 推断 → 选择唯一数据源搜索
  │     │     # 生产抓取使用频道主数据源（wikipedia/tmdb，默认 wikipedia）；
  │     │     # 评测/手动搜索与业务统一选择 tmdb/wikipedia/bangumi
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
- 同模块的 `extract_episode_fallback` 提供 episode/season 通用回退（`normalize_parsed_fields` 在 episode/season 为 None 时调用）：覆盖方括号集号 `[NN]`（限 1-3 位数字，允许发布变体后缀 `[01a]`/`[02v2]`，`[1080p]`/`[2026]` 不误判）、`SxxExx`、`Season N`、`N(st|nd|rd|th) Season`（如 "2nd Season"）、`S N`、`第N季`（含中文数字）。版本/字母后缀不写入集号，仅保留数值集号，且不参与去重。各频道 field_mapping 的 episode 正则通常只覆盖 `- NN` 形式，该回退保证 fansub 方括号编号在抓取期即可解析；存量修复见 `scripts/repair_episode_parse.py`。字幕组仍以标题/频道映射为第一来源；通用标题回退支持 `[Group]`、`【Group】`、`［Group］` 三种开头括号且不覆盖已有值。标题没有结果时，torrent 文件清单富化阶段调用 `extract_subtitle_group_from_filename` 识别海外发布常见的 `...-GROUP.mkv`/`...[GROUP].mkv` 尾缀，过滤纯数字和 codec/source 技术词，并且只有整份 torrent 得到唯一一致组名时才回填 `FileResource.subtitle_group`。
- 对已关联 TV 单文件资源，torrent 清单中的确定性文件名集号（包括 `[01a]`）还会回写资源级 `season/episode`（`episode_confidence=raw`）；电影和音频资源不套用该规则。
- 同模块还提供抓取期预解析器：`detect_batch`（合集识别）、`detect_absolute_episode`（`NN(MM)` 双标记提取）、`detect_subtitle_langs`（字幕语言 BCP-47 标签）、`strip_season_from_title`（去尾部季后缀），语义详见 data-models.md 的合集与集号 reconciliation 章节。

### Metadata 匹配流程（metadata_service）

标题解析对明确的中/拉丁双语分隔符（`/`、`／`）做保守拆分，去除季号、合集范围和技术尾缀后回填缺失的 `title_en`/`search_title`，不覆盖已有值。`FLAC`、`ALAC`、采样率和位深仅是编码质量信息，不能单独把资源判为 AudioWork；音频类型须由 ASMR、Drama CD、Radio、OST、Soundtrack 等语义标记或 metadata 结果确认。AudioWork 不参与 TV 合集范围校验，已链接 AudioWork 不进入自动未匹配回填。

Metadata source 的 `found=false` 不是作品标题匹配证据，不得用其 `clean_title` 覆盖 parser 已得的 `FileResource.search_title`，空值也不从失败 verdict 回填；只有确认命中才允许细化搜索标题。但发布标题解析与作品搜索结果相互独立：verdict（包括缓存的 `not_found`）若识别出 `subtitle_group`，应在资源字段为空时回填，已有频道映射或人工修订值不得覆盖；该回填须先于本地作品标题短路，避免旧 miss 被历史作品证据纠正时丢失有效字幕组。作品级标题短路索引除 TVSeries/Movie 自身标题与 aliases 外，还收录 `metadata_matched_at` 非空、无 collection 的历史已链接 FileResource 的 `title_cn/title_en/search_title`，把同频道过往成功修订/匹配形成的译名与罗马字拼写作为确定性别名；同一别名指向多个作品时仍按 ambiguous 排除，绝不猜测。

缓存命中顺序区分 verdict：成功与 `non_work` 可直接返回；普通 `not_found` 必须先让上述本地历史标题索引尝试短路，只有仍未识别时才应用缓存 miss，避免较早的搜索失败压住后来形成的人工修订/兄弟资源成功证据。franchise 资源一旦进入 collection 形态，随后普通 title-level metadata 即使命中单个作品也不得恢复 `series_id/movie_id/audio_work_id`，collection 所有权与 flat FK 互斥不变量始终优先。

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
  │         按 ratio 降序取 top1；若 top1 ratio >= 85 自动链接——但有两道守卫：
  │           ① resource.title_year（标题解析出的年份）与作品 start/release 年份
  │              相差超过 ±1 → 不自动链接（同名翻拍/重启防护，如 攻壳机动队 2026）；
  │           ② 归一化 search_title 同时精确等于 >1 个本地作品的 title_cn/title_en
  │              （TVSeries+Movie 合计）→ 不做 top-1 自动链接。
  │           被守卫拦截的候选与 70-84 分一样 fall through 到 Layer 4。
  │         否则跳过（留 LLM 层处理，避免误匹配）
  │     # Movie 同理
  │
  ├─ Layer 4: 统一 MetadataAgent（仅当 channel.metadata_agent_enabled == True 时执行）
  │     调用 UnifiedMetadataAgent.process() — ReAct 循环
  │     数据源：频道、评测与手动搜索统一使用 wikipedia/tmdb/bangumi；三主源未命中可走受可信站点约束的 wigolo 回退
  │     单次执行只允许调用所选数据源的工具，不做跨数据源级联或 fallback
  │     结果直接写入 resource.series_id/movie_id 并写 MetadataCache；
  │     若首选结果被 agent 标记为 work 级 ambiguous（`ambiguous: true`）→ 不链接，
  │     记录 not_found，保持可手动 link
  │
  └─ 全部失败 → resource 保持未链接（series_id/movie_id 均为 null）
```

**Bangumi 频道源匹配流程（`metadata_bangumi`，镜像 wikipedia 的 search-then-judge 形态）**：复用 wikipedia 的 query 清洗（`_candidate_queries`）→ `POST /v0/search/subjects` 限 `type=[2]`（动画分类，命中即动漫）→ 确定性 auto-link（归一化标题相等 + 标题年份守卫 ±1，**唯一命中**才免 LLM）→ 否则单次 LLM judge（专用 prompt `_BANGUMI_JUDGE_SYSTEM_PROMPT`）→ 命中条目经详情（`GET /v0/subjects/{id}`）+ 剧集（`GET /v0/episodes` 分页）端点展开为 matched_entity：`external_id=bangumi:{id}`（身份证据自动落 `is_anime=True`）、title_cn=name_cn、original_title=name、description=summary、rating=rating.score、start_date=date、number_of_episodes=eps、genre=tags（出口钳制）、`is_anime=True`；TV 条目不伪造 `seasons`/`number_of_seasons`，而携带仅在本次匹配链路使用的 `single_season_entry=true`——Bangumi 的一个 subject 本身就是一季，因此标题无季标记时可由统一缺季规则安全补 `season=1`，同时不污染作品级总季数；platform=剧场版时不写该证据。episode_list 取本篇（type=0）的整数 sort，season 标签用资源解析出的季号（无则 1）；platform=剧场版 → content_type=movie。found=False 走统一有序网络搜索回退（仅补身份）；缓存命名空间 `metadata_agent:bangumi`；手动搜索/评测（`process_title_only`）同样支持，手动搜索响应保留 `single_season_entry` 供链接时判定。客户端 `bangumi_client`：UA 固定 `robinqu/RSSRipple`，token 走 `runtime_config.bangumi_api_key`（env `BANGUMI_API_KEY` 或设置页覆盖），API 基础地址走 `settings.bangumi_api_base`（env `BANGUMI_API_BASE`，默认 `https://api.bgm.tv/v0`，可指向镜像/mock），无独立启用开关——token 即启用。

**季号验证规则（season never guessed）**：季号只能来自标题季标记或经元数据证据验证，绝不猜测。

- **Agent-free 链接路径**（Layer 2/3 与 Layer 4 链接成功后的 `_reconcile_with_series`，以及 S1 known-work 短路与 `manual_link_metadata`）：`apply_episode_reconcile` 之后若 `resource.season` 仍为 None，统一走共享 helper `resolve_missing_season(resource, entity)`（metadata_episode_reconcile；`entity` 为 `{number_of_seasons, seasons, single_season_entry}` 证据 dict）——剧集恰好可验证为 1 季 → `season = 1`；多季或季数未知 → `episode_confidence = "ambiguous"`（进入所属 Channel 的文件资源待确认）。`season_evidence_from_series` 会对主身份为 Bangumi 的已关联作品恢复条目级单季证据，因此 known-work/历史资源定向重跑同样生效，但不改写作品总季数。合集资源与 `manual` 行不触碰；电影链接不受影响。存量剧集链接资源用 `scripts/reconcile_season_backfill.py`（dry-run 默认，`--apply` 执行）回填，不创建 Agent PendingDecision。
- **MetadataAgent 路径**（`_apply_verified_season_default`）：finalize 结果 `content_type=tv` 且 `inferred_season` 为空时，用 `matched_entity` 的 `number_of_seasons`/`seasons` 证据做同一判定——恰为 1 季 → `season=1`；否则置 `season_ambiguous=True` 载体（`ResourceMetadata` 字段，随 MetadataCache 往返）。`_apply_to_resource` 在 `apply_episode_reconcile` **之后**检查：reconcile 可能已从 `absolute_episode` 合法推导出季号，只有季号仍为 None 才落 `episode_confidence="ambiguous"`（合集与 manual 除外）。
- **一致性交叉检查**（`apply_episode_reconcile`）：资源同时带 season 与 `absolute_episode` 且 confidence 非 `manual` 时，用 `locate_absolute_episode` 复核；绝对集号算术定位到的 (season, episode) 与现值不一致 → `episode_confidence="ambiguous"`（标题季标记与绝对算术冲突，绝不静默信一方）；locate 返回 None 视为无证据，不改动。
- **历史优先 reconciliation**：已链接作品的单集 TV 资源先用同频道/同作品近邻 `manual|reconciled` 记录推导发布组的 absolute 编号偏移，然后才走作品 seasons 算术。同组相邻 manual 一条即可作为证据；跨组需双样本共识，冲突继续待确认。对于只有季号缺失的同集发布变体（如同组 E07 简体/繁体/简繁版），同频道+同作品+同发布组的同 episode 人工修订一条即可继承 season；无 manual 时至少两条结构化历史必须同季一致，有冲突绝不默认。全局 5 分钟 metadata backfill 同时扫描可修复的 `ambiguous/raw/reconciled/NULL` 行，修复后对 active Agent 定向运行（不推进水位线），以清理待确认决策并正常派发。
- **LLM 指引**：系统/judge prompt 与 finalize 工具说明均要求——标题无季标记时必须依据工具结果的 `number_of_seasons`/`seasons` 验证（单季 → `inferred_season=1`；多季 → `ambiguous=true` + `ambiguous_candidates`），且输入带 `title_year` 提示时优先年份一致的候选（年份冲突 >±1 是反对证据）。

`extract_search_title(resource)`（同步、无 LLM；Layer 2/3 的匹配 key 来源）优先级：

1. `title_cn` 或 `title_en`（field_mapping 已解析），剥离尾部季后缀（`第三季` / `S04` / `Season 4` / `3期`）后返回。
2. `title_raw` 正则清洗：去除首个 `[字幕组]` 括号对、按 ` / ` 分隔只取第一个 alt-title 段、依次剥离 `- 集数` 尾部 / `SxxExx` / 游离季标记（`S04`）/ 尾部独立集数 / 尾部单个质量标签括号，最后去季后缀并修剪装饰分隔符。
3. 清洗结果为空时兜底返回原始 `title_raw`。

该函数设计上是保守的：多括号标题（`[Group][Work][S04][13]`）无法用正则可靠分离作品名与发布元数据，交由 LLM agent 处理；本函数只需"足够好"以支撑本地 DB/FTS 匹配和 agent 禁用/失败时的兜底。

`create_or_update_series_from_external(db, data)` 逻辑（P3 起查找顺序）：
1. **身份袋反查**：按 canonical `(source, external_id)` 查 WorkExternalId 身份袋——任何曾入袋的 id（langlinks pageids、网络搜索回退 id 等）确定性收敛，同 `work_type` 才命中。
2. **legacy 主列查找**：按 `external_id（canonical + raw 两种形态）+ external_source IN (data.external_source, 'llm_search')` 查询——兼容 canonical 化/身份袋之前写入的旧行。
3. **标题精确回退**：`title_cn/title_en/original_title`（含去季后缀形式）**加上 `alt_titles`**（Wikipedia langlinks 的跨语言页面标题）匹配已有行的三个标题列。

后续动作：
- 存在 → 更新字段（合并 aliases：新别名 append 去重；poster_url 若本地缺失则下载）；若原 `external_source="llm_search"` 则迁移为新 source。
- 不存在 → 创建新实体（aliases 同样纳入 `alt_titles`）。
- **成功 upsert 必写身份袋**：matched_entity 的主 id + `alt_external_ids: [{source, id}]`（P3 新增键，如 wikipedia langlinks pageids）全部入袋；主 id 列维持 creator-wins（仅既有填充/迁移规则可改它）。
- 返回实体。

`create_or_update_movie_from_external` 同理。

`create_or_update_audio_work_from_external` 同为幂等 upsert，但带**空壳守卫**：创建新记录时 `title_cn/title_en/original_title` 全空则拒绝（记 warning、返回 None）——LLM verdict 可能偏离 schema 返回无标题的 audio 类 matched_entity，空 AudioWork 没有价值（更新已有行不受限）。对应地，`_apply_to_resource` 的 audio 分支在调用前做两级修补：注入 `meta.content_type`（LLM 的 content_type 在 ResourceMetadata 顶层、从不在 matched_entity 里，缺它创建会错误兜底为 `"other"`）；matched_entity 无任何标题时依次用 `meta.title_cn/title_en/clean_title` 兜底回填。回填后仍无标题则不建 AudioWork、不设置 `resource.audio_work_id`，资源保持未匹配（记 warning）。

### Wikipedia 跨语言收敛（langlinks）

Wikipedia 的 page id 按语言站点独立编号，同一作品的 zhwiki 页面（如 `wikipedia:7727654`"黃泉使者"）与 enwiki 页面（`wikipedia:70545449`"Daemons of the Shadow Realm"）external_id 不同、标题槽各填一个语言，标题回退无法命中 → 同一作品被建成多行。修复：`_execute_get_wikipedia_page` 抓取页面时一并取 **langlinks**（仅 en/zh/ja），search-then-judge 两条 finalize 路径据此：

- 补齐缺失语言的标题槽（auto-link 直接填；LLM judge 仅在对应槽缺失时回填）；
- 把全部 langlink 标题放入 `matched_entity.alt_titles`，upsert 的标题回退与 aliases 合并都纳入它们。

**P3 增强（langlinks pageids 入身份袋）**：wikipediaapi 的 langlinks 只带标题/URL，`_execute_get_wikipedia_page` 额外用 MediaWiki `prop=info` 逐语言解析出 pageid（`_fetch_langlink_pageids`，并行、best-effort），经 `langlink_pageids` 键穿线到 search-then-judge 两条 finalize 路径，作为 `matched_entity.alt_external_ids`（`{source: "wikipedia", id: "wikipedia:<pid>"}`）在 upsert 时全部入身份袋。此后任何语言页的 pageid 命中都直接经袋反查收敛到同一作品行，不再依赖标题。

效果：第二种语言的匹配通过 alt_titles 精确命中已有行实现收敛，且 aliases 一旦带上跨语言标题，每日 04:00 的 metadata 去重（按共享标题/别名聚类）也能自动合并历史重复行。

### Wikipedia 季/集内容提取（Phase P2，确定性解析，无 LLM）

wikipedia 主源频道的**季/集内容一律来自 Wikipedia 页面本身**（源一致性规则：内容不从其他源补；LLM judge 仍只做作品识别，judge schema 不含 seasons）。链路：

1. **抓取**：页面被选中后（search-then-judge 的 auto-link 短路**和** LLM judge 选中两条路径都会走），`_attach_wikipedia_content` 用 `fetch_wikipedia_wikitext`（MediaWiki `action=parse&prop=wikitext`，httpx + Wikimedia UA）取选中页面的原始 wikitext；完全解析失败时经 langlinks 重试**一次**（zh↔ja，剧集列表常只在一侧）。
2. **解析**（`app/services/wikipedia_episode_parser.py`，纯函数）：infobox **仅取 `{{Infobox animanga/TVAnime}}` 块**（zh/ja 同名；块到下一个 `{{Infobox animanga/` 子模板为止；无 TVAnime 块的页面——如史萊姆主页面——返回 None，绝不把 Novel/Manga 块的 話數 当 TV 季数）→ `parse_seasons_from_infobox`（支持 `{{ubl|...}}` 与 `<br />` 分隔、`第N季/第N期/第一季`（汉字序数）、`話數/话数/話数/集數` 字段、全/共、全/半角数字；无季标记的 `全M話` 视作单季、但仅当恰好一个 plain 计数 TV 块）；`各話列表/各話リスト` 章节 → `parse_episode_list`（zh `{{劇集列表/base}}` 与 ja 规则变体 `{{エピソードリスト/base}}`；`Chapter = 第N季/期` 设当前季，`Number = 第M話`（含汉字数字 `第一話`）成行，Title/Subtitle 去 `{{lang|ja|...}}`/wiki 链接/简单模板，Aux5 解析 `'''2023年'''<br />10月8日` 与续年 `10月15日` 为 ISO 日期）。无 Chapter 标记 → 单季（season=1）；章节内编号若从 >1 开始（跨年绝对编号）则重排为季内 1 起；`番外編` 等无集号行跳过。无剧集章节 → 返回 None。已采样确认 zh/ja 真实页面（100カノ zh+ja、無職転生、小書痴、史萊姆、攻殻機動隊 SAC——后者无剧集章节，按 None 处理）；其余语言/模板形态本阶段不支持（代码注释注明）。
3. **合并进 matched_entity**：`seasons`（infobox 集数权威；剧集列表季数更多时以列表为准——连载中 infobox 滞后）、`number_of_seasons`、`number_of_episodes`（求和）与新键 `episode_list`。`ResourceMetadata.from_dict`/MetadataCache 整体携带 `matched_entity`，`episode_list` 随缓存往返不丢失。此外，若实体尚无 `start_date`，从 `episode_list` 最早的非空 `air_date` 派生（series upsert 只认 `start_date` 键，而 wikipedia 路径此前无任何环节产生它）；已有 `start_date` 不覆盖。
4. **落库**：`create_or_update_series_from_external` 在 `episode_list` 存在时调用 `upsert_episodes`（按 `(series_id, season, episode)` 幂等 upsert title/air_date；只增不删）；wikipedia 来源携带 `seasons` 时**覆盖** `series.seasons`/`number_of_seasons`（tmdb/回退路径不变），但带**防退化 guard**（`seasons_overwrite_allowed`，`number_of_episodes` 一并门控）：现有结构为空、或解析季数 ≥ 现有季数才覆盖；解析季数**更少**（如合并建模的 {1:51} 覆盖已验证 4 季）一律拒绝并记 warning。陈旧的 TMDB 建模行在下一次刷新时被纠正。
5. **覆盖率评测与回填**：`scripts/wikipedia_seasons_eval.py`（dry-run 默认）对所有 wikipedia 链接的 TVSeries 逐部抓取 + 解析并输出覆盖率汇总；`--apply` 复用同一 `evaluate_series` 读路径执行写回填（覆盖 seasons/number_of_seasons/number_of_episodes + `upsert_episodes`，批量提交），并走同一防退化 guard——被拒的报告打印 `[guard-skip]`，seasons 与 Episode 均不写入——对 wikipedia 主源作品取代 `series_seasons_backfill.py` 的 seasons 回填。

网络搜索回退仍只补身份/链接（`seasons`/集数继续被剥离），内容以 Wikipedia 主源为准。

### TMDB 季/集内容提取（Phase P4，与 Wikipedia 对称）

tmdb 主源频道的**季/集内容一律来自 TMDB API 本身**（同一源一致性规则）。TMDB series details 只带 `seasons[]`（季数/每季集数），逐集数据需逐季 `GET /tv/{id}/season/{n}`：

1. **抓取**：`fetch_tmdb_episode_list(tmdb_id, seasons)`（`metadata_source_io.py`，httpx）按季并发拉取（并发上限 4，跳过 season 0 特别篇，单季失败容忍仅缺该季，>30 季直接跳过），产出 `[{season, episode, title, air_date}]`——与 wikipedia 解析器同形。
2. **接线**：`_attach_tmdb_episode_list` 在 tmdb ReAct finalize 后（`process` 与 `process_title_only` 两处）触发——仅当 found=True、content_type=tv、matched_entity 携带 tmdb id 且有 `seasons` 且无 `episode_list` 时填充；回退实体 seasons 已剥离，天然不触发（单源规则）。`episode_list` 复用 P2 消费路径（`create_or_update_series_from_external` → `upsert_episodes`），随 MetadataCache 往返。
3. **回填**：`scripts/tmdb_episodes_backfill.py`（dry-run 默认，`--apply` 执行，批量提交）对 canonical `tmdb:` 身份的 TVSeries 跑同一抓取 + `upsert_episodes`；`series.seasons` 缺失时用 TMDB details 补齐输入（不改写 series 字段）。

### genre 归一化（统一分类标签）

作品 genre 统一为 **TMDB 封闭分类集（27 类）英文 canonical 名**，权威清单一处定义在 `app/services/genre_registry.py`（取值约定见 data-models.md「genre 取值约定」）。多数据源归一靠"prompt 注入 + 出口钳制"而非逐源映射表：

- **prompt 注入**：ReAct `_SYSTEM_PROMPT`、wikipedia judge `_JUDGE_SYSTEM_PROMPT`、web fallback judge `_WEB_FALLBACK_JUDGE_SYSTEM_PROMPT` 三处 matched_entity schema 注入 `genre_prompt_block()` 生成的完整枚举清单，LLM 根据外部作品详情（摘要/categories/snippet）一并输出 genre；指令为**尽力推测**——源未显式列标签时须依据简介推断，有简介至少给一个，杜绝"不确定就留空"。judge schema 的 genre description 同步列枚举。wiki auto-link 路径不经 LLM，genre 留空由兜底/回填补齐。
- **兜底推断**：`_ensure_genre`（`metadata_agent.py`）在钳制后仍无 genre 且 matched_entity 有 description 时，用 `genre_inference_system_prompt()` 发一次低成本 LLM 调用按简介分类，结果再过 `normalize_genres`；失败静默不阻塞匹配。由此 judge 留空、auto-link 无 LLM、回退仅身份三条路径产出的作品都能拿到标签。
- **出口钳制**：统一 finalize 消费点 `_clamp_finalize_genre`（`metadata_agent.py`，`process` 与 `process_title_only` 各一处）对 `matched_entity["genre"]` 调 `normalize_genres`——id 直译、大小写不敏感、少量别名，表外值丢弃（debug 日志），空结果置 None 视为"未提供"，genre 绝不阻塞匹配。TMDB 直连的 `_tmdb_genre_map` 动态拉取与注册表取交集、失败回退注册表静态表。同一消费点紧跟 `_normalize_finalize_dates`：确定性日期键名归一——series/tv 无 `start_date` 时依次从 `first_air_date`/`release_date` 补（TMDB 工具结果带 `first_air_date`，LLM 不总按 prompt 键名抄录），movie 对称补 `release_date`；只补空缺、绝不覆盖已有值。
- **写回**：`metadata_service` 全部 genre 写入点（series/movie/audio 的新建/更新、`refresh_work_metadata` 填空）先过 `normalize_genres`；非空才覆盖，归一化为空不清空旧值。
- **缓存**：judge schema/指令变更属 verdict 逻辑变更，`METADATA_CACHE_GENERATION` 当前为 4（3=genre 入 schema + 钳制；4=prompt 改尽力推测 + `_ensure_genre` 兜底），旧缓存惰性失效重跑。
- **存量**：`scripts/genre_backfill.py`——模式 A（默认）就地规范化既有 genre 数组；模式 B（`--refresh-empty`）对仍为空的 series/movie 调 `refresh_work_metadata` 重跑补齐（身份源为 wikipedia/tmdb 时用原源，否则回退 wikipedia 标题判定；有网络/LLM 成本，`--limit/--delay` 限速）。
- **消费面**：通知 payload `work.genre` 快照同样归一化；Filter DSL 新增 `series.genre`/`movie.genre`（list-of-string 逐元素语义，见 filter-dsl.md）。

### is_anime 分层判定（三态动漫标记）

作品 `is_anime` 的取值约定与确定性信号（身份源 / Wikipedia infobox / TMDB / LLM）见 data-models.md「is_anime 判定约定」；本节描述运行时的分层判定。统一入口 `classify_is_anime_post_link`（metadata_service）挂在资源成功链接作品的**全部 5 处落点**（`metadata_repository._apply_to_resource` + metadata_service 匹配流程 4 处：Layer 2 mapping 链接、Layer 3 精确/模糊自动链接两处、Layer 4 LLM 链接）：

1. **频道默认标记**（`apply_channel_default_is_anime`）：`channels.default_is_anime`（创建后不可改）开启的频道，其资源链接到的作品 `is_anime` 先置 True（已有 True 不动）。
2. **第一层 Bangumi 验证**（`maybe_verify_is_anime_via_bangumi`）：未开默认标记的频道，作品 `is_anime` 仍为 NULL 且有 Bangumi token 时，按作品标题（title_cn/original_title/title_en）搜 Bangumi（**不带 type 过滤**，让三次元条目可作实拍证据），`anime_signals.bangumi_verdict` 在归一化标题相等 + 年份守卫（±1，作品年份未知则放行）后按条目类型判定：type 2（动画）→ True、type 6（三次元）→ False、其他类型忽略；已判定（非 NULL）作品跳过；验证异常静默记 warning，不阻塞匹配。
3. **第二层上下文推断**：既有信号经 `apply_is_anime` 在 upsert 时赋值（True sticky / False 只填 NULL）——Wikipedia infobox（TVAnime + 新增 Movie|Film|OVA 检测 `has_animanga_film_infobox`，剧场版/OVA 的确定性信号）、TMDB Animation+ja/JP、judge/ReAct 的 `is_anime` 输出（prompt 已强化制作公司指引）。
4. **最终 NULL = 无法判断**：留待用户在作品详情页手动修正（详情页统一「编辑」表单中的动漫判定字段，走 PUT series/movies 保存）。

### 人工编辑保护（自动扫描不覆盖人工编辑字段）

作品详情页从「单字段编辑」（如动漫判定三态 Select）改为**统一「编辑」入口**：一个编辑表单可改 `MANUAL_EDITABLE_FIELDS`（标题三字段、动漫判定、简介、评分、分类、状态、季集、日期等），数据源等系统托管字段（external_id/external_source/wikipedia_*/seasons/collection_id/content_type 等）不可编辑。保存走 `PUT /series/{id}` / `PUT /movies/{id}`，后端按显式发送字段记入 `manually_edited_fields`。

- **自动扫描一律跳过**：`create_or_update_*_from_external` 更新分支、`apply_is_anime`、`apply_channel_default_is_anime`、`maybe_verify_is_anime_via_bangumi` 在写字段前检查 `field_manually_edited`（metadata_service），命中即不改写；新建作品无该列表不受影响。
- **刷新元数据默认保护人工字段**：手动流程经 `/metadata/search → /works/metadata/preview → /works/metadata/apply`，同步候选提供的非人工字段；候选空值不清空。仅显式 `override_manual_edits=true` 时覆盖人工字段。
- **语义**：人工编辑是"最后写赢"的显式声明——即便用户把某字段清空为 null，自动扫描也不会再回填（除非勾选覆盖）。

### Metadata 一致性防护（缓存版本化 / 跨表守卫 / 修正联动 / 跨表去重）

针对"错误 verdict 被缓存复用 + Movie/Series 分表产生跨表重复"这一类问题（案例：franchise 页被旧分类器误判 movie 后，新频道的同标题资源命中陈旧缓存，在已有 Series 的情况下又建了 Movie）：

- **缓存逻辑版本化**：`MetadataCache.generation` 记录产生 verdict 的逻辑版本（`METADATA_CACHE_GENERATION`），读取时旧代条目视为未命中并懒删除。分类/判定逻辑变更时 bump 常量即全部失效（详见 data-models.md）。
- **落库前跨表守卫与在线纠错**：`metadata_repository` 的 upsert 分发处，movie verdict 先按 canonical external_id 查 TVSeries（tv verdict 对称查 Movie）。正常的相反表 owner 仍采用 creator-wins：翻转到已有行，避免一次错误 verdict 产生跨表重复；但若 TVSeries 行自身已明确 `content_type="movie"` 且没有 Episode/单集资源证据，则它属于历史错表数据，立即创建/复用 Movie、迁移 FileResource/AgentWork/ChannelRawTitleMapping/PendingDecision/合集 links 与文件 assignments、合并身份袋并删除错误 TVSeries。这样明确电影不会因旧 FK 被要求 season/episode。
- **手动修正联动清缓存**：手动 link（`manual_link_metadata`）提交后，按 `external_id` 删除命中的 MetadataCache 行，让用户的类型纠正当即传播到后续资源。
- **每日去重的跨表合并**：`merge_cross_type_duplicates`（随 04:00 去重任务运行）检测共享 canonical external_id 或共享归一化标题（含别名、简繁折叠）的 (Movie, TVSeries) 对。存留规则：任一侧存在带集号的资源或 Episode 行 → 保留 Series；否则保留 Movie。失败方的 FileResource / AgentWork / ChannelRawTitleMapping / PendingDecision 引用全部改指存留方并合并标题/别名后删除。

### WorkCollection（大 IP 合集分组）

**定位：collection 是组织层而非消歧核心**——匹配/派发仍以单个作品行（TVSeries/Movie）为准，合集只提供浏览分组、详情页"同系列作品"和 DSL 过滤维度。

- **确定性 TMDB 链接（不经 LLM）**：`link_movie_collection(db, movie)`（collection_service）在 Movie upsert 后调用——`metadata_repository._apply_to_resource` 两处与 `metadata_service` 的 Layer-4 落库、`manual_link_metadata`。当 `movie.external_id` 为 canonical `tmdb:<digits>` 且 `collection_id` 为空时，直接 httpx 拉 TMDB movie details 读 `belongs_to_collection`（{id, name, poster_path}），按 `(external_source="tmdb_collection", external_id=原始数字 id)` 幂等 upsert WorkCollection 并回填 FK。不能用 LLM `matched_entity`（Layers 1-3、S1 短路、缓存命中都绕过 TMDB details；bangumi 模式没有 TMDB 工具）；不能用 `canonicalize_external_id`（会把 collection id 改写进电影 id 空间）。TMDB 未配置/禁用、电影无合集、已链接时静默 no-op。存量电影用 `scripts/collection_backfill.py`（dry-run 默认，`--apply` 执行）回填。
- **同名作品注入（替代合集注入）**：Agent 消息在 ReAct 循环前构建，只有本地库信息，故不做 collection 注入。改为 `_find_same_title_works`（原 `_has_same_title_collision` 重构为返回碰撞作品列表，Layer-3 布尔语义以列表真值保持）：≥2 个本地作品归一化标题精确碰撞时，把列表（标题、年份、类型、季数）注入 `_build_production_message`，提示结合标题年份选择正确作品——修复的真实错误模式是"链接到错误的既有本地作品"。
- **去重合并保留合集归属**：`_merge_movie_group`/`_merge_series_group` 中存留方保留自己的 collection_id，为空时取重复行的（collection 链接是作品级副作用，不 bump `METADATA_CACHE_GENERATION`）。
- **Wikidata TV 归组回填（确定性带外脚本，非 agent 数据源）**：TV 没有 TMDB collection 等价物，故 series 归组走 `scripts/tv_collection_backfill.py`（dry-run 默认，`--apply` 执行，逻辑在 `app/services/wikidata_collection.py`）。对每个 `collection_id IS NULL` 的 TVSeries：先按行上 LLM 挂载的 `wikipedia_url`（URL host 锁定语言版，可信）→ `wikipedia_page_id`（不带语言版，逐 en/zh/ja 尝试且实体 label/alias 必须与作品标题精确匹配才采纳）→ `wbsearchentities` 兜底（仅当恰好一个结果 label/alias 与标题精确匹配，消歧即跳过）解析 Wikidata 实体 QID；再读实体的 P179（"part of the series"）claim——无 P179 跳过、多个不同 P179 值判 ambiguous 跳过（绝不猜 franchise）；单个则按 `(external_source="wikidata", external_id=franchise QID)` 幂等 upsert WorkCollection（title_cn/title_en 取 franchise 实体 zh/en label）并回填 `series.collection_id`。同 franchise 作品经唯一约束收敛到同一行。全程确定性、无 LLM、不进 metadata agent 循环。
- **TMDB collection parts 按需可见性（不落库，非 agent 数据源）**：`GET /collections/{id}?include_parts=true` 对 `tmdb_collection` 合集实时拉 TMDB `/collection/{id}` 的 parts（`fetch_tmdb_collection_parts`，进程内 10 分钟 TTL 缓存防刷新打爆 TMDB），与本地电影 canonical `tmdb:<id>` external_id 集合（`tracked_movie_tmdb_ids`）求差集（`filter_untracked_parts` 纯函数），响应附 `untracked_parts`——本地库尚未收录的 franchise 作品（如未上线的新作）。已收录部分本就在 `works` 中。parts 永不持久化：WorkCollection 保持轻量分组实体，非 TMDB 合集或 TMDB 未配置时不输出该字段（拉取失败输出空数组）。

### Metadata Search Agent 数据源策略

MetadataAgent 不再采用多级搜索或跨数据源 fallback。每次搜索必须选择且只选择一个数据源，由 LLM 基于该数据源返回的证据做标题理解、集数/季数推断和最终结构化输出。

**统一三数据源架构**：频道、手动搜索、作品刷新、批量任务与评测均只允许 `wikipedia | tmdb | bangumi`。历史来源不可运行；Jina 的配置、客户端、ReAct tools 与缓存路径均已删除。

所有交互与评测调用统一 `search_metadata_candidates` 服务。在线请求显式携带 `source` 与 `trusted_sites`；可信站点既是 Wigolo 域名白名单也是候选优先级，`null` 使用默认顺序，`[]` 禁用回退，并同时执行 `include_domains` 下推与 URL 注册表硬过滤。

运行时支持的数据源（`SUPPORTED_METADATA_SOURCES = {"tmdb", "wikipedia", "bangumi"}`）：
- `wikipedia`：Wikipedia Search，代码默认数据源（`DEFAULT_METADATA_SOURCE = "wikipedia"`）。仅使用 Wikipedia 搜索与页面工具；未命中时可触发下方网络搜索回退。
- `tmdb`：TMDB Search。仅使用 TMDB API 的搜索/详情工具，适合结构化影视库匹配；未命中时同样可触发下方网络搜索回退（P4 起与 wikipedia 路径统一）。
- `bangumi`：Bangumi Subject Search（限动画分类 type 2），search-then-judge 形态同 wikipedia（确定性 auto-link 优先，否则单次 judge，流程详见「Metadata 匹配流程」）；命中作品自动落 `is_anime=True`；未命中时同样走下方网络搜索回退。无独立启用开关——配置了 token（`BANGUMI_API_KEY` 或设置页）即启用。

**有序网络搜索回退（wigolo，三频道主源未命中时统一触发）**：主源判定返回 found=False 后（wikipedia 路径在 judge found=False 时、tmdb 路径（P4 起）在 ReAct finalize found=False 时、bangumi 路径在 auto-link 未中且 judge found=False 时；transient 失败如 agent error/超时**不**触发，避免用确定性回退 verdict 掩盖基础设施故障），`web_fallback_judge`（模块 `metadata_web_fallback`，函数名保留 exa 时代的 `exa_fallback_judge`→已更名，历史缓存 reason 由 transient marker 兼容匹配）用自托管 **wigolo** 搜索守护进程 + 单次 LLM judge 在频道 `metadata_fallback_sources`（JSON 有序站点白名单；NULL=默认顺序 `bangumi → mal → anilist → tmdb → wikipedia → imdb → douban`，`[]`=禁用回退；`process_title_only` 无频道上下文，用默认顺序）限定的站点内补身份：候选 URL 按白名单硬过滤，靠前站点在证据呈现中优先（tiebreak）。回退只提供身份/链接——matched_entity 不得携带 `seasons`/`number_of_seasons`/`number_of_episodes`（LLM 输出也会被剥离），内容一律以主数据源为准。站点身份体系为 7 站注册表 `metadata_source_registry`（wikipedia/tmdb/bangumi/mal/anilist/imdb/douban；baidu_baike、eiga 已移除）。wigolo 对接走 **REST `/v1/search`**（`wigolo_client`，POST `{WIGOLO_BASE_URL}/v1/search`，非回环绑定的 daemon 需 `WIGOLO_API_TOKEN` Bearer），`WEB_FALLBACK_ENABLED=false` 时跳过回退。与被替换的神经检索不同，关键词引擎吃不下原始发布标题噪声，因此：①查询复用共享清洗器 `_candidate_queries`（≤3 个候选变体按序尝试，首个有产出者即停，全部无产出回退裸标题）；②白名单经 `SITE_DOMAINS` 映射下推为请求参数 `include_domains`（子域名命中），后置硬过滤仍保留作双保险；③hit URL 一律 `unquote` 归一化后再提取身份（百分号编码 slug 与明文必须收敛为同一 external_id）。wigolo 自身失败（网络/限流/协议错误）记为 transient 不缓存；回退命中时 `search_method` 分别为 `search_then_web_fallback`（wikipedia）/ `react_then_web_fallback`（tmdb、bangumi 共用共享回退函数 `_maybe_web_fallback`）。无法解析出注册表身份的页面沿用历史标记 `external_source="exa_web"`（已落库值，保持稳定）。

数据源选择规则：
- 一个数据源当且仅当启用且凭证已配置时才在 UI 中可选；`wikipedia` 无需凭证，`bangumi` 配置 token 即启用。启用开关为 `WEB_FALLBACK_ENABLED` / `TMDB_ENABLED` / `WIKIPEDIA_ENABLED`。
- 作品库手动刷新必须通过统一来源目录选择来源、可信站点和具体候选；批量刷新同样携带 `source + trusted_sites`，但只自动应用确定性候选。
- **频道级定期刷新（原全局自动刷新已废除）**：Channel 新增 `metadata_refresh_enabled` / `metadata_refresh_interval_minutes`（NULL=默认 1440）/ `metadata_refresh_full_scope` 三列。开启后调度器按间隔入队 `refresh_channel_works`（stable key `channel-refresh:<id>`），handler 只做参数派生——source = 频道自身 `metadata_source`、`override_manual_edits=False`——作品选集经 `select_channel_works_for_refresh`（默认缺失门控：只选确有待填空字段的作品，谓词与 `refresh_work_metadata` 的 fill 清单一致；`full_scope=True` 跳过门控刷全部关联作品），随后复用与手动刷新完全相同的 `refresh_work_metadata → search_metadata_via_llm → process_title_only` 单一管线执行。存量频道开关一律收敛为关闭（轻迁移）。

兼容规则：
- `combined` 仅作为旧评测数据集值保留；运行时归一化为默认 `wikipedia`，不得作为新数据集或新搜索任务的数据源类型。
- 单次搜索必须保持"只使用一个数据源"的约束，不得跨数据源 fallback（三频道主源的有序网络搜索回退是唯一的、仅补身份的例外）。
- eval 标注平台的新建 Dataset 必须选择 `tmdb` / `wikipedia` / `bangumi` 并保存相同的 `source + trusted_sites` 搜索配置；历史来源数据集只读且不可重跑。

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
  │     ├─ a. Channel 文件资源确认门禁:
  │     │     未链接 metadata、适用于当前形态的 Channel 必选字段缺失、单集 TV
  │     │     季/集号不确定、合集覆盖范围不确定，任一命中即不参与 Agent
  │     │     过滤/下载（unrecognized++）。问题仅进入 Channel 文件资源待确认，
  │     │     不创建 AgentSuggestion/PendingDecision；修订后定向重跑。
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
  │     ├─ c. 合集分支（resource.is_batch=True）:
  │     │     先查同一 FileResource 的 active/completed 任务（防重跑重复派发）。
  │     │     再按内容覆盖度去重（_batch_coverage_key）：电影包→movie_id；
  │     │     单季包→(series_id, season)（season 未知则覆盖度未知）；
  │     │     跨季包→(series_id, batch_seasons 季集合)（batch_seasons 由
  │     │     torrent 内容检测持久化；未知已由步骤 a 的 Channel 门禁拦截）。
  │     │     覆盖度已知 → 先查同
  │     │     agent 同作品 active 任务中是否有覆盖度完全相同的合集
  │     │     （Python 侧比对；剧集包受 enable_episode_dedup 门控、电影包
  │     │     与单集电影一致不门控），命中则 duplicates_skipped++；否则以
  │     │     ("batch", work_id, coverage) 进 candidates_by_key，与单集走
  │     │     同一冲突解决（ask → PendingDecision，episode 置哨兵 -1 与
  │     │     无集号单集决策区分，reason 用合集文案；auto → 优选偏好 →
  │     │     LLM pick → 启发式评分）。覆盖度不同的合集（S1 包 vs S2 包、S1-S2 vs
  │     │     S1-S3）互不冲突、各自派发；franchise 包作品 FK 全清走不到
  │     │     这里。已知限制：同一作品不同季集合的 multi_season 冲突会
  │     │     合并进同一条 PendingDecision（幂等键编不下季集合）。
  │     │
  │     ├─ d. 去重检查（仅单集资源）:
  │     │     电影：按 movie_id 查询 active DownloadTask，存在则跳过；key=("movie", movie_id, None, None)
  │     │     TV 单集：dedup = work.enable_episode_dedup if work else True
  │     │             dedup 且 episode 非空时按 (series_id, episode) 查询 active 任务并做
  │     │             **季兼容比对**（_find_existing_episode_task：季号相等，或任一方
  │     │             season=None 即视为同一槽位；两个都有编号且不同的季仍互不去重），
  │     │             存在则跳过——防止同一集的两个发布变体因季归属差异（如链接先后
  │     │             导致 S1 vs season=None）跨运行重复下载
  │     │             key=("series", series_id, season, episode)
  │     │
  │     └─ e. candidates_by_key[key].append(resource)；matched++；
  │           matched_resource_ids.append(resource.id)
  │
  ├─ 4. 候选聚合处理:
  │     先做**季未知归并**：key=("series", sid, None, E5) 的候选组并入其有编号同集组
  │     ("series", sid, S, E5)——季归属差异不再拆成两个单候选组各自直发。
  │     for key, candidates in candidates_by_key.items():
  │         if len(candidates) == 1:
  │             dispatch_download(agent, candidates[0])  # dispatched++
  │         else:
  │             if agent.conflict_resolution == "ask":
  │                 create_pending_decision(agent, key, candidates, db)
  │                 # upsert 同一 (agent, target, season, episode, pending) 行；合并 candidates；
  │                 # 建议经 _suggest_pick 填充 llm_picked_resource_id 与
  │                 # llm_suggestion（用户可点 "AI 自动处理" 一键采纳）：
  │                 # 优选偏好唯一命中 → 确定性建议（reason 标注规则来源）；
  │                 # 打平 → _generate_llm_pick
  │             else:  # "auto"
  │                 tier, _ = pick_by_preferences(candidates, agent.pick_preferences)
  │                 # 优选偏好先行（只排序不过滤）：唯一胜者直接派发、零 LLM 调用；
  │                 # 打平子集交 LLM（agent.llm_prompt 优先，否则内置默认 prompt）；
  │                 # LLM 未启用 / 调用失败 / 无有效选择时回退启发式 score_and_pick
  │                 chosen = tier 唯一 ? tier[0] : (LLM 选中候选 or score_and_pick(tier, agent, work))
  │                 # 启发式评分：分辨率高（2160p>1080p>720p）> 文件体积大 > 发布时间新
  │                 #   > 字幕语言加分（zh-CN > zh-TW > 无，末位 tie-break）
  │                 dispatch_download(agent, chosen)
  │
  ├─ 5. 兼容清理旧版遗留的资源修订型 PendingDecision，使其不再出现在 Agent 列表。
  │
  └─ 6. 返回 RunResult（dispatched / pending_decisions / filter_failed /
        duplicates_skipped / unrecognized / suggestions / errors / matched_resource_ids）
        ——run_agent 据此回填 AgentRun 记录与 Agent.last_run_status
          （均为运行结束时的快照；待决策处理完后的状态修正见下）
```

**待决策处理后的状态复位**：PendingDecision 经 confirm/skip/batch 端点处理（status → decided/skipped）后，若该 agent 已无其他 pending 决策且 `last_run_status == "pending_decisions"`，复位为 `"success"`（decisions.py 的 `_maybe_reset_agent_run_status`，同事务内完成）。历史 AgentRun 行不回写，由 `GET /agents/{id}/runs` 读时修正（见 api-endpoints.md）。

**优选偏好层**（`pick_by_preferences`，P5）：`Agent.pick_preferences` 是有序 FieldCondition JSON 列表（复用 Filter DSL 的字段白名单与叶子求值 `evaluate_field_condition`，校验走 `validate_field_conditions`，保存时 422），在冲突解决中提供**确定性排序**：字典序语义——第一条规则把候选分命中/未命中两档、命中者优先，打平由下一条决胜；在当前档内全命中或全不命中的规则无区分度、跳过。**偏好只排序、绝不过滤候选**（无偏好/单候选/全员打平时原样返回）。唯一胜者直接入选（不调用 LLM，LLM 关闭或不可用时照样生效）；打平子集交 `_generate_llm_pick` 决胜，再回退启发式。三处接入：`auto` 路径直接派发；`ask` 模式经 `_suggest_pick` 生成建议（偏好命中时 reason 标注「命中优选偏好规则（确定性选择）」与 LLM 建议区分）；`POST /decisions/{id}/ai-pick` 同样偏好先行。`pick_preferences` 为空 = 行为与引入前完全一致。典型用法：`{"field": "subtitle_langs", "operator": "contains", "value": "zh-CN"}` 确定性优先简体中文字幕版本。

**LLM 候选选择器**（`_generate_llm_pick`）：`conflict_resolution="auto"` 多候选自动选择、`"ask"` 模式下的 LLM 建议、以及 `POST /decisions/{id}/ai-pick` 共用同一逻辑。返回 `(picked_resource_id, reason)`：使用 `agent.llm_prompt`（若非空）否则内置默认 prompt（metadata 字段最完整 > 清晰度最高 > 带字幕 > 发布时间最新），要求 LLM 返回 JSON `{"pick": <候选编号>, "reason": "<一句话理由>"}`，`_parse_llm_pick` 兼容 markdown 包裹与裸数字兜底。发给 LLM 的候选摘要包含 `title`（资源原始标题）与关联作品的 `year`（电影 `release_date` / 剧集 `start_date` 的年份）、`rating`（0-10 分），无关联作品或字段为空时为 `null`；prompt 中附带字段说明。LLM 未启用 / 无 API key / 调用失败 / 未给出有效选择时返回 `(None, None)`，`"auto"` 回退到纯启发式评分（分辨率 > 体积 > 发布时间）。结果缓存在 `PendingDecision.llm_picked_resource_id`，AI 自动处理优先复用缓存值。

`dispatch_download(agent, resource)`：
1. 创建 `DownloadTask(status="pending")` 并 `db.add()`（**不立即 flush**：flush 会发出 INSERT 并持有 SQLite 写锁跨过整个 Transmission RPC）。
2. 解析下载目录：`effective_download_dir = join(downloader.download_dir, agent.download_subdir)`；若 `download_subdir` 为空则直接使用 `downloader.download_dir`。
3. 校验 `download_subdir`：必须是相对路径，禁止绝对路径、`..`、空段逃逸、控制字符；标准化后不得跳出 `downloader.download_dir`。
4. 将 `effective_download_dir` 写入 `DownloadTask.download_dir`，用于审计、重试与后续配置变更隔离。
5. 调用 `TransmissionWrapper.add_torrent(payload, download_dir=effective_download_dir)`，payload 由 `resolve_torrent_payload(resource)` 得出：`resource.torrent_file`（通道 A 缓存的本地 .torrent）存在时读字节推送（读失败回退 URL），否则透传 `torrent_url`（URL/magnet 原样）。
6. 成功 → 更新 `task.status="downloading"`, `task.transmission_torrent_id=返回值`, `task.confirmed_at=now`。
7. 失败 → 更新 `task.status="error"`, `task.error_message=异常信息`；触发重试逻辑（若 retry_count < max_retries 则入队重试）。
8. RPC 结束后统一 `flush`，由调用方（请求路径）或 autocommit（后台路径）提交。

`dispatch_download` 的"创建任务 + 提交下载器"主体已抽取为 `create_and_submit_task(resource, downloader, db, agent_id, download_dir)`，供手动创建复用。手动创建下载任务（`POST /tasks`）完全绕过 Agent：用户从资源列表/资源详情选择 Downloader，后端直接以 `agent_id=None`、`download_dir=downloader.download_dir`（根目录，不加子目录）调用同一 `create_and_submit_task` 提交；不做去重检查（手动创建是显式用户意图，允许重复任务）。

### 手动 metadata 搜索与修正流程

```
用户在资源详情点击"修正 metadata"
  │
  ├─ 1. 用户输入 query、选择 content_type、主来源与可信站点
  │
  ├─ 2. 前端 POST /metadata/search（只读，返回统一候选）
  │
  ├─ 3. 用户选择本地作品或在线候选；在线候选仅进入向导草稿
  │
  ├─ 4. 最终 PUT /resources/{id}/associations
  │     同一事务物化在线作品并应用 works/assignments/FK；失败完整回滚
  │
  └─ 5. commit 后仅入队一次定向 Agent 重跑
```

### Schedule 调度

APScheduler（AsyncIOScheduler，内存 store）在 **worker 进程**启动时初始化（`APP_ROLE=worker` 或 `all`；`APP_ROLE=web` 的进程不起调度器、不消费队列）。**所有周期 job 只 enqueue 不执行**。两层机制保证 N 个 worker 各跑一份调度器时每个 interval 只执行一次：① tick 节流——`queue.throttle(<type>, ttl=interval)`（Redis SET NX EX，键 `rssripple:tick:<type>`），本 interval 内第一个 tick 胜出，其余直接跳过（active-key 去重只挡并发重复，挡不住错峰 tick，必须有这层）；② 队列 active-key 去重兜底并发窗口，BLPOP 单消费者保证恰好执行一次。job 函数体仍可直接调用（单测依赖）。

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
  │         # 频道级定期作品元数据刷新（可选，默认关）：同 pattern 注册
  │         # id=f"channel-refresh:{ch.id}"、IntervalTrigger(minutes=间隔 or 1440)，
  │         # enqueue "refresh_channel_works"（stable key 同 id，active-key 去重防重叠）
  │
  ├─ 2. Channel CRUD 时动态调整:
  │     创建/更新 → active? add_job/reschedule_job（fetch 与 channel-refresh 两 job）: remove_job
  │     删除/paused → remove_job（两 job 一并移除）
  │
  ├─ 3. 全局每分钟任务:
  │     enqueue "sync_progress"（key=job:sync_progress）
  │
  ├─ 4. 全局每小时任务:
  │     enqueue "check_downloaders"  # handler 调用 POST /downloaders/{id}/test
  │
  ├─ 5. 每 30 秒任务（仅 Turso 后端注册；PostgreSQL 无边车，
  │     search_text + pg_trgm 由 ORM 钩子同事务维护）:
  │     enqueue "fts_drain"          # FTS 边车同步：把 fts_outbox 变更行定向投递到
  │                                  # 边车影子表（ORM 钩子与作品行同事务入队，
  │                                  # 幂等全量写；写入失败留给对账兜底）。
  │                                  # 搜索路径额外在读前 drain（search_*_fts 顶部
  │                                  # 先清空 outbox），保证"刚提交即可搜到"的
  │                                  # read-your-writes 一致性（手动 link 后立即
  │                                  # 搜索、调度器关闭的部署都成立），此 30 秒任务
  │                                  # 退化为批量化与脚本/非 API 写入的兜底。
  │
  ├─ 5b. 每 5 分钟任务:
  │     enqueue "backfill_metadata"  # 重试可重试的未匹配资源；顺带重跑
  │                                  # stale raw 集数 reconcile（链接时作品
  │                                  # 尚无逐季数据、后补齐的绝对集号资源，
  │                                  # reconcile_stale_raw_episodes）
  │
  ├─ 5c. 每小时任务（仅 Turso 后端注册）:
  │     enqueue "fts_reconcile"      # FTS 影子表对账：全量 diff 基表 vs 影子表，
  │                                  # 修补绕过 outbox 的路径（脚本、直连 SQL）
  │
  ├─ 6. 每分钟任务（NOTIFY_ENABLED 开启时）:
  │     enqueue "download_notifications"  # handler 三段式 tick：
  │                                    # ① 入队：为 completed 且无通知的任务停种（best-effort）
  │                                    #   + 补建 DownloadNotification（download_task_id 唯一
  │                                    #   + SAVEPOINT 竞争回读，幂等；仅当 Agent 有启用
  │                                    #   webhook 或存在启用 OrganizeRule 时才补建，
  │                                    #   两者皆无不生成通知）；
  │                                    # ② fan-out：ensure_deliveries 为"通知 × 每启用
  │                                    #   webhook"补建缺失的 pending WebhookDelivery；
  │                                    # ③ 投递：deliver_due_deliveries 并发投递到期 delivery
  │                                    #   （每 tick 上限 50 条、Semaphore(10) 并发、180s
  │                                    #   超时、指数退避，超限转 failed）。唯一投递路径，
  │                                    #   详见 notifications.md
  │
  └─ 7. 全局每日任务（CronTrigger + misfire_grace_time=3600：默认 1s 宽限
        会被 LLM 重负载阻塞的事件循环反复错过，宽限放大到 1 小时兜底）:
        enqueue "daily_cleanup"      # 删除 completed 且 completed_at < now - task_expire_days 的任务
                                 # （跳过其通知存在任一非 done delivery 的任务；
                                 # 超过 NOTIFY_RETENTION_DAYS 保留期的通知整行删除，
                                 # delivery 随级联清理）+ 过期 pending decision → expired
        enqueue "daily_dedup"  # 04:00 运行：合并重复的 TVSeries/Movie 行（安全网，
                            # 防止 metadata agent 偶尔为同一作品新建第二行）。聚类 key 基于
                            # 共享的 title_cn/title_en/original_title **+ aliases**（归一化
                            # 含 OpenCC 繁转简），只折叠可证明为同一作品的行；**年份守卫**：
                            # 首播/发行年相差 >1 年的同标题聚类不合并（重制/重启/同名系列，
                            # 记 note 跳过）；子表重指向防撞——Episode/AgentWork/
                            # PendingDecision 自然键冲突的重复行删除而非强转（幂等）。
                            # P3：合并时身份袋取并集（存留方继承重复方的主 id 与袋行，见
                            # merge_external_id_bags；跨表合并同样处理，标题配对同样受
                            # 年份守卫约束，外部 id 相等不受）
```

任务队列使用 MemoryQueue（默认）或 RedisQueue（配置时），承载手动触发的 fetch/run 与全部周期任务；同 key 去重（分布式锁）保证同一 Channel/Agent/周期任务不会被并发执行。**web/worker 分离**（`APP_ROLE`）：web 进程只 HTTP + enqueue（`queue.start(consume=False)`）；worker 进程（`python -m app.worker`）跑调度器 + 消费队列。RedisQueue 必须先取得并发槽位，再以原子 `LMOVE` 把 job 从持久 backlog 移至该 consumer 的 processing 列表，禁止无界预取到进程内等待；consumer 以短 TTL 租约持续心跳，正常完成才从 processing 确认删除。worker 优雅退出时未完成 job 原样重新入队；进程崩溃时描述符仍留在 processing，其他 worker 在租约过期后通过全局恢复锁幂等重新入队，状态回退为 queued 且保留 active-key 去重锁，禁止产生永久 running 僵尸或窃取仍存活 consumer 的任务。旧版本遗留、没有可恢复描述符的 running 状态在升级启动时标记 failed 并释放 active 锁。`sync_progress`、`download_notifications`、`check_downloaders` 三类短周期运维 job 走队首优先级，避免下载状态与通知被 LLM 长任务积压。每个 job handler 执行前重读 `load_runtime_config`（进程本地缓存，跨进程设置变更靠此收敛）。分布式 compose 默认 1 web + 3 worker；standalone 单进程 `APP_ROLE=all` 行为不变。

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

通过统一工厂 `app.clients.downloader.get_downloader_client(downloader)` 根据 `downloader.type` 分派到 `TransmissionWrapper` 或 `MockDownloaderWrapper`；两者共享同一异步接口（`test_connection` / `add_torrent` / `list_torrents` / `get_torrent` / `get_torrent_files` / `pause_torrent` / `resume_torrent` / `remove_torrent` / `free_space`），所有 Agent / scheduler / API 调用点均无需感知具体类型。`get_torrent_files` 返回 `{name, files: [{name, size}]}`，供下载通知快照锁定 torrent 内文件清单（mock 默认单文件，测试可向 `_TorrentState.files` 注入多文件）。

Mock downloader 面向本地开发和自动化测试；生产环境应使用 `transmission` 类型。

### Torrent 文件关联的抓取期收敛与遗留回填

- 文件清单确定性解析以 `torrent_inspect.analyze_torrent_files` 为唯一实现：频道抓取、下载完成后的 Magnet 补齐及 organize 历史快照回退均复用它；不得在 planner 内维护另一套季集正则。所有主视频作品形态（单集 TV、单部电影、season/multi_season/franchise/movies）均建立 assignment，LLM 仍只处理确定性层无法完成的合集歧义。

- 新资源先缓存并确定性解析 torrent 清单，metadata 作品链接完成后执行第二次无猜测绑定：资源恰好链接一个 series/movie 时，把仍为 `source=auto` 且未绑定作品的 assignment 全部挂到该作品，并补 `resource_work_links(source=auto)`；manual/llm 行不覆盖，多作品包不进入此分支。
- 频道详情页「手动抓取」先弹窗确认，默认不勾选「重新抓取所有条目的 metadata」并发送 `force=false`；勾选后发送 `force=true`。`force=true` 无扫描条数上限地重跑该频道全部既有资源，跳过 MetadataCache 与本地已知作品短路，重新查询 metadata 源以补齐作品缺失字段（包括由 `start_date`/`release_date` 派生的必选年份），并对所有作品形态重新执行 torrent 文件关联富化；有本地 torrent 缓存时即使原 URL 是 magnet 也可复用。普通抓取与全局 backfill 也会按较长冷却周期选取“已关联但仍缺 Channel 必填字段”的资源，继续执行作品富化、发布字段写回和 torrent 文件关联；未匹配资源仍按原有失败冷却与限额回填，避免周期任务反复全表扫描。
- 内容 LLM 对普通单集文件输出互斥的 `season + episode`；仅当一个物理视频文件实际包含连续多集（例如文件名明确为 E01-E02）时才输出 `season + episode_start + episode_end`，torrent 是整季合集本身不构成单文件范围的理由。服务层校验真实路径后将单集规范化为 assignment 的闭区间 `episode_start == episode_end` 落库，保持现有数据模型兼容。
- 向导文件 LLM 分析使用 `MetadataCache(source=batch_file_analysis:v4)` 持久缓存，key 为资源 ID、搜索标题、文件路径/大小清单及逻辑版本的 SHA-256 指纹，任一输入变化自然失效；主动重新解析用 `force=true` 覆盖缓存。缓存未命中时 web 以 `batch-analysis:<fingerprint>` 为 key 入队 `analyze_batch_files`，由 worker 完成确定性解析、LLM 调用和缓存写入；队列 result 字段承载中间状态与最多 50k 字符的累计输出，SSE 只轮询并增量转发。Redis `SET NX` active-key 在多 web/多 worker 间原子去重，关闭任一窗口不取消任务；MemoryQueue 保持 APP_ROLE=all 单进程兼容。
- 向导 LLM 输入包含服务端生成的候选作品清单：`candidate_key=series|movie:<UUID>`、work_type/work_id 与 title_cn/title_en/original_title/canonical_name 全部非空别名；输出必须复用 exact candidate_key。后端只接受输入集合内的 key，并由 key 对应候选还原作品类型/ID，拒绝模型生成或篡改的 UUID；前端优先按 candidate_key 绑定，标题归一化仅作旧结果兼容回退。最终 `season_ranges` 合并经路径与候选 ID 双重校验的 LLM 单集结果。
- 特别篇文件统一映射到媒体库规范的 Season 0：文件名明确的 `SPxx` / `Special xx` / `OVA xx` / `OAD xx` 直接解析为 `S00Exx`；发布顺序式小数标签（`11.5`、`22.5`）不存入整数 episode 字段，而在作品关联后与 Episode 表的 Season 0 行按顺序一一校准。若作品源尚无 Season 0 数据但清单中存在明确 `.5` 插播标签，则按标签顺序映射为 `S00E01..N`；已有 Season 0 数据但数量不一致时保持未解析，禁止错配。单作品自动绑定和向导 SSE 确定性结果复用同一规则。

---
