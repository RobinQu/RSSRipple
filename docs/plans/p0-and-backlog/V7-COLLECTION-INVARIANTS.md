# V7：剧集合集归属（必要性复核，未实施）

## 问题与优先级

2026-09-20 在无业务配置的独立副本 `/tmp/rssripple-v7-collection-work` 调用实际 API、读取实际 Turso 提交结果。修正初版测试的必填字段、work_type 和 attach=201 契约后，最终三项均在 `member.collection_id is not None` 断言失败：手工创建 TVSeries、删除成员所在合集、从合集解绑 TVSeries。**3 failed，4.32 秒**，`/tmp/rssripple-v7-collection-all-red.xml`。三条请求本身成功，读取到的空合集身份是被测行为。前面请求契约错误的失败不计入缺陷证据。

[复现补丁](probes/collection-invariants-reproduction.patch) 仅新增测试，未应用主工作树。标题与行均为合成数据；执行的是生产路由和真实数据库。此证据不代表生产当前已有多少孤儿。保持原 P0-6 的 P1 分级；新发现的单成员解绑须纳入同一修复，不能只修 DELETE /collections。

## 方案边界与待论证问题

1. 创建季作品必须在同一事务内建立或明确选定壳合集；复用既有 `_create_series_collection` 的标题/别名构造，不靠同名盲目合并不同作品。
2. 删除合集和解绑剧集必须重挂壳合集，电影仍可为空；API 失败应整体回滚。解绑最后一个成员、跨季成员、同季冲突、已有空壳及重复请求语义都要明确。
3. 资源的 collection_id 与 resource_work_links/文件指派必须保持一致；多作品资源无法唯一归属时不能任选一个新合集。删除旧合集的身份袋清理与 P1-D6 相邻，需论证纳入本批的最小完整范围。
4. 启动回填只处理孤儿且幂等；已有多 worker 启动竞争需双库交错事务验证。历史快照零行不代表无需升级路径。暂不把 DB NOT NULL 作为修复前提。
5. P1-D1 唯一索引依赖存量收敛：当前 database.py 明确留给 season_split_migration，不能直接创建索引令有重复数据的升级启动失败。与归属补修的先后关系须再评估。

## 严格集成标准

修复前保留以上红测；补创建/删除/解绑的正反例及合法电影行为。覆盖真实 API→commit→重新读取、资源与身份袋一致性、故障回滚、回填二次执行和并发启动；Turso 与隔离 PostgreSQL 均验证。涉及旧数据时使用明确标注的历史形状夹具，尽可能复用审核语料中的作品/资源结构。任何 Compose 必须唯一项目名。最后同步权威设计/API文档并通过完整单元/API 95%、隔离集成 85% 门禁才删除 TODO。

V6 的 ai 完整门禁正在运行；本批仅论证/红测，不修改其冻结源码，也不抢先宣称修复完成。


## 关联复现与创建原型

扩大真实 API/Turso 复现 **5 failed，3.20 秒**（`/tmp/rssripple-v7-association-red.xml`）：前三项孤儿断言继续失败；删除无资源合集后，原 `WorkExternalId(work_type=collection)` 仍在；删除带资源合集在 DELETE SQL 处抛出 `immediate foreign key constraint failed`。因此 P1-D6 的合集身份袋清理和资源指针处理是本批完整修复的一部分，不能只修改 TVSeries.collection_id。未据此关闭 D6 的 series/movie 删除路径。

创建子路径已在独立副本复用 `_create_series_collection`，与 TVSeries 同事务建立壳合集；单项不变量＋既有完整剧集 API **19 passed，9.29 秒**（`/tmp/rssripple-v7-create-green.xml`）。[原型补丁](probes/collection-invariants-prototype.patch) 目前只修创建，含五项回归测试，整体仍有已知红测；不可将其直接当作本批已完成版本。下一步实现删除/解绑的原子重挂、资源关联重算和合集身份袋清理，再补回填与双库并发/回滚。V6 主工作树继续冻结。


## 生命周期原型与双库专项

原型现已实现删除/解绑时重挂壳合集，按受影响资源分批读取作品 FK、work_links 和文件指派，只有全部已指派作品可归到唯一合集时才重设资源 collection_id；多新合集资源清该指针并保留人工 links/assignments。删除清除旧合集身份袋及 collection-only 资源指针，不创建猜测的作品/季。调用方控制 commit/rollback。

首五项 **5 passed，6.57 秒**。补多作品不猜合集及清理失败回滚后，扩大 API **47 passed、2 failed，64.68 秒**；两个失败是既有用例要求解绑后 collection_id=None。改为断言有效的新合集，并让多季夹具明确设置季号 1/2。孤儿回填每批最多 100 条、PG FOR UPDATE、Turso 使用现有 retry_on_lock 在新事务重试，接入两个后端 create_tables 路径。创建/删除/解绑/回填及完整相关 API 最终 **50 passed，66.71 秒**（`/tmp/rssripple-v7-api-backfill.xml`）。

