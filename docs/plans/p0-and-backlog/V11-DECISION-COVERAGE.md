# V11：覆盖度与待决策唯一性（P1-D4 / M4 / M5）

## 必要性复核（2026-09-20）

当前 _batch_coverage_key 对季包仅使用作品 ID，忽略 episode_start/episode_end；两份合成 S1 包（1–6 与 1–12）得到完全相同键。实际 process_resources + Turso 运行四个候选、两组不同的关联作品集合，统计 pending_decisions=2，但数据库仅保存一行、合并全部四个候选。两项负向测试均失败（1.80 秒），不是仅凭静态代码推断；复现补丁见 probes/decision-coverage-reproduction.patch。

D4 已用两个真实 PostgreSQL 连接执行生产 create_pending_decision 并提交。LLM 调用替换为屏障，保证两个请求都完成空槽查询后才插入；同一 agent/movie/NULL 集号得到两条 pending，每条只有各自两个候选。首次两轮是夹具必填字段及 PG 时间类型错误，修正后上述竞争实际复现。普通队列按 agent key 去重不代表直接 API/回填事务不能竞争，不能将此问题归因于固定 worker 数量。

三者保持 P1 并合并设计：只给旧 nullable 列组合增加 unique 无法解决 NULL、不同覆盖度塌缩或候选丢失。此次未修改运行代码；测试数据为合成，不声称当前生产已有重复决策。

## 修复方案待验证

1. 在候选分组、跨运行下载去重、待决策持久化和确认时使用同一规范化覆盖描述。单集包含作品身份/集号；季包须区分完整已知覆盖区间；多季包须保留每个关联季作品的覆盖信息，不能只用平面 FK 或一个 -1 哨兵。先审计文件指派与已知集数的可靠性，未知覆盖继续进入 Channel 待确认，不把缺区间当整季。
2. PendingDecision 增加不含 NULL 歧义、带版本的规范化决策键；仅 pending 状态唯一。存规范化描述供审计，不仅存难以解释的摘要。决定最大键长度/摘要方式前检查数据库索引限制及真实作品覆盖规模。
3. 同槽并发必须合并候选且不丢更新。候选归一化、唯一冲突后的 SAVEPOINT 恢复、已有行锁与状态变化共同验证；不能只捕获 IntegrityError 后返回第一条。事务内部不因网络 LLM 等待延长锁占用；推荐内容依据最终候选集合更新。
4. 历史决策逐条只读审计其所有候选的真实覆盖度。混合槽位要拆分、同覆盖度重复槽要合并；候选消失、未知覆盖和已处理状态不可猜测。保留原 ID/候选/状态/人工决定的迁移报告与显式审核路径。作品去重/迁移会重指向外键，须同步决策键，避免固定旧键导致后续重复。
5. 确认/批量确认/AI 选择重新检查当前候选与覆盖度，拒绝过期或非等价集合；覆盖度变更后的历史推荐不能直接派发。此项同时检查已有下载派发幂等边界，避免把候选分槽修正误当完整并发派发保障。

## 严格验收

双库：同键并发创建只留一条且保留全部合格候选；不同键并发不互相塌缩；状态从 pending 离开后可创建新决策；候选更改/作品合并/故障回滚/重复迁移保持一致。实际 pipeline 验证半季与整季、多个 links-only 集合、顺序不同但覆盖相同、未知覆盖门禁；确认端点须防过期推荐。沿用原生产录制资源/文件清单作为可用回归来源，并明确区分合成并发数据。最后同步模型/API/业务/迁移文档，完成单元/API 95% 与隔离集成 85% 两道完整门禁。当前仅完成必要性复现，D4/M4/M5 均不关闭。

## 录制数据核查与覆盖描述原型（2026-09-20）

只读审计原 prod_works_v1（SHA256 d11651d2162ced23e8d919af0bff2d9f316e203234cc854909ba5f444a35ec32）：559 个资源中 101 个是合集，40 个缺文件指派或存在未明确绑定/集号证据。已知指派中没有发现区间缺口，缺口用例必须明确标为合成，不能声称生产已观察。3 条历史决策均为 decided、各有 1 个候选，不足以证明 pending 迁移方案。审计报告保留全部 101 个合集的作品/季/区间描述及不确定行 ID；这是迁移前快照，不是当前生产状态，也不证明指派覆盖了 torrent 的全部媒体文件。

独立 resource_coverage 叶模块原型以文件指派为优先证据，合并重叠/相邻区间但保留缺口，不展开大集数区间；每个关联作品都必须有合法指派且季号与已加载作品相等，拒绝真正未拆季作品。没有指派的单季包仅接受明确起止集号，单个边界不推断整包；多作品无指派保持 unknown。未加载关系不当作空列表，真实 Turso 持久化行验证不会触发隐式 SQL。

最初 11 passed/1 failed 暴露了瞬态对象空关系与未加载状态混淆；追加季身份/旧多季及真实持久化关系边界后最终 **16 passed、1 warning，1.58 秒**。两轮 bk 测试夹具仍错误地把显式 None/带修改历史的瞬态对象当作未加载，已改成真实持久化后的 expire 并观察 SQL。历史失败记录保留。

这是两个文件的独立原型，尚未接入 production pipeline；M4/M5 原始两项红测仍未修复。后续需统一所有调用点的 eager loading、torrent 完整性前置条件、Channel unknown 门禁以及既有下载/决策迁移，再实现持久唯一键、并发合并和确认时验证。候选不完整时不能为了通过既有测试放宽 unknown 门禁。

## 派发路径负例与接入原型（2026-09-20，bm）

扩大必要性验证实际得到 **4 failed，4.91 秒**：除了范围键和决策塌缩，生产 process_resources 在真实 Turso 中把已有半季 completed 下载当作整季重复，并派发缺少集数覆盖证据的季包。下载 RPC 用 AsyncMock，断言业务去重/派发行为，不将其称作真实下载。

独立原型现接入 _batch_coverage_key、process_resources 与严格 Channel 预检；批量按 100 个资源加载作品身份、links 和 assignments，已有下载查询也载入这些证据。缺范围不派发、半季不再压掉整季；规范化区间用于跨运行和同轮分组。组合 **19 passed / 1 failed，6.52 秒**，剩余失败明确为 M5 的持久决策槽位仍合并两组候选。新 M5 夹具提供完整文件指派，避免因为前置 unknown 门禁而掩盖该失败。

尚未更新全部 Channel/UI 同步预检调用的加载路径，尚无持久唯一键、迁移和确认端点保护；不能合入此五文件原型或关闭任一待办。下一步先完成其余覆盖证据加载，再实现版本化规范键与事务并发合并，复用 PG 空槽屏障验证。

Dashboard 的 bounded scan 已追加文件指派加载；两条 metadata 补全扫描在同步检查前按批加载覆盖证据。资源详情原有 _DETAIL_LOAD_OPTIONS 已包含所需关系，无需重复查询。新增真实 Turso + HTTP 验证：有完整指派但没有标题集数范围的包不误报 unknown，无指派/无范围包在详情与 Dashboard 都报告 batch_coverage_unknown，**1 passed，1.79 秒**。目前八文件原型；扩大回归、唯一键/历史迁移与确认派发保护仍待完成。

### 持久键迁移的写路径清单

复核发现 metadata_dedup._repoint_series_children 按旧 `(agent,season,episode,status)` 元组直接删除碰撞决策，另有系列→电影批量 UPDATE；P8 的 _route_decisions 直接重指 series_id。新覆盖键不能只在 create_pending_decision 中写入：以上路径必须重算候选覆盖、合并同覆盖 pending 的候选并保留已处理历史，避免 FK 已变而键仍指向旧作品。

confirm、ai-pick、batch 三条处理入口目前均需纳入新描述校验和状态竞争验证；候选被编辑后，旧缓存推荐仍在 candidates 数组内也不代表覆盖度仍相同。批量处理需每条独立失败恢复，不能因一条完整性错误污染整个事务。数据库约束拟为 `(agent_id,versioned_key)` 的 pending 部分唯一，并保留规范化描述用于摘要碰撞/数据不一致诊断；历史 unkeyed pending 须在迁移中审计，不能永久留 nullable 旁路来声称唯一性已建立。

扩大确认/补全回归 bo 为 **79 passed / 3 failed，101.84 秒**。三条失败均为旧夹具仅提供季列表却期望覆盖已知：保留 legacy 季列表作为 unknown 负例，两个正例补明确的合成文件指派，未修改录制夹具。bq 复验运行中，句柄见 `/tmp/rssripple-v11-state.json`；原型现十文件，M5 持久键尚未修复，不能验收。

## 规范化键与并发存储原型（2026-09-20，bs）

