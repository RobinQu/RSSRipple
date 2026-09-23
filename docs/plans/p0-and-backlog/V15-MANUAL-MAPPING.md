# V15：人工标题映射优先级（P1-M1）

## 下一步续接

B4 已合入本地 main `7781a81`，当前无运行中的测试。V15 原型仍在 `/tmp/rssripple-v15-manual-mapping`，尚未合入；最近 ub 348 passed、uc 6 passed。下一步先将其未修改运行文件同步到已合入 B4（包括后补 processing 删除竞态修复），并对修改文件/业务文档做基线核对，不能把旧基础文件覆盖 main；随后补并发映射/资源身份变更及完整门禁。当前补丁/哈希用于保留进展，不是可直接盲目应用的合入凭据。


当前状态：必要性已动态复现，普通未链接路径已有独立原型，完整修复和验收未完成。B4 仍在独立完整集成 sn 轮，不能修改其冻结原型。问题继续保留 TODO。

sr 初步原型位于 `/tmp/rssripple-v15-manual-mapping`，从 B4 冻结 app 副本建立、独立于运行门禁。两个入口共用 `apply_manual_title_mapping`，普通未链接资源先应用有效映射再读缓存；同一录制标题红测已转绿，退出 0，独立会话确认两路径均指向人工电影，Ruff 通过。证据 `probes/manual-mapping-sr.log`；补丁及基线/候选哈希 `manual-mapping-prototype.patch`、`manual-mapping-source.json`。

这只是普通路径的部分实现，不代表 P1-M1 完成：force_refresh 的身份保持与信息补齐、已链接语义、失效映射、季作品和多作品边界及全量门禁仍待完成。原型代码暂留显式 pending 注释提醒该缺口，未合入运行主干；TODO 继续保留。

## 必要性

### 原型回归进展

ub 五个完整相关文件回归已完成：348 passed、1 warning，278.12 秒，退出 0（Agent、Repository、fetch_service、人工映射、合集映射）。证据 `probes/manual-mapping-ub.log/.xml`。uc 外部身份六项为同期独立补验。下一步重点为并发期间映射/资源/作品身份变化及完整入口集成，尚未进入 V15 完整门禁或合入。

uc 外部身份矩阵已完成：6 passed、1 warning，10.08 秒，Ruff 通过。season_bag/collection_bag/legacy_primary × 同目标/其他目标，实际 Turso upsert；拒绝后主动 commit，再独立会话确认两个作品字段与身份袋不变，同目标 description 实际更新。所有外部 ID 均合成，不代表实时提供者质量。证据 `probes/manual-mapping-uc*`。并发期间映射/目标变化、生产入口完整门禁仍待补验。

ua 合集形态矩阵扩展到 9 项，全部通过（13.20 秒）：Agent 和两条真实 metadata pipeline 的强制刷新，以及普通三内层/两 pipeline/metadata GET。预存人工 Movie/TV 两个 ResourceWorkLink 和两个 ResourceFileAssignment，独立会话逐字段验证 ID、工作、路径、季集、大小、source 保留。标题/作品/文件路径均明确合成，未验证真实视频字节。证据 `probes/manual-mapping-ua*`。ub session 18114 正在运行五个完整相关文件：Agent、Repository、fetch_service、电影/季映射、合集映射；日志 `/tmp/rssripple-v15-regression-ub.log`，尚无终态。

tz 六入口矩阵已退出 0，全部通过，证据 `probes/manual-mapping-tz.log/.xml`；这不替代强制刷新、已有关联保留和完整回归。

tu 已完成：278 passed、1 warning，184.08 秒（Agent/Repository/映射），tt 补充 search_title 回滚断言通过。tw 真实抓取 metadata 流程两路径通过，2 passed、3.64 秒；外部 inspection/graph 明确替身，实际匹配与最终提交未替换。

ty 真实 ASGI metadata GET 红测确认外层缺口：HTTP 200 后独立会话发现 franchise.movie_id 非空。tx 首次因 unit 目录无 client 夹具 setup error，不计产品缺陷；ty 改为本测试数据库的真实 FastAPI router 后 1 failed，2.13 秒。于是三处单作品匹配入口现先检测 franchise，执行既有形态保护并返回，不把多作品匹配当单作品成功；tz 六入口矩阵正在 session 74945，日志 `/tmp/rssripple-v15-batch-tz.log`。需补强制刷新/已识别关联保留及既有 Agent 回归，尚不可合入。

