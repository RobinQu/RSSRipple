# V12 候选批次：作品删除的身份清理与人工关联保护

状态：必要性已复现，删除策略与事务回滚已有局部原型；并发协议尚未实施，未验收。P1-D6 保留 TODO。主干基线 3570707；独立副本 /tmp/rssripple-v12-deletion。V11 完整门禁仍运行，未将此批次代码混入其冻结副本或 main。

## 必要性与优先级

实际 DELETE API＋Turso 四例红测（gg，4 failed，5.50 秒，退出 1）：剧集和电影删除均返回 200，但 WorkExternalId 残留；创建替代作品后调用生产 add_external_id 返回 False，不能重新登记相同身份。另两例同时持久化 manual work link 和 manual file assignment，DELETE 200 后两者均消失。不是仅根据 CASCADE 声明推断。

维持 P1：用户显式删除作品不应静默抹去独立资源的人工文件映射，也不应留下阻断后续识别的身份。真实录制 prod_works_v1.json 含 213 条 link（4 manual）、3765 条 assignment（95 manual）、249 条身份袋；这些仅证明历史图有相应数据形态，不是线上影响数量。SHA256 d11651d2162ced23e8d919af0bff2d9f316e203234cc854909ba5f444a35ec32，未改写。

## 方案论证与边界

1. 允许删除的作品必须在同一事务中清理身份袋、解除自动关联并删除作品；异常整体回滚，不能先提交身份清理。禁止仅在查找 miss 时抢占残留身份来掩盖删除缺陷。
2. 没有替代目标时不猜测人工映射的归属。建议有 manual link/assignment 或明确人工标题映射时返回 409 DELETE_BLOCKED，并提供引用计数/编辑入口；先通过现有作品合并或资源关联编辑完成转移/解除，再执行删除。不可默默 CASCADE 丢人工证据。此项会修改删除契约，需同步 API/前端提示和业务文档。
3. 保留 AgentWork 已有删除阻断。自动 links/assignments 的移除与资源状态更新需评估是否进入待确认；历史下载/通知快照不得改写。V11 决策身份和归档需要一起检查，不能仅清直接 pending FK 而遗留可派发旧 scope。
4. 并发检查必须与删除串行化：作品行锁、人工关联插入/更新、AgentWork 新订阅及身份登记竞争均需实际 PG 交错证明。不能单靠 count→DELETE；锁顺序必须兼容 V11 确认、合并及资源编辑。
5. 存量孤儿身份清理单独提供只读审阅与显式应用；不在启动或 API 请求中无条件删除历史记录。

## 严格验收

- 当前四个红测转绿：普通删除清理身份并允许替代作品重新登记；人工链接/文件映射阻断且作品与全部证据保持不变。
- 追加独立 manual link、独立 manual assignment、人工标题映射、纯自动关联、无引用、订阅阻断、404 和重复删除；错误码与 details 可供前端处理。
- 故障注入覆盖身份袋清理后、关联变更后、作品删除后，验证完整事务回滚。
- 真实 PG 两连接覆盖并发关联新增/改为 manual、并发订阅、反向删除与合并/确认；既验证保护，也验证提交后可继续。
- 使用未改写的真实录制图回放可安全删除与受保护记录；合成边界明确标注，不能伪称录制 pending 数据。
- 完整单元/API ≥95%，唯一 Compose 项目完整集成 ≥85%，runner/应用退出为 0、证据导出、冻结源码复验与项目清理全部完成后才能关闭 D6。

红测补丁：probes/work-deletion-necessity-tests.patch；机器结果：probes/work-deletion-necessity-result.json。

## 真实录制回放补证（gh）

未改写的 prod_works_v1.json 经 ORM 完整加载，每例使用独立临时 Turso，再调用实际 HTTP DELETE /series。录制作品 1d721de9-dbf9-46af-977a-fda92eb336c1 删除返回 200，其 1 条 manual work link 全部消失；作品 4bc342c7-4701-4d95-afbe-0e6b976cd9bd 删除返回 200，其 26 条 manual file assignment 全部消失。两例保护断言失败（2 failed，5.63 秒，退出 1）。这是录制图上的实测删除影响，不是当前线上数据损失结论；生产数据库未改动。

红测补丁已包含 API 合成边界和 season_model 真实录制回放两个文件。下一步实施时不能只让合成测试通过：录制回放必须同时转绿，事务/并发保护及身份重新登记仍须验证。

## 删除策略原型与回滚验证（gm）

独立副本 `/tmp/rssripple-v12-implementation-gl` 基于当前 V11 原型，未修改 V11 冻结集成副本或 main 运行代码。剧集/电影删除新增人工引用阻断，并在原事务中清理身份袋。实际 HTTP API、Turso、两例未改写录制图回放以及身份清理后故障注入共 **8 passed，5.47 秒**；JUnit 与终端日志完整，进程句柄已失效，未单独恢复 runner 退出码，不能作为完整验收。故障注入后作品、资源 FK 与身份袋均保留。

目前仍是 count→DELETE，不能保护并发新增引用或自动引用改为人工。身份登记竞争、V11 决策归档、自动关联解除后的状态、其他故障阶段和完整门禁均未完成，D6 继续保留。补丁 `probes/work-deletion-policy-prototype.patch` 是相对 V11 原型的增量，不能直接当作 main 上可合入的修复；基线/文件哈希及测试证据见 `probes/work-deletion-policy-result.json`。

