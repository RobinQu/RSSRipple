# V15：人工标题映射优先级（P1-M1）

## 下一步续接

候选已冻结，文件哈希见 `probes/manual-mapping-vr-frozen.json`。本轮审查见 [V15-FINAL-REVIEW.md](V15-FINAL-REVIEW.md)：形态编辑 vm 红测与音频映射 vo 红测均已修正；vp 243 passed（81.56 秒），vq 正式双库 15 passed（30.69 秒），全仓 Ruff 通过，vq 容器清理退出 0。

完整单元/API vr 正在 session 81952，日志 `/tmp/rssripple-v15-unit-vr.log`，JUnit/coverage XML 同前缀，门槛 95%。完整隔离集成项目 `rssripple-v15-final-vs` 启动已退出 0；完整 test-runner 正在 session 13955，日志 `/tmp/rssripple-v15-integration-vs.log`，工作目录 `/tmp/rssripple-v15-manual-mapping`。运行期间不得修改候选 app/tests/scripts/config；先轮询原句柄，禁止重复启动。两轮均尚无最终验收结果，M1 未合入 main。

## vi–vl：正式双库集成矩阵

新增 `tests/integration/metadata/manual_mapping_driver.py` 与 `test_manual_mapping_concurrency.py`，八项 PostgreSQL + 五项 Turso 场景已进入正式集成收集。vk PG 八项通过（16.59 秒）；vl 完整新矩阵 **13 passed，26.68 秒，退出 0**，相关 Ruff 通过，PG 容器清理退出 0。最终外键限定恢复已经复跑通过；真实 23505 唯一约束错误即使映射同时变化仍上抛。候选、测试清单及业务设计已同步到原型补丁，未合入 main。

vi 驱动复制时用了错误相对路径，测试缺驱动失败；vj 7 passed/1 failed 是唯一约束注入前 autoflush 先触发外键失败，尚未执行唯一约束 SQL。修正该注入为 no_autoflush 后 vk/vl 才是有效结论，保留失败日志防止混淆。vl 使用真实录制标题，数据/替身/FTS 边界已写入 integration-inventory；每个 PG 用例独立数据库并 finally 清理。证据 `probes/manual-mapping-vi*` 至 `-vl*`，成功子进程日志在 `manual-mapping-vl-drivers/`。

下一步进行合入前五轴审查并整理剩余具体缺口，再冻结候选运行全量单元/API ≥95% 与完整隔离集成 ≥85%；13 项通过不能替代这两道门禁。

## vd–vg：提交阶段合并实际触发外键失败

vd 的“合并必然等待”假设被证伪：人工关联尚未 flush，合并重指资源时看不到它，因此可以先提交。ve 移除该假设继续真实写入，确认旧 movie_id autoflush 触发 PostgreSQL 23503 外键失败。不能把这一现象仅记录为无害的排序差异。

原型把外部候选写入（含人工关联应用）包在保存点内，遇外键失败则回滚候选、显式刷新 resource，再读取映射；只有映射确已改变才放弃旧候选，否则原样上抛。vf 首次恢复因回滚后过期对象触发 MissingGreenlet；修正显式 refresh 顺序后 vg 退出 0，旧目标不存在、资源没有旧关联，后续正常 process 实际 commit 到 survivor。专用容器清理退出 0。

vg 后又收紧捕获条件为 PostgreSQL 23503 / Turso foreign-key violation，其他 IntegrityError 直接上抛；该最后过滤条件尚需单独复跑，不能把 vg 归到修改后的源码。vh 四文件回归在收紧过滤前启动，已终态：257 passed、1 warning，94.67 秒，退出 0；证据 `probes/manual-mapping-vh.log/.xml`，结果须按该基线解释。下一步复跑最终恢复路径、验证无关完整性错误不会被吞，并整理正式集成驱动，暂不合入。

## uz–vc：真实合并入口与 Turso 对照

uz 使用真实 `POST /works/merge` 在外部查询期间合并人工映射目标：HTTP 200，mappings_updated=1；独立观察确认映射指向 survivor、旧目标不存在、旧查询被放弃，退出 0。va 增加下一次正常 process，实际 commit 后资源链接 survivor，退出 0，证明不会停留在永远不处理状态。采用同一录制标题，合并作品及外部候选仍明确合成，未替换合并服务或数据库写入。

