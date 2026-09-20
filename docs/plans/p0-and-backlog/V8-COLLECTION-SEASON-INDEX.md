# V8：合集内单季唯一约束（P1-D1）

## 必要性与边界（2026-09-20）

真实 Turso 的生产 `create_tables()` 路径允许同一合集出现两个 S0 或 S1 作品。四项复现分别覆盖新装库（先删除全部模型表，再启动）和已有表缺索引的升级库；均实际写入重复作品，未抛出 IntegrityError。保持 P1：它破坏单季身份与关联的唯一性，但本轮没有证明文件丢失等 P0 后果。

当前权威设计把部分唯一索引留给离线单季迁移；新装库没有历史数据需要收敛，却同样缺索引。仅在 ORM 声明索引能修复新装库，不能补齐已有表，已由两项升级红测验证。D1 不能仅凭模型声明或 schema 文本检查关闭。

## 独立原型

目录 `/tmp/rssripple-v8-index-review` 的 app 是独立复制，未改动根工作树或正在运行的 V6/V7 源码。可恢复补丁见 [prototype](probes/collection-season-index-prototype.patch)，哈希及结果见 [result](probes/collection-season-index-result.json)。补丁以 V6 当前工作树为基线；与 V7 的 database.py 修改合并时必须核对上下文。

1. ORM 声明 `uq_tv_series_collection_season`，Turso/PG 均采用 `WHERE collection_id IS NOT NULL`；S0 与普通季一样受保护。
2. 轻量迁移在补齐 season_number 后幂等创建同名索引，补齐已有表。
3. 冲突升级暂采用数据库拒绝建索引、启动事务回滚的原型行为，不自动删除、合并或重新分配历史作品；实际测试确认两个人工编辑作品的 ID、标题、合集、季号和保护字段均保留。**这会使有重复数据的库无法启动，部署前必须完成迁移/修复；最终方案须补充可操作诊断和恢复步骤，不能直接发布当前原型。** 不以捕获异常后继续无约束运行冒充 D1 已修复。
4. NULL 合集仍在部分索引之外，消除孤儿由 V7 负责；本轮不把部分索引称为 NOT NULL 约束。

## 当前证据

| 版本 / 选择集 | 真实 Turso 结果 |
|---|---|
| 原实现，新装/升级 × S0/S1 | 4 failed，8.88 秒；重复写入未被拒绝 |
| 仅模型索引，同一四项 | 2 passed、2 failed，8.84 秒；升级仍失败 |
| 模型＋轻迁移，同一四项 | 4 passed，8.78 秒 |
| 增加历史冲突与人工编辑保护 | 5 passed，10.37 秒 |

所有数据为明确标注的合成作品，数据库及启动路径真实执行。每项正例同时验证其他季、其他合集及两个 legacy NULL 父项可写，重复写入在 SAVEPOINT 中被拒绝，重跑启动幂等。这里没有声称使用真实来源 API 录制数据，PostgreSQL 续验结果见下。

## 验收前剩余工作

- PostgreSQL 已补真实新装/升级双进程启动和同季竞争写入；后续与 API 竞争回归合并。所有 Compose 显式唯一项目名。
- 已补已有合法作品、Episode 与身份袋的 Turso/PG 升级保留测试；仍需结合完整迁移回归。
- 审计创建/编辑/合集指派和 metadata upsert 的冲突处理：违反同季唯一性应按 API 409 契约返回，事务必须完整回滚；不能让数据库约束变成未处理 500。结合 V7 有序父行锁验证跨进程竞争。
- 审计离线迁移及测试中的 legacy 多季/同季数据：迁移前旧 schema 夹具应明确去除索引，不为兼容非法新数据而弱化生产约束。
- 完善冲突库部署前诊断与恢复 runbook，验证启动失败没有部分迁移落库；不自动选择人工编辑作品的幸存者。
- 同步 data-models/per-season-works/db-migration/API 权威契约及测试清单，按 code-review-and-quality 复审，再运行完整单元/API ≥95%、隔离集成 ≥85%；测试和覆盖率均退出 0，源码哈希不变，应用优雅退出后导出证据并清理。

当前仅原型，不关闭 TODO，也未合并 main。

## 双库续验（2026-09-20）

Turso 新增启动前已存在 S3 作品、Episode 与身份袋的实际升级保留用例，扩大 **6 passed、1 warning，11.04 秒，退出 0**。升级后再次插入同季被拒绝，原 ID、合集、标题、季号、人工编辑保护及关联均保持。

独立 PostgreSQL 项目 `rssripple-v8-index-20260920-ao` 验证通过，驱动退出 0：新装和有数据升级均分别启动两个真实 Python 进程调用生产 create_tables，四个进程全部退出 0；S0/S1/S3 重复写入为 23505；竞争 S5 先观察到 pg_blocking_pids 非空，再提交第一事务，第二事务以 23505 回滚，只保留第一作品。冲突旧库启动失败后完整保留所有作品行；未猜测合并。项目已 down -v 清理。驱动及机器结果见 [PG probe](probes/collection_season_pg_probe.py)、[PG result](probes/collection-season-pg-result.json)。

此结果未覆盖 API 冲突响应、旧数据迁移 runbook 或完整门禁，仍不关闭 D1。
