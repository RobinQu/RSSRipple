# V24：重新解析入队与确认隐藏状态

状态：必要性复核、红测及独立持久请求候选已完成首轮专项；main 运行代码未改。独立副本 `/tmp/rssripple-v24-reparse-54_450v_`，运行基线已验收 V21 `938a7fb`，不含未验收 CORS/B6。

## 必要性与优先级

保留 P2（原 P1-B8）。当前 endpoint 先把 confirmation_ignored_at 写为当前时间并提交，再入队；handler 完成后清空。dashboard 的用户主动忽略也写同一字段。因此不能把所有非空标记当成“孤立任务”，也不能看见 409 就认定没有后台工作。

a 使用真实资源 API、per-test Turso 与新会话查询；队列结果明确注入。成功接受对照 1 passed；明确未接受任务即 ConnectionError 后，资源仍隐藏，1 failed，合计 4.19 秒。两例的 enqueue 回调均从独立会话看到标记已提交，证明必须保留 commit-before-enqueue。失败不代表已经验证 Redis 接受后断网、进程崩溃或全部 409 路径。资源与队列输入明确合成，不涉及真实用户元数据。

## 方案复核

- 明确提交前/接受前失败、已接受但响应丢失、已有活跃任务返回 None、任务完成与新提交竞争、进程在 DB commit 与 enqueue 之间崩溃等不同状态。不能将未知接受结果等同无任务。
- 标记需要具有可比较的任务归属；失败恢复只能处理本次仍拥有的状态，保留原有人工忽略或后续用户操作。当前 dashboard 在标记非空时不再写新时间，单纯 timestamp CAS 不能辨认所有后续人工忽略意图，需复核是否分离人工忽略与临时重解析状态。
- handler 在 try/finally 之前会 refresh config 与检查队列执行权，不能只增加 finally 分支就宣称全部异常收尾已覆盖。旧执行者不得清除新任务的标记；没有执行权时不能强行写数据库。
- 对 commit→enqueue 崩溃窗口，需要持久化可恢复提交意图或按权威队列状态回收机制；恢复任务必须有界、可重入，并保护未知/活跃执行。方案选择须结合现有队列的 job_id、稳定 key 与租约语义，不以一个新布尔字段代替生命周期设计。

## 严格验收

以真实临时数据库、生产 API/handler/队列为主，分别补 Memory/Redis 的接受与失败边界；真实 Redis 网络失败/重启探针与合成注入明确分开。覆盖原人工忽略、后续忽略、重复请求、完成/新提交交错、过期执行权、崩溃恢复，以及失败后 dashboard 可见性（不只检查字段）。保留提交先于入队的实际观察。同步 API/业务/队列/数据模型文档，五维审查后完整单元/API ≥95%、独立集成 ≥85%，零失败并审计跳过/哈希/退出/清理。当前不得关闭 TODO。

## b/c：接受边界与人工忽略（2026-10-07）

b 使用真实 MemoryQueue 与独立 Redis（consume=False）完成四例：正常接受、接受后注入响应丢失，两种后端均保留同一 queued job；随后重复提交返回 409，未创建第二个任务。**4 passed，8.13 秒**。这里的接受是真实队列写入，响应丢失仍为合成异常，不宣称真实网络故障或 worker 恢复已验证。测试专用 `rssripple-v24-redis-b` 已停止并自动删除。

c 使用真实 API、Turso 和生产 handler 的 finally，替换元数据处理、配置刷新与队列所有权检查以隔离状态生命周期。两例人工忽略分别发生在重解析前、元数据处理期间，任务成功收尾后人工忽略均被清空；无人工忽略对照通过。**2 failed、1 passed，5.31 秒，退出 1**。这是新增缺陷证据，不能当作验收通过；还须用真实所有权和并发进程扩大验证。原始日志、JUnit、探针源码与属性见 [b/c 结果](probes/reparse-submission-b-c-result.json)。

这些证据排除“所有 enqueue 异常清标记”和“finally 无条件清标记”两种方案。下一步候选采用独立的持久重解析请求身份，人工忽略字段只由人工操作维护；同资源重复提交不替换现有请求，任务携带请求 ID，收尾只能确认自身请求。提交后再唤醒队列，DB 提交至唤醒的崩溃窗口由有界恢复扫描补发。请求记录与队列状态的关联、回退展示和故障终态尚须实现前论证，尤其不能用超时猜测任务已退出。

实现前需明确以下边界：