tv 新增 franchise 形态三入口矩阵（纯合成作品/标题、真实 Turso），mapping/Agent/legacy 均失败：直接调用后资源 movie_id 被赋值。证据 `probes/manual-mapping-tv*`。范围必须限定：fetch_service 的真实外层 pipeline 在两入口之后已有 enforce_franchise_resource_invariant 收尾，因此本红测只证明内层函数会留下扁平 FK，尚未证明正常抓取最终持久化违规。后续先补真正入口正反例和 force_refresh 语义，不能把内层快照扩大为生产外层已失守，也不能简单提前退出而误报 metadata 成功。

tt 完整映射矩阵已通过：36 passed、1 warning，51.81 秒，退出 0。Agent 固定季 ID 经 repository 传至 upsert，错误 TV 无新增作品/合集/缓存，正确同季候选实际补齐季日期；跨类型负例和电影矩阵继续通过。新增显式 search_title 回滚断言后，tu 完整 Agent/Repository/映射回归在 session 78892，日志 `/tmp/rssripple-v15-regression-tu.log`。四个涉及文件 Ruff 通过。尚需外部身份袋分支、并发映射变更、批次形态和完整入口集成，不关闭 M1。

ts 已退出 0，完整结果见 `probes/manual-mapping-ts.log/.xml`。随后已执行 Agent/repository 接入：固定季 ID 传至真实 upsert，跨表电影身份冲突提前抛错；保存点回滚候选资源字段后 refresh，映射保留。三运行文件 Ruff 通过；tt 完整人工映射矩阵正在 session 63540，日志 `/tmp/rssripple-v15-mapping-tt.log`。尚未取得绿测，不能宣称季身份已修复；并发及批次边界仍待验证。

Agent 接入事务设计：repository 会先改 resource.search_title 等解析字段，再执行作品 upsert。固定季作品路径需要保存点覆盖候选落库，捕获 MetadataTargetMismatchError 后刷新资源，保留保存点之前的人工映射；正常刷新必须继续完成协调与缓存。接入脚本 `/tmp/wire_v15_guard.py` 已准备但尚未执行，等待 ts 完整服务回归结束，避免运行中改被测模块。

tr 固定目标基础实现：series upsert 新增可选 expected_series_id，并传递给合集成员解析；新建/歧义/改选分支在写入前抛 MetadataTargetMismatchError，同目标保留原更新逻辑。三个真实 Turso 用例通过（4.94 秒）：同季日期更新、其他季拒绝、新合集拒绝；拒绝后主动 commit 再独立会话确认行数、别名及身份袋未污染。仍未接入 Agent，tq 已知红测尚未修复；外部 ID 各分支、并发与资源事务边界须继续验证。

tp 完整 Agent 回归已终态：204 passed、1 warning，85.78 秒；对应跨类型守卫版本，不是尚未接入的 series 约束验收。ts session 25115 正在执行完整 metadata_service 与已通过映射基础回归，显式排除仍待接入的 manual_season_mapping 六例，不能据此宣称 M1 全绿。日志 `/tmp/rssripple-v15-service-ts.log`。

tq 1 failed、5 passed，9.32 秒；其他 TV 强制刷新负例在 TVSeries 行数断言即失败，确认不仅改绑资源，还创建额外作品。证据 `probes/manual-mapping-tq*`，修复必须禁止该副作用，而不是事后恢复 FK。

身份守卫实施前补审：`_resolve_collection_member` 在成员确定前调用 `_merge_collection_aliases`，歧义/身份冲突分支会写合集身份袋；新建分支还会先建合集。因此固定目标检查必须先于这些写入，不仅约束资源最终 FK。tq 在六项矩阵增加 TVSeries/WorkCollection 行数各为 1 的独立会话断言，session 11852，日志 `/tmp/rssripple-v15-mapping-tq.log`。跨类型运行修复后的完整 Agent 回归 tp 在 session 34974，日志 `/tmp/rssripple-v15-agent-tp.log`；均须读取终态再记录。

