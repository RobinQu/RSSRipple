# V9：升级库外键与新装库对等（P1-D2）

## 必要性复核（2026-09-20）

当前 create_tables 的轻迁移为若干外键列只执行裸 ADD COLUMN；表已存在时 create_all 不会补充约束。真实 Turso 首轮复现：新装库拒绝不存在的 collection_id，模拟旧 TVSeries 表经两次生产启动后 PRAGMA foreign_key_list 仍为空，实际非法 INSERT 成功。结果 1 passed、1 failed，3.07 秒。

扫描轻迁移 additions 与当前 ORM 的外键声明得到以下七个候选；完整真实 Turso 新装/缺列升级矩阵确认七个升级均缺约束，新装全部正常。加上实际非法写入用例，共 8 passed、8 failed，9.43 秒。保持 P1：已经证明关联完整性失效，但没有据此宣称生产数据已损坏或直接发生文件丢失。

| 表.列 | 目标 | 删除动作 |
|---|---|---|
| file_resources.audio_work_id | audio_works.id | SET NULL |
| tv_series.collection_id | work_collections.id | NO ACTION |
| movies.collection_id | work_collections.id | NO ACTION |
| downloader_instances.volume_id | storage_volumes.id | SET NULL |
| libraries.media_server_id | media_server_instances.id | SET NULL |
| libraries.volume_id | storage_volumes.id | SET NULL |
| file_resources.collection_id | work_collections.id | NO ACTION |

上述为合成旧表结构、真实 Turso 与生产 create_tables；不是生产库 DDL 录制。旧表只移除待新增列和引用该列的 FK/复合约束，保留其余当前结构。libraries 的 root_path nullable 特殊重建可能在部分历史版本上补齐 FK，不能据此认为所有升级路径都已恢复。

## 方案与独立原型

目录 `/tmp/rssripple-v9-fk-review` 的 app 已独立复制；不改动正在跑 V7/V8 的源码。初步原型仅把七处新增列 DDL 改为显式 REFERENCES，并保持对应 ON DELETE。可恢复补丁及结果在 probes/upgrade-foreign-keys-prototype.patch、probes/upgrade-foreign-keys-result.json。

最终范围必须同时覆盖三类数据库：新装、目标列尚不存在、目标列已存在但缺 FK。当前原型只解决缺列路径；为已经升级过的库新增了单独红测，不能凭新装/新增列通过就关闭 D2。

后续方案：

1. 先核对实际 catalog，比较目标列、被引用表/列和删除动作，不能仅按约束名称或“列存在”跳过。PG 与 Turso 分别以真实数据库验证。
2. PG 补齐既有列约束，并在事务内处理并发启动；存在悬空数据时输出可诊断失败，不自动删/改业务关联。
3. Turso 对既有列缺约束的修复先验证后端支持的 DDL；若需重建表，必须在临时库证明完整保存列、索引、唯一键、未声明的历史列以及所有子表引用。禁止直接复制现有 DROP/RENAME 模式而忽略 RESTRICT/CASCADE/SET NULL 副作用。当前 libraries 重建注释与规则模型的删除动作并不一致，实施前必须以真实带关联数据验证。
4. 升级前只读列出悬空行，保留业务数据与人工映射；修复应基于明确关联证据，不猜测父项。仅列约束不等价于自动修复历史脏数据。
5. 吸收 V8 的启动索引改动时核对基线，不覆盖其 DDL 或预检流程；D1 与 D2 分别验收。

## 严格验收

- 两库 × 七个 FK × 新装/缺列/既有列缺约束的 schema 对等；重复启动幂等。
- 不存在父项的 INSERT/UPDATE 被拒绝；合法关联可保留；实际 DELETE 验证 SET NULL 或拒绝语义，不只检查 catalog 字符串。
- 带 Episode、资源、整理规则/计划及身份关联的数据升级前后 ID/值/人工保护不变；重建不能触发子表清理或置空。
- 悬空旧数据、迁移中途失败、PG 双启动/锁等待及 Turso 回滚后的恢复；不以捕获异常继续启动当作完成。
- 权威数据模型/迁移文档及测试清单同步；最终复审、完整单元/API ≥95%、完整隔离集成 ≥85%，均需测试退出 0、覆盖率导出、优雅退出和项目清理。

D2 当前仍为待办，未合并任何运行代码。

V9 新增列原型的扩展矩阵终态：**16 passed、7 failed、1 warning，31.71 秒**。七个失败全部为“列已存在但没有约束”，新装及缺列升级通过；真实非法 collection_id INSERT 在修正后的缺列升级库也被拒绝。该结果证明部分修复有效及存量修复仍缺失，不构成完整验收。

## Turso 重建方式与模型接入验证

