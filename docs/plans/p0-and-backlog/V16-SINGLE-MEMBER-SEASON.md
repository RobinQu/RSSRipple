# V16：单成员合集不等于单季证据（P1-M2）

## 必要性复核

独立临时 Turso 的真实 `create_or_update_series_from_external` 调用已复现：仅有一个 season_number=3 的合集，没有 season_hint、季数、逐季列表或外部身份，按合集标题匹配的候选仍返回该第 3 季。明确断言失败，退出 1；真实 upsert 和 commit 未替换，作品/标题为合成夹具，不能宣称生产资源已错误绑定。证据 `probes/single_member_season_probe.py`、`single-member-a.log`、`single-member-a-result.json`，数据库已清理。首次探针尚未使用录制输入，后续需增加真实无季标题回放。

维持 P1：违反季号不猜测，可能将合集级候选/资源写到碰巧已入库的一季；当前实验不证明文件丢失，不能仅据不变量上调 P0。修复的重点是证据语义，不能把 `len(members)==1` 改成 `season_number==1` 就宣称解决。

## 方案论证

- 季号由显式 hint、季级身份、可信标题季标记或经校验的单季证据确定；库内成员数量不构成来源季数证据。
- 合集身份/标题已确定但季号未知时，应保持 collection 身份并返回未定季状态；不自动选择现存唯一成员，也不增加替代季作品。
- 显式季号和季级 identity 仍应命中准确季，可信单季证据的既有入口需保留。需审查空合集 fallback，避免同一种无证据默认 S1 从另一条路径回流。
- M1 已有 expected_series_id 约束也引用单成员条件；M1 验收完成后以合入代码为基线统一更新，不改当前冻结候选。

## 严格验收矩阵

覆盖现有唯一成员 S0/S1/S3、零/多成员；无季数/可信单季/多季矛盾证据；显式 hint 与季级身份的正反例；集合级身份袋和精确标题两入口。断言真实返回值、资源工作 FK 与 collection_id、作品数量和身份袋归属，拒绝路径实际 commit 后再用独立会话核验。加入录制无季标题及明确标注的来源响应替身。先在原代码红测，再双库及实际 metadata/fetch 入口验证；最终完整单元/API ≥95%、隔离集成 ≥85%，跳过项/退出/清理/冻结哈希均通过后才关闭 TODO。

当前只有必要性红测和方案，尚未修改实现；M1 vr/vs 正在完整门禁，禁止改其 app/tests/scripts/config。


## b：季证据边界矩阵

独立 Turso 扩大八场景：**5 failed、3 passed，退出 1**（脚本收集全部结果后统一断言，不是 pytest 计数）。失败为无证据唯一 S0/S1/S3、来源明确三季但本地唯一 S1、空合集无证据默认创建 S1。通过为多成员未知季保持未定、显式 hint=3 正确选 S3、可信单季 count=1 选 S1。每项实际 commit，再独立会话核对作品数量；空合集错误新增一行，其余错误选择既有季。记录 `probes/single-member-b.log` 和 `single-member-b-result.json`，临时库已清理，原执行脚本哈希保留后才修正 lint 换行。

因此修复必须同时去掉已匹配合集的无证据空库 S1 fallback，保留正例；不能仅在单成员条件上增加一个判断。候选 `number_of_seasons` 只作为外部证据输入，未写入退役 ORM 列。此矩阵仍是合成数据，不把录制语料中的历史 season=1 作为真值；后续真实标题回放应只复用原始输入、独立设置证据与期望。


M1 衔接约束：`expected_series_id` 来自明确人工目标，其单季身份本身是用户指定证据，应与“仅因合集当前有一个成员而自动猜季”区分。V16 不应因禁止基数推断而破坏有效人工目标的同身份补全；若需要定季，应从已确认的目标作品取得季号并验证归属/身份，禁止再通过成员数量代替证据。此路径需在正式正例矩阵单独覆盖。


## c/e：独立原型与正式季证据测试

已从冻结 M1 复制到 `/tmp/rssripple-v16-single-member`，不修改 M1 文件。`_resolve_collection_member` 对未知季直接保留合集身份并返回 None，取消单成员选季和空合集默认 S1；明确 expected_series_id 在同合集且季号/身份兼容时，从用户指定作品取得季号。原八场景 c 全部通过（脚本断言，退出 0），独立库已清理。