- 未提交请求时绝不入队；持久请求创建成功但队列不可达时，明确 API 是返回可恢复 pending 还是失败并显示待确认，前端响应与文档必须一致。不能仍返回失败却永久隐藏。
- 稳定 resource key 可能对应上一请求尚未退出的任务；补发受到 dedup 时保留当前请求，不能确认或删除它。请求 UUID 防止旧 finally 清除新请求。
- 所有权未知/丢失时不写确认；配置刷新失败、队列终态、worker 崩溃、Redis 状态过期必须分别验证，状态缺失不等于权威退休。
- 迁移无法从旧 confirmation_ignored_at 分辨人工忽略与历史重解析隐藏，禁止批量清空。需提供只读盘点与保守迁移规则，不伪造历史归属。
- 新表遵循 UUID/UTC、resource FK 级联与单资源唯一约束；恢复批次与退避必须有界，成功/失败收尾后 dashboard 按人工忽略和确认规则重新计算。

## f：真实 TCP 拒绝与 dashboard 可见性

为避免仅凭异常注入推断实际 Redis 故障，探针在回环地址绑定临时端口但不监听，由真实 Redis 客户端与生产 enqueue 发起连接，内核返回 ECONNREFUSED。未连接、停止或修改现有 Redis 服务。提交前生产 dashboard 分页函数确认资源可见；调用重解析实际发生连接拒绝后，数据库忽略标记保留、同一 dashboard 查询看不到该资源。**1 failed，3.40 秒，退出 1**，失败断言为“失败后应仍可见”，不是连接异常未捕获。该测试使用合成资源，仅证明实际连接拒绝，不涵盖接受后网络断开或 Redis 重启。

之前 d/e 两次探针误读 dashboard 返回结构（实际是 resource.id）导致 KeyError，均保留日志但不计作缺陷证据。f 已修正结构并通过前置可见性断言，结果、原始日志和源码见 [d–f 审计](probes/reparse-submission-d-f-result.json)。这补强了 a 的合成异常结果；上述持久请求/人工状态分离方案仍需实现及完整验收，B8 保留未完成。

## g/h：独立持久请求候选

候选引入独立 resource_reparse_requests 表，单资源唯一、UUID 请求身份、UTC 请求/补发时间、故障次数及固定脱敏错误类别。API 先提交请求，再唤醒稳定 resource key；重复请求保持原 ID 并返回 409。响应 pending 表示数据库已接受请求，队列不可达时仍保留可恢复意图，同时恢复待确认展示（人工忽略仍有效），而不是返回请求失败却永久隐藏。

handler 只确认 payload 的 request_id，旧请求直接 superseded；配置刷新纳入普通失败收尾，失权时保留请求。人工忽略始终不被后台清理。每 5 秒有界认领最多 50 项，认领事务先结束再入队；异常退避 30 秒至 1800 秒，未知队列结果不删除请求。遗留无 ID 载荷继续运行但不猜测旧忽略字段归属。权威子文档在候选内同步，尚未合入 main。

g 九项真实 API/Turso 生命周期回归全部通过（19.33 秒）；h 加入既有资源 API、遗留载荷及失权回归，**45 passed、1 warning，78.90 秒，退出 0**。h 前 lint 指出一处 import 排序，结束后仅修正排序；此后候选哈希以 [h source](probes/reparse-submission-h-source.json) 为准。源码归档 [h candidate](probes/reparse-submission-h-candidate.tar.gz)。

这些结果不证明进程崩溃、真实 Redis worker 或双库并发已经验证。下一轮先补这些边界、迁移/回滚和历史标记只读盘点，再加入录制元数据回放、完整门禁与最终五维审查。当前原型未合入，TODO 继续保留。

## i–o：真实 Redis worker、双库重叠提交与补发边界

i 首轮真实 Redis 消费者 1 pass、2 timeout；API conftest 会把 ≥1 秒 sleep 直接跳过，导致心跳高速更新参与 WATCH 的租约键。j 在该探针中恢复正常 asyncio sleep 后，**3 passed，6.10 秒**。独立真实 producer/worker 跑生产 handler 和实际 Redis token/active/consumer 租约检查，成功、合成元数据失败及真实接受后注入响应丢失三例均确认自身请求并保留处理期间人工忽略。不是进程崩溃或真实网络断开的验收。专用 Redis i/j 均已停止并自动删除。

k 原并发探针强求数据库立即抛冲突异常，遇到唯一插入等待后超时；不把该超时计作产品缺陷。l 改为观察两个实际写入重叠，**1 passed，2.38 秒**：Turso API 返回 200/409，仅一行请求、一次入队。m 的 PostgreSQL 探针遗漏必填 torrent_url，属夹具错误；n 补齐后 **1 passed，0.76 秒**，实际 pg_blocking_pids 证明第二请求在数据库等待，提交后同样为 200/409、一行、一次入队。每测独立 PG 库已删除，专用 PostgreSQL 容器退出并自动删除。

