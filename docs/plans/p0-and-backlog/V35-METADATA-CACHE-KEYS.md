# V35 元数据缓存标题与键长度

## 必要性与优先级（2026-10-08）

基线 main `2f9cf00`。`FileResource.title_raw` 可存 1024 字符，但 `MetadataCache.title` 只有 VARCHAR(512)，且有 title 单列索引和 (title, source) 复合唯一键。真实 `_set_cache` 仅 strip 后写入原始标题，未做长度限制；`_get_cache` 按完整标题和 source 命名空间读取。

固定 SHA 的 prod_works_v1 共有 559 个资源标题，最长 176 字符，超过 512 的为零。因此这是合法边界的可复现缺口，继续按 **P2** 处理，不声称录制数据或生产库已有损坏。探针使用第一条录制标题的前 80 字符及录制 GUID/URL，追加明确标记的确定性合成 ASCII/CJK 后缀；不访问录制 URL 或外部来源。

## a/b 真实双库证据

先在真实数据库提交完整 FileResource，再调用实际 metadata_repository `_set_cache`，成功时独立会话 `_get_cache` 和原始列重读，检查 source 隔离。来源 verdict 明确合成，不伪称完整 MetadataAgent/LLM 流程。

- a：512/513/1024 × ASCII/CJK × PostgreSQL/Turso，**4 failed、8 passed，24.29 秒、实际退出 1**。PostgreSQL 的四个 513/1024 缓存写入失败，但资源行此前已成功保存；两个 512 对照与全部六个 Turso 场景通过。
- b：仅在独立测试库把缓存列扩为 VARCHAR(1024)，执行相同六个 PG 用例，**1 failed、5 passed、6 deselected，11.99 秒、实际退出 1**。1024 字符 CJK 仍失败：`uq_metadata_cache_key` 索引行 2968 字节超过该 PostgreSQL 实例 btree 限制 2704。不是把列改成 TEXT 或 1024 就能完成修复。

原始源码、日志/JUnit、实际退出与数据来源见 [a/b 摘要](probes/metadata-cache-title-a-b-result.json)。专用项目 `rssripple-v35-cache-a`（32911）已清理，容器/网络/卷标签为空。主干 app/tests/scripts 无改动，无本批后台任务。

## 待验证修复方向

1. 保留完整原始标题，采用 TEXT 加定长 SHA-256 摘要键，唯一性按摘要与 source 命名空间建立，移除对完整长标题的 btree 索引依赖。缓存读取和冲突更新还须比较完整标题；摘要碰撞不得命中或覆盖另一标题结果。不得通过截断、仅取前 512 字符、跳过普通长标题缓存或吞异常来冒充修复。
2. 所有缓存 writer 统一计算同一规范化键，保持现有 strip、来源隔离、generation 与完整 metadata_json 语义。验证同键并发更新与陈旧 generation 删除竞争，避免删除/插入竞态或过期 reader 删除新结果；不新增外部依赖或要求数据库扩展权限。
3. 实际旧库升级必须覆盖两后端：有界回填摘要、完整保存原 id/title/source/payload/generation/时间，目录/索引定义校验，失败整体回滚与可重试。显式处理异常旧数据及冲突，不自动清空整张缓存；需要表重建时先证明无遗漏引用，并遵守停旧写入进程与重连要求。新旧库约束必须一致。
4. 下一阶段用真实 MetadataAgent 调用方补验长标题缓存 miss→写入→hit、source 隔离、force refresh 与失败行为；当前 a/b 仅验证资源和 repository 边界，不能声称完整抓取流程已修复。

## 严格验收

- 上述 ASCII/CJK 矩阵在实际双库全部通过，加入共用长前缀而尾部不同的标题、空白规范化、来源隔离、旧 generation、完整结果保留与人工构造摘要碰撞。
- 旧库新键迁移、重复启动、异常数据拒绝、DDL/回填故障回滚、并发启动与停止旧连接流程；大数据回填须有界，不能用只含空表的迁移测试替代。
- 来源文本继续复用录制快照，长文本/来源结果/故障明确合成；不编造生产超长样本。
- 在前批验收后正式重基与全输入冻结，完整单元/API ≥95%、隔离集成 ≥85%、零失败，逐项审计跳过、退出、覆盖率导出、清理和五轴审查。