vb 首次 Turso 探针建表后未启用 MVCC，在 fixture INSERT 阶段失败，排除出产品缺陷和覆盖结论。补齐与生产一致的 PRAGMA 后 vc 通过：真实 Turso 独立会话、已有映射、录制标题、查询期间 associations API 返回 200，最终人工电影与标题保留。简化建库没有创建 FTS sidecar 表，日志有 FTS 降级，不能据此声称覆盖 FTS 正常路径。

证据 `probes/manual-mapping-uz*`、`-va*`、`-vb*`、`-vc*`。所有进程终态，PostgreSQL 专用容器清理退出 0，Turso 临时文件清理记录 `manual-mapping-vc-cleanup.json`。本轮没有修改候选运行实现。下一步将这些真实入口场景整理为正式集成测试，并评估剩余候选检查到提交间的实际合并交错，再进入全量门禁。

## uu–ux：缓存及本地匹配也须保护编辑

录制标题和真实 associations API 交错显示：缓存返回前编辑已 commit（uu）、本地匹配返回前编辑已 commit（uv）均被旧快捷结果覆盖，两个目标断言失败。将最终外部候选的检查抽为 `_lookup_scope_is_current`，成功/非作品缓存、已知作品、确定性缓存未命中及非媒体分支在写入前复用；缓存 subtitle_group 延至相应检查后应用，避免查询本地作品时 autoflush 缓存字段。uw/ux 同一探针分别退出 0，最终人工关联和标题保留，隔离数据库清理退出 0，相关 Ruff 通过。

外部/缓存/本地 lookup 结果为明确合成替身，真实资源事务与 API 写入未替换；证据 `probes/manual-mapping-uu*` 至 `-ux*`。uy 四份完整相关回归已终态：**257 passed、1 warning，86.79 秒，退出 0**；证据 `probes/manual-mapping-uy.log/.xml`。该轮不覆盖音频专用解析或其他字段并发修改，不作为完整验收。

## uo/uq：录制标题、反向编辑与提前持锁

uo 使用录制 case `00e48de8-b5fd-4e99-8467-d384bd4a3183` 的原始标题，作品/外部响应仍合成。自动识别先持资源锁后启动编辑 API；实际 `pg_blocking_pids` 确认编辑连接等待锁，自动事务提交后 HTTP 200，最终独立读取保留人工电影及标题，退出 0。

uq 预先提交人工映射，再在 force_refresh 外部查询边界执行编辑 API，退出 1：5 秒超时，栈定位 `request_channel_resources` 查询引发的资源 autoflush 等待。原型提前应用映射后的数据库查询使外部调用开始前已有写锁，不能继续保留该编排。up 因探针编辑使用错误相对路径，未真正加入已有映射分支，虽然命令退出 0，也不计入该场景覆盖。证据 `probes/manual-mapping-uo*` / `-up*` / `-uq*`；专用容器清理退出 0。


## um/un：当前编辑向导 API 红绿对照

使用真实 `PUT /api/v1/resources/{id}/associations` 路由、独立 PostgreSQL 编辑会话、真实关联服务/请求记录与 commit，在自动识别外部查询返回前提交电影及标题修订。um 原型退出 0，HTTP 200，最终独立会话确认 movie_id/search_title 均为人工选择。un 使用同一探针在未包含 M1 的当前 main 上退出 1，明确失败于 “Old lookup overwrote the committed manual choice”。因此资源快照守卫具有当前产品入口的必要性证据，区别于 ul 的直接映射表插入。

证据 `probes/manual-mapping-um*`、`manual-mapping-un*`；测试容器清理退出 0，探针 Ruff 通过。外部候选和 genre 补全使用替身，后续 queue.enqueue 替换避免后台作业污染并发时序；HTTP 路由认证中间件未纳入本探针。标题/作品为合成夹具，真实数据回放仍由已有录制标题映射测试承担。尚须验证相反顺序（候选锁定后用户编辑）、去重/删除交错、已有映射长持锁及完整门禁，不关闭 M1。

## ul：最终检查之后的写入与必要性复核