原型新增可审计 decision_scope 与固定长度 v1 SHA256 decision_key，pending 的 `(agent_id,decision_key)` 部分唯一；CHECK 拒绝 pending 的 NULL 键，已处理历史允许保留无键。key 来源包含规范化作品/季/集或完整 batch 覆盖描述，比较已存描述可发现摘要/数据不一致。LLM 在锁前计算；存储层先锁 Agent，再重新读取 pending 并合并候选，SAVEPOINT 仅捕获目标唯一冲突，其他完整性错误上抛。候选变化后旧推荐失效，避免 LLM 失败仍沿用旧候选集结果。

组合 bs **22 passed、1 warning，8.28 秒**，包括原始四个业务负例及推荐失效；确认/补全扩大 bq **82 passed、1 warning，100.31 秒**。实际 PG 两连接都先完成空槽查询，再并发提交，最终返回相同 decision id，仅一条 pending，完整保留四个候选；直接重复键与 NULL pending key 均被数据库拒绝，已处理无键历史记录可保留。见 probes/decision-identity-pg-result.json，验证脚本须在应用原型后的独立副本运行。

这是十二文件原型，未应用主工作区。仍缺旧库 schema/data 升级与审核迁移、Turso 并发写入行为验证、confirm/AI/batch 的当前覆盖和状态校验、去重/P8 重指后的 rekey，以及两道完整门禁。不能把新装表约束通过当作升级库已修复，D4/M4/M5 保留 TODO。

Turso 双连接直接调用在实际 INSERT 上复现 database is locked（bu）；bt 首次仅为 MVCC 初始化顺序错误，不计作业务复现。使用现有 production retry_on_lock 包住整个新 session/transaction 后，bw 得到同一决策 ID、一条 pending、全部四个候选及真实约束拒绝；初始双请求屏障已到达，记录包含重试的 picker 调用次数。bv 曾将 entered==2 当作屏障结果，重试第三次调用导致该观察字段错误，历史保留并由 bw 修正。

仍不能关闭 D4：process_resources 当前可能把锁错误记入 result.errors 而非抛给 HTTP retry middleware；background 的新 session 候选单元也尚未接 retry_on_lock。只应在 ask 的纯数据库待决策单元重试，不能盲目重放可能已经执行的下载 RPC。需追加生产边界验证，再讨论完整双库并发已完成。证据见 probes/decision-turso-concurrency-result.json。

## V11 生产事务重试边界补验（2026-09-20）

后台 ask 多候选分支仅持久化决策，因此以全新 Session 重试整个候选事务；单候选下载分支不增加自动重试。请求事务遇到锁冲突向外抛出，交由现有请求事务边界处理。专项 **25 passed、1 warning，4.44 秒**，包含两个不同 Session 的重试、请求错误传播和下载分支不重复调用。

ca 探针直接并发调用生产 `process_resources(autocommit=True)`，没有探针外层 retry：两个实际 Turso 连接初次同时进入，生产边界发生一次重试，最终同一决策 ID、一条 pending、四个候选。唯一键及 pending 非空约束通过，历史非 pending 空键保持。数据为合成，LLM 用屏障替代；不宣称真实模型调用或跨进程验证。证据与可复跑脚本见 probes/decision-pipeline-turso-result.json、pending_decision_pipeline_turso_probe.py。旧 schema 升级、确认/AI/批量端点状态与覆盖复核、元数据合并后 rekey、完整门禁仍未完成，D4/M4/M5 不关闭。

## V11 旧决策只读审核原型（2026-09-20，cc）

新约束不能直接加到旧表：按实际候选覆盖重分组后，旧行可能需拆分，同槽旧行可能需合并。新增 decision_review 保留全部原始行及人工决定历史，按 agent 与规范化覆盖生成提案；缺失资源、未知覆盖、损坏候选列表和单候选组保留审核标记。报告含源决策 ID 与指纹，不执行删除、自动过期或派发，也不代表已授权迁移。后续 apply 必须锁定并重新审核，同时检查候选资格，不能仅依赖当前覆盖指纹。

**6 passed、1 warning，0.44 秒，退出 0**：包含实际 Turso 的旧表（没有 decision_key/decision_scope），SQL 观察器确认导出只执行 SELECT；混合槽拆分、重复槽合并、不同 agent 隔离、历史人工结果保留和候选作品改变使指纹失效。数据均为合成，原生产录制的 3 条 decided 历史不足以覆盖 pending 升级。原型现 14 文件，旧库 apply/schema 升级及确认端点保护仍未完成；未合入主干运行代码。V10 bz 完整集成仍运行，沿用句柄 26134，不重启。

## V11 确认入口覆盖漂移复现与初修（2026-09-20，cd–cf）

实际 Turso + HTTP 红测 **3 failed，1.70 秒**：创建同电影两候选决策并缓存推荐后，将被推荐资源改挂另一电影；confirm、ai-pick、batch AI 都错误成功并把决策标为 decided。下载派发由 AsyncMock 替代，不声称已真实下载。

新增共享 current_choice_error：检查 pending/有效期、至少两个不同且仍存在的候选、已存身份，以及每个候选的当前规范覆盖。手动和单条 AI 入口返回 409 INVALID_STATE，批量 AI 报该条失败；AI 计算后再复核一次。cf **12 passed、1 warning，3.21 秒**，覆盖三个入口的合法正例与漂移负例，以及六项只读审核测试；拒绝不派发、不改 decided 状态。

原型现 16 文件。这里不宣称已解决 TOCTOU：候选/Agent/决策锁顺序、等待模型期间状态变化、批量 SAVEPOINT 故障隔离、规则资格重验、旧库迁移和 rekey 仍待实施；现有旧测试的无键 PendingDecision 夹具也须按新契约逐项调整。主干运行代码未变，TODO 不关闭。V10 bz 原句柄 26134 继续运行。

## V11 批量事务失败隔离（2026-09-20，cg–ch）

真实 HTTP/Turso 红测 **2 failed，1.39 秒**：第一条的实际 NOT NULL flush 失败使整个 Session 进入 PendingRollbackError；另一场景 action 返回业务失败后，失败条目被改为 decided 的状态仍提交。必要性由数据库结果确认。

批量循环改为逐条 SAVEPOINT，失败返回通过局部异常回滚，成功计数只在 flush 和 SAVEPOINT 完成后增加。事先保存 ID，避免失败后读取过期 ORM 属性；锁冲突、外层事务失效和连接丢失继续向请求边界传播。组合 **14 passed、1 warning，4.31 秒，退出 0**，失败条目保持原始 pending/reason，下一条实际提交；同时复验三个入口的覆盖漂移/合法确认与只读审核。最后仅将局部异常类改名为 RejectedChoiceError 以满足 Ruff，检查通过。

原型现 17 文件，action 为故障注入，不声称测试了真实下载 RPC 失败。PostgreSQL 批量错误、候选资格/锁顺序与并发状态、旧 schema 升级及 rekey 仍待补；D4/M4/M5 保持未完成。V10 bz 已越过磁力解析等待继续执行，原句柄 26134 保持有效。

## V11 当前订阅和过滤资格复核（2026-09-20，ci–cj）

新增实际 HTTP/Turso 红测 **6 failed、6 passed，6.64 秒**：只修改 Agent filter_config 或将 scope_channel_wide 改为 false（无订阅作品），原 confirm/AI/batch 仍接受旧决策。候选身份没有改变，因此仅检查覆盖度不够。

current_choice_error 现在重新加载 Agent.works，复用生产 _build_rule_set/_resource_matches_rules，并校验资源频道仍匹配 Agent；资源的作品、合集关系显式加载，避免 DSL 静默读不到关联字段。cj 组合 **20 passed、1 warning，8.03 秒，退出 0**，包括三入口的正常确认、覆盖漂移、过滤修改、范围修改及批量失败回滚/旧表审核。测试为合成候选，下载派发替换为 AsyncMock。

Channel 必填元数据变更的资格复验仍待补，当前规则复核也尚无锁保证；旧库迁移、rekey、并发状态与双库完整门禁继续保留。原型仅存独立副本与可应用补丁，未合入主干运行代码。V10 bz 继续运行至真实 feed 格式回放，未重启进程。

## V11 Channel 元数据门禁复验（2026-09-20，ck–cl）

频道在决策创建后新增必填 subtitle_group，三个确认入口仍绕过缺字段检查，红测 **3 failed、12 passed，8.72 秒**。正例的合成 Movie 明确提供 release_date 与 is_anime=False，保证其满足基础门禁；未改动真实录制数据。

current_choice_error 显式加载 Agent.channel，并复用 inspect_resource_confirmation(resource, current_required_fields)。组合 **23 passed、1 warning，9.69 秒，退出 0**：三入口缺字段均不派发、不改 decided，合法元数据仍可确认，已有范围/过滤/覆盖复核和逐条回滚继续通过。Ruff 通过。