## c/e 新库原型（未合入）

在 main `efc3698` 的独立归档上修改四个运行文件：完整 title 用 TEXT，SHA-256 与 source 构成唯一键；查询与原子 upsert 同时核对原文。Metadata repository 和批量分析 SSE 共用读写规则；ORM 插入/标题修改更新摘要，旧 generation 删除增加观测版本条件，避免同 id 新版本被无条件删除。未新增依赖。

- c：原 a 的同一组双库长标题用例 **12 passed、无跳过，14.98 秒，实际退出 0**。
- d：新增探针有 6 个异步 fixture 装配错误；当时既有 repository/资源 API 回归仍运行（session 54261）；其终态见下方 d–i。原探针保留。
- e：修正装配后的双库对抗探针 **6 passed、无跳过，8.05 秒，实际退出 0**，覆盖人工强制摘要碰撞不命中/不覆盖、source 隔离、长公共前缀尾部区分、strip 语义、同会话 upsert 身份/创建时间保留、ORM 改名重建键及旧 generation 清除。碰撞和长文本均明确为合成边界。

[证据与四文件 SHA](probes/metadata-cache-title-c-e-result.json)附原型归档、日志/JUnit 和原/修正探针。当时专用项目 `rssripple-v35-cache-c`（32912）仍供 d 使用；最终清理见下方 d–i。候选路径 `/tmp/rssripple-v35-cache-uyusqqzh`。

**旧库迁移尚未实现，当前原型不能用于升级或合入**。并发写、陈旧读竞争、完整 MetadataAgent 调用方、双库迁移与全部门禁仍待完成，不关闭 TODO。V33 u/v 门禁及 V34 候选不受本探针影响。

## d–i 回归与真实并发补验

- d 已终止：157 passed、8 skipped、6 errors，329.64 秒，实际退出 1。6 个错误均为新增探针异步装配（已在 e 修正并通过），8 个跳过均为已退役的 resource-scoped metadata 端点。不可把本轮记为全绿。
- f：2 passed、1 failed，3.63 秒，实际退出 1。PostgreSQL 同键并发及真实双会话旧 generation 读后刷新均通过；Turso 原始双事务提交触发 `Write-write conflict`。原子 upsert 不能取代事务层 MVCC 重试。
- 核验调用方：抓取 metadata 的 `_process_resource_metadata` 已在新会话完整事务边界使用 `retry_on_lock`；批量分析后台缓存 writer 原先没有重试。本原型为 `_store_batch_analysis` 加入现有重试机制，每次新会话，失败先 rollback；不在 repository 局部重放调用方事务。
- g：实际批量 writer 双库并发 **2 passed，3.58 秒，退出 0**。PG 两会话直接成功；Turso 真实提交冲突一次，第三个新会话成功，最终唯一行保留完整结果。
- h：修改后重新执行双库边界/碰撞/实际 writer 及选中的批量分析 API/SSE，**29 passed、118 deselected，42.56 秒，退出 0**。另 i 补跑选择未覆盖的缓存 helper，**2 passed，4.48 秒，退出 0**。这是专项验收，不是全套覆盖率门禁。

[原始日志、JUnit、实际退出和后继原型 SHA](probes/metadata-cache-title-d-i-result.json)已归档。所有本批测试均终止；`rssripple-v35-cache-c` 已 down -v 退出 0，容器/网络/卷标签为空。旧库迁移仍未实施，原型未合入。下一步保留完整数据实现双库有界迁移，补全实际 MetadataAgent 调用链与竞争验证。

## j–l 旧缓存迁移原型

必要性仍为已证实的 P2 边界，非生产长标题损坏。新键上线必须保存旧行，不能只修改新表 ORM。本轮添加独立 `metadata_cache_schema` helper，尚未接入启动流程：PG 独占表锁内添加摘要、每批 100 行回填、移除旧原文索引/唯一键并建立新键；Turso 在普通 DDL 事务中重建，按相同边界逐批复制完整旧行。校验受管列、唯一键、索引、引用和 trigger，遇到异常拒绝继续；这些拒绝分支仍需专门反例覆盖，不能只凭代码宣称安全。