[PostgreSQL 驱动](probes/collection_pg_probe.py) 对显式 loopback organize_test 临时库先清空再建表，禁止用于业务库；[结果](probes/collection-pg-result.json) 验证第一事务持有行锁时第二个 SELECT FOR UPDATE 已发起，两个回填分别为 3/0、重跑为 0，无额外壳合集，资源和季号一致；另验证真实删除后的重挂及身份袋清理。项目 rssripple-v7-collection-20260920-aj 已清理。全部数据为合成 ORM 行，事务/数据库实现真实。

原型仍未同步主工作树。启动迁移/合集服务扩大回归日志 `/tmp/rssripple-v7-startup-expanded.log` 尚待结果；API 同时删除/解绑/新增成员的竞争、更多电影及跨页资源边界、权威文档和最终完整门禁仍待完成。不得据目前专项关闭 TODO。


## 启动接线与预加载 ORM 边界

首次启动扩大回归 **70 passed、1 failed、3 skipped、2 warnings，54.35 秒**，失败为 PostgreSQL 分支单测的 `_Session` 替身没有 scalars，不能执行新增孤儿查询。保留该测试的隔离边界，新增回填调用及顺序断言；同时增强真实 Turso create_tables 用例，先植入 S3 孤儿，再运行两次启动并断言同一壳合集、季号保留。复跑 **71 passed、3 skipped、2 warnings，55.08 秒**，`/tmp/rssripple-v7-startup-green.xml`，退出 0。

复审 ORM 关系发现：预先 selectinload 旧合集的成员后，仅修改 collection_id 再删除父对象，会被 SQLAlchemy 的关系同步再次置空。真实 Turso 红测 **1 failed，1.07 秒**，`/tmp/rssripple-v7-preloaded-red.xml`。改为 `series.collection = shell` 并 flush，确保关系双向状态与 FK 一起更新；相关 API 扩大回归 **51 passed，65.01 秒**，`/tmp/rssripple-v7-preloaded-green.xml`。不能用此前 aj PostgreSQL 结果代替该改动后的复验。

当前原型包含 7 个实现/测试文件，未应用主工作树。还需 PostgreSQL 预加载关系复验、API 同时删除/解绑/新增成员的事务所有权与锁顺序测试、更多电影/大资源集边界、权威文档及最终完整门禁。尤其当前 API 在读取旧合集时未取行锁，不得直接宣称并发删除已安全。


## PostgreSQL 预加载与重复删除

ak PostgreSQL 在显式 selectinload 合集成员后复验，确认 ORM 关系修正有效，同时并发回填 3/0、资源/季号一致、删除后身份袋清理仍通过。另用 [实际删除端点事务探针](probes/collection_delete_pg_probe.py) 控制第一请求读到成员后暂停：旧原型两个请求均 200，留下两个壳合集，且无数据库阻塞。已复现读后删除竞争导致多余无主壳合集，不把 HTTP 200 当作一致性证明。

原型在修改/删除/attach/detach 端点对目标合集使用 FOR UPDATE，并在 attach/detach 读取作品时取行锁；GET 不加锁。相同 PostgreSQL 探针修正后 **[200,404]**，pg_blocking_pids 证实第二请求被阻塞，最终恰好一个有效壳合集；[结果](probes/collection-delete-pg-result.json)。加锁后 API 不变量 **9 passed，3.83 秒**，`/tmp/rssripple-v7-lock-api.xml`。ak 项目已清理。

仍需其他 attach/detach 交错、锁顺序和跨父合集移动边界，不能把重复删除结果外推为所有并发已解决。主工作树无 V7 运行代码；独立副本已继承 V6 最新两份夹具修正，避免后续再次命中相同旧测试失败。


## 跨页资源、反向移动与 an 门禁

真实 API/Turso 的 12 组跨页场景全部通过（6.03 秒，`/tmp/rssripple-v7-resource-boundaries.xml`）：每组 103 条资源，覆盖电影/剧集、删除/解绑、直接 FK/work_links/文件指派；验证人工映射、无作品依据资源和无关对照不被误改。

跨合集反向壳吸收出现新的锁顺序问题：原型先锁目标、再删除来源，两个反向请求交错导致 PostgreSQL `40P01`，结果为 201/500。已保留 [探针](probes/collection_swap_pg_probe.py)。改为按 ID 排序锁来源与目标，再锁作品并重新读取归属；若已变为未锁定第三个合集则 409 INVALID_STATE，目标已消失则 404。修正后实际 pg_blocking_pids 观测到第二请求等待，结果 201/404，只有一个合集且两个作品均保留；[结果](probes/collection-swap-pg-result.json)。am 专用测试库已清理。相关 API 扩大 **45 passed，19.42 秒**（`/tmp/rssripple-v7-lock-order-api.xml`）。