to 修正季级日期夹具后 1 failed、5 passed，3.76 秒：整剧 start_date=2020-01-01，seasons[season_number=2].air_date=2024-01-01；同季强制刷新保留身份且实际补齐 2024 日期，证明 tn 日期失败为输入契约错误。唯一红测是其他 TV 的强制刷新改绑。证据 `probes/manual-mapping-to*`。

身份守卫方案复核：`find_series_by_external_id` 只查旧主列，不能覆盖身份袋/合集成员/标题回退；`find_collection_for_entity` 也不是完整的季作品选择器。不可用两者简单拼接来假称与 upsert 同义。后续应让真正的 series upsert 接收固定目标约束，所有选择分支在任何作品/合集/身份袋写入前比对目标；合集成员解析亦须传递约束，并验证同季正例、不同身份负例与并发变更。仅复制一份简化查找规则会造成判定漂移，不作为最终方案。

tn 已终态：2 failed、4 passed，10.08 秒。其他 TV 的强制刷新实际改绑到新 series，证实同类型身份缺口；同季 TV 保留原关联但 start_date 未补齐，需先核对第二季日期的 entity/season 数据契约，尚不能判定为产品缺陷。证据 `probes/manual-mapping-tn*`，下一步须使用正确的季级日期证据后修复身份守卫。

tm 已在候选写入/缓存前拒绝与人工映射不同的内容类型或未找到结果；29 passed、1 warning，47.44 秒，退出 0。季→电影负例额外确认 Movie 与 MetadataCache 行数为 0；日志/JUnit `probes/manual-mapping-tm*`。仅解决跨类型，仍不保护同类型不同 TV 身份。tn 正在 session 50626 扩充电影/其他 TV/同季 TV × 普通/强制刷新六矩阵，同季强制刷新必须实际补齐 start_date，不能跳过刷新取巧；日志 `/tmp/rssripple-v15-mapping-tn.log`。

tl 已终态：显式 TMDB/ReAct 替身后仍是强制刷新跨类型失败、普通匹配通过，证据 `probes/manual-mapping-tl.log/.xml`。因此缺陷不是 tk 的外部 judge 401 所致；下一步须在写入前保护人工季作品身份，并同时验证同季缺字段仍能补齐。

tj 新增冲突已有链接和调用方 rollback 两项真实 Turso 断言，27 passed、1 warning，41.28 秒。tk 季作品跨类型刷新矩阵 1 failed、1 passed（16.92 秒）：普通匹配保留第二季，强制刷新被电影候选覆盖。tk 默认 Wikipedia judge 意外执行并返回 401，再落到 ReAct 替身；已将测试频道显式设为 tmdb，tl 在 session 91264 重跑，排除该外部请求后才能作为隔离红测证据。运行代码尚未修复季身份守卫，不能关闭 M1。

tc 已终态：**229 passed、1 warning，75.75 秒，退出 0**，包含完整 MetadataAgent 回归与 25 项真实 Turso 映射矩阵/查找断言；四个修改文件 Ruff 通过。证据 `probes/manual-mapping-tc.log/.xml`。电影普通/强制刷新及已有链接串行路径已修复，仍缺剧集、并发和完整入口集成，不能关闭 M1。

ta 将已有链接纳入矩阵，6 failed、19 passed，14.12 秒，确认已有链接被成功缓存覆盖以及强制刷新绕过有效人工映射。原型现对普通已有链接先协调后返回；强制刷新仍查询映射，但冲突映射不覆盖已有链接，同目标映射继续固定电影身份。tb 新矩阵全部通过，联合结果 228 passed、1 failed（82 秒）；唯一失败是旧本地匹配夹具预设自动匹配覆盖已有剧集，违反 Layer 1 优先契约，已改为未链接夹具。tc 正在复跑两个完整测试文件，session 2186，日志 `/tmp/rssripple-v15-mapping-tc.log`，不得预记通过。ta/tb 失败证据已保留。剧集强制刷新、并发映射变化和正式入口集成仍待完成。


sz 已接入普通未链接资源的人工电影固定目标：force_refresh 仍执行自动获取，但在候选字段/upsert/缓存写入前，使用同一电影 lookup 比对目标；冲突或未找到时保留人工绑定，不应用候选；同目标继续原字段更新。**13 passed、1 warning，7.35 秒**，包含只读 lookup 和 12 项矩阵，Ruff 通过，证据 `probes/manual-mapping-sz*`，补丁已刷新。