资格复核已接入共享政策，但候选和决策在检查到派发之间仍需事务并发保护，不能据此关闭 D4/M4/M5。下一步重点验证模型等待期间另一事务改变 decision.status/candidates 的行为，再确定锁和推荐阶段边界；旧库迁移、rekey、双库扩大和完整门禁仍待完成。V10 bz 原进程继续执行 HTTP 端到端流水线。

## V11 AI 等待期间状态竞争（2026-09-20，cm–co）

独立 PostgreSQL 双连接已实际复现：_generate_llm_pick 替身在另一 Session 提交 status=skipped，旧请求仍 dispatch 一次并把最终状态覆盖为 decided。红测退出 1，原始结果与正向结果见 probes/decision-state-pg-result.json。模型/下载替身明确标注；并发提交是真实 PostgreSQL 操作，同进程两连接。

修复将模型选择保留局部变量，避免重读前 autoflush 旧对象；模型返回后按 Agent→PendingDecision 的顺序加锁并 populate_existing 重读，再检查候选集合/规范覆盖快照与当前资格。cn 复验退出 0：另一事务确已提交，dispatch_calls=0，ok=false，最终仍 skipped。临时 PG 项目已 down 清理，退出 0。已有 HTTP/Turso/审核组合 co **23 passed、1 warning，9.43 秒**，Ruff 通过。

本次只证明 AI 等待后不覆盖已提交的 skipped。手动确认和 skip 的统一锁、批量锁顺序、并发候选/作品/指派变更、下载幂等、旧库升级和 rekey 仍待完成；不能宣称整个确认路径并发安全。V10 bz 原进程继续执行 metadata HTTP 用例。

## V11 手动与 skip 终态保护（2026-09-20，cp–cq）

HTTP/Turso 红测 **3 failed、3 passed，3.12 秒**：confirm 已拒绝终态，但 skip 对 decided/skipped/expired 全返回成功并重写处理时间。历史记录无新键，仍须保留其原状态、人工结果与时间。

手动确认、单条 skip 和批量 skip 写入前复用 Agent→PendingDecision 锁顺序并重读；skip 仅允许仍 pending 的选择决策。组合 cq **29 passed、1 warning，11.75 秒，退出 0**，终态拒绝后保留原 decided_resource_id/decided_at，资格检查、正常确认与批量回滚继续通过。Ruff 通过，原型现 18 文件。

这轮不是双连接并发证明。复审还发现批量 AI 的外层事务在第一条写入后持续持锁，后续 _generate_llm_pick 可能在锁内等待。下一步须拆开批量推荐与锁定写入阶段，并验证模型等待期间另一事务仍可更新，以及写入阶段拒绝过时推荐；不能只把单条 AI 的锁后重读当作批量已完成。旧库迁移、资源/指派锁与 rekey 继续待办。V10 bz 已继续到队列 HTTP 测试。

## V11 批量推荐移出写锁阶段（2026-09-20，cr–ct）

批量先逐条计算不可变推荐（候选 ID 集、规范覆盖快照、picked_id），计算阶段不写决策；随后逐条 SAVEPOINT 中加锁重读和资格复核后派发。单条 AI 复用同样的 prepare/apply 逻辑，模型返回候选集外 ID 被拒绝。失败项保留独立错误，数据库事务失效仍向外传播。

实际 PG 对照探针在第二次模型回调用另一连接 UPDATE Agent，并设置 1500ms lock_timeout。拆分前代码放在独立副本运行，实际触发 LockNotAvailableError，只有第一条派发，退出 1；拆分后另一连接正常提交，两次模型调用、两条派发、零失败，退出 0。原始结果见 probes/decision-batch-model-lock-result.json，脚本 decision_batch_lock_pg_probe.py；均为合成资源，dispatch/model 替身，数据库锁和另一连接提交真实。cr HTTP/Turso 组合 **29 passed、1 warning，11.99 秒**。

本轮解决批量后续模型调用持有前项写锁的问题，不代表资源/作品/指派竞争、迁移或 rekey 已完成；还需双库扩大和完整门禁。V10 bz 已推进到 P0 季号持久化集成，继续使用原句柄 26134。

## V11 继承 V10 与旧表约束安装原型（2026-09-20，cu–cx）

独立副本继承 main `7f8492a`（V10 代码 `6852cf0`），保留自身 18 文件；570 个非原型源码/测试/配置文件已同步，录制夹具未改。组合 cu **89 passed、1 warning，31.89 秒**，覆盖 V11 业务与 V10 退役字段/清理，全 app/scripts/tests Ruff 通过。

新增 decision_schema 安装器原型：未审核或重复键 pending 先失败并列 ID，不猜键、不修改候选/状态；只有已处理历史或已审核键时补列及 pending 唯一索引。PostgreSQL 设计为 CHECK，Turso 旧表用 INSERT/UPDATE 触发器补空键拒绝（保留未知历史列），新装模型仍有 CHECK。尚未接入启动或提供审核 apply。

cv 首次 **1 failed/1 passed** 暴露测试使用 BEGIN CONCURRENT，DDL 被真实 Turso 拒绝；改为迁移专用普通事务后 cw **1 failed/1 passed**，实际触发器正确拒绝，但抛 DatabaseError 而非 IntegrityError。cx 按实际异常语义验证 **2 passed、1 warning，退出 0**：INSERT/UPDATE 空键拒绝、同键唯一、离开 pending 后复用键、重复安装、历史列保留、未审核 pending 拒绝前后 schema/data 不变。

安装器仍须补 PostgreSQL、事务回滚、已有同名索引/约束定义检查，以及完整的审核数据迁移与启动接线；不得将空旧表升级通过宣称 D4 完成。原型现 20 文件，main 运行代码保持 V10 已验收状态。

## V11 旧库约束定义及双库 DDL 回滚（2026-09-20，cy–dc）

Turso 三项负例确认 IF NOT EXISTS 会静默接受错误同名定义：非唯一索引、仅 decided 的部分索引、空操作触发器，cy **3 failed/3 passed**。安装器现先检查列顺序、唯一属性、pending 谓词、触发器定义及 PG CHECK 定义；错误定义在任何 DDL 前被拒绝。Turso 故障注入确认安装后抛错可完整回滚新增列、索引、触发器并保留历史数据。

实际 PostgreSQL 首轮 da 失败于 asyncpg InvalidCachedStatementError：SELECT * 的结果形状跨 ADD COLUMN/回滚失效。修正为按已存在字段显式投影后 db 退出 0，验证 DDL 回滚、重复安装、INSERT/UPDATE 空键拒绝、未知历史列保留和错误索引拒绝。脚本与结果见 probes/decision_schema_pg_probe.py、decision-schema-pg-result.json；仅访问受保护的独立测试库，使用合成历史行。测试项目清理退出 0。最后 Turso dc **6 passed、1 warning，退出 0**，Ruff 通过。

安装器尚未接入 startup；已审核 pending 的数据变换/完整覆盖与键一致性校验仍待完成。空/已处理旧表的双库证据不替代混合历史决策的显式审核迁移，D4/M4/M5 不关闭。

## V11 显式审核数据迁移原型（2026-09-20，dd–de）

新增 DecisionMigration 事务档案与 apply_decision_review。执行要求 approved_fingerprint 精确匹配、supersede_pending=true；重新读取的审核指纹不匹配或仍有 unknown/missing/singleton 组则拒绝。使用当前重新计算的组，不信任用户修改的提案内容。仅旧 pending 保留原 ID 并变为 expired；历史非 pending 不变，新 pending 按等价覆盖创建且不继承缓存推荐；全部原始报告与新旧 ID 映射存入同事务档案。重复指纹返回已完成结果。

真实 Turso 旧表（无键列）合成数据：两个混合电影槽拆分并合并为两个等价选择，两个原 pending 保留，已决定历史不变；完整原始记录可从档案恢复。dd **3 passed、1 warning，1.25 秒**，包括成功/重复执行、后置故障导致新列/行变换/档案全部回滚、陈旧审核无修改拒绝。de 加上缺显式审批负例和 schema/review 回归 **17 passed、1 warning，2.48 秒**。模型/迁移权威子文档已在独立副本同步，原型现 26 文件。

该数据执行路径尚缺 PostgreSQL 验证、未知及单候选实际拒绝测试、多季/季包混合迁移、已审核键与覆盖一致性检查、CLI/启动接线及后续 rekey。数据库停写/备份仍为调用方前置条件；当前不向生产执行，不宣称 D4/M4/M5 完成。

## V11 审核迁移双库边界与离线 CLI（2026-09-20，df–di）

