# V21：过期资源清理保留权威关联

状态：必要性已复现，独立原型 `/tmp/rssripple-v21-cleanup-fs3vtp33`，基于已验收 main 运行代码 `b0e2ce1`；未合入、未完整验收。S1 ah/ai 门禁期间未修改其冻结目录。最新 V21 十四文件候选/基线哈希见 `probes/cleanup-associations-u-source.json`，可从同名前缀 candidate.tar.gz 恢复。

## 必要性与优先级

原清理条件只检查 series/movie/audio FK、metadata_matched_at、manual 集号和 DownloadTask，遗漏 collection_id 与权威关联/指派。生产 `park_resource_on_collection` 会形成合集待确认状态，`apply_association_update` 保存多个作品后清空主表作品 FK，且不保证写 matched 时间或 manual 集号。因此不能把这些字段皆空直接等同于未处理。

a 六项失败源于测试漏填 Channel.field_mapping，不能当缺陷证据。修正夹具后 b **2 passed / 4 failed，9.45 秒**：自动/手动清理正常删除未处理对照，但均误删挂合集资源及由实际编辑向导保存的多作品资源，后者两条 work-links 和 27 条文件指派随之级联删除。测试使用未修改的录制 torrent `912c2bd9bd70a9556cfaa974cd29d0f1748c05e26dbf6ae87453418a7f801ef1`；工作身份、年龄、频道设置及人工选择季 1 为合成输入，不宣称线上实际误删统计。媒体未下载。

本项继续按 P2 推进：自动清理默认不启用，风险要求显式启用或手动触发；但已证实会丢失人工关联，所以在其余 P2 中优先处理。不存在“仅补一个 NULL 条件即可关闭”的充分证据。

## 方案与证据

排除已有合集、work-links、绑定到作品的文件指派或人工指派。未绑定 auto/llm 文件分析仍允许清理，否则一次失败的内容分析会使资源永久豁免。保留原年龄/开关/已匹配/人工集号/下载保护，force 仅绕过频道开关，不绕过数据保护；服务仍由调用方提交。

c 原型和既有清理回归 **21 passed，30.89 秒**。d 增加六个 PostgreSQL 场景及五个指派边界，**32 passed，39.15 秒**。e 再以 PostgreSQL 两会话执行生产编辑向导，观察真实 FK 锁等待，得到 **1 failed，0.65 秒**：仅加 NOT EXISTS 仍会在等待写事务提交后按旧快照 DELETE，实际返回 deleted=1。

因此 PostgreSQL 增加两阶段：按最多 500 行 FOR UPDATE SKIP LOCKED 获取候选父行，跳过持有 FK 锁的编辑事务；随后新 DELETE 语句重新检查全部条件，父行锁阻止新子关联写入。依赖应用默认 READ COMMITTED，未改事务隔离级别，不在 service 内 commit。当时 Turso 保留完整条件的单条删除（下文 k/l 并发证据证明仍须父行写屏障）。f 双库/边界/真实竞争/旧回归 **33 passed、1 warning，16.10 秒，退出 0**。独立 PG 容器 rssripple-v21-cleanup-pg-d 已停止，--rm/tmpfs 清理；每个 scratch DB 在 fixture finally 删除。全部红/绿日志及 JUnit 压缩原件、机器摘要均保存在 probes/cleanup-associations-*。

## 扩大验证与 Turso 并发方案复核（g–u）

g PostgreSQL 8 项通过，新增 501 行跨批次、被锁行跳过、完整回滚和父锁释放验证；按主键 keyset 推进批次。h 17 项通过，新增真实 API 与 scheduler 提交入口，保留录制 torrent 原字节。

i 的 SELECT 未固定物理快照，属于夹具错误；j 用 SAVEPOINT 固定后通过。k/l 用真实先写事务固定快照，另一会话提交关联，再清理：实际提交后出现 `(deleted=1, parent=None, child_count=1)`，没有锁错误触发重试。这证明仅依赖完整 DELETE 条件与现有 retry_on_lock 不充分，不能按单条 SQL 原子性推断安全。

m/n 探针证明子表写入时对父行做等值 UPDATE 可触发 Turso MVCC 冲突并让现有事务级重试重新读取；id=id 不改变业务字段或时间。原型在 Turso 的 resource_work_links、resource_file_assignments、download_tasks 安装 INSERT/UPDATE 六个触发器，固定内部表名，不接收用户 SQL。fresh metadata after_create 与升级入口均幂等安装；PostgreSQL 不安装。p 的 12 类真实双会话场景覆盖三种子表、插入/移动关联、SAVEPOINT/先写快照，连同安装回归共 13 项通过。q 综合清理与迁移单元回归 103 项通过。

r 暴露父表重建时触发器引用临时不存在的表，26 passed / 2 failed；修复为同一 DDL 事务内移除并恢复父行屏障。s 升级/并发组合 41 项通过。t 新故障回滚测试因 Turso PRAGMA foreign_key_check 不返回结果集而失败，非数据丢失；u 改用 FK catalog 与显式孤儿查询，2 项通过，确认中途异常恢复原触发器及父子数据，成功重建后约束也保持。历史孤儿数据修复不在本原型实现范围。

原始 g–u 日志/JUnit 与逐轮结果见 `probes/cleanup-associations-g-u-results.json`。历史 test-only 写屏障探针归档为 `cleanup-parent-fence-probe.py.txt`，其中导入名称来自当轮候选，不作为最新可运行测试入口。独立 PG 容器 rssripple-v21-cleanup-pg-g 已正常停止并随 --rm 清理。候选全仓 Ruff 通过。

## 严格验收与续接

- S1 验收合入后将十四文件候选对齐最新 main，保留 S1 同文件变更；权威业务/API/数据模型/迁移/集成文档已在候选同步，尚未应用主干。
- 五维审查必须包含新增 Turso DDL 生命周期、升级回滚与事务重试范围；冻结后执行完整单元/API ≥95% 和唯一隔离集成 ≥85%，零失败，审计跳过、哈希、退出、覆盖率、导出及清理。
- 当前专项不能替代完整门禁；本项仍为 P2 未完成，不从 TODO 删除。