ul 把独立会话的映射插入移动到真实 `_apply_to_resource` 调用之前、最终映射快照检查之后；实际落库仍执行原函数。PostgreSQL 退出 1，目标断言确认旧候选仍被提交，专用容器清理退出 0。证据 `probes/manual-mapping-ul.log`、`manual-mapping-ul-result.json`。增加读取次数无法解决这个窗口。

需要修正此前对用户入口的推断：全 app 引用检查确认 `manual_link_metadata` 只有定义，旧 metadata/link API 测试已明确 skip 为退役入口。现有 `PUT /resources/{id}/associations` 调用 `apply_association_update` 修改资源、作品 links 和文件 assignments，不创建频道标题映射。因此 ul 是数据库级竞争证明，不能称为当前 UI 用户操作复现，也不能据此立即给全局映射表加锁。下一步先以现有 associations API 的真实并发编辑验证资源保护，并覆盖确实存在的去重/删除映射变更；再决定映射终态协调的最小适用范围。M1 默认消费已有人工映射的必要性仍由录制标题缓存红测证明，不受入口退役影响。

## uj：查询期间映射变更保护

候选在查询前后记录有效映射的 ID、series/movie 目标和覆盖标题；后读取通过 populate_existing 避免 ORM 身份缓存返回旧值，并关闭 autoflush。映射不同则刷新资源、放弃旧候选。uh 同一 MAPPING_ONLY PostgreSQL 探针 **uj 退出 0**：提交后资源保持未链接，未写入旧自动目标；容器清理退出 0，相关 Ruff 通过。证据 `probes/manual-mapping-uj.log`、`manual-mapping-uj-result.json`。

这是对已提交变更的保护，尚未协调最终检查与提交间的新变更。缺失映射行插入、现有映射更新/删除、目标删除、缓存路径和长事务仍须验证；不得据此关闭 M1。uk 人工映射及 Agent 两份回归已终态，退出 0；完整计数与耗时见 `probes/manual-mapping-uk.log`，JUnit 为 `probes/manual-mapping-uk.xml`。

## ui：映射查询与写入口复核

已抽取 `find_manual_title_mapping`，原应用函数复用同一 normalized-key→raw-title 回退规则。新增真实 Turso 测试确认解析后资源仍未链接、标题与匹配时间不变，会话没有脏对象。映射文件回归 **37 passed、1 warning，18.51 秒，退出 0**，相关 Ruff 通过；证据 `probes/manual-mapping-ui.log/.xml`。此为方案基础重构，不关闭 uh 并发红测。

写入口复核发现：除 `manual_link_metadata` 外，`metadata_dedup` 多处分支批量重指映射，series/movie 删除 API 也会清空目标，数据库 FK 的 SET NULL 同样影响映射。因此只协调人工关联入口不足。需要统一保证最终映射读取与写入之间的并发顺序，包括无行时插入；不能依靠对不存在的映射行执行 FOR UPDATE。当前仍无完整协调实现，尚不可合入。

## 必要性

### 原型回归进展

uf 对 ue 同一 PG 探针已转绿，退出 0，专用容器清理退出 0。候选在网络查询后的落库入口以 SELECT FOR UPDATE 读取资源当前关联/合集/search_title，关闭 autoflush 避免把旧候选先写出；与查询前快照不一致则 refresh 并放弃旧结果，保持编辑会话提交的 movie_id 和标题。对应 I/O 方法在纯编排替身显式 mock，真实数据库路径未跳过。该实现仍只保护最终自动候选写入：缓存/本地快捷路径、映射表单独变化、删除、不同数据库并发及原本已有映射造成的提前 flush 均须继续验证，不能当作完整并发方案。ug session 60368 正在跑 Agent 及三份映射回归，日志 `/tmp/rssripple-v15-regression-ug.log`。

ud 基线同步完成：V15 保留 8 个修复文件，更新 115 个未修改基线文件到本地 main `9b9a5fa`（含 B4 后补修复、测试及配置）；业务文档唯一 M1 段落先剥离验证旧基线哈希，再叠加到当前主干。新补丁 apply --check 通过，清单 `probes/manual-mapping-ud-rebase.json`。