同一组实际数据迁移测试支持每测试独立 PG schema，固定临时账号和 loopback 地址限制；Turso df **8 passed、1 warning，2.82 秒**，实际 PostgreSQL dg **8 passed、1 warning，3.71 秒**。新增未知覆盖、缺失候选和单候选组均阻止整批执行，重新导出指纹不变、无档案写入；成功/回滚/幂等/陈旧及显式审批也在双库通过。PG 项目及 schema 已清理，退出 0。

新增离线 scripts.review_pending_decisions：只读导出不带批准字段、不覆盖已有报告；apply 要求 --writers-stopped/--backup-confirmed，在打开数据库前检查，服务仍要求报告显式指纹与 supersede_pending 审批。命令不调用应用 startup、不派发、不入队，Turso 去掉 CONCURRENT URL 参数并使用普通 BEGIN。权威迁移文档已同步命令与参数的责任边界。

CLI 首轮 dh **1 failed/8 passed**：父进程 dispose 后原生 Turso 文件锁仍使子进程无法打开。修正测试流程为独立种子进程退出后再启动 CLI，di **9 passed、1 warning，5.58 秒**，实际导出/缺确认拒绝/审核执行/另一进程幂等重跑通过。没有复制活数据库或绕过文件锁。原型现 27 文件，启动接线、多季混合覆盖、重指 rekey、剩余并发与完整门禁继续未完成。

## V11 季包/多季审核及持久键一致性（2026-09-20，dj–dm）

新增两种实际 Turso 旧表场景：单季包与 links-only 多季包，每种包含半范围 1–6 和完整范围 1–12，各两个版本，版本的关联/指派顺序相反。旧决策混合半包与整包；迁移后按真实覆盖分成两条选择，并调用生产 create_pending_decision 验证复用迁移生成的同一 ID，不另造键。单季平面 FK/季号和多季 NULL FK 均核对。dj 初轮 **2 failed/9 passed** 为旧表夹具加载 Agent 默认 selectin pending_decisions 时查询不存在的新列；改为 Agent 标量查询后 dk **11 passed、1 warning，6.26 秒**。

新增损坏键负例 dl **1 failed/6 passed**：非空 v1:wrong 与合法 scope 仍被安装器接受。提取 stored_choice_matches，按生产 choice_identity 重建版本化描述/摘要并比较；损坏或形状不匹配必须重新审核。dm 组合 **26 passed、1 warning，9.57 秒**，覆盖约束、迁移及生产创建，Ruff 通过。模型权威说明同步。

当前多季数据为合成，未声称原生产已观察混合 pending；PostgreSQL 的多季新增用例仍待复跑。下一步接入启动的安全拒绝路径，再处理作品重指 rekey 和资源并发。V11 仍为未验收原型。

## V11 启动安全接线与 PostgreSQL 多季补验（2026-09-20，dn–dp）

prepare_existing_decision_schema 在任何业务 startup/backfill 前检查旧表；旧 pending 未审核/键损坏/重复时失败，未创建表则由 ORM 新装约束处理。Turso 使用独立普通 BEGIN 的短 DDL 事务；PG 在启动 advisory lock 后、create_all 前检查。

实际 Turso CLI 进程验证：未审核启动退出 1 且重新导出指纹不变，离线审核 apply 后真正 create_tables 退出 0；连同既有迁移回归 do **58 passed、3 skipped、2 warnings，21.43 秒**。PG dn 初轮 **2 failed、8 passed、1 skipped** 发现手写旧表夹具把 decision_status 枚举建成 TEXT，生产 ORM 比较不成立；改回原枚举而非放宽实现。

PG dp **11 passed、1 skipped、1 warning，5.68 秒**：新增单季/多季迁移与生产键复用通过，实际 _create_tables_postgres 在审核前拒绝且原始指纹保持、审核后完整启动 DDL 分支成功。唯一 skip 是 Turso 专属 CLI 文件锁测试。临时项目清理退出 0，权威迁移文档同步。原型现 28 文件，仍须 rekey、资源/指派并发、既有测试夹具适配与两道完整门禁；未关闭 D4/M4/M5。

## V11 同类型作品合并 rekey 首轮（2026-09-20，dq–ds）

真实 Turso 生产 _merge_movie_group 红测 dq **1 failed，0.61 秒**：相同电影合并按旧槽位删掉一个 pending，四个候选丢为两个；旧实现同样会删碰撞的已处理历史。

同类型 series/movie 的重指函数移除 PendingDecision 碰撞删除，先锁受影响的 Agent，搬移全部关联后重算 pending 覆盖。同覆盖合并、不同覆盖拆分，稳定组保留 ID；变化旧 pending 保留为 expired，操作快照/替代关系存 DecisionMigration(operation=work_rekey)，不冒充人工审批。不带旧推荐，不自动派发；未知及 singleton 不产生可选 pending。

电影实际正例 dr **1 passed，0.60 秒**：四候选保留为一条新选择，两条人工已处理历史仍在。补后置异常回滚和重复 rekey 后，连同离线迁移 ds **13 passed、1 skipped、1 warning，8.94 秒**：失败恢复原四行且无档案，重复运行保持 pending ID/档案数量。唯一 skip 为 PG 专属启动测试。业务/模型文档在副本更新，原型现 32 文件。

尚未证明所有重指路径正确：当前受影响 Agent 发现仍按决策平面 FK，links-only 批量可能漏选；跨类型转换、P8、系列正例、实际 PG 锁竞争仍待补。不能用该电影专项关闭 M4/M5 或宣称完整 rekey 完成。

## V11 links-only 多季影响范围（2026-09-20，dt–du）

真实剧集合并负例 dt **1 failed、2 passed，1.51 秒**：S1 重复作品分别与同一 S2 组成两组候选，两个 PendingDecision 的平面 FK 均 NULL；合并后关联已重指，但仍保留旧的两条 pending，证实只查 decision.series_id 会漏选。新增夹具显式建立 WorkCollection，满足单季作品归属与唯一季槽。

lock_work_choice_agents 现在按 pending ID 游标每 100 行检查，候选资源 ID 同样每 100 个查询；同时匹配资源平面 FK、ResourceWorkLink 和 ResourceFileAssignment，找到受影响 Agent 后按固定 ID 顺序锁定。不会加载全量资源表。du 组合 **22 passed、1 skipped、1 warning，12.50 秒**：links-only 两组重建为一条包含四候选的决策，描述仅包含存活 S1/S2；电影保留历史/回滚/幂等、生产创建和离线迁移继续通过。业务说明同步。

本轮不证明跨类型/P8 或并发创建与影响范围扫描之间已完全串行化。后续先统一跨类型影响 Agent 的锁顺序，再接 rehome/cross-type/P8；D4/M4/M5 继续保留。

## V11 跨类型 rekey 验证（2026-09-20，dw/dy）

核对上一轮 dw 实际终态：13 passed、1 warning，5.68 秒。跨类型合并与定向 rehome 在修改关联之前统一发现源/目标作品涉及的 Agent，按 ID 顺序锁定；关联更新后在同一事务重建 pending 决策键，保留历史及候选档案。

新增电影转回剧集的两个边界场景：明确 S2E3 的源/目标候选合并；缺季号候选进入 unknown_coverage 档案，不猜季、不混入可派发决策。dy 组合回归 **15 passed、1 warning，6.86 秒，退出 0**，JUnit `/tmp/rssripple-v11-reverse-dy.xml`。测试使用标注合成数据、实际 Turso 数据库及生产合并服务，不能称为新增真实来源录制。dx 尝试在修正测试导入前主动终止（143），不作为通过证据。

原型与权威文档已保存到 decision-coverage-core-prototype.patch，尚未合入运行代码。下一步仍是 P8 迁移衔接、资源/关联并发修改与锁顺序的双库验证，再进行完整单元/API 和隔离集成门禁；D4/M4/M5 继续保留 TODO。

## V11 P8 新结构衔接（2026-09-20，ea–ec）

必要性：原 P8 只重指 PendingDecision.series_id，不重建持久键或按当前候选拆分。实际迁移红测 ea 为 **1 failed、1 passed，1.07 秒**：两个季各两个候选拆季后仍只有一个 pending；回滚正例通过。dz 首次失败为夹具漏填 reason，不是缺陷证据。

独立原型为 migrate_series 增加事务内决策重建：迁移前发现并锁受影响 Agent，子关联迁移完成并 flush 后重建，调用方继续拥有 commit/dry-run rollback。eb 组合 **17 passed、1 warning，7.72 秒**；ec 完整 P8 单元与 rekey **20 passed、1 warning，9.56 秒，退出 0**，JUnit `/tmp/rssripple-v11-p8-ec.xml`。既有 P8 空候选夹具改为 skipped 历史记录并新增状态保持断言；有效 pending 的两季分槽、四候选保留、旧记录过期、档案及回滚由新增测试验证。沿用既有作品标题形状，新增候选明确为合成数据，不是生产待决策快照。

