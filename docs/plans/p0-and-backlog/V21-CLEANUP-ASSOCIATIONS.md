# V21：过期资源清理保留权威关联

状态：必要性已复现，独立原型 `/tmp/rssripple-v21-cleanup-fs3vtp33`，基于已验收 main 运行代码 `b0e2ce1`；未合入、未完整验收。S1 ah/ai 门禁期间未修改其冻结目录。V21 六文件候选/基线哈希见 `probes/cleanup-associations-f-source.json`，可从同名前缀 candidate.tar.gz 恢复。

## 必要性与优先级

原清理条件只检查 series/movie/audio FK、metadata_matched_at、manual 集号和 DownloadTask，遗漏 collection_id 与权威关联/指派。生产 `park_resource_on_collection` 会形成合集待确认状态，`apply_association_update` 保存多个作品后清空主表作品 FK，且不保证写 matched 时间或 manual 集号。因此不能把这些字段皆空直接等同于未处理。

a 六项失败源于测试漏填 Channel.field_mapping，不能当缺陷证据。修正夹具后 b **2 passed / 4 failed，9.45 秒**：自动/手动清理正常删除未处理对照，但均误删挂合集资源及由实际编辑向导保存的多作品资源，后者两条 work-links 和 27 条文件指派随之级联删除。测试使用未修改的录制 torrent `912c2bd9bd70a9556cfaa974cd29d0f1748c05e26dbf6ae87453418a7f801ef1`；工作身份、年龄、频道设置及人工选择季 1 为合成输入，不宣称线上实际误删统计。媒体未下载。

本项继续按 P2 推进：自动清理默认不启用，风险要求显式启用或手动触发；但已证实会丢失人工关联，所以在其余 P2 中优先处理。不存在“仅补一个 NULL 条件即可关闭”的充分证据。

## 方案与证据

排除已有合集、work-links、绑定到作品的文件指派或人工指派。未绑定 auto/llm 文件分析仍允许清理，否则一次失败的内容分析会使资源永久豁免。保留原年龄/开关/已匹配/人工集号/下载保护，force 仅绕过频道开关，不绕过数据保护；服务仍由调用方提交。

c 原型和既有清理回归 **21 passed，30.89 秒**。d 增加六个 PostgreSQL 场景及五个指派边界，**32 passed，39.15 秒**。e 再以 PostgreSQL 两会话执行生产编辑向导，观察真实 FK 锁等待，得到 **1 failed，0.65 秒**：仅加 NOT EXISTS 仍会在等待写事务提交后按旧快照 DELETE，实际返回 deleted=1。

因此 PostgreSQL 增加两阶段：按最多 500 行 FOR UPDATE SKIP LOCKED 获取候选父行，跳过持有 FK 锁的编辑事务；随后新 DELETE 语句重新检查全部条件，父行锁阻止新子关联写入。依赖应用默认 READ COMMITTED，未改事务隔离级别，不在 service 内 commit。Turso 保留完整条件的单条删除。f 双库/边界/真实竞争/旧回归 **33 passed、1 warning，16.10 秒，退出 0**。独立 PG 容器 rssripple-v21-cleanup-pg-d 已停止，--rm/tmpfs 清理；每个 scratch DB 在 fixture finally 删除。全部红/绿日志及 JUnit 压缩原件、机器摘要均保存在 probes/cleanup-associations-*。

## 严格验收与续接

- 补 PostgreSQL 超过单批上限、锁定资源与普通资源混合、事务回滚及锁释放验证；继续检查新语句快照和 Turso 并发提交边界，不把 f 单个竞争用例扩大为所有时序安全。
- 验证手动 API 与真实 scheduler 入口的事务行为，核对清理计数与父子行保留；生产种子缓存保持原字节。
- S1 验收合入后将候选对齐最新 main；权威业务/API/集成文档已在 V21 候选同步，但尚未应用主干。
- 五维审查后冻结完整候选，执行完整单元/API ≥95% 和唯一隔离集成 ≥85%，零失败，核验新增跳过、源哈希、正常退出、四路覆盖率、导出和清理。当前专项绝不替代完整门禁，TODO 不删除。