ue PostgreSQL 并发红测已确认：自动查询启动时没有映射，查询替身内部另一会话创建人工映射并更新资源 movie_id/search_title、commit 成功；旧查询返回后覆盖新 movie_id。退出 1 的目标断言为预期红测；专用 tmpfs PostgreSQL 容器已清理（退出 0）。证据 `probes/manual_mapping_concurrency_probe.py`、`manual-mapping-ue*`。标题/身份/响应均合成，两个真实会话及 ORM commit 未替换。修复必须在候选写入前刷新并校验资源/映射，而非只保存查询开始时目标；同时验证不存在跨网络长持写锁与目标编辑/删除分支。

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

## vr / vs 全量失败，禁止合入

已确认原运行句柄退出 1：vr 单元/API 3985 passed、6 failed、15 skipped；vs 集成 3174 passed、10 failed、17 skipped。两个应用 SIGINT 后均退出 0，覆盖率汇总退出 0，报告已导出。覆盖率通过不能抵消测试失败。原始失败日志及 JUnit 已保存至 probes。

6 项单元及部分集成失败涉及旧 SimpleNamespace/MagicMock 未适配人工映射查询；其余必须单独处理：initial_d_franchise 回放请求未完全消费，以及三个 P0 季号案例在已链接短路返回时丢失返回对象的 season（持久化断言此前通过，不能据此关闭返回契约回归）。继续修复原型并重跑严格门禁，未合入 main。

## vu 返回契约补修候选

vr 冻结的 3043 个文件重新校验无差异。独立目录 `/tmp/rssripple-v15-vu` 基于该冻结候选补齐已链接/人工映射短路返回的 season、episode、season_ambiguous；原 P0 断言保留。仅对明确使用假数据库的编排测试补齐映射/锁查询替身，真实并发测试不改。增量补丁 `probes/manual-mapping-vu-increment.patch`，专项句柄 15846，结果待定。另有旧候选诊断 vt（93257）：准备脚本因错误相对路径未执行修改，故该轮不得计为修复验证。录制 initial_d_franchise 未消费请求原因仍待定位，未放宽完整消费规则。

## vv 专项结果与环境阻塞诊断

vt/vu/q/合集诊断四个进程因 Turso 沙箱连接等待而中止，退出 143。纯内存 SELECT 1 对照：沙箱内 12 秒超时（124，工作线程与事件循环均等待），沙箱外立即成功（0）。重新运行 vv：79 passed，6.79 秒，含原 P0 断言；日志/JUnit 已归档。

独立合集诊断最终审核图无差异，但原录制有 7 项未消费（两个 LLM judge、三个 Bangumi search、两个网络搜索）。诊断临时捕获完整消费异常以导出图，不算正式通过，正式 Cassette 规则未改。须审查这些查询是否属于已不再需要的单作品匹配，再建立新录制版本与严格回放。

## vw 严格语料回放通过

已审查 7 个未消费请求：全包标题的 Bangumi/Web judge、三个 Bangumi search（整包标题/BD/AV1）、两个网络搜索（整包标题/BD）。它们属于 inspection 已建立合集后多余的单作品匹配。保留原录制，新增派生 cassette 及来源哈希/移除键审计，不改变保留响应或审核答案。vw 使用正式完整消费检查：3 passed，4.58 秒。

当前候选移至 `/tmp/rssripple-v15-vu`，17 文件哈希与可恢复归档见 `probes/manual-mapping-vu-source.json`、`manual-mapping-vu-candidate.tar.gz`。vx 扩大回归句柄 49318，完整门禁仍待重跑。

## vy / vz 新完整门禁启动

vu 候选 Ruff 全部通过，3045 文件冻结于 `manual-mapping-vy-frozen.json`。完整单元/API vy 句柄 37266，覆盖率要求 95%；完整集成 vz 句柄 61155，唯一 Compose 项目 `rssripple-v15-final-vz`（启动退出 0），汇总要求 85%。结果未定，源码不得修改。结束后必须 SIGINT 应用、核对退出、汇总覆盖率、导出报告并清理测试卷；不得把启动成功当作验收通过。vx 扩大回归及 M2 t 尚在运行。

vx 扩大回归已退出 0，完整日志/JUnit 已归档；新全量 vy/vz 仍在运行。