该实现仍不可合入：已有链接资源尚未纳入映射固定目标，剧集季身份尚缺守卫；查找与 upsert 之间的并发身份/映射变化还须验证并保护，不能把串行正反例解释为无竞争窗口。完整 Agent 回归需在最终实现后重跑，资源入口集成、事务取消和完整门禁仍待完成。

sy 已终态：**169 passed、1 warning，46.67 秒，退出 0**；现有 metadata_service 回归及只读目标查找的新断言均通过，证据 `probes/manual-mapping-sy*`。session 66534 已关闭。下一步接入候选身份守卫并重跑 sx 矩阵，不能把本次基础重构回归当作强制刷新已修复。

sy 准备：将电影 upsert 的身份袋→旧主 ID→精确标题选择部分拆为 `find_existing_movie_for_external`，原 upsert 复用同一函数，避免身份守卫与真实落库选择规则漂移。新测试确认调用后字段及身份袋未修改；完整 `test_metadata_service.py` 与此测试正在 session **66534**，日志 `/tmp/rssripple-v15-lookup-sy.log`、JUnit `/tmp/rssripple-v15-lookup-sy.xml`，Ruff 已通过。这一步尚未接入强制刷新守卫，sx 两个身份冲突红测仍待修；补丁/哈希已刷新。

sx 扩到 12 项，新增同作品强制刷新补齐 release_date 正例，**2 failed、10 passed，7.28 秒**；两个失败仍仅是不同候选覆盖有效映射。四个同作品刷新组合均实际持久化日期，后续不能以跳过刷新让红测转绿。证据 `probes/manual-mapping-sx*`，补丁已刷新。另复核 `find_local_work_for_entity` 会调用 add_external_id 写身份袋，不能直接作为拒绝前的只读验证器；新的固定身份检查必须在任何作品或身份袋写入之前完成。

sw 将正式映射矩阵扩为 force_refresh False/True × 四种映射，结果 **2 failed、6 passed，5.07 秒**。失败仅为强制刷新下 normalized/raw_fallback 两个有效映射；独立会话确认关联变为自动候选，且 `_get_cache.assert_not_awaited()` 通过，因此缺陷位于刷新候选落库而非读取旧缓存。`_run_react` 和 genre enrichment 使用明确替身，真实资源/作品写入未替换；日志中测试库缺 FTS 表的降级信息不是断言失败原因。证据 `probes/manual-mapping-sw*`，含红测的原型补丁已刷新。

下一步应在刷新候选写入前固定并验证人工目标身份；不能先 upsert 错误作品再回写 resource FK，也不能通过直接返回映射掩盖缺字段刷新需求。需补同身份候选能更新缺失字段、不同身份候选无作品/身份袋污染、失败无内部提前提交的正反例，再实现该边界。

st 已结束：**204 passed、1 warning，77.86 秒，退出 0**，证据 `probes/manual-mapping-agent-st*`。新增正式 `tests/unit/test_manual_title_mapping.py` 使用录制 case `00e48de8-b5fd-4e99-8467-d384bd4a3183` 和真实 Turso，覆盖 normalized/raw_fallback/empty_target/other_channel 四项；sv **4 passed、1 warning，2.68 秒，退出 0**，Ruff 通过，结果 `probes/manual-mapping-sv*`。独立会话确认合法映射优于成功缓存，无目标及其他频道映射不阻断原缓存路径。补丁/哈希已刷新，仍为普通路径部分实现，不能替代完整入口集成与强制刷新验收。

ss 现有 MetadataAgent 文件回归：196 passed、8 failed、24.79 秒。8 项均因纯编排测试资源替身没有 title_cn，在新映射查询前失败；证据 `probes/manual-mapping-agent-ss*`。按 Agent 已有缓存/本地匹配 I/O 边界新增 `_apply_manual_mapping`，两个纯编排替身显式返回无映射；实际 ORM 映射函数未跳过。su 真实 Turso 录制标题探针再次通过，相关 Ruff 通过，证据 `manual-mapping-su.log`。这不替代其他边界验收。