当前仅验证已有键结构。下一步必须解决旧 pending_decisions 没有 key/scope 列时 P8 与审核迁移的顺序：未拆季资源不能提前通过身份审核，而现有 ORM 路径读取新列。需要真实旧表复现和离线迁移测试，不能以当前绿色结果宣称升级闭环。随后继续双库并发与完整门禁，D4/M4/M5 保留 TODO，原型不合入 main 运行代码。

## V11 无键旧表 P8 衔接（2026-09-20，ee–eg）

旧表实际复现缺 decision_key 列失败（ee）；ed 初次在测试准备的 Agent eager-load 阶段失败，不作为 P8 本体红测。实现按表列检测：有键结构继续同事务 rekey；无键结构仅使用旧字段重指决策作品引用，保留候选和状态，拆季后另行导出/批准审核，不自动批准或提前安装约束。P8 的 Agent 名称查询改为标量查询，避免带出新决策列。

扩大 ef 为 2 failed、30 passed、1 skipped：发现赋值查询结果形态回归，以及同会话审核沿用拆季前关系。修正后 eg **32 passed、1 skipped、1 warning，18.08 秒，退出 0**，JUnit `/tmp/rssripple-v11-p8-eg.xml`；跳过 PostgreSQL 专属启动测试。旧表测试确认四个混季候选在 P8 阶段原样保留、没有新增键列，随后审核产生两个正确季槽并归档原决策。数据明确为合成候选，实际 Turso 旧表及生产迁移路径。

原型及权威迁移说明已更新。尚需 PostgreSQL 旧表同场景、升级回滚、并发修改及完整门禁；D4/M4/M5 不关闭，未合入 main 运行代码。

## V11 旧表升级双库验收专项（2026-09-20，eh–ek）

在唯一隔离项目 `rssripple-v11-p8-20260920-eh` 的 PostgreSQL 16 上执行实际旧表迁移。新增同事务 SAVEPOINT 故障注入，断言拆季失败后决策指纹、四个候选资源的原作品归属/季号、作品数量恢复，随后重新拆季并审核成功。

PG eh 首轮 1 failed、11 passed、1 skipped，失败是合成夹具 parsed_at 带时区、目标列不带时区，发生在迁移前；按已有夹具约定清空无关字段后 ej **12 passed、1 skipped、1 warning，6.55 秒，退出 0**。唯一 skip 为 Turso 专用 CLI 测试。最终同代码 Turso ek **1 passed、12 deselected、1 warning，0.64 秒，退出 0**。没有修改生产配置或数据；测试数据明确为合成。容器、网络清理退出 0。机器摘要见 probes/decision-p8-upgrade-result.json。

此专项证明旧表 P8 → 重新导出 → 显式审核 → 新约束的双库衔接及 P8 回滚。尚不代替资源/链接/指派并发验证和完整测试门禁；下一轮优先补确认派发与关联修改交错事务。V11 继续为独立原型，D4/M4/M5 保留。

## V11 资源修改与派发竞争（2026-09-20，el/em）

两个真实 PG 连接复现：资格校验后、派发边界内，第二连接把候选 movie_id 清空成功，第一连接仍标记 decided。专项使用合成候选与模型/下载替身，不声称实际 RPC 完成。独立原型在最终确认校验中按资源 ID 排序取得 FOR UPDATE；AI 推荐准备保持无资源锁。红测 writer_blocked=false，补修后 writer_blocked=true，提交后同一修改成功；证明锁确实持有并释放。机器结果与可重跑脚本见 probes/decision-resource-pg-result.json、decision_resource_pg_probe.py。

确认资格、批量事务、终态保护 API 回归 em **23 passed，11.63 秒，退出 0**。隔离项目 rssripple-v11-resource-20260920-el 已清理，退出 0。尚需文件指派/链接/作品元数据并发以及跨路径锁顺序验证，后续完整门禁仍未运行；D4/M4/M5 不关闭。

## V11 文件指派竞争与电影包资格（2026-09-20，en–eq）

初次 en 在资格门禁前失败：批量覆盖 reload 使用 populate_existing 却未加载 movie 等关系，合法电影包缺元数据。补齐资格相关 eager-load 后 eo 双 PG 连接红测复现实际指派竞争：派发边界内将指派 movie_id 清空成功，决策仍 decided。

原型最终确认依次锁资源、作品链接、文件指派，各表按 ID 排序；ep 文件指派 UPDATE 被数据库 lock_timeout 阻止，确认提交后同一更新成功。模型与下载为替身、候选为合成数据，仅证明数据库边界。eq 扩展三类确认 API 的普通电影/电影包正反例及覆盖度单元 **46 passed、1 warning，17.24 秒，退出 0**。隔离项目已清理退出 0，结果与复跑脚本见 probes/decision-assignment-pg-result.json、decision_assignment_pg_probe.py。

下一步仍需关联插入/删除、作品元数据和交叉写路径锁顺序，不以已有 UPDATE 场景推断全部并发已覆盖。完整门禁未运行，D4/M4/M5 保留。

## V11 关联增删并发矩阵（2026-09-20，er）

必要性：此前仅验证已有指派 UPDATE，不能据此推断新关联插入或删除会阻塞。独立 PostgreSQL 16 项目通过实际确认函数，在下载调用边界让另一连接分别 INSERT/DELETE 文件指派、INSERT/DELETE 作品链接。四场景均观察到 SQLSTATE 55P03，确认提交后同一操作均成功；决策均 decided、派发调用一次。新增子记录由资源父行锁的 FK 检查阻止，已有记录删除由子行锁阻止。本轮无需修改运行实现。

顺序运行四场景退出 0，项目 `rssripple-v11-associations-20260920-er` 容器及网络清理退出 0。明确使用合成电影包和模型/下载替身，数据库及确认函数真实；不是实际 HTTP 编辑路径或下载器端到端证明。机器结果与复跑脚本见 probes/decision-association-matrix-pg-result.json、decision_association_matrix_pg_probe.py。

下一步检查作品元数据变化，以及实际资源编辑/作品合并的交叉锁顺序；完整门禁未执行，D4/M4/M5 继续保留，V11 不合入 main。

## V11 作品元数据竞争（2026-09-20，es–eu）

真实 PG 双连接红测：最终资格校验后清空电影 release_date 成功，第一事务仍 decided（es）。原型现在在资源/子关联锁之后，从稳定引用收集作品，按模型/主键顺序加共享行锁，再读取并锁定所属合集，随后重新加载资格证据。et 同一更新被阻止，确认提交后成功。

确认 API、批量事务、终态与审核 eu **44 passed、1 warning，21.01 秒，退出 0**。项目 `rssripple-v11-work-20260920-es` 已清理退出 0。候选与作品为合成，模型/下载调用替换，PG 连接与确认路径真实；专项只证明电影上映日期更新边界。机器结果/脚本见 probes/decision-work-pg-result.json、decision_work_pg_probe.py。

下一步需扩大剧集/合集、频道必填及订阅规则变化，同时验证实际编辑/合并路径锁顺序；本轮不宣称所有元数据竞争已解决。完整门禁仍未执行，D4/M4/M5 保留。

## V11 既有回归兼容性（2026-09-20，ev/ew）

扩大运行原有决策 API、metadata_dedup、agent_service：ev **118 passed、42 failed，66.058 秒，退出 1**。失败包括无键 pending、单候选冲突、缺少明确覆盖度却期待派发、旧去重删除行为；不能用此前专项绿测代替此扩大结果。逐项失败名称与首行错误已保存 probes/decision-expanded-compatibility-result.json。

先修正 API 夹具：明确建立合成电影作品、为两候选绑定同一身份并生成规范键；剧集序列化场景补合集、季号、上映日及人工集号证据；单候选防御测试保留单候选错误状态及 409 断言，但使用规范持久键。保留原列表/跳过/确认/真实 dispatch_download（下载器 mock）/批量/状态重置断言，没有绕过数据库约束或确认门禁。

全部原有与新增决策 API ew **58 passed，32.70 秒，退出 0**。仅说明其中 API 的 17 项旧失败已解决；metadata_dedup 3 项、agent_service 22 项尚待处理，不宣称剩余 25 项通过。下一轮从明确候选基数和覆盖度证据入手，必要时修复实现回归，不降低断言。V11 仍未通过完整门禁，D4/M4/M5 保留。

## V11 去重既有用例修订（2026-09-20，ex）

旧三项失败用例以缺失资源 ID、单候选或空候选构造无键 pending，且要求删除“冲突”决策；这既不符合新约束，也会容许候选静默丢失。改用明确合成的实际作品、资源及两候选规范决策。原 Episode/AgentWork/人工映射去重断言保持；决策改为验证两条源记录 expired 加一条四候选 pending、独立重指的新键与原候选相等、不同 Agent 的候选不串槽。

