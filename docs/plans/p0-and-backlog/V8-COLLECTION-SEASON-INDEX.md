# V8：合集内单季唯一约束（P1-D1）

## 必要性与边界（2026-09-20）

真实 Turso 的生产 `create_tables()` 路径允许同一合集出现两个 S0 或 S1 作品。四项复现分别覆盖新装库（先删除全部模型表，再启动）和已有表缺索引的升级库；均实际写入重复作品，未抛出 IntegrityError。保持 P1：它破坏单季身份与关联的唯一性，但本轮没有证明文件丢失等 P0 后果。

当前权威设计把部分唯一索引留给离线单季迁移；新装库没有历史数据需要收敛，却同样缺索引。仅在 ORM 声明索引能修复新装库，不能补齐已有表，已由两项升级红测验证。D1 不能仅凭模型声明或 schema 文本检查关闭。

## 独立原型

目录 `/tmp/rssripple-v8-index-review` 的 app 是独立复制，未改动根工作树或正在运行的 V6/V7 源码。可恢复补丁见 [prototype](probes/collection-season-index-prototype.patch)，哈希及结果见 [result](probes/collection-season-index-result.json)。最新补丁已重建到 V7 当前未提交工作树基线；应用前必须核对 root_before 哈希，不能套到旧 V6 的 database.py/collections.py 上。

1. ORM 声明 `uq_tv_series_collection_season`，Turso/PG 均采用 `WHERE collection_id IS NOT NULL`；S0 与普通季一样受保护。
2. 轻量迁移在补齐 season_number 后幂等创建同名索引，补齐已有表。
3. 冲突升级暂采用数据库拒绝建索引、启动事务回滚的原型行为，不自动删除、合并或重新分配历史作品；实际测试确认两个人工编辑作品的 ID、标题、合集、季号和保护字段均保留。**这会使有重复数据的库无法启动，部署前必须完成迁移/修复；最终方案须补充可操作诊断和恢复步骤，不能直接发布当前原型。** 不以捕获异常后继续无约束运行冒充 D1 已修复。
4. NULL 合集仍在部分索引之外，消除孤儿由 V7 负责；本轮不把部分索引称为 NOT NULL 约束。

## 当前证据

| 版本 / 选择集 | 真实 Turso 结果 |
|---|---|
| 原实现，新装/升级 × S0/S1 | 4 failed，8.88 秒；重复写入未被拒绝 |
| 仅模型索引，同一四项 | 2 passed、2 failed，8.84 秒；升级仍失败 |
| 模型＋轻迁移，同一四项 | 4 passed，8.78 秒 |
| 增加历史冲突与人工编辑保护 | 5 passed，10.37 秒 |

所有数据为明确标注的合成作品，数据库及启动路径真实执行。每项正例同时验证其他季、其他合集及两个 legacy NULL 父项可写，重复写入在 SAVEPOINT 中被拒绝，重跑启动幂等。这里没有声称使用真实来源 API 录制数据，PostgreSQL 续验结果见下。

## 验收前剩余工作

- PostgreSQL 已补真实新装/升级双进程启动和同季竞争写入；后续与 API 竞争回归合并。所有 Compose 显式唯一项目名。
- 已补已有合法作品、Episode 与身份袋的 Turso/PG 升级保留测试；仍需结合完整迁移回归。
- 审计创建/编辑/合集指派和 metadata upsert 的冲突处理：违反同季唯一性应按 API 409 契约返回，事务必须完整回滚；不能让数据库约束变成未处理 500。结合 V7 有序父行锁验证跨进程竞争。
- 审计离线迁移及测试中的 legacy 多季/同季数据：迁移前旧 schema 夹具应明确去除索引，不为兼容非法新数据而弱化生产约束。
- 完善冲突库部署前诊断与恢复 runbook，验证启动失败没有部分迁移落库；不自动选择人工编辑作品的幸存者。
- 同步 data-models/per-season-works/db-migration/API 权威契约及测试清单，按 code-review-and-quality 复审，再运行完整单元/API ≥95%、隔离集成 ≥85%；测试和覆盖率均退出 0，源码哈希不变，应用优雅退出后导出证据并清理。

当前仅原型，不关闭 TODO，也未合并 main。

## 双库续验（2026-09-20）

