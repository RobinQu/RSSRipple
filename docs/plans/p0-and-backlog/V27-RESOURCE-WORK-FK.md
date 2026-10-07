# V27 资源工作 FK 互斥约束（P2，未验收）

## 必要性与范围

V25 最新完整门禁 au/av 仍在运行，V26 已有海报专项证据但待重基。本批在独立副本重新验证原 P0-5：三个平面工作 FK `series_id/movie_id/audio_work_id` 至多一个非空。`collection_id` 不属于互斥集合；合集与单季关联的合法共存不能被破坏。franchise 的业务形态规则仍由原服务维护，不扩展本次数据库约束。

既有 `prod_works_v1.json` 于 2026-09-04 导出，SHA-256 `d11651d2162ced23e8d919af0bff2d9f316e203234cc854909ba5f444a35ec32`。559 条资源的三个字段均完整，违规 0，工作/collection 共存 1。这只是录制子集，不能代表当前生产。真实资源 `bac4892f-dd3b-41b2-b648-ff20a589779b` 的 ID/GUID/标题/torrent URL 与 series/collection 身份用于回放，频道、音频父记录及冲突组合显式合成，不请求外部网络。

原代码双库直接 SQLAlchemy Core INSERT/UPDATE 全组合测试 a：**32 failed、32 passed，62.30 秒，退出 1**。全部非法双/三工作 FK 写入提交成功，期望的 DB 拒绝没有发生；空关联、单工作关联及可选 collection 共存对照均通过。证明数据库约束缺口，未证明生产已有脏数据，因此保持 P2。原始报告及精确红测源见 `probes/resource-work-fk-a-*`。

## 方案论证

- 新库使用命名 CHECK `ck_file_resources_work_fk`；表达式只计算三个工作 FK 的非空数量 ≤1，NULL 全空合法。
- PostgreSQL 旧库通过命名 CHECK 的幂等安装与验证升级；同名约束漂移必须识别，不能仅凭名字当作正确。现存违规必须报告并阻止宣称升级成功，不自动决定删除哪个身份。
- Turso/SQLite 旧库优先使用 INSERT/UPDATE 拒绝触发器，避免为单一约束重建大表；先验证当前 Turso 对新库 CHECK 的实际支持，不能假设与 SQLite 完全一致。
- 必须在旧库列补齐后安装保护；后续父表重建不能丢失约束。保持正常清空 FK、切换工作类型以及 ON DELETE SET NULL 行为。
- 三列互斥是数据库状态约束；应用真实写入若分两次产生中间非法组合，应修正为一次原子更新，不能让数据库约束容忍非法提交。

## 严格验收

1. PostgreSQL/Turso 两后端：8 个 FK 空/非空组合 × collection 有/无 × INSERT/UPDATE，全部预期正确；失败更新原值不变，失败插入无残留。
2. 新库及真实旧表升级、重复运行、缺少历史列、同名约束/触发器漂移、已有非法行、安装中途失败回滚，均需正式集成证据；违规历史不得自动删除或猜测身份。
3. 录制合法共存资源、直接 SQL、真实 associations/metadata/rehome 写入、清空与跨类型切换、父工作删除以及迁移表重建回归。
4. 合并顺序为 V25、V26 后重基；当前候选只基于本地 main `ef3c2d8`，不改动前两批。权威数据模型、迁移和测试清单必须同步。
5. 最终五维审查；冻结后完整单元/API ≥95%、完整唯一 Compose 集成 ≥85%，零失败，审计全部跳过、哈希、真实进程退出和资源清理，才可合入本地 main。

候选 `/tmp/rssripple-v27-work-fk-j9q729vt`。新库命名 CHECK 原型 b 已通过全部 **64 项，51.68 秒，退出 0**，证明当前 Turso 实际执行该 CHECK；尚未实现旧库升级，不可关闭 TODO。六文件候选及双方哈希见 `probes/resource-work-fk-b-result.json`，正式测试与权威文档修改仅在候选内。专项无缓存 Ruff 通过，本轮唯一 PG 项目已完整清理；无 V27 测试进程继续运行。阶段五维审查见 [V27-REVIEW.md](V27-REVIEW.md)。


## 旧库升级验证 c–e（2026-10-07，未验收）

c 在真实旧表上两次运行轻量迁移后，两个后端仍允许非法双工作 FK 提交：2 failed、2.56 秒，退出 1。d 加入保护后 PostgreSQL 通过，Turso 以 “DDL statements require an exclusive transaction” 失败：1 failed、1 passed、2.57 秒，退出 1。原因是用于启动物理事务的零行 UPDATE 开启了 CONCURRENT，不能用于 DDL；失败源和日志已保留。

方案调整：PostgreSQL 在现有启动 advisory lock/事务中先锁表、审阅历史行和已有 CHECK 定义，再安装并验证 CHECK。Turso 在轻量列补齐后，单独拥有显式普通 BEGIN 的启动事务，安装两个拒绝触发器，再进入既有 FK 修复阶段；不提交调用方已有业务事务。历史违规和同名保护漂移必须使启动失败，不自动清理关联。新库 CHECK 与旧库触发器共用模型中的三字段表达式。

