# V23：Agent 更新后的有效作品上限

状态：必要性复核与实际 API/Turso 红测完成，尚未修改运行实现。副本 `/tmp/rssripple-v23-work-limit-ji74j3mq`，运行基线 S1 `6bf8c1c`；不修改 V21/V22 候选。

## 约束和优先级

保留 P2（原编号 P1-B6）。权威 constraints.md、data-models.md 规定仅 scope_channel_wide=false 时 AgentWork 最多 10 条。不能把频道全范围保留 11 条覆盖配置本身当作错误，也不能只对 works payload 数组加 max_length=10，因为那会破坏有效的全范围例外。

当前 create/add-work 有条件限制，而 PUT agent 在替换作品或改变 scope 后未检查有效状态。测试通过真实生产 router 与 per-test Turso，种入 11 个明确合成 Movie，经 API 创建再更新，并重新查询数据库计数、scope/name。这里只验证列表计数与事务，不需要录制 feed/torrent；频道 URL 校验及后台服务使用既有 API 测试夹具，未访问外部服务，也不把此称为完整集成门禁。

a 四项均因测试漏传 content_type 在创建阶段失败，非缺陷证据。修正后 b 2 passed / 2 failed，6.89 秒：限定范围替换为 11 条、全范围已有 11 条再切回限定范围，均返回 200 并持久化 11 条；合法替换 10 条和切到全范围保留 11 条均成功。数据库观察属性保留在 JUnit 与 probes/agent-work-limit-b-result.json。

## 修复方案与严格验收

1. 在任何字段、作品删除或回填副作用前，按本次 scope 与 works 参数和原状态计算更新后的有效范围/数量；若限定范围超过 10，返回既有 422 VALIDATION_ERROR，保留全部原字段、链接、水位线与队列状态。
2. works 未传、null、空数组，以及 scope 未传/切换需要分别检查，不能依赖 truthiness 丢失空数组语义。允许超限存量通过删减或切至全范围修复，不自动删订阅。
3. 复核 create/add-work/update 的并发不变量：同一 Agent 两次 add 或替换/切范围可能各自观察到旧计数。先建立真实双库并发证据，再决定父行锁/事务冲突重试的共同策略，不能把单个 PUT 检查当作全部入口已安全。
4. 补真实生产 API 提交与新 session 读取、回填/入队未发生副作用、并发和合法边界。权威 API/业务约束同步更新；五维审查后执行完整单元/API ≥95%、隔离集成 ≥85%，零失败、跳过/哈希/退出/清理审计。当前问题保持 TODO。

## 顺序更新原型与实际并发红测（c/d）

候选在任何字段、作品删除、回填或进度副作用前检查更新后的有效 scope/works，兼容未传/null/空数组。c 7 passed，11.95 秒；拒绝超限时原 name、scope、订阅数保持不变，允许同时缩减至 10 或清空再切限定范围。Ruff 已通过。该部分尚未同步权威文档或验收合入，因为并发安全仍未满足。

d 加真实双请求测试，控制两请求在实际 flush 前完成 count=9 判断，不替换 SQL 结果或锁异常；最终两次添加成功、持久化 11 条，7 passed / 1 failed，13.65 秒。已有 `_lock_agent_rules` 在 PostgreSQL 实际编译为 FOR NO KEY UPDATE（不是 KEY SHARE），但 Turso 不提供同样的行锁。没有 AgentPublicationProgress 行时 invalidate_running_scope 不能提供共享父行写冲突，因此不可将后续更新存在当作充分保护。

下一步在 Turso 的现有规则锁入口建立父行版本写屏障，并以真实冲突及整请求重试验证 add/add、replace/add、范围切换；PostgreSQL 需保留现有锁语义并跑实际双会话测试。并发测试的时序协调必须同时支持“第二请求已在真实锁/冲突处被阻止”的正确实现，不能用强制两个请求均进入插入阶段造成夹具死锁。源码/基线和未验收两文件候选见 `probes/agent-work-limit-d-*`。

## Turso 写屏障与 HTTP 重试请求体缺口（e）

原型在现有规则锁入口对 aioturso 执行参数化 `UPDATE agents SET id=id WHERE id=:id`，读取订阅前触碰共享父行版本，不改变时间字段；PG 分支保持 FOR NO KEY UPDATE。并发夹具允许第二写事务真实冲突时释放第一事务，不伪造 DatabaseError。e 前七项通过，但并发请求未退出。

独立三秒诊断使用生产 install_db_retry_middleware 与 FastAPI/HTTPX：端点第一次读取 JSON 后主动抛合成 Write-write conflict，第二次进入端点却不能再读完 JSON；观察 entered/body-read/entered 后真实 asyncio 超时，退出 1。这直接证明现有 HTTP 重试无法重放已消费请求体；合成错误只用于诊断重试机制，不是 Turso 冲突证据。