Turso 新增启动前已存在 S3 作品、Episode 与身份袋的实际升级保留用例，扩大 **6 passed、1 warning，11.04 秒，退出 0**。升级后再次插入同季被拒绝，原 ID、合集、标题、季号、人工编辑保护及关联均保持。

独立 PostgreSQL 项目 `rssripple-v8-index-20260920-ao` 验证通过，驱动退出 0：新装和有数据升级均分别启动两个真实 Python 进程调用生产 create_tables，四个进程全部退出 0；S0/S1/S3 重复写入为 23505；竞争 S5 先观察到 pg_blocking_pids 非空，再提交第一事务，第二事务以 23505 回滚，只保留第一作品。冲突旧库启动失败后完整保留所有作品行；未猜测合并。项目已 down -v 清理。驱动及机器结果见 [PG probe](probes/collection_season_pg_probe.py)、[PG result](probes/collection-season-pg-result.json)。

此结果未覆盖 API 冲突响应、旧数据迁移 runbook 或完整门禁，仍不关闭 D1。

## API 冲突续验及 V7 基线同步

唯一索引原型下两项实际 API 红测（孤儿直接指派、壳合集吸收）均因未处理 IntegrityError 失败，2 failed，2.74 秒。独立 V8 app 已同步 V7 通过单元/API 的生命周期与有序父锁实现；在持有目标/来源父锁后检查目标同季占用，明确 409 DUPLICATE_SUBMISSION。两项 API 连同六项迁移回归 **8 passed、1 warning，4.51 秒**；拒绝后作品归属、两个合集、目标别名和来源身份袋均不变。

后台不遵循父锁的写入与 API 交错、season_number 编辑路径和异常 SAVEPOINT 处理仍需验证。既有合集 API 与新冲突用例扩大回归 **26 passed，10.95 秒，退出 0**，日志 `/tmp/rssripple-v8-collections-expanded.log`。PG ao 证据对应此前索引/迁移原型；不能外推为 V7 合并后的 API 并发已验收。最新可恢复补丁包含五个文件，root V7 ap 冻结源码未修改。

静态入口核对：当前 TVSeriesCreate/TVSeriesUpdate 不包含 season_number 或 collection_id，普通作品创建/编辑不能直接写入这两个字段；不应为了 D1 新增此类写入口。剩余季号写入审计应聚焦 metadata upsert、合集吸收与离线迁移；API/后台竞争仍需实际交错事务证据。

## D1 API 检查后的竞争写入（2026-09-20，aq）

真实 PostgreSQL＋生产合集 router/异常处理器的确定性交错复现：API 检查 S1 空闲后暂停，另一事务把目标合集已有 S2 成员改为 S1 并提交，再恢复壳吸收。第一版返回 **500 INTERNAL_SERVER_ERROR**；数据库拒绝重复并回滚，作品/来源合集/身份袋/别名虽保住，但 API 契约错误。此写入不改 collection_id，不能依赖父 FK 锁消除竞争。

原型把壳吸收/直接挂载的完整变更放进 SAVEPOINT，特定 `uq_tv_series_collection_season` 或 Turso 对应列唯一约束才转换为 409。相同交错改为 **409 DUPLICATE_SUBMISSION**，来源合集、incoming 归属、身份袋、目标别名与作品行数全部保留，probe 退出 0。另以实际身份袋唯一约束故障注入确认非季号异常仍抛出，不被误报为季冲突；连同既有合集 API 和迁移回归 **33 passed、1 warning，15.27 秒**。之后仅修复测试 import 排序，Ruff 通过。

驱动见 probes/collection_season_api_pg_probe.py；红/绿机器证据见 probes/collection-season-api-pg-result.json。所有数据明确合成，真实 SQL/HTTP ASGI/事务执行；`rssripple-v8-api-20260920-aq` 项目已清理。待补两个实际 API 同时抢占空槽、metadata/迁移扩大回归、冲突升级预检/恢复步骤、权威契约和完整门禁。离线旧迁移有碰撞合并行为，不能未经人工保护审计就推荐它自动修复所有冲突库。

## 双 API 抢槽、只读预检与迁移扩大（2026-09-20，ar）

两个真实 HTTP ASGI 请求向同一目标挂载不同壳合集的 S1：第一请求检查后暂停，第二请求的 pg_blocking_pids 明确非空；放行后 **201/409**，只有第一作品进入目标。失败方作品/壳/身份袋/资源归属保留，成功方资源与身份随之迁移，目标别名只合并成功方。驱动退出 0，证据为 probes/collection-season-pair-report-pg-result.json，ar 项目已清理。

