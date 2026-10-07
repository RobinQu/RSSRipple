# V27 阶段审查（未批准合入）

按 code-review-and-quality 五维复核新库原型；本批是 P2 数据库约束加固，不声称已有生产数据损坏。

- 正确性：固定哈希录制数据加显式合成组合，在两后端直接 INSERT/UPDATE 验证全部 8 种工作 FK 组合及 collection 共存。a 32 失败/32 对照，b 64 通过；失败插入无残留、失败更新保留原值。当前只证明新表 CHECK，旧表尚未升级。
- 可读性：约束名称与 SQL 表达式集中在 FileResource 模型，三个 CASE 非空计数清楚表达至多一个目标；修正过期 collection 互斥注释。
- 架构：工作身份互斥属于表状态不变量，数据库约束覆盖直接 SQL。collection 不参加约束，franchise 形态仍由业务层负责。既有迁移路径、真实写入时序与后续表重建必须继续验证。
- 安全：数据库仅为唯一 Compose 项目及临时 Turso 文件，已清理；所有测试值由 SQLAlchemy Core 绑定，未访问生产、未获取外部 torrent/metadata，无新增依赖。
- 性能：每行检查固定三个字段；尚未测旧库升级扫描、锁时间与写入影响，不能从新库矩阵推断升级成本。

Required：完成旧库升级/幂等、同名保护漂移、历史违规审阅和失败回滚；验证真实 associations/metadata/rehome、工作切换与删除、表重建；同步最终权威文档；V25/V26 接受后重基；完整单元/API ≥95% 与唯一项目完整集成 ≥85%，零失败，skips/hash/exit/cleanup 审计。任何一项未完成均不得关闭 TODO。

b 结果：64 passed，51.68 秒，退出 0；专项无缓存 Ruff 通过。六文件候选、双方哈希及报告见 `probes/resource-work-fk-b-*`。PostgreSQL 项目 `rssripple-v27-necessity-a` 已 down，容器/网络/卷标签为空。当前无 V27 后台测试或测试栈，下一步从旧库升级开始。**不批准合入运行代码。**
