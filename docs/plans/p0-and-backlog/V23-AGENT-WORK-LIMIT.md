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