基于此具体诊断，中止仍运行的 e（SIGINT，退出 2，7 passed/152.13 秒），不将其计为通过或普通完整终态。V21 两套完整门禁不受影响。下一步需在独立副本修复 HTTP 请求体重放，并测试 JSON/空体/不可重试异常/已发送响应/重试上限及取消边界，然后重跑真实并发。不能只在测试手工重发请求来掩盖生产重试缺陷。当前两文件 e 候选与日志、限时诊断源码均已归档；数据库重试运行代码尚未修改。

## 可重放 HTTP 请求与真实冲突绿测（f/g）

独立原型将 DB 重试从 BaseHTTPMiddleware 的重复 call_next 改为纯 ASGI：按需记录实际读取的请求分块，每次重试重放已有块后继续原始流；内存上限 1 MiB，超过后使用私有临时文件，IO 在线程中执行。响应开始、已断连、非可重试异常或达到五次上限时原样传播；取消不吞掉，finally 关闭文件，非 HTTP 直接透传。

原限时探针修复后返回 200，entered/body-read 重复两次。f 真实 API/Turso 8 passed，13.26 秒；并发用例记录真实 DatabaseError 1 次、响应 [201,400]，最终 10 条。g 17 passed / 45 deselected，1.88 秒，覆盖既有重试中间件契约、空体/分块/大于 1 MiB/部分读取、响应开始、断连、取消、非锁错误和次数上限。g 的冲突是合成单元输入，真实冲突证据仅来自 f。

已在候选同步 API/业务/错误处理/测试清单。重试层无法撤销端点此前提交或外部副作用，仍依赖已有幂等和事务边界，不能宣称通用事务安全。九文件源码/基线及候选归档见 `probes/agent-work-limit-g-*`。剩余：PostgreSQL 与 add/replace/scope 混合并发、反向规则确认兼容、请求重放的完整回归、五维审查和完整 ≥95%/≥85% 双门禁。V21/V22 均未混入本候选。

## PostgreSQL 与 Turso 混合编辑并发（h–k）

h 三项因下载器夹具缺 download_dir 失败，非产品缺陷。修正后 i add/add、replace/add 通过，scope/add 未经过显式 flush 暂停点而超时，属于测试协调问题。j 改为取得真实父行锁后暂停第一请求，通过 pg_blocking_pids 观察第二请求等待后才释放，三项全部通过（2.19 秒）；第二请求均 400，最终 10 条且限定范围。scratch 库 fixture finally 删除，专用 rssripple-v23-pg-h 停止 --rm 清理，退出 0。

k 对应扩大 Turso 三种场景：第一请求取得父行写屏障后暂停，第二请求真实可重试 DatabaseError 触发放行，再由生产 HTTP 层重放；add/add、replace/add、scope/add 均保持 10 条，分别 [201,400]/[200,400]/[200,400]。连同顺序边界共 10 passed，16.92 秒；finally 取消并收拢所有未完成测试任务。两种数据库都未伪造 SQL 输出或锁异常。Ruff 通过。

十文件候选/基线和原始 h–k 证据见 `probes/agent-work-limit-k-*`、`-pg-*`。继续检查拒绝操作的完整副作用、既有决策确认兼容；待 CORS 验收后将本候选三方对齐新 main（当前仍基于 S1），执行五维审查及完整双门禁，尚未合入。

## 超限拒绝无副作用与决策范围回归（l/m）

l 同跑作品上限和既有 decision_scope，40 passed / 2 failed，73.32 秒；两失败均发生在夹具读取 DB 生成 updated_at，未 await refresh 导致 MissingGreenlet，尚未发送待验证拒绝请求。修正夹具后仅重跑受影响两项 m，2 passed / 10 deselected，3.51 秒。运行实现未为这次夹具修正变化。

新场景提交超限作品同时变更 name/channel/status 并携带空或非空 dispatch_resource_ids，真实 API 返回 422；重新查数据库确认字段、updated_at、last_consumed_at、原订阅 ID、publication generation/baseline/cursor 均保留，没有 DownloadTask。回填入口使用会抛 AssertionError 的 spy，既有 API fixture 的 queue mock 均未调用；不将这两项 mock 断言描述为真实下载器或队列端到端证据。五维原型审查见 [V23-REVIEW](V23-REVIEW.md)。当前十文件 m 候选归档尚未完整验收，待对齐 CORS 验收主干。

## 完整生产栈的请求重放与回滚（n）

新增 `tests/api/test_request_replay_production.py`，使用实际 app.main 的 Auth/GZip/DB 重试栈与 get_db，生产路由前仅插入临时测试端点，底层为 per-test Turso。小体与大于 1 MiB 的 JSON 请求首次实际写 Movie 并 flush 后注入合成冲突，第二次成功读取相同请求内容，最终新 DB 会话只看到一条记录；大响应保持 gzip。n 2 passed，2.56 秒，Ruff 通过。

这里数据库写入/回滚真实，但冲突为显式注入，不替代 f/k 的实际 MVCC 竞争证据。临时端点与 middleware_stack 在 finally 恢复。十一文件最新候选归档见 `probes/agent-work-limit-n-*`。尚未与未验收 CORS 合并，后续重基必须再验证两类中间件组合及完整门禁。