正式 `test_collection_member_season_evidence.py` 覆盖标题/合集身份袋两入口 × 八种证据组合，以及明确人工 S3 目标在单/多成员合集内的补全：**18 passed、1 warning，29.51 秒，退出 0**；相关 Ruff 通过，业务与单季设计已在原型同步。证据 `probes/single-member-c.log`、`single-member-e.log/.xml`，原型补丁及基线/候选哈希 `single-member-prototype.patch`、`single-member-source.json`。此补丁基于尚未合入的 M1 原型，不可直接应用 main。

d 扩大 metadata_service/人工映射/身份回归已终态：210 passed、3 failed、1 warning，167.31 秒，退出 1。M1 vr/vs 门禁仍在原冻结副本运行。


## d–g：旧测试契约复核

三项失败逐项核实：两个跨语言 Wikipedia 身份归并测试未提供季号，却要求落同一季作品；为保留其身份归并目标，在两次 upsert 均明确 season_hint=1，保留原 work ID、英文标题和别名断言。第三项测试直接要求“空合集无证据创建 S1”，与本次修复目标相反；改为不建季作品，真实 commit 后独立会话验证合集无成员、TMDB 身份仍归合集。没有以降低断言覆盖掩盖问题。

f 相关三项加十八项正式矩阵：20 passed、1 failed、30.32 秒；唯一失败为新独立查询漏导入 select（测试错误），修正后 g 单项通过，1.77 秒。保留 f 失败记录，结果不相加冒充完整一轮通过。相关 Ruff 通过。下一步正式 PostgreSQL/真实资源入口和录制输入回放，最终重跑完整回归及全量门禁；尚未验收合入。M1 冻结文件仍保持不变。


## h/i：录制标题与真实资源持久化、双库正式测试

复用录制原始标题 case `011c6d44-68cf-43a8-bad3-f0398ce20a95`，不使用其历史 season 作为真值；TMDB ID、合集季成员与来源响应为合成。新增 repository/强制 MetadataAgent × 空/唯一 S3 合集 × 未知/明确 S3 八项：真实 upsert、资源写入及 commit 后独立会话检查，未知季工作 FK 空、挂合集、season=None、ambiguous，不增作品；明确季号必须正确创建/关联 S3，身份袋归合集。

h Turso 正式文件 **26 passed、1 warning，38.34 秒，退出 0**。i 正式 PostgreSQL 集成驱动复用全部 26 项断言，pytest **1 passed，4.30 秒，退出 0**（不得称 pytest 26 passed），包含独立建库与 finally 清理；容器清理退出 0，相关 Ruff 通过。证据 `probes/single-member-h*`、`single-member-i*`；集成清单已更新原型，仍未合入。

尚需扩大默认缓存/抓取路径、审查更外层的无证据兜底、完整回归及最终两道覆盖率门禁。M1 vr/vs 仍在运行且禁止修改其冻结文件。


## j–m：默认缓存与真实抓取事务

j 成功缓存四项通过（6.72 秒）：真实写入 MetadataCache 后调用默认 Agent，明确断言不调用外部 ReAct，同时保持未知季待确认/显式 S3 正例。随后接入实际 `_process_resource_metadata_once` 的独立会话和 commit，外部 torrent 缓存/inspection 为替身，实际 Agent/repository/发布未替换。

k 四项失败均因夹具没有 B7 要求的初始 created publication，生产函数按契约回滚；补齐真实 `publish_resource(kind=created)` 后，断言 metadata publication 恰有一条且资源/身份袋最终符合季证据。l 完整 Turso 文件 **34 passed、1 warning，48.97 秒**；m 正式 PostgreSQL 驱动 **1 passed，5.68 秒**，内含同组 34 项实际断言。两轮退出 0，专用 PG 容器清理退出 0，失败/成功日志和 JUnit 均保留 `probes/single-member-j*` 至 `-m*`。不把 mock 网络组件解释为实际 torrent 或实时提供者验收。

M2 尚需最终扩大回归、合入前审查及完整覆盖率门禁；原型运行实现本轮未变化，仅扩大正式断言。M1 vr/vs 仍在运行，冻结候选未修改。


## n/o：外层同名与新建路径也存在默认选季

审查新增两条真实 upsert 红测：系列级 TMDB 候选、未知季数/季号，①无既有合集/作品时默认新建 S1；②合集名不匹配但仅有同名 S3 时，绕过合集成员函数直接选择 S3。n **2 failed、34 deselected，3.01 秒**，原局部修复不能覆盖两条路径。