旧表 DDL 由已验收 main 模型编译并归档，使用固定 SHA 录制标题加明确合成历史标记，237 条行覆盖多个批次和 generation。逐字段独立读回比较 id/title/source/content_type/payload/generation/created_at/updated_at，摘要另按标准 SHA-256 验证。故障在第一批已执行后抛出，必须完整回滚并可重试；正常升级再运行一次。

- j：4 failed，6.59 秒，实际退出 1。PG 升级后旧连接 prepared statement 失效；Turso inspector 返回 VARCHAR 不带长度，后置校验误拒绝。
- k：停写升级后 dispose 旧连接，PG 两项通过；Turso 两项仍失败（2 passed/2 failed，5.78 秒，退出 1）。这不提供在线无中断升级保证。
- l：Turso 通过 sqlite_master 原始 DDL 核对摘要列 VARCHAR(64)，增加旧字段类型/空值及重建默认值检查，**4 passed，6.09 秒，实际退出 0**。每次实际回填批次为 100/100/37，回滚场景首批后失败，历史值完整保留。最终 Ruff 退出 0；初始 E501 保留记录，l 后仅折行修正。

[原始失败/通过日志、JUnit、旧 DDL、探针与五文件原型 SHA](probes/metadata-cache-title-j-l-result.json)。专用项目 `rssripple-v35-upgrade-j`（PG 32914）已清理，容器/网络/卷标签为空。V34 冻结候选未改动。

尚待异常 schema/自定义索引与引用/trigger、摘要冲突的迁移回滚反例、实际启动接入与并发、完整 MetadataAgent 调用方以及正式重基双门禁。独立迁移函数通过四项测试不等于升级流程已经完成，仍不关闭 TODO。

## m–o 迁移拒绝边界与实际启动

本轮继续 P2 完整数据保护要求，不将保存普通旧表等同于自定义 schema 安全。每库 237 条历史行，新增自定义索引、入向 FK、强制摘要碰撞、自定义标题默认值反例。拒绝时独立读取历史全字段及完整目录，必须与迁移前一致；Turso 不得残留临时表。

m：**6 passed、2 failed，11.47 秒，实际退出 1**。索引、引用和碰撞回滚均通过；两库自定义 title 默认值未被拒绝，Turso 重建会丢弃默认值。为 title/hash 加默认值与 computed/identity 检查后，n 同一反例与既有四项迁移组合 **12 passed，16.40 秒，退出 0**。不隐藏这次实际缺口。

随后将 helper 接入候选真实 create_tables：PG 沿用 startup advisory/DDL 事务，在 UTC 默认升级后执行；Turso 在轻迁移后显式普通事务内升级缓存，再执行资源 FK 守卫。o 实际启动双库正常/首批故障四项 **4 passed，8.07 秒，退出 0**，重复启动及错误解除后重试均保留 237 行原字段，升级后实际 repository 可以写入/读回超过旧 512 限制的标题。新库启动仍须独立测试，不能由旧库通过推断。启动代码初始 Ruff I001 已在 o 后仅调整导入排序，最终 Ruff 退出 0。

权威模型、业务、迁移文档已在独立候选同步。原失败与修正后的日志/JUnit、探针及九文件原型见 [m–o](probes/metadata-cache-title-m-o-result.json)。项目 `rssripple-v35-reject-m`（PG 32915）清理退出 0，容器/网络/卷标签为空。

剩余：新库实际启动、trigger/类型/其他异常目录边界、实际并发启动、完整 MetadataAgent 调用方、正式重基及完整门禁。V34 冻结双门禁继续运行，未修改其输入；V35 原型未合入。

## p–v 新库、调用链与正式集成测试

继续复核：P2 长标题与升级完整性需求不变；本轮补此前缺失的真实新库和调用方证据，不把模拟来源输出描述为在线模型验收。

