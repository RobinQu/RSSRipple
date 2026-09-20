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