e 实际升级入口连续两次执行后，双库拒绝非法写入：2 passed、2.61 秒、退出 0。这只证明最小升级路径。扩大 f 正在验证新旧库全组合、历史违规、定义漂移及第二条保护 DDL 失败后的原子回滚；这些结果尚不能提前计为通过。缺列历史形态、真实服务写入和最终完整门禁仍待完成。当前专用 PG 项目为 `rssripple-v27-legacy-c`，完整清理需等本阶段结束。


f 扩大验证已结束：**134 passed、0 skipped，153.62 秒，退出 0**。新库与实际升级后的 128 项全组合，加双库历史违规/定义漂移/第二段 DDL 失败回滚六项均通过；失败后历史资源逐列不变，已完成的第一段 DDL 随事务回滚，移除故障后可再次升级。

g 缺失 audio_work_id 的真实旧表验证 **2 passed、3.07 秒、退出 0**：补列后保留录制资源身份、collection 共存与原关联；Turso 随后 FK 表重建保留拒绝触发器，合法一次 UPDATE 切换到音频、父记录删除 SET NULL、非法双 FK 拒绝均通过。h 真实 associations/metadata/rehome 服务在双库新表与升级表中 **12 passed、15.59 秒、退出 0**；作品身份来自固定哈希录制集，编辑、metadata 匹配结果和错误类型历史显式合成，不宣称复刻生产误分类事件。

i 扩大回归正在运行：新旧库及服务专项与既有轻量迁移、FK 升级测试合跑。候选已避免原生 CHECK 存在时重复安装触发器，并明确迁移只保证新保护安装事务的原子性，不声称此前轻量迁移全部回滚。须检查 i 原始退出、全部失败、源码与项目清理后归档；仍未全量验收或合入。


i 已结束：**1 failed、225 passed、277.43 秒，退出 1**。唯一失败来自旧 PostgreSQL SQL 记录器的 SimpleNamespace 假连接不能执行 SQLAlchemy inspect；不代表真实 PostgreSQL 升级失败。仅在该分支遍历测试中替换保护调用并断言准确委派，真实保护安装/冲突/回滚仍由上述双库正式集成覆盖。j 整个迁移文件 **50 passed、0 skipped、51.43 秒，退出 0**，包含三项真实 PostgreSQL 迁移；运行实现未因该失败改动。

最新 11 文件候选、主干/候选哈希与补丁见 `probes/resource-work-fk-j-*`。无缓存全仓 Ruff 通过。`rssripple-v27-legacy-c` 清理退出 0，容器/网络/卷标签均空，无 V27 进程继续运行。i 的失败原样保留，j 仅消除了该测试替身缺口，不把两轮专项包装成完整门禁。下一步补原生 CHECK 漂移、PG NOT VALID 约束的历史验证，并在 V25/V26 接受后重基父键屏障实现；仍不批准合入。


## 原生约束目录与并发边界 k–n（2026-10-07）

本轮重新确认：同名保护不代表定义正确，PG NOT VALID 也不证明历史已验证；因而不能只查询名字后跳过迁移。k 六项通过，l 补 Turso 旧物理事务后 **7 passed、7.64 秒、退出 0**。覆盖两库错误 CHECK 阈值、Turso 保留同样非括号 token 却改变语义的错误 CHECK、PG NOT VALID 的合法/违规历史和真实 convalidated 标志，以及 PostgreSQL 表锁关闭扫描到提交之间的写入窗口。错误定义和历史数据保持不变，不自动清理以让测试通过。

Turso 使用零行 UPDATE 建立真实 CONCURRENT 事务并读取旧快照；新触发器在另一连接普通事务中实际提交后，旧写入被 **Database schema conflict** 拒绝，新事务非法双 FK 被命名保护拒绝。m 将真实分支/错误写入 JUnit 属性；n 收紧断言，排除无关数据库错误假通过：**1 passed、1 warning、1.84 秒、退出 0**。该 warning 来自 pytest 的 record_property/xunit2 兼容提示，属性实际已保留；不扩大为运行服务在迁移期间无错误的保证，正式迁移仍应停写。

本轮未修改运行实现。最新十二文件候选、哈希、补丁与报告见 `probes/resource-work-fk-n-*`。无缓存全仓 Ruff 通过，专用项目 `rssripple-v27-catalog-k` 已清理，容器/网络/卷标签均空。原生定义与 NOT VALID 阶段缺口已补齐；下一步在 V25/V26 接受后重基，复核共同修改的父键屏障/表重建，再进行完整双门禁及最终审查。V27 仍未验收合入。


## V25/V26 上的迁移组合验证 o（2026-10-07）