st 重跑同一现有测试文件，session **25832**，日志 `/tmp/rssripple-v15-agent-st.log`、JUnit `/tmp/rssripple-v15-agent-st.xml`；进程仍在运行，继续轮询原句柄。V15 已有独立 tests 副本，未改 B4 冻结测试；聚合补丁现在包含两个运行文件和一个单元测试文件，后续还须增加真实映射正式回归、强制刷新及完整门禁。

`probes/manual_mapping_cache_probe.py` 从 v1 录制语料选取电影发布标题，在临时 Turso 数据库创建两部明确标注的合成电影、人工映射和冲突的成功缓存。普通 `fetch_and_link_metadata` 关联到人工选择，`UnifiedMetadataAgent.process` 却关联到缓存电影；提交后独立会话确认两者不同。`probes/manual-mapping-sq.log` 的目标断言失败证明缺陷。so/sp 是临时数据库配置及夹具字段错误，不是产品缺陷。探针不验证外部 metadata 提供者质量，不触碰生产数据。

## 方案边界

1. 共用人工映射查询与应用逻辑，保持频道隔离、search_title_key 优先及旧 raw_title 回退。默认 Agent 在成功缓存和自动匹配之前识别有效人工约束，不能靠禁用缓存解决。
2. 已链接资源、显式强制刷新和未链接资源分别验证。权威四层匹配的第一层保留已有链接；`force_refresh=True` 又要求绕过缓存和本地标题捷径，补齐作品信息，不能用无条件提前返回破坏该用途。
3. 有人工映射时，刷新须保留所选作品身份；显式刷新仍应通过该作品的权威身份刷新元数据并尊重人工字段保护。实现前复核现有 work refresh 入口，避免再造一条来源匹配管线。未知/失效映射不能直接标记匹配成功，须保留可诊断状态或继续符合契约的回退。
4. 链接季作品后仍执行统一 reconciliation，季号不能由映射的存在本身猜测。电影关联需清理不适用的剧集疑义；多作品合集不得因平面映射丢失权威 links/assignments。
5. 人工选择不能污染通用自动缓存；保留 force_refresh 的缺失字段补齐能力，同时不允许刷新重新选择另一作品。所有数据库写入保持现有事务和 B4 所有权保护边界。

## 严格验收

### 刷新入口复核

`metadata_search.refresh_work_by_source` 接收现有 work，但按标题调用 `search_metadata_candidates`，再取首个确定性候选；这不等同于按人工映射身份获取详情。其 season-0 回填路径自行 commit，`apply_work_metadata` 也有自己的提交边界，因此不能将带有资源未提交修改的会话直接传入。实现须先复核 B4 合入后的版本，优先复用提供者详情获取和候选应用能力，明确身份冲突拒绝与事务归属；若沿用作品刷新编排，必须在独立受控事务中执行且不能改变已确认资源的目标身份。新增验证需要在刷新失败/取消时确认资源绑定与消费发布不会被内部 commit 提前提交。此为方案发现的约束，尚未实现新的刷新分支。

| 用例 | 必须证明 |
|---|---|
| 已录制标题＋人工映射＋冲突成功缓存 | 默认入口提交人工选择，缓存不得覆盖 |
| 标准化标题不同集数/清晰度 | 同频道映射可复用；不同频道不得串用 |
| 旧 raw_title 映射 | 新标准化键未命中时保持兼容 |
| force_refresh 与缺失日期等字段 | 所选作品身份保持，同时实际刷新能补字段 |
| 已链接资源与另一个映射 | 明确遵循已有链接/显式编辑契约，无隐式换作品 |
| 季作品、未知季、电影和多作品包 | reconciliation、FK 互斥及关联形态不被破坏 |
| 无映射、失效映射、网络失败 | 原自动路径可用，不制造虚假成功或静默换作品 |

回归须覆盖真实 ORM 持久化和完整 fetch/backfill/reparse 入口；外部服务使用明确替身，尽量复用录制标题与种子，不把生产旧关联当真值。必要性红测先在原实现失败，再在候选上通过；全量单元/API ≥95%、唯一项目完整集成 ≥85%、跳过项审计、正常退出和数据清理后才关闭。完成前不合入 main，不因探针通过缩小到只修成功缓存一个分支。