5 份权威文档已在副本准备，原型共 13 个实现/测试/文档文件，未合入主工作树。缺失的已跟踪脚本/配置已补到独立副本，未覆盖原型；除本批改动外的源码哈希与主工作树一致。完整单元/API 95% 门禁已在副本启动，日志 `/tmp/rssripple-v7-unit-an.log`，句柄 `/tmp/rssripple-v7-gates-an.json`，冻结清单 `/tmp/rssripple-v7-source-an.json`（493 源码/配置文件）。终态前不修改副本源码，不关闭 TODO；完整集成、最终复审及同步仍待完成。主工作树 V6 al 完整集成仍运行。

## V7 单元/API 终态与 ap 完整集成（2026-09-20）

an 完整单元/API **3561 passed、14 skipped、6 warnings，1465.99 秒，21261/21735（97.82%）**，退出 0；JUnit failures/errors 均 0，493 个冻结源码/配置文件哈希不变。最终复审了 API 创建/删除/解绑事务、跨页资源映射、启动批次回填、预加载关系与跨合集父锁顺序；无新增依赖或未解决的合并阻断项，全仓 Ruff、差异空白检查通过。

13 个实现/测试/权威文档文件先核对原型与当前主工作树基线，再同步到 root，尚未提交。完整集成新项目 `rssripple-v7-complete-20260920-ap`，日志 `/tmp/rssripple-v7-integration-ap.log`，状态 `/tmp/rssripple-v7-gates-ap.json`，冻结 `/tmp/rssripple-v7-source-ap.json`。保留 TODO，等待 runner 终态、应用优雅退出、覆盖率汇总/导出与清理。之前“仅独立副本”及“V6 仍运行”的记载为历史阶段；V6 已提交 7fd8121 并完成清理。

## V7 ap 终态与 au 复验（2026-09-20）

ap 完整集成 **3135 passed、1 failed、17 skipped、8 warnings，1660.44 秒，退出 1**。唯一失败是 HTTP 合集测试在剧集解绑后仍断言 collection=None，与本批“不产生孤儿”的契约冲突；其他 API 功能步骤已通过。应用 SIGINT 正常退出 0，覆盖率汇总退出 0，**19526/21735（89.84%）**；因测试失败不验收。完整证据 `/tmp/rssripple-v7-artifacts-ap`，机器摘要 probes/collection-complete-ap-result.json，ap 项目已清理。

仅修改该 HTTP 断言：返回 200，剧集属于不同于旧目标的新合集，读取新合集验证成员恰好原作品。保留电影最终可无合集、重复解绑 404、错误 work_type 422 等断言。与 ap 冻结文件相比唯一变化为 tests/integration/http/test_coverage_supplement.py；实现及单元/API 源码不变，沿用 an 的 3561 项、97.82% 门禁。新项目 `rssripple-v7-complete-20260920-au`，冻结 `/tmp/rssripple-v7-source-au.json`，后续从 `/tmp/rssripple-v7-gates-au.json` 续接。原型扩为 14 文件。

V8 at 正在运行，保持其 496 文件冻结；副本仍含上述旧 HTTP 断言，须在 at 终态后、启动其完整集成前同步这一个修正，不要在运行中覆盖。


## V7 最终验收（2026-09-20，au 轮）

完整集成 **3136 passed、17 skipped、8 warnings，1688.63 秒，退出 0**；两个应用 SIGINT 正常退出 0，覆盖率汇总退出 0，**19524/21735（89.83%）**。单元/API 沿用未变实现的 an 轮 **3561 passed、14 skipped，97.82%**。493 个冻结源码/配置文件全部哈希不变，JUnit failures/errors 均 0，证据导出 `/tmp/rssripple-v7-artifacts-au`，隔离项目容器、网络、卷已清理。机器结果见 [au 验收摘要](probes/collection-complete-au-result.json)。

按 code-review-and-quality 复审创建/删除/解绑事务、预加载 ORM 关系、资源映射分页、父锁顺序、启动幂等回填及跨进程竞争；无新增依赖。合集成员/身份袋/手工文件映射保留及 PostgreSQL 并发有真实数据库证据；测试数据为明确构造的关系数据，完整集成继续包含捕获标题和真实 torrent 清单，未宣称新增在线来源录制。

原 P0-6 的四条 API/回填待办已验收，从 pending-only TODO 删除。D6 仅合集删除部分完成，剧集/电影删除与人工映射策略继续保留；D1/D2 原型仍未验收。历史 ap 失败与运行中记录保留，不代表当前验收状态。