必要性：V25 扩展作品父行版本屏障，并在 FK 表重建前后移除/恢复相关触发器；V27 旧表的工作互斥保护也依赖触发器。各自专项通过不能证明两类保护在共同升级后仍同时生效。因此组合采用已冻结 V25 bm 与 V26 l 的独立副本，再叠加 V27 n；数据模型文档通过三方合并，迁移文档/测试清单均为相同基线上的追加段，保留两方内容，无运行代码冲突。

候选 `/tmp/rssripple-v27-combined-s3q1i1iz`，完整组合相对 main 为 67 文件，源码及哈希见 `probes/resource-work-fk-o-source.json`、`o-candidate.tar.gz`。新增 `test_parent_guard_interaction.py` 两项：真实缺列旧表启动升级两次并重建、验证工作互斥保护仍拒绝非法组合，再调用真实并发合并及直接/向导关联写入，最终检查不得留下悬空引用。保留原 559 条录制预检及全组合/迁移/服务/目录测试，不把合成并发历史描述成生产事故。

无缓存 Ruff 通过，o 全部双库工作 FK 测试及作品屏障升级测试正在执行（句柄 77691，独立项目 `rssripple-v27-combination-o`，端口 32868）。运行入口与清理责任见 `probes/resource-work-fk-o-running.json`。这只是父候选尚未接受前的兼容性验证；仍需父批验收、正式重基、五维终审及自身完整双门禁，不能关闭原 P0-5/P2。


o 组合验证已完成：**159 passed、0 skipped、2 warnings，252.21 秒，退出 0**。新增两项表重建后实际并发关联保护均通过，原双库全组合/升级/故障回滚/目录语义/真实服务路径及已有父行屏障升级测试一起通过。67 个组合变更文件哈希未变；专用项目清理退出 0，容器/网络/卷标签均为空。日志、JUnit 与清理审计见 `probes/resource-work-fk-o-result.json`；组合源码已存档。

组合审查：两类触发器名称/安装职责独立，FK 重建在同一事务内恢复父行屏障及工作互斥保护，现已用实际并发而非仅目录名称验证；未新增依赖或网络权限，历史违规仍不自动改写。启动扫描/表锁的生产规模成本未量化，继续保留迁移停写约束。V25、V26 运行修复仍未接受，o 不是最终完整门禁；后续须在父提交上正式重基并检查差异，完成全量单元/API 和完整集成后才可关闭。


## p/q 正式重基与预检

V26 已通过 q/r 完整门禁并合入 `7da83a2`，V25 漏掉的离线脚本亦已在此前 `cf1c785` 补齐。本轮再确认必要性：工作 FK 至多一非空仍是权威约束，已有录制子集无违规不代表数据库具有拒绝能力；直接 SQL 红测及双库迁移写入保护证据仍有效，保留 P2，collection 不参与互斥。

原 n 候选相对其 Git 基线独立推导恰为 12 文件，无遗漏；在已验收 main 上正式叠加，并加入预组合 o 的表重建后并发父行保护测试，形成 **13 文件**候选 `/tmp/rssripple-v27-rebased-q3o9mg4x`。三份文档的新增内容经三方合并保留；运行代码无冲突。相对当前主干的完整非计划范围也恰为这 13 文件。源及可恢复 tar 见 [p-source](probes/resource-work-fk-p-source.json)，范围见 [p-scope](probes/resource-work-fk-p-scope.json)。

无缓存全仓 Ruff 退出 0。唯一 PostgreSQL 项目 `rssripple-v27-preflight-q` 已健康启动；全部 resource_work_fk 正式集成与完整数据库迁移单元文件预检正在运行，句柄及报告入口见 [q](probes/resource-work-fk-q-running.json)。这不是完整双门禁；预检终态后核对范围/清理并冻结，再运行单元/API ≥95% 和集成 ≥85%，零失败及全部审计后才允许合入。


## q 预检终态与 r/s 完整门禁启动

q 已正常退出 0：**207 passed、0 failed、0 skipped、3 warnings，135.49 秒**。其中 resource_work_fk 正式集成 157 项、数据库迁移单元 50 项。历史 o 的 159 项另含两个 work_parent_guard_upgrade 单元测试，数量差异不是丢失测试；该文件由本轮完整单元门禁收录。专用预检项目 down 退出 0，容器/卷/网络标签均空。原始日志、JUnit、清理日志和审计见 [q-result](probes/resource-work-fk-q-result.json)。

候选 13 个有效文件哈希未变，冻结 **6,183 个输入文件**，见 [p-frozen](probes/resource-work-fk-p-frozen.json)。r 完整单元/API（≥95%）与 s 完整隔离集成（≥85%）已经启动；专用 PostgreSQL 和集成项目均健康，实际句柄、唯一项目名及报告路径见 [r-s-running](probes/resource-work-fk-r-s-running.json)。预检完成不等于完整验收；门禁终态后还须逐项核验 skip、完整输入范围、应用正常退出、原始覆盖率导出及资源清理。V27 未合入，TODO 保留。