- p：空库实际 create_tables 与重复启动，双库真实 UnifiedMetadataAgent.process 对 1024 字符中文标题走确定性 not_found miss→write→hit、force refresh、瞬态错误不覆盖旧确定结果、wikipedia/tmdb 命名空间隔离，**2 passed，7.07 秒，退出 0**。只有来源/LLM 返回边界合成，缓存/资源更新/事务提交真实；此轮未覆盖匹配成功后的作品 upsert，不外推成功身份链。
- q：自定义 trigger、额外列和 JSON 类型漂移加入已有迁移拒绝矩阵，**14 passed，19.09 秒，退出 0**；拒绝后历史全字段、目录及临时表检查保持不变。
- r：两个真实 PG create_tables 被第三连接的同一 advisory 锁阻塞，pg_stat_activity 观测到两个等待者后放行；摘要列只添加一次，历史完整，**1 passed，1.40 秒，退出 0**。Turso 保持单进程文件独占，不声称多进程并发启动可用。
- s：整理全部有效探针到正式 `tests/integration/metadata_cache/`（旧 DDL 成为 SQL fixture，移除临时路径依赖），统一 **47 passed、0 skipped，63.30 秒，退出 0**。旧未加事务重试的 Turso 原始并发红测保留在历史证据；实际批量 writer 的真实冲突恢复属于正式测试。
- t：旧 PG 分支替身不支持新迁移 helper 的 scalar/目录接口，单测 **1 failed，0.14 秒，退出 1**。分支 walker 改为显式断言 helper 被 await，真实迁移语义由上述数据库测试承担。
- u：修正后完整数据库迁移单测文件，专用 PG 显式启用，**50 passed、0 skipped、2 既有 warnings，55.63 秒，退出 0**。没有以关闭生产 schema 校验修复替身。

新测试及权威模型/业务/迁移/测试清单纳入候选，全仓 Ruff 无缓存退出 0。独立从基线 efc3698 全部 Git 非计划输入及新增 Python/SQL 推导变更，现为 **24 文件**；[来源与可恢复归档](probes/metadata-cache-title-v-source.json)、[实际结果](probes/metadata-cache-title-p-v-result.json)。项目 `rssripple-v35-caller-p`（PG 32916）已 down，容器/网络/卷标签为空，全部本批测试终止。

尚未正式重基到已验收 V33/后继 V34，完整 95%/85% 门禁和最终 schema/性能/安全审查未完成。匹配成功的身份 upsert 调用链仍可补强；拒绝测试仅证明已列边界，不声称穷尽所有自定义 DDL。V35 尚未合入，V34 两个原会话仍在运行。

## w–z 成功匹配、schema 复查及后继兼容性

必要性仍为 P2 缓存合法边界与升级完整性；上一轮 not_found 链不足以证明成功作品链接，本轮补真实 process→cache→Movie upsert，录制电影标题配明确合成来源身份。通过观测实际 `_get_cache` 返回验证首次 miss/第二资源 hit，来源边界仅调用一次；独立读回同一个 Movie、完整缓存标题及两个资源链接，海报仅允许 None，不访问网络。

w：**2 passed、2 failed、14 deselected，5.92 秒，退出 1**。成功匹配双库通过；新增自定义 title collation 在两库都未被拒绝，Turso 重建会丢失旧比较语义。修正为 PG 读取真实 information_schema collation、Turso 检查原始 DDL，并统一拒绝生成列后，x 全部正式缓存集成 **51 passed、0 skipped，66.10 秒，退出 0**。原失败源码/日志保留；候选权威设计与测试清单同步，全仓 Ruff 通过。

[y 来源](probes/metadata-cache-title-y-source.json)相对 efc3698 为 **25 文件**。随后从 main aad9b93 加冻结 V34 ah 构建兼容副本 `/tmp/rssripple-v35-with-v34-2ectsrx2`，逐项校验 V34 父哈希，再三方应用 V35。唯一运行代码冲突为 database.py 迁移插入点：PG 缓存迁移留在 UTC/default 分支内，之后继续共享搜索列检查；保留 V34 critical token 列及 Turso 普通事务路径。文档保留双方独立契约。