在既有只读校验器增加 `--collection-conflicts-jsonl PATH` 独立模式，完整导出冲突成员 ID、合集、季号、主身份、标题与人工保护字段；不调用启动或创建索引。Turso 两项真实测试 **2 passed，0.88 秒**，覆盖 103 个冲突成员与合法对照，并记录全部 SQL 均为 SELECT。真实 PostgreSQL CLI 干净数据退出 0、冲突数据退出 1，完整 ID/保护字段与数据库行保持；不把预检退出 1 记为失败测试。

五份权威契约在独立副本准备，包含部署前备份/维护副本预检、冲突先留在旧版本人工核对、通过已支持解绑建壳的版本纠正归属，再预检/完整 verify/双启动；不推荐旧迁移自动合并保护作品。尚需实际恢复流程演练。

季拆分、合集与 franchise 服务扩大首轮 **71 passed、1 failed，23.12 秒**；唯一失败在兄弟列表测试夹具把同合集两作都默认 S1。只将夹具明确为 S1/S2，保留原排除/列表断言，正在复跑相同选择集，日志 `/tmp/rssripple-v8-migration-green.log`。最新原型 13 文件（含五份契约），仍未应用 root；V7 ap 继续冻结运行。

V8 相同迁移/合集/franchise/预检选择集复跑终态：**72 passed、1 warning，22.63 秒，退出 0**。仅修正兄弟列表夹具的明确季号，未弱化约束或断言。后续先审计 metadata 写入与演练冲突库恢复，再执行完整门禁；V7 ap 原句柄继续运行。

## metadata 并发、恢复演练与 at 门禁（2026-09-20，as）

metadata 扩大初轮 **236 passed、1 failed，65.46 秒**；日期回退夹具先放 S0 再创建另一 S0，与新约束冲突。改为在已有特典日期与无日期 S1 的合集创建 S2，仍断言不能借用特典日期，未弱化日期逻辑。

真实 PG 双 upsert 红测发现同一合集空季槽两个 matcher 同时新建时，一成功一 IntegrityError。成员解析改为先 FOR UPDATE 刷新已有父合集再查询/创建，标题回退复用合集同样进入该路径。绿测观察到第二 backend 阻塞，两个事务成功返回同一作品 ID，数据库恰好一行。metadata 服务、单季 upsert、repository、身份修复扩大 **237 passed、1 warning，61.54 秒**。该锁可能覆盖既有海报未命中时下载（既有 30 秒超时），同合集并发会等待；不宣称消除了所有 metadata 并发身份问题。

恢复演练：PG 合成同季冲突与人工编辑数据，实际预检 CLI 退出 1；子进程加载独立 V7 app 调用其解绑端点，为明确指定作品建新壳；预检转 0，V8 连续两次 create_tables 成功且索引存在。原两个作品 ID/人工标题/保护字段/Episode/身份袋/资源关联完整保留。驱动与红绿结果见 probes/collection_season_metadata_pg_probe.py、collection_season_recovery_pg_probe.py、collection-season-metadata-recovery-pg-result.json；as 项目已清理，未操作真实业务数据。

独立副本补齐缺失支持文件，未覆盖原型；全 app/tests/scripts Ruff 通过。最新原型 16 文件，六份权威文档已准备。完整单元/API at 已启动，日志 `/tmp/rssripple-v8-unit-at.log`，句柄 `/tmp/rssripple-v8-gates-at.json`，冻结 `/tmp/rssripple-v8-source-at.json`（496 文件）；不改动冻结源码。尚需终态、最终复审及完整集成，D1 不关闭。


## at 失败修正与扩大回归（2026-09-20）

at 完整单元/API **3564 passed、8 failed、14 skipped，1563.65 秒，97.77%**，不能验收，见 probes/collection-season-complete-at-result.json。六项涉及同合集重复季号的旧夹具；去重继承合集是真实写入顺序问题，须在事务内先 flush 删除重复行，再继承合集。Wikidata 原正例的两作品改为明确 S1/S2，同时补同季真实 DB 负例：apply/dry-run **2 failed，1.41 秒**（此前错误返回 linked）。补修后遇占用返回 ambiguous，拒绝前不更新标签，实际写入先锁已有目标合集。