完整 metadata_dedup + decision_rekey **40 passed、1 warning，16.00 秒，退出 0**，JUnit `/tmp/rssripple-v11-dedup-ex.xml`。本轮仅修改测试数据及对应保留语义断言，未改运行实现。原 ev 去重 3 项失败已解决；Agent 服务仍有 22 项已知失败未解决，下一轮处理候选基数和批量覆盖证据。V11 未达到完整门禁，D4/M4/M5 保留。

## V11 候选基数与旧记录边界（2026-09-20，ey/ez）

必要性复核：PendingDecision 只用于至少两个不同候选，原字段/幂等/推荐跳过测试的单候选输入不再有效；同一季作品同时挂 S1/S4 也违反单季模型。正例改为两个真实资源，季号隔离用独立 S1/S4 作品，保留字段/原因/ID 幂等/候选合并断言。新增单候选及重复 ID 拒绝、数据库无决策写入的负例。

旧确认记录退役测试保留其畸形空候选输入和旧原因，但补规范键；正常冲突改为两个真实资源并断言未被误退役。旧未拆季覆盖用例改为“即使关系已加载且标题区间明确，也不能派发”，符合不猜季约束。未修改运行实现或降低基数校验。

ey 候选创建 **9 passed，3.91 秒**；最终 ez **12 passed、98 deselected、1 warning，5.10 秒，退出 0**，JUnit `/tmp/rssripple-v11-candidates-ez.xml`。原 Agent 服务 22 项失败中的 10 项已解决，余下 12 项集中于批量/links-only 覆盖；尚未再次声称全文件通过。下一轮补齐明确覆盖的正例，并保留未知覆盖拒绝。完整门禁未执行，D4/M4/M5 继续保留。

## V11 批量覆盖既有回归（2026-09-20，fa）

剩余 12 项原 Agent 失败复核：单季正例缺少范围、多季正例只填 batch_seasons 或 links，无法证明实际内容覆盖。正例补明确 1–12 区间；多季用独立季作品、links 及逐季 ResourceFileAssignment，顺序反转仍应归一。未知覆盖拒绝及修订后再运行用例保持，未给通用资源工厂自动补范围。

完整 Agent 服务文件 fa **110 passed、1 warning，39.39 秒，退出 0**，JUnit `/tmp/rssripple-v11-agent-fa.xml`。原 ev 42 项失败经 API、去重、候选基数与本轮覆盖测试分别解决；本结论不是完整单元/API 门禁。测试明确使用合成证据。权威 constraints.md 同步修正旧“季作品集合即覆盖度”定义，纳入区间与缺口。

下一步继续尚未完成的频道/订阅规则并发、实际编辑合并锁顺序与全量门禁，并同步其余权威契约及集成夹具。V11 仍为原型，不关闭 D4/M4/M5。

## V11 全范围诊断启动与规则竞争红测（2026-09-20，fb/fc）

已启动独立副本 tests/unit + tests/api 全范围诊断（--maxfail=20，尚非完整门禁），现有进程句柄 **7667**，日志 `/tmp/rssripple-v11-diagnostic-fb.log`，JUnit `/tmp/rssripple-v11-diagnostic-fb.xml`，尚无终态。冻结清单 `/tmp/rssripple-v11-diagnostic-fb-source.json` 含 521 个 Python 源文件，保存时全部哈希未变化。必须续查该句柄，不因观察超时重跑；运行期间不改副本源码。

只读审查与隔离 PG 双连接 fc 另复现两处尚未修复竞争：最终校验后 Channel.required_metadata_fields 新增字幕类型，或 AgentWork.filter_overrides 改为仅允许 4K，第二连接均可提交且第一连接仍派发、标记 decided。模型和下载替身，候选为合成，数据库真实。专项期望阻塞而失败（退出 1），不可计为通过。项目 rssripple-v11-rules-20260920-fc 已清理退出 0。脚本/机器证据见 probes/decision_rule_pg_probe.py、decision-rule-pg-result.json。

下一步：待 fb 终态后处理诊断失败，并补频道/现有订阅记录的规则稳定性及订阅增删的协作锁。Agent 现有 NO KEY UPDATE 不能阻止子订阅字段单独更新或新增；不能直接升级为 FOR UPDATE 而忽略资源修订持久请求 FK 与锁顺序。需用实际 API 交错验证修复，V11 不合入 main，D4/M4/M5 保留。

## V11 规则锁独立补修（2026-09-20，fd–ff）

原全范围 fb 句柄 7667 仍运行，为保留诊断源码，从其副本复制 `/tmp/rssripple-v11-rules-fd` 独立修改。最终确认对频道和现有订阅规则加共享锁；Agent 更新及订阅增删改 API 先取得既有 Agent NO KEY UPDATE 父锁，协调新增订阅，不升级锁强度而阻断持久请求 FK。

fe PG 五场景（频道/订阅直接 UPDATE，实际 create_work/update_work/delete_work API 函数）均在确认期间被锁阻止、提交后成功；模型/下载替身，数据合成，数据库与函数真实。项目已清理退出 0。不是完整 HTTP 并发证明，也不保证任意外部 SQL 订阅 INSERT 遵守协作锁。

fd Agent/决策 API 回归 **81 passed、3 failed，113.02 秒**，失败为旧运行记录展示用例无键/单候选夹具。独立副本补真实双候选与规范键、保留原展示断言后启动 ff 重跑：**句柄 36693**，日志 `/tmp/rssripple-v11-rules-ff.log`，JUnit `/tmp/rssripple-v11-rules-ff.xml`，尚无结果。

增量补丁为 probes/decision-rule-fix-prototype.patch（相对于冻结主原型，不可直接当作 main 补丁），前后哈希/五场景结果/进程信息见 decision-rule-fix-result.json；重跑脚本 decision_rule_api_pg_probe.py。待 fb 与 ff 终态后核验并归并，勿覆盖运行源码或重启同一测试。V11 仍未验收。

## V11 规则锁双向交错及原型整合（2026-09-20，ff/fg）

ff API 重跑终态 **84 passed，137.56 秒，退出 0**，JUnit `/tmp/rssripple-v11-rules-ff.xml`；此前 fd 的三项展示夹具失败已解决。fg 另覆盖相反顺序：实际模型等待回调中，另一连接提交频道必填、订阅过滤、实际订阅更新/删除 API 函数；四场景最终校验均拒绝、下载调用 0、决策保持 pending。该验证也证明模型准备期间这些写入不受确认锁阻塞。项目已清理退出 0。模型与下载均为替身，数据库和 API 函数真实。

比对四个增量文件的原/新 SHA-256 后，将当前原型目录切换为 `/tmp/rssripple-v11-rules-fd`，完整 decision-coverage-core-prototype.patch 已含规则补修。旧 `/tmp/rssripple-v11-decisions` 不再编辑，继续服务 fb 全范围诊断 **句柄 7667**；该诊断覆盖规则补修前快照，不能作为最终新原型门禁。当前原型没有合入 main。增量补丁保留历史，后续使用完整补丁及 state.json 目录为准。

下一步续查 fb 终态，按失败补齐其他夹具或实现；仍需资源编辑/作品合并交叉锁顺序、完整门禁与权威文档审查。D4/M4/M5 保留。

## V11 资源修订与持久请求锁顺序（2026-09-20，fh/fi）

必要性：确认持有 Agent 锁后等待资源，而真实 PATCH 先写资源再插入 AgentResourceRequest；如果父锁过强，会形成反向等待。fi 通过事件屏障让 PATCH 的实际 flush 先完成、确认实际 Agent/决策锁后才继续 request_channel_resources。编辑成功写 revision=1 请求并提交，确认随后读取 720p 并派发一次，最终 decided。两连接实际 PostgreSQL、真实 correct_parse_fields 和持久请求函数；只替换下载、缓存获取和入队唤醒。合成电影候选，10 秒只是测试失败上限，时序由事件而非睡眠决定。

fh 初次脚本 schema 导入路径错误，未进入交错；修正后 fi 退出 0。项目已清理退出 0。机器结果与可重跑脚本见 probes/decision-resource-edit-pg-result.json、decision_resource_edit_pg_probe.py。本轮无运行实现修改，验证既有 NO KEY UPDATE 与持久请求 FK 锁兼容。

fb 全范围诊断句柄 7667 仍运行，已到约 88%，出现新的失败/错误，尚无终态，必须沿用原进程。下一步先处理其报告，再补作品合并竞争与最终全量门禁；V11 仍未验收。

## V11 权威契约与验收清单同步（2026-09-20）

只修改当前原型文档：业务流程图和单季化契约统一覆盖度为作品/季号/区间，去掉“仅作品集合即覆盖”旧说法；API 契约补最终资格重校验、过期选择拒绝和批量 SAVEPOINT/外层失效处理；集成清单列明真实双库、真实数据回放与替身边界及最终覆盖率/退出/清理门禁。