实际后端拒绝 ADD CONSTRAINT（near CONSTRAINT syntax error）。真实 DML 后 foreign_keys=OFF 可读取为 0，但 BEGIN CONCURRENT 中执行重建 DDL 失败，首轮 3 failed、4.63 秒。显式 BEGIN 后 DDL 可执行，然而该版本 foreign_key_check 不返回行，第二轮 3 failed、4.28 秒；不能把无行结果当成无悬空关联。改为明确 LEFT JOIN/非空父键检查后，正常提交、末尾故障回滚、悬空数据拒绝三项 **3 passed，4.07 秒**；CASCADE/SET NULL/RESTRICT 子表均保留，旧列、自定义索引/触发器与回滚后原 schema 均经断言验证。

独立原型新增 schema_foreign_keys.py：Turso 在 schema 阶段提交后、孤儿回填前开单独显式 BEGIN；根据 ORM 目标/删除动作核对实际 FK，预检悬空数据，复制原始 CREATE 定义和实际列，保留显式索引/触发器，外键关闭状态确认后同事务替换，finally 恢复外键。已有但动作/目标不符的 FK 明确拒绝，不猜测改写。只读 schema 检查正常路径不重建。

实际新装/缺列/既有列缺 FK 模型矩阵加重建原语 **26 passed、1 warning，36.53 秒**。随后增加带 Episode/FileResource/身份袋/人工保护及历史额外列、额外唯一索引的模型升级测试，正在运行（/tmp/rssripple-v9-fk-populated.log，句柄见 /tmp/rssripple-v9-fk-state.json）。当前实现仅接入 Turso；PG、新旧关联删除行为、故障注入与完整门禁仍须完成，不关闭 D2。初次独立能力脚本遗漏启用 MVCC 的设置错误已修正，不计为产品缺陷。

带实际模型数据的扩大终态：**27 passed、1 warning，38.06 秒，退出 0**。重复启动后原作品 ID/人工标题/保护字段、Episode、资源、身份袋及额外历史列/唯一索引完整保留。该结果不替代 PG 或完整门禁。


## PostgreSQL 与真实外键动作（2026-09-20，aw）

一次性 PostgreSQL 三类库 × 七外键复现：新装和缺列升级 14 项通过，既有列缺约束 7 项失败。补修在启动已有 advisory transaction lock/DDL timeout 内检查 catalog；缺失约束先按 LEFT JOIN 诊断悬空 ID，再 ALTER ADD CONSTRAINT 验证全部行；已有错误目标/动作明确拒绝。修正后重复生产 startup 的 21 项均通过。

随后为两库矩阵加入直接 SQL 的非法 INSERT/UPDATE 与父项 DELETE：异常必须是 foreign key，不能由其他唯一约束代替；SET NULL 必须保留原 child ID 并置空，NO ACTION 必须拒绝且关联保留。Turso 扩大 **27 passed、1 warning，27.10 秒**（`/tmp/rssripple-v9-fk-actions-aw.xml`）；PG 三类库全部 21 项动作通过、探针退出 0，aw 项目已清理。矩阵数据为明确合成关系，非生产录制。PG 探针复用独立副本 tests/unit/test_upgrade_foreign_keys.py 的直接 SQL 动作断言，运行前须先应用原型或使用该副本。

当前原型仍基于 V7，需要吸收 V8 索引改动；实际完整 helper 故障回滚/脏数据拒绝、PG 并发启动与更多带规则/计划数据的重建，权威文档及完整门禁仍待完成，不关闭 D2。


## D1 共存、故障恢复与预检（2026-09-20，ax/ay/az）

独立 V9 已吸收 root V8 av 的索引与写入修复。扩大 ax **83 passed、1 failed、3 skipped，80.75 秒**：唯一失败是旧 PG 锁重试测试的 FakeConn 不支持新增 catalog 步骤；该测试将新迁移步骤隔离为 AsyncMock 并断言成功重试后仅执行一次，实际 PG catalog/DDL 由真实数据库探针验证。

Turso 新增带 Episode/资源/身份袋/历史额外列的脏数据拒绝、换表后故障回滚、纠正已知父项后重试；并增加资源/下载器/Library 三表重建，完整比较下载任务、通知、RESTRICT 规则、SET NULL 计划及冻结 payload，末尾故障要求全部表恢复。ay 扩大 **86 passed、3 skipped、2 warnings，49.54 秒**。D1 索引与只读同季预检仍通过。

PG 脏数据启动明确拒绝并诊断子行 ID；七个待补约束均保持缺失，证明前面已执行的 ALTER 也回滚，原业务行完全不变。补回明确的合成父项后，两个真实新进程启动，经 pg_blocking_pids 观察均被 advisory lock 阻塞，然后均退出 0，七 FK 齐全，人工字段保留；ax 清理完成。首版探针因 pg_stat_activity 事务快照缓存观察超时，子进程已终止；增加 pg_stat_clear_snapshot 后取得阻塞证据，这是测试观察问题而非产品故障。