o 扩大生命周期验收 **12 passed，25.32 秒**，新增每批最多 50 项且下批处理余项、认领提交先于投递、故障退避 30→1800 秒封顶、不持久化异常中的凭证、成功清错误、完成确认早于入队响应时不复活请求。所有结果与失败尝试见 [i–o 审计](probes/reparse-submission-i-o-result.json)，19 文件候选见 [o source](probes/reparse-submission-o-source.json)。

下一轮须将当前位于 API 测试目录的 Redis 探针整理为完整集成默认收集的专用 harness（避免全局 sleep 补丁影响消费者），补实际失权/进程崩溃恢复、升级回滚及历史标记盘点、录制元数据回放，再执行完整双门禁。当前仅独立候选，仍不可关闭 B8。

## p–t：正式集成、实际崩溃与双库升级

已把 Redis worker 探针迁至 `tests/integration/resources/`，自有 PG/HTTP 夹具不导入 API conftest 的 sleep 加速。默认集成收集包含本组；严格门禁缺少显式测试 PostgreSQL/Redis 即失败。每测创建并删除 PG scratch DB；Redis 使用专用测试服务 DB14，仅删除本例已知键，不清空其他 DB。p 四项正式集成通过，2.64 秒。

q 两项实际子进程崩溃恢复通过，3.82 秒：提交请求后 os._exit(97)，新扫描补发；worker 已持有真实 Redis 执行权并进入元数据替身后 SIGKILL(-9)，等原消费者租约到期，新 worker 恢复同一 Redis job_id 并确认请求。两例人工忽略均保留；元数据提供者仍为明确合成替身，不是录制管线回放。

r 升级首轮 PostgreSQL 通过，Turso 夹具的主库 reparse.db 与侧库 reparse.fts 同 stem 导致日志路径冲突，失败保留、不计作通过。s 将侧库改为生产同款不同 stem reparse_fts.db 后，**双库两项通过，2.66 秒**。调用真实 create_tables 两次，验证请求数据与历史人工标记保留、数据库唯一约束、请求事务 rollback 和 FK cascade；不将事务 rollback 宣称为应用版本回滚。

t 对整个正式目录重跑，**8 passed，8.23 秒，零跳过、退出 0**。专用 PG 中 scratch DB 查询为空，PG/Redis 两容器均停止 0 并自动删除。原始日志/JUnit/属性见 [p–t 审计](probes/reparse-submission-p-t-result.json)，候选见 [t source](probes/reparse-submission-t-source.json)。源码仍基于 V21，基线哈希保留原记录；后续须与已合入 CORS 及待验收 B6 重基，尤其不能直接覆盖新权威文档。

下一步：历史标记只读盘点/保守迁移策略、部署回滚兼容、录制元数据管线回放、组合基线与完整双门禁；B8 未合入、未关闭。

## u–w：录制标题/种子管线回放与历史盘点

新增 `test_reparse_captured.py` 使用原语料 case `affd5114-f61d-40b6-95f1-0d0b937495ab` 的真实已录制标题和 torrent（SHA-256 `6936d731675641e065b7af4c3780b3c36c27b2782e65fc3989a6557b0cff75c3`）。没有 RSS 原文时不宣称有 RSS 原文。生产 handler 与 `_process_resource_metadata`、缓存校验、合集分析、文件指派、元数据 publication、提交均实际执行；仅队列投递与外部身份提供者使用替身（身份未匹配），不下载媒体。

u 首轮双库均失败：夹具没有写入创建 publication，生产事务按不变量回滚；失败断言检出持久字段未更新，不能只看 handler 返回 done。v 补齐规范创建发布后 **2 passed，2.64 秒**：12 文件路径、集数、大小及原种子哈希保持，season batch 范围 1–12，任务确认后未匹配资源重新进入待确认。w 连同升级/只读历史标记游标盘点共 **4 passed，5.01 秒**。只读报告不把人工忽略或未知旧任务标记误判为可清理。专用 PG u/w 及其 scratch DB 已清理。结果见 [u–w](probes/reparse-submission-u-w-result.json)，24 文件候选见 [w source](probes/reparse-submission-w-source.json)。

候选迁移文档已补保守历史盘点 SQL 和回退前提：先停止新提交，排空持久请求及活跃队列，再停新 worker；回退保留新表、不批量清除忽略。该 runbook 尚不等于实际部署回退验收。下一轮将候选对齐已合入 CORS `69e931d` 与 B6 `accd024`，完成回退兼容验证、最终审查与完整双门禁。