从全部 Git 非计划输入与新 Python/SQL 独立推导 **3362 文件**：V35 相对 V34 **25 文件**，相对当前主干合计 **48 文件**，历史探针导入维护单列。兼容候选 Ruff 无缓存退出 0。[z 来源与完整归档](probes/metadata-cache-title-z-source.json)。

z 联合执行缓存、搜索、UTC、AgentRun 旧库升级及完整迁移单测，专用项目 `rssripple-v35-review-w`（PG 32917），会话 **37219**，日志 `/tmp/rssripple-v35-z-compat.log`，尚在运行，项目暂保留。必须续接实际结果，不能把合并/lint 当兼容测试通过。[w–z 结果](probes/metadata-cache-title-w-z-result.json)。V34 原完整双门禁仍运行，冻结候选未改；V35 未合入，后续还需正式主干重基、全输入冻结、完整 95%/85% 门禁与终审。

## z 联合回归终态

原会话 37219 已实际退出 0：**344 passed、0 skipped、2 既有 warnings，394.59 秒**，覆盖缓存、搜索、UTC、AgentRun 旧库迁移和完整数据库迁移单测。JUnit/原始日志已归档，warnings 仍为旧 event_loop_policy 弃用与 begin_nested 替身未等待；本轮无新增失败或跳过。

全部 **3362 非计划输入**与来源中组合变更/基线哈希一致，V34 的 **6785 冻结输入**另行逐项核验未改动。专用项目 rssripple-v35-review-w 已 down -v 退出 0，容器/网络/卷标签为空，z 无后台任务。更新的 [w–z 结果](probes/metadata-cache-title-w-z-result.json)保存实际退出和清理。此为兼容专项，仍不替代 V35 正式重基后的完整双门禁。等待 V34 验收后，从其最终主干构建正式候选。

基于联合候选的五轴预审见 [V35-REVIEW](V35-REVIEW.md)：已核对完整键、原子更新、迁移保留与回滚边界，并明确目录检查/维护窗口成本。正式完整双门禁尚未执行，因此预审不构成合入批准；等 V34 最终主干验收后再冻结后继候选。

V34 已完成完整双门禁并合入 main `2affb9a`（有效 28 文件＋单列探针维护）。下一步可直接基于该已验收主干，按 z 相对来源的 25 文件重建正式 V35 候选，验证全部非计划输入等同已通过 344 项的兼容副本，冻结后启动完整双门禁。

## aa 正式重基与 ab/ac 完整门禁

2026-10-08 再次论证：V34 已验收，V35 的合法长标题/原文 btree 边界仍有真实红测，采用完整文本＋定长键及保留历史的迁移方案，继续 P2，不声称生产事故。由已验收 main **83a72c2** 的 Git archive 构建 `/tmp/rssripple-v35-rebased-jhysx9no`，逐项验证 z 的父哈希后应用 **25 文件**。全部 **3362 非计划输入**与已通过 344 项联合回归的副本完全一致；Ruff 无缓存退出 0。不重复相同专项，进入正式全量门禁。

冻结 **6871 文件**（[aa 来源](probes/metadata-cache-title-aa-source.json)、`metadata-cache-title-aa-frozen.json` 和候选归档）后启动：

- ab 完整单元/API，≥95% 门禁，会话 **18465**，专用项目 `rssripple-v35-unit-ab` / PG **32918**；日志 `/tmp/rssripple-v35-ab-unit.log`。
- ac 完整隔离集成，会话 **74751**，专用项目 `rssripple-v35-final-ac`；启动会话 **69082** 实际退出 0，两应用健康，固定镜像身份与 V34 相同。日志 `/tmp/rssripple-v35-ac-integration.log`。首次启动请求自动审批超时而未执行，允许的一次重试成功；不是测试失败或不安全判定。

[门禁会话记录](probes/metadata-cache-title-aa-ab-ac-result.json)。当前尚无最终结果，不关闭 TODO。续接原句柄；结束后须正常停止应用写出覆盖率、组合 ≥85% 门禁、导出 5 份原始覆盖率/4 份语料报告，核对 V34 ai/aj 跳过基线、冻结哈希和项目清理，完成五轴终审及真实 Git 内容核验。未修改 main 运行代码，未推送或部署。