重新核验 prod_works_v1.json SHA-256 为 d11651d2162ced23e8d919af0bff2d9f316e203234cc854909ba5f444a35ec32，与原捕获审计一致。559 资源、101 批量及三个已处理单候选历史决策，不可充当真实生产混合 pending 的证据。没有改写真实数据以适配断言。

旧快照 fb 诊断句柄 7667 仍在运行，约 93% 后继续推进，已有失败/错误，待最终报告逐项处理；未重启进程，也未修改其 521 个冻结源码。V11 尚未验收。

## V11 全范围诊断 fb 与夹具适配（2026-09-20）

原诊断进程已结束（退出 1）：3729 passed、9 failed、8 errors、15 skipped，1635.61 秒。521 个源码文件与冻结快照一致。该轮无覆盖率统计，不构成完整验收；失败明细见 probes/decision-full-diagnostic-fb-result.json。

当前独立原型已修复其中 3 个 Agent 夹具失败（此前 ff 轮通过）；本轮修复覆盖 helper 调用参数并移除重复指派构造，保留两组覆盖与候选集合断言，fj 轮 8 passed（3.54 秒，退出 0）。决策列表、缺失资源拒绝及 Dashboard 的决策键夹具适配后，fk 轮 9 passed、3 skipped（5.17 秒，退出 0）；跳过项不计作验证。缺失资源用例使用真实持久化的合成作品和两个不存在的候选，保留 409 与未派发断言。

仍有 10 个原诊断失败/初始化错误待处理，集中于 scheduler、dashboard_extra 和 resources 夹具；不得关闭 D4/M4/M5。后续先适配有效候选与身份，保留专门的旧数据负向用例，再执行真实录制数据回放、并发审查与完整覆盖率门禁。当前运行代码未合入 main。

## V11 剩余诊断修复与真实录制回放（2026-09-20）

scheduler、dashboard_extra、resources 的旧夹具已适配：有效决策绑定同一作品的两个不同候选，过期/未过期决策使用独立身份，分页保留刻意构造的异常单候选记录作为负向用例。fl 轮三个完整文件 **163 passed、8 skipped、1 warning，71.26 秒，退出 0**。原 fb 的 17 个失败/错误至此均有后续专项通过证据，但尚未重跑全量门禁。

新增 season_model/test_decision_coverage_captured.py，使用未经改写的真实 prod_works_v1.json 工作图和实际 Turso 数据库。加载 101 个 batch，核验同作品同季、标题范围均为 1–7 的两个录制资源：完整七条文件指派得到精确覆盖；七条指派证据不足的资源保持 unknown，并进入 Channel 确认。保留 3 条历史 decided 单候选记录，验证政策查询不写库。fm 轮 **1 passed，退出 0**，报告 /tmp/rssripple-v11-captured-fm.xml。该快照没有 pending 决策，不能作为真实待决策迁移或并发证据。

当前独立原型及新增测试已保存到 combined prototype patch。后续仍需审查跨业务锁顺序、适配集成夹具并完成 ≥95% 单元/API 与 ≥85% 完整隔离集成门禁；D4/M4/M5 保留 TODO，未合入运行代码。

## V11 集成诊断与 rekey 空槽竞态（2026-09-20）

三个集成文件 fn 诊断退出 1：46 passed、22 failed，20.75 秒。失败涉及旧单候选/无候选夹具、已退役覆盖键预期及合并后决策历史语义；详见 probes/decision-integration-diagnostic-fn-result.json。scheduler 夹具已改为持久作品和两个真实候选，fq 完整文件 7 passed，2.95 秒，退出 0；仍有 21 个 fn 失败待处理。

**必要性补证：作品合并的 pending 发现存在空槽窗口。** 两个实际 PostgreSQL 连接以事件屏障控制：合并调用 lock_work_choice_agents 返回空集合后，另一连接通过生产 create_pending_decision 创建首条决策并提交；随后完成真实 _merge_movie_group。结果 pending.movie_id 已迁至目标，但 decision_scope.work_id 仍为已删除的源作品，canonical_after_merge=false，fp 探针按预期退出 1。初次 fo 在准备数据时因时间夹具错误退出，不计入竞态证据。使用合成作品/候选，跳过 LLM，无下载 RPC。独立 Compose 项目 rssripple-v11-discovery-20260920-fo 已清理（退出 0）。探针与结果见 probes/decision_rekey_discovery_pg_probe.py、probes/decision-rekey-discovery-pg-result.json。

**修复方案待实现和论证：** 现有“扫描 pending → 锁发现的 Agent”不足以协调首条决策。需要在发现之前建立与创建路径共享的事务级身份变更协调协议，并在创建等待结束后刷新候选、重新核对 canonical scope；仅重扫或仅锁当前结果都不能排除再次插入。锁顺序必须兼容已有 Agent→decision→resource→work 确认路径，避免把资源/作品锁提前导致交叉死锁。下一轮先设计该协议，再以创建先行、合并先行、LLM 等待期间合并、回滚及 links-only 多作品四类边界建立红/绿证据。当前仅证明缺陷，未宣称修复；V11 继续禁止合入运行代码。

## V11 身份协调原型与定向绿测（2026-09-20）

已在独立原型实现 PostgreSQL 事务级 advisory 协调：创建使用共享模式，作品 rekey 在发现 pending 前使用排他模式；try-lock 失败立即抛出可重试数据库锁错误，禁止持有业务锁时等待造成反向锁序。工作合并较少，当前以全局 pending 身份变更域协调，允许不同创建事务并行，但合并期间会短暂拒绝所有创建；该吞吐取舍须继续评审。Turso 仍依赖原生写冲突与完整事务重试。创建在 LLM 之后获取协调锁及 Agent 锁，再刷新候选，核对实际 canonical scope；旧身份不能落库。

fs 实际 PG 两连接绿测退出 0：合并扫描空集合后创建遭 coordination_busy；合并完成后旧 key 遭 stale_identity；按目标作品身份重分组重试成功且 scope/FK 一致。fu 三连接核验共享创建并行、合并排他、创建排他与 rollback 释放均通过。临时项目 rssripple-v11-identity-20260920-fs 清理退出 0。原 fp 红测继续保留，不覆盖失败事实。

fr 初次回归 15 passed、2 failed，失败源于测试通过正常创建接口构造历史未知 TV 集号决策；改为显式历史数据，并补正常创建拒绝断言后，ft 为 17 passed、1 warning、8.19 秒、退出 0。保留完整 rehome/cross-type 历史合并断言。扩大 fv 回归运行中，session 56604；请继承进程而非重启。

静态核验 job_handlers 的错误路径保留 last_consumed_at 并 defer 持久请求；API 回填 result.errors 会回滚外层事务。仍需实际创建先行/模型等待期间合并、完整边界重试与 links-only 多作品竞争测试，以及完整门禁，不能把当前定向绿测当作 V11 已验收。

fv 已终止（退出 1）：158 passed、2 failed、1 skipped、1 warning，68.11 秒。失败为 test_create_pending_decision_series_no_episode 与 test_create_pending_decision_legacy_3tuple_key；下一轮核验这两项旧身份预期，不再轮询 session 56604。

## V11 身份契约适配与完整单元门禁启动（2026-09-20）

复核 fv 两项失败后，未知 TV 集号改为明确拒绝且不创建决策；三元组 key 兼容测试保留，但资源明确携带所属季号，禁止用 key 默认值替候选猜季。batch 覆盖集成改用 ORM 指派、作品、精确区间；legacy 未拆季与仅链接无指派继续拒绝。历史确认退休用例保留异常单候选负向数据，正常组绑定真实候选。

fw 单元 Agent＋集成 Agent：151 passed、3 failed，50.11 秒，退出 1；剩余三项修复后 fx 完整 Agent 集成：44 passed，9.32 秒，退出 0。fy 完整 metadata_dedup 集成：17 passed，5.90 秒，退出 0；决策历史不再删除，合并后 2/4 候选组与候选保留断言通过，原作品/订阅/映射/指派迁移断言继续保留。fn 原 22 个失败（含此前 scheduler fq）及 fv 原 2 个失败均有后续专项通过证据，不等同全量通过。

已冻结 523 个 Python 源文件到 /tmp/rssripple-v11-gate-fz（manifest /tmp/rssripple-v11-gate-fz-source.json）。初始 fz 复制规则误排除 .coveragerc，已主动终止并取得退出 143，不能作为门禁；恢复原配置后启动 ga 正式完整单元/API ≥95% 门禁，session **20821**。日志 /tmp/rssripple-v11-unit-ga.log，JUnit /tmp/rssripple-v11-unit-ga.xml，覆盖率 /tmp/rssripple-v11-unit-ga-coverage.xml。请继承该进程，不编辑冻结副本、不因观察超时重启。当前 rules-fd 原型可继续独立评审；若实现变更，ga 只证明其冻结版本。尚缺剩余并发边界和完整隔离集成验收，V11 未合入 main。