对没有可信单季证据的系列级候选，外层同名匹配只归并到合集，无法唯一确定合集时保留独立合集身份；新系列同样先保留合集身份，不创建默认 S1。明确季号、可信单季及季级来源身份沿用正常分支。o 完整 Turso 季证据文件 **36 passed、1 warning，53.38 秒，退出 0**。权威业务/单季文档已在独立原型同步，未改 M1。

p 扩大 metadata_service/repository/三份人工映射回归正在 session 3996，日志 `/tmp/rssripple-v16-regression-p.log`、JUnit 同前缀 `.xml`。PG 正式驱动已增加两项外层断言至 36 项，但尚未重跑；之前 m 的 34 项只证明修正前基线，不挪作当前运行实现的最终证据。还须等待 p 结果逐项处理兼容性、完成双库和完整门禁。

## p 扩大回归结果

255 passed、5 failed，退出 1；原始日志和 JUnit 已保存。失败涉及人工映射更新、LLM fallback、别名合并、人工身份保护和无共享合集多候选。须逐项区分无季证据的预期变化与实际回归，不得统一改断言绕过。M2 尚未验收或合入。

## q 定向验证准备

p 五项失败均缺少季证据。四项映射/别名/身份保护测试补充显式 season=1 或 season_hint=1，保留原行为断言；多候选测试保留无季号输入，要求返回 None、现有 S1/S2 数量不变并保存合集身份。未修改运行代码。定向五项测试句柄 60195，日志 `/tmp/rssripple-v16-q.log`，仍在运行；不能视为通过。

q 环境中止后重跑 r：4 passed、1 failed（2.97 秒）。剩余多候选测试无新季作品断言已通过，新增外部身份袋断言 NoResultFound；须检查 llm_search / wikipedia:999 合成身份是否符合规范，不可直接删除身份归属断言。

r 身份袋失败源于旧合成输入使用 llm_search（身份袋明确排除该非规范源）。改用合成 TMDB 系列身份 tmdb:90006486，保留身份归属断言；s 五项通过，2.82 秒。t 扩大回归（含 36 项季证据）句柄 58115，未完成完整验收。

## t / u 扩大验证通过

t 回归 296 passed，208.23 秒，包含完整 service/repository/manual mapping 与 36 项季证据。u 正式 PostgreSQL 父测试 1 passed，8.52 秒，驱动实际执行全部 36 项断言；结果 JSON 与驱动日志已归档。专用容器 rssripple-v16-evidence-u 已删除（含测试卷），仍需在 M1 验收后同步基线并执行 M2 完整门禁。

## v 最新 M1 基线重放

独立目录 `/tmp/rssripple-v16-rebased` 已将八文件 M2 差异移到冻结 M1 vu 基线。非冲突文件逐一核对旧/新基线相同；business-logic 两边仅末尾追加，保留两段完整内容。v 扩大回归含 P0 季号和严格语料，句柄 42944，日志 /tmp/rssripple-v16-v.log。M1 冻结候选未改。

v 最新基线扩大回归：305 passed，366.96 秒，含 P0 季号与严格语料。合入审查发现现方案保留 season-granularity 外层兜底，单季条目身份不等于已知系列季号，与禁止猜季要求存在潜在冲突。新增 w 两项真实数据库反例（合成 Bangumi 身份，无季号；空库/同标题 S3），结果见日志；该边界解决前不启动 M2 最终门禁，不将旧通过矩阵当作完整修复。新测试位于 /tmp/rssripple-v16-rebased，旧候选归档为历史。

## x 取消来源粒度猜季

去掉外层按 granularity=season 放行的无证据标题匹配和新建 S1 兜底；明确季证据在前置阶段计算，已链接身份仍可复用。x 38 passed，57.21 秒。y 扩大调用链/P0/录制语料回归已启动（日志 /tmp/rssripple-v16-y.log），需检查旧兜底依赖，不可只改预期掩盖产品问题。PG 驱动待扩到 38 项后重跑。文档检查点提交 36cfdf6 仅含计划/证据，运行代码仍未合入。

z 正式 PostgreSQL 驱动 38 项断言通过（父测试 1 passed，9.08 秒），含新增季粒度身份无季号的空库/同标题两项反例。结果 JSON、驱动日志和 JUnit 已保存，专用容器及卷已清理。y 扩大回归仍运行（79858），未验收。