保留原断言目的：Bangumi stale S1 从独立壳合集开始，继续验证修正到 S2/人工保护；手工资源映射不删除；人工日期案例仍有可用正季日期而必须保持不变。七个相关测试文件扩大回归 **206 passed、1 warning，56.31 秒**，`/tmp/rssripple-v8-writepaths-av.xml`。V7 的 HTTP 解绑断言已在 at 终态后继承。需要重跑完整门禁，206 项不能替代验收。


## av 完整复验启动

V7 已提交本地 main `e535e1f`。V8 23 个实现/测试/权威文档文件经主工作树基线哈希比对后同步，未提交；全仓 Ruff 与差异检查通过。root 的 496 个源码/配置文件冻结于 `/tmp/rssripple-v8-source-av.json`。完整单元/API 与隔离集成 av 正在执行，句柄与日志见 `/tmp/rssripple-v8-gates-av.json`；终态前不得修改 root app/tests/scripts/config。at 失败仍有效，TODO D1 保留。


## V8 av 终态与 bc 集成复验

av 完整单元/API **3574 passed、14 skipped、6 warnings，1622.13 秒，21291/21764（97.83%）**，退出 0。完整集成 **3119 passed、17 errors、17 skipped、8 warnings，1660.27 秒，退出 1**；全部 17 个错误发生在 season_model 共用的迁移前生产录制数据加载阶段，新 ORM 自动创建唯一索引导致历史重复季槽无法载入。两个应用正常退出 0，覆盖率汇总退出 0，**19517/21764（89.68%）**。证据 `/tmp/rssripple-v8-artifacts-av`，机器摘要 probes/collection-season-complete-av-result.json；av 项目已清理，因测试错误不验收。

仅修正 tests/integration/season_model/conftest.py：初始化历史库时移除新索引，完整加载原始生产录制数据；实际单季迁移后明确断言唯一索引已重建。同时将加载过程纳入 finally，失败也恢复全局测试 engine/factory。没有改写录制数据、跳过测试或删除旧断言。定向完整 season_model **17 passed，25.66 秒**（`/tmp/rssripple-v8-season-fixture-bc.xml`），Ruff 修正局部 import 排序后全仓通过。

av 冻结文件仅这一个集成夹具变化，应用/单元/API 源码不变，复用已通过的 3574 项门禁；原型累计 24 文件。新完整集成项目 `rssripple-v8-complete-20260920-bc`，冻结 `/tmp/rssripple-v8-source-bc.json`，句柄 `/tmp/rssripple-v8-gates-bc.json`。V9 bb 独立副本仍冻结，其同名集成夹具须在 bb 终态后继承再启动完整集成；不能在运行中覆盖。

## V8/V9 联合最终验收（2026-09-20，bg）

完整单元/API：3612 passed、14 skipped、6 warnings，2255.37 秒，退出 0；覆盖 21343/21833（97.76%），95% 门禁通过。完整集成：3136 passed、17 skipped、8 warnings，1716.83 秒，退出 0；覆盖 19576/21833（89.66%），85% 门禁通过。两个应用退出均为 0，覆盖率汇总/证据导出/项目清理全部完成，报告在 `/tmp/rssripple-v89-artifacts-bg`。冻结的 2891 个源码/支持文件终态哈希一致；Ruff 全库及 diff whitespace 检查通过。

最终复核覆盖：新装/升级唯一索引、旧冲突只读预检与失败保留、真实 PG API/metadata 抢槽、SAVEPOINT 内关联原子回滚、Turso 实际错误文本转换，以及七处 FK 的双库新装/缺列/已有列矩阵、真实 INSERT/UPDATE/ON DELETE、带关联数据原子重建、故障回滚、并发启动与只读孤儿报告。无新增依赖；权威模型/API/业务/迁移/单季化/集成清单已同步。历史失败轮保留，不以更改原录制数据规避失败。

D1/D2 验收完成，从 pending-only TODO 删除。当前生产库未执行迁移或数据修复；新约束遇到既有冲突/悬空关联会拒绝启动，须按只读报告及迁移 runbook 修复，不能自动删业务行。机器可读证据见 probes/database-invariants-complete-bg-result.json。V10/V11 及其他待办继续保留。