新增 `scripts/verify_upgrade_foreign_keys.py --output PATH` 只读 JSONL 预检，不运行 startup，完整列出所有悬空引用。Turso 四模式首轮 3 passed/1 failed（夹具 DML 后执行 DDL 的事务方式错误），修正建表顺序后四项通过；实际 SELECT/PRAGMA-only 检查与 103 项全量导出断言通过。PG 实际 CLI 输出全部 103 个 ID/父键，业务行和人工字段不变，脏库退出 1、清洁库退出 0。

复审发现同一列同时有正确与错误 ON DELETE 时原型 any() 会放行：实际 Turso **1 failed、1 passed，2.87 秒**。改为要求所有相关 FK 均匹配，并在 PG 加同样边界。az 扩大回归与 ay 最终 PG 矩阵正在运行，见 `/tmp/rssripple-v9-fk-state.json`；完整门禁尚未开始，不关闭 D2。三份权威文档已在副本准备，原型 11 文件，尚未应用 root。


## V9 最终扩大验证与单元/API 门禁启动

az 扩大终态 **92 passed、3 skipped、2 warnings，103.17 秒，退出 0**；最终 PG 三类 schema/真实动作 21 项及两种异常 FK 语义均通过，ay 项目清理完成。最终按 code-review-and-quality 复审 SQL 标识符引用、原始 DDL/索引/触发器保留、事务所有权与回滚、悬空引用不猜测修复、父表删除副作用、幂等性及流式只读预检；无新增依赖，未发现未解决的合并阻断项。全 app/tests/scripts Ruff 与原型应用检查通过。

11 文件独立原型基于 V8 av 冻结源码，未应用 root。完整单元/API az 已启动，502 个源码/配置文件冻结于 `/tmp/rssripple-v9-source-az.json`，句柄 `/tmp/rssripple-v9-gates-az.json`。尚待完整门禁，D2 继续保留 TODO；root V8 av 仍运行，不得为后续任务覆盖源码。


## V9 门禁环境修正与 bb 续接

az 全量收集阶段因副本漏复制 tests/unit/fixtures/wikipedia 的非 Python 录制文件而 **2 errors、退出 2，13.75 秒**，未运行完整测试，不能验收。补环境脚本第一次在副本执行 git ls-files（副本无 .git）失败，后续 ba 同样收集失败 **2 errors、退出 2，3.13 秒**；两轮均已终态，不是超时重启。随后明确从 root 仓库枚举、实际补齐 34 个支持文件，并验证原 502 源码哈希完全不变。新清单 `/tmp/rssripple-v9-source-bb.json` 纳入全部跟踪测试/脚本/应用支持文件，共 2927 项；bb 全量已启动，句柄 `/tmp/rssripple-v9-gates-bb.json`。复制的是既有录制语料，没有用合成文件代替真实样本。


## V9 bb 单元/API 终态

完整单元/API **3611 passed、14 skipped、6 warnings，1626.49 秒，21340/21833（97.74%），退出 0**。JUnit failures/errors 均 0，2927 个冻结源码/支持文件哈希不变。随后仅继承 V8 bc 的历史迁移库集成夹具修正，应用/单元/API/配置不变；副本新清单 `/tmp/rssripple-v9-source-bd.json`。尚待 root V8 bc 完整集成终态，再核对基线同步 V9 并运行其完整集成；当前不关闭 D2。

## V8/V9 联合最终验收（2026-09-20，bg）

完整单元/API：3612 passed、14 skipped、6 warnings，2255.37 秒，退出 0；覆盖 21343/21833（97.76%），95% 门禁通过。完整集成：3136 passed、17 skipped、8 warnings，1716.83 秒，退出 0；覆盖 19576/21833（89.66%），85% 门禁通过。两个应用退出均为 0，覆盖率汇总/证据导出/项目清理全部完成，报告在 `/tmp/rssripple-v89-artifacts-bg`。冻结的 2891 个源码/支持文件终态哈希一致；Ruff 全库及 diff whitespace 检查通过。

最终复核覆盖：新装/升级唯一索引、旧冲突只读预检与失败保留、真实 PG API/metadata 抢槽、SAVEPOINT 内关联原子回滚、Turso 实际错误文本转换，以及七处 FK 的双库新装/缺列/已有列矩阵、真实 INSERT/UPDATE/ON DELETE、带关联数据原子重建、故障回滚、并发启动与只读孤儿报告。无新增依赖；权威模型/API/业务/迁移/单季化/集成清单已同步。历史失败轮保留，不以更改原录制数据规避失败。

D1/D2 验收完成，从 pending-only TODO 删除。当前生产库未执行迁移或数据修复；新约束遇到既有冲突/悬空关联会拒绝启动，须按只读报告及迁移 runbook 修复，不能自动删业务行。机器可读证据见 probes/database-invariants-complete-bg-result.json。V10/V11 及其他待办继续保留。
