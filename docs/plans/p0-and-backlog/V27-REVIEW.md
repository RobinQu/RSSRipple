# V27 阶段审查（未批准合入）

按 code-review-and-quality 五维复核独立候选；本批是 P2 数据库约束加固，不声称已有生产数据损坏。

- 正确性：固定哈希录制资源身份配合显式合成工作 FK 组合。新旧双库全组合与历史违规/定义漂移/安装故障 134 项通过；缺失 audio 列的实际升级、Turso FK 表重建、一次 UPDATE 切换工作类型与 SET NULL 删除两项通过；真实 associations/metadata/rehome 新旧双库 12 项通过。失败安装保留资源全部值与此前保护定义，已创建的首段 DDL 随失败回滚。扩大 i 为 225 passed/1 failed，唯一假连接目录检查失败已在 j 全迁移文件 50 passed 中修正。
- 可读性：名称和三字段 CASE 计数表达式集中在模型；安装器负责历史审阅与保护定义检查，启动编排拥有事务。PostgreSQL 输出的加法括号可归一化，但不能将比较运算挪入求和后误判为等价定义。
- 架构：collection 不参加互斥，franchise 形态仍由业务层负责。PostgreSQL 复用启动 advisory lock 与有界 DDL 锁等待；Turso 在列补齐后单独开启普通 BEGIN，避免 CONCURRENT DDL 失败。新库原生 CHECK 无需重复触发器，旧库使用触发器避免本批为互斥单独重建表；后续既有 FK 修复保留触发器。V25 对父键屏障的修改仍须在正式重基后共同验证。
- 安全：仅操作唯一 Compose 项目 `rssripple-v27-legacy-c` 与临时文件库；SQL 数据绑定，无生产或外部元数据访问，无新增依赖。异常只列资源 ID/字段名，既有违规不自动删除或选边。专用项目已清理，容器/网络/卷均为空。
- 性能：每行写入保护只检查固定三个字段；升级审阅会扫描历史行，PG 需要表锁，尚无生产规模时延结论。此前轻量迁移可能独立提交，不能把新保护安装回滚描述为整个启动流程原子回滚。

Required：继续覆盖同名原生 CHECK 漂移和 PostgreSQL NOT VALID 的实际历史验证；V25/V26 接受后重基并复核作品父键屏障与 FK 重建；同步最终权威文档；完成全量单元/API ≥95% 与唯一项目完整集成 ≥85%，零失败，skips/hash/exit/cleanup 审计。任一项未完成均不得关闭 TODO。

b 新库原型为 64 passed；c 旧库缺保护两失败，d Turso CONCURRENT DDL 一失败，均已归档并作为修复依据。e 两项最小升级通过；f 134 项、g 两项、h 12 项分别退出 0。正式源、日志、JUnit 及阶段结果见 `probes/resource-work-fk-*`。当前不是完整验收，**不批准合入运行代码。**

最新十一文件候选与审计见 `probes/resource-work-fk-j-result.json`；i 失败和 j 修正结果分别保留，无缓存全仓 Ruff 通过，当前无 V27 后台进程。后续仍按上述 Required 推进。