新增三种人工引用各自独立阻断的剧集/电影参数化测试（6 例），检查精确引用计数及目标保留；gn 轮共预计 14 例，仍运行，尚无通过结论。此前 gm 的 8 例通过不覆盖这些新增断言。

## 删除后旧身份登记的必要性与补修（go / gp）

实际 DELETE 成功后，再用旧作品 ID 调用生产 add_external_id，剧集与电影均返回 True 并重新产生孤儿身份：go 两例失败，1.15 秒，退出 1。这是确定性提交顺序复现，尚不是 PG 并发证明。原型登记函数增加目标存在检查及 PG FOR KEY SHARE，防止登记事务提交前作品被删除；Turso 仍依靠事务写冲突。

包含新增身份边界、六例独立人工引用、原有删除/回滚及两例录制回放的 gp 轮 **16 passed，9.14 秒，退出 0**。原 gn 沙箱进程仍存活且无输出，未据观察超时重启；gp 是修复新缺陷后的扩大验证。身份服务调用兼容性、PG 锁序与交错仍未验证，不据此接受 D6。

## PostgreSQL 交错红测（gr）

身份登记、单季 upsert、metadata fallback 现有回归 59 passed（14.42 秒，退出 0）。但真实 PG 两连接交错仍复现孤儿：生产 add_external_id 插入后未提交并持有作品 KEY SHARE；另一连接调用实际 delete_movie，完成身份清理后阻塞于作品删除（pg_stat_activity 确认 Lock）；登记连接提交，删除继续成功，作品消失而身份仍残留。gr 断言失败、退出 1，不能把 gp 的 16 例或上述 59 例当作并发验收。

这证明仅登记端加存活锁不够：删除必须在身份清理前取得排他保护，同时兼容 Agent→决策→资源→作品的锁序；还需保护既有自动引用更新为 manual。后续设计应统一处理这些竞争，不能只调换一处 DELETE 掩盖其余竞态。探针与结果保存为 probes/work_deletion_identity_pg_probe.py、probes/work-deletion-identity-pg-result.json。

## 自动文件覆盖度保护（gs / gt）

真实 API/Turso 的合成双电影包用例 gs 复现：删除其中一部电影，关联的自动文件映射被 CASCADE 删除（1 failed，0.64 秒，退出 1）。该文件从覆盖度输入中消失，不能保证包仍保持未知。原型删除事务现先把自动 assignment 目标置空，保留文件路径及其他文件证据；manual 引用仍阻断。断言验证初始覆盖已知、删除后两文件仍存在、删除目标文件未分配、最终覆盖未知。

扩大专项 gt **17 passed，9.58 秒，退出 0**，包含前述身份、人工保护、录制回放及回滚测试。原型 API/业务文档已同步。PG gr 所揭示的删除竞态仍未修复，此结果不构成 D6 完整验收。

## 身份登记与删除串行化补修（gu）

删除 API 在清理身份前 SELECT 作品 FOR UPDATE NOWAIT；55P03 时整体 rollback，返回 409 INVALID_STATE，提示待当前操作完成后重试。登记端 KEY SHARE 与此互斥。真实 PostgreSQL 四个交错均通过（series/movie × 登记先行/删除先行），退出 0，隔离项目清理退出 0：登记先行时删除先返回冲突，登记提交后重试删除不留孤儿；删除先行时通过 pg_stat_activity 确认登记阻塞于 Lock，删除提交后登记返回 False，最终作品和身份均不存在。

这只修复 gr 身份竞态；现有子行改为 manual 的竞争、资源/决策反向锁序与历史决策归档仍未解决。证据见 probes/work-deletion-identity-orders-pg-result.json；保留 gr 红测说明修复必要性。

目标锁补修后的 gv 回归为 **41 passed、1 warning，16.89 秒，退出 0**（删除 API 专项、录制回放、身份服务）。仍不替代完整门禁或剩余并发矩阵。

## 既有引用并发修改（gw / gx）

PG gw 红测在实际 delete_movie 的人工检查之后暂停，另一连接成功把 auto link/assignment 改为 manual 并提交；删除继续，人工目标丢失，两类引用均复现，退出 1。作品行锁不会阻止不改 FK 的 source UPDATE。

原型在作品锁之后，对关联资源以及现有 link/assignment/title mapping/AgentWork/直接 PendingDecision 行按 ID 排序加 NOWAIT 锁，再检查人工计数；遇到竞争回滚为 409 INVALID_STATE，避免持有作品锁等待反向资源锁。gx 两类引用的编辑先行交错通过：编辑未提交时删除返回冲突；提交后重试返回 DELETE_BLOCKED，作品与人工目标均保留，退出 0。该 PG 探针目前只覆盖电影、编辑先行，尚需扩展剧集、删除先行、新增引用及决策关联完整矩阵。

## 决策生命周期红测（gz）

引用锁改动后 gy 回归 41 passed、1 warning，17.63 秒，退出 0。新增实际 create_pending_decision→HTTP DELETE→数据库检查 gz 复现：作品删除后原决策仍为 pending，未失效；1 failed，0.69 秒，退出 1。必须补身份变更协调及归档，不能只把 PendingDecision.movie_id 置空。该新测试尚未修复，不计入 gy 通过范围。