y 扩大回归退出 1：306 passed、1 failed（372.39 秒），唯一失败 initial_d_franchise 的录制请求过用/未消费。aa 独立诊断临时放宽已有请求的次数上限（不改变响应、不允许新请求），以避免过用异常污染最终关联观察；随后恢复原计数记录严格错误，不能当作正式验收。诊断句柄 53293，输出 /tmp/rssripple-v16-franchise-aa.json。须分析最终图和请求原因后再决定修复，不能直接更新审核答案。

aa 诊断结束：最终图仅三个 OVA 的 MAL 壳作品/指派发生差异（mal:821、3931、5228，旧预期均为 S1；新结果不建季作品，三个文件 work/season=None）。普通六季、已有特典与电影关联保持一致；13 个请求计数变化。需逐条审查录制源是否有明确季证据以及原审核答案的 S1 依据，不能恢复默认猜季，也不能把诊断次数放宽直接用于验收。

aa 录制源重审：原 evidence_note 明确三 MAL 条目为 identity-only，三个录制 finalize 的 inferred_season=null，均无 S1 依据。新增独立 review/cassette 版本，保留旧版；仅移除三默认 S1 壳作品期望，三个文件 work/season=null，原其他字段与未知覆盖不派发断言保持不变。调整精确录制请求次数（不改响应），来源哈希/旧指派/计数差异均留审计。ab 正式严格回放 3 passed，6.80 秒。当前 13 文件哈希与可恢复归档见 single-member-v-rebase.json、single-member-ab-candidate.tar.gz；尚待最终审查/完整门禁。

## ac / ad 完整门禁启动

13 文件候选以已合入 M1 为基线，核验未列入清单的文件无差异。相关 Ruff 全通过；3051 文件冻结于 single-member-ac-frozen.json，验证期间禁止改动。ac 完整单元/API（64931）要求 ≥95%；ad 完整集成唯一项目 rssripple-v16-final-ad，启动退出 0，报告要求 ≥85%。五维审查见 V16-FINAL-REVIEW.md，仅允许验收，未批准合入。结束后必须完成应用 SIGINT/退出码、覆盖率汇总、报告导出、跳过审计及项目清理。

## 全量门禁失败的独立定位（ae）

ac 单元/API 与 ad 集成原进程继续运行，冻结候选未修改。通过 collect-only 节点顺序定位 ac 首批失败，在独立 `/tmp/rssripple-v16-failure-review` 复现：14 failed、108 passed，51.27 秒，退出 1；日志/JUnit 为 probes/single-member-ae.*。失败集中于 anime、genre、外部身份袋和 franchise 测试中无季号证据却预期创建 S1。

方案再论证：字段更新/身份收敛用例应显式提供合法季号证据，保留原字段与身份断言；两个 franchise 无证据测试原本明确要求创建 S1，违反单季化不猜季约束，须改为无 TVSeries、无可派发关联，且保留明确 First Stage 的正例。不能恢复默认 S1 来通过旧测试。独立副本先验证夹具修订与现有 38 项证据矩阵，全量冻结候选仍不允许合入。

夹具 af 尝试 12 failed、148 passed，131.90 秒：两项不猜季负例与 38 项矩阵通过；补在来源数据顶层的 season_number 不属于 upsert 输入契约，剩余失败仍在原路径。已归档 af 证据；ag 改用直接调用的 season_hint=1 与 franchise 合成来源的明确 Season 1 别名，保留其余断言，正在同一独立副本复验。四份测试修改见 probes/single-member-ag-fixtures.patch，未修改运行代码，也未改动 ac/ad 冻结文件。

权威 business-logic.md 的 franchise ④/⑧ 两处仍描述“无证据创建独立 S1 壳”与“基础名可默认 S1”，与 M2 终态冲突。已在独立候选中同步改为未定季不建作品/派发关联，保留明确 First Stage、specials 和已有季身份路径；修订随 ag 补丁归档。

ag 已退出 0：**160 passed、1 warning，128.49 秒**。覆盖四个原失败文件及完整 38 项季号证据矩阵，五份候选修改哈希核对一致，Ruff 通过。证据 probes/single-member-ag.*。ac 原全量已执行约 2160 项，已见失败仍为首批 14 项；ad 原隔离集成仍运行。必须等待完整终态并排查其余失败，再应用候选夹具修订和重新执行严格全量门禁，不能以 ag 专项替代验收。