## V11 完整集成启动与身份时序补证（2026-09-20）

独立完整集成 gb 已启动：项目 rssripple-v11-complete-20260920-gb，冻结目录 /tmp/rssripple-v11-integration-gb，2967 文件 manifest 为 /tmp/rssripple-v11-integration-gb-source.json。录制 fixtures 已复制为实际文件，避免容器内解析宿主绝对符号链接。四服务健康、启动退出 0，runner session **63542**，日志 /tmp/rssripple-v11-integration-gb.log。当前必须保持项目，等 runner 终态后再 SIGINT 两个应用、确认 exit 0、合并四份 coverage ≥85%、导出 data、校验冻结文件并 down --volumes；仅启动成功不算验收。

单元/API ga session **20821** 仍在运行；冻结副本 /tmp/rssripple-v11-gate-fz 的 523 个 Python 文件及 gb 的 2967 文件本轮均复验未变。两个门禁均无终态，不因观察超时重启。

gc 新增两个实际 PostgreSQL 连接时序：①生产 create_pending_decision 已写入但未提交时，合并遭协调冲突；创建提交后以新事务重试合并，保留旧 expired 决策并生成目标作品的同候选 pending；②模型等待期间允许真实作品合并提交，模型返回后旧身份被拒绝，新事务按当前身份创建成功，旧建议不复用。候选均保留。使用合成作品/资源和受控模型返回，探针退出 0、专项项目清理退出 0；见 probes/decision_identity_orders_pg_probe.py 与 decision-identity-orders-pg-result.json。本证据为函数级实际事务、手动新事务重试，不冒充完整任务处理器自动恢复。V11 继续待验收。

## V11 links-only 多作品身份竞争补证（2026-09-20）

gd 实际 PostgreSQL 探针扩展至没有直接 series_id/movie_id、仅通过 work_links 与 file_assignments 绑定两个电影的合集。四个时序均通过：普通电影/links-only 合集各自的创建先行、模型等待期间合并；创建先行的旧 pending 保留为 expired，目标 pending 保留两候选；模型等待的旧身份拒绝，重建后不复用旧推荐。探针使用实际生产创建/合并和两个事务连接，合成资源/模型，显式新事务重试，不能代表完整任务处理器自动恢复。退出 0；项目 rssripple-v11-links-20260920-gd 已清理退出 0。结果见 probes/decision-links-identity-orders-pg-result.json。

完整单元/API ga（20821）与完整集成 gb（63542）继续运行，无终态。下一步继续继承门禁进程并补实际 job-handler 恢复路径；V11 未验收。

## V11 实际任务处理器恢复（2026-09-20）

ge 实际 PostgreSQL 专项通过：直接调用生产 _handle_run_agent，使用真实 process_resources、作品合并、请求快照/退避/确认及水位线持久化。模型等待期间另一连接完成作品合并；首轮返回 changed identity 错误，消费水位线保持原值，两条请求 attempt_count=1 且 next_attempt_at 为 +30 秒，没有 pending 决策。观测退避落库后推进测试时钟 31 秒，再调用处理器；由处理器自行重新读取/分组资源，生成目标身份的一条 pending、保留全部候选、确认删除请求、推进水位线。AgentRun 状态为 failed→pending_decisions。未替换 process_resources 或 RunResult；仅控制模型返回和请求时钟，素材为合成作品/资源。直接任务调用不等同 broker/worker 崩溃恢复。

探针 decision_job_recovery_pg_probe.py 与结果 decision-job-recovery-pg-result.json 已保存，退出 0；独立项目 rssripple-v11-job-20260920-ge 清理退出 0。完整单元 ga（20821）和完整集成 gb（63542）仍未结束，继续继承这两个进程。V11 不计作已验收。

## V11 初步质量审查（2026-09-20）

按 code-review-and-quality 复核正确性、可读性、架构、安全与性能，详见 probes/decision-quality-review-result.json。当前 app/tests/scripts Ruff 退出 0。临时副本根目录还包含早期 scratch probes，全目录 Ruff 有 42 项问题，不能宣称整个副本 lint 通过；这些临时文件不在待应用文件列表中。迁移/在线 rekey 的 scope→flat fields 投影有小段重复，最终审查需评估收敛；尚未改变运行代码以免污染在跑门禁。

当前审查为阶段记录，非批准合入：ga（20821）完整单元/API 和 gb（63542）完整集成尚未终止。确认接口、迁移指纹/归档、精确覆盖、创建协调与实际处理器重跑的专项证据不能替代完整门禁。

## V11 完整单元/API ga 通过（2026-09-20）

原 session 20821 已结束，退出 0：**3746 passed、15 skipped、6 warnings，1718.46 秒**。XML 覆盖率 **21918/22482＝97.49%**，95% 门禁通过；冻结 manifest 的 523 个 Python 文件复验无变化。JUnit /tmp/rssripple-v11-unit-ga.xml，coverage /tmp/rssripple-v11-unit-ga-coverage.xml，机器摘要 probes/decision-unit-complete-ga-result.json。此前 fz 配置错误的终止轮仍保留为无效记录，不混算。

完整隔离集成 gb session 63542 仍运行（项目 rssripple-v11-complete-20260920-gb），尚未汇总应用覆盖率、应用退出码或清理，不能关闭 D4/M4/M5 或合入 V11。D6 的后续红测仅在独立 V12 主干副本，不属于 ga 源码。

## V11 gb 完整失败收尾、语料复审与 gk 复验（2026-09-20）

gb 原进程退出 1：3139 passed、3 failed、17 skipped、8 warnings，1709.78 秒。两个应用 SIGINT 正常退出 0/0；四份 coverage 汇总退出 0，20004/22482＝88.98%，但测试失败因此本轮不通过。证据已导出 /tmp/rssripple-v11-artifacts-gb，2967 文件冻结复验未变，项目清理退出 0。结果见 probes/decision-integration-gb-result.json。

两个 misc_services 确认正例缺少精确区间/指派，已显式补齐；所有未知覆盖负例保留。真实 initial_d_franchise 失败先补 runner 的 load_batch_coverage（避免未加载关系假阴性），gi 为 61 passed、1 failed；残余差异仍仅为 confirmation.kinds。独立核对既有黄金记录：Battle Stage 3 文件 work=None，原审阅文字同样明确来源证据不足、不得绑定，故 V11 的覆盖未知确认是正确语义。仅将该确认预期 [] 改为 [batch_coverage_unknown] 并更新审阅说明；完整作品/文件指派等预期结构逐项比对未变，全部原始录制文件 byte-preserved，未删案例。gj 相关完整文件 62 passed，5.73 秒，退出 0。证据见 probes/decision-corpus-policy-review-result.json。

新冻结目录 /tmp/rssripple-v11-integration-gk 与 gb 仅差上述三个测试/预期文件，app 运行源码与 ga 已通过版本一致。项目 rssripple-v11-complete-20260920-gk 四服务健康、启动退出 0；完整 runner **session 2584**，日志 /tmp/rssripple-v11-integration-gk.log，manifest /tmp/rssripple-v11-integration-gk-source.json。当前继承此进程，不再轮询已结束的 ga/gb。仍需 runner、应用退出、coverage、导出/清理全部成功，V11 才能验收。

## V11 完整集成 gk 通过（2026-09-20）

冻结副本完整集成 **3142 passed、17 skipped、8 warnings，1699.48 秒，runner 退出 0**。两个应用 SIGINT 后均退出 0；四份覆盖率汇总退出 0，**20013/22482（89.02%）** 达到 85%。导出退出 0，报告位于 /tmp/rssripple-v11-artifacts-gk；2967 个冻结文件哈希全部不变。gb 三处失败对应夹具/加载器/审核规则修订已在本轮全量通过；原始录制图未改写。此前 ga 单元/API 97.49% 的运行代码未变化。

尚需合入前质量复核及主工作树应用，D4/M4/M5 暂不从 TODO 删除；D6 独立原型不在本次验收范围。机器证据见 probes/decision-integration-gk-result.json。

## V11 本地主干接收

54 个运行代码/测试/权威文档文件与冻结验收副本逐字节一致，应用后全仓 Ruff、git diff --check 均通过。五轴复核无阻断项，代码已提交本地 main：`11ee930`；D4/M4/M5 从 TODO 移除。gk 项目清理退出 0。未推送远端，未执行生产数据库迁移；已有 pending 数据必须依照 db-migration.md 审阅并显式应用离线迁移，不能跳过启动保护。D6 原型不在该代码提交中，下一步见 V12。
