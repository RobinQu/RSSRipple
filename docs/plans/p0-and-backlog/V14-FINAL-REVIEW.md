# V14 合入前审查记录

状态：审查完成，批准本地合入。候选以 `probes/queue-final-th-source.json` 为准；ti 完整集成已通过并收尾，th 单元/API 3937 passed、97.38%，已退出 0。

## 队列与下载预留复核

本轮读取 `task_queue.py` 的恢复、领取、完成、旧版本恢复与作业退休判断，以及 `download_dispatch.py`、`download_dispatch_cleanup.py` 和 `agent_service.py` 的 RPC 调用边界。

| 维度 | 核对结果与范围 |
|---|---|
| 正确性 | claim/finish/recovery 的 WATCH 覆盖 job、active、lease，完成还比对 execution token/consumer；processing 的非原子空表 DELETE 已删除并有 te 红测、tf/tg/ti 修复证据。预留事务先提交不可变参数，参数漂移拒绝；RPC 返回后再次检查执行权；settled 且任务已删除不重建。 |
| 可读性 | 预留和结果提交分离为两个明确函数，清理独立模块；队列 WATCH 循环重复但对应不同原子操作，不在本批为压缩代码引入通用事务抽象。 |
| 架构 | 独立预留事务只提交 intent，不提交调用者业务状态；调用者不得已持有写锁。持久 operation_key 绑定逻辑 job/resource/agent，执行 token 与逻辑 job 身份分离。 |
| 安全 | 外部参数用于结构化 ORM 查询；URL/种子内容仅以摘要加入预留参数。清理遇到无法读取队列状态时停止，不凭年龄删除仍活跃作业的预留。 |
| 性能 | 清理按 created_at/id 键集分页，每页 500；不跨 RPC 持有预留写事务。Redis 恢复扫描仍按 processing 列表读取，未据本次测试声明海量 backlog 性能。 |

边界：下载 RPC 可能在租约过期交错下多次调用，远端按 torrent 内容幂等，不能称为通用 exactly-once。Redis/SQL 之间不是分布式原子提交。以上仅覆盖所列模块，不代替通知、metadata、magnet、整理、迁移和文档的最终复核。

## 合入前仍需完成

- th 完整单元/API 终态、覆盖率 >=95%、冻结哈希复核与证据归档。
- 通知、metadata、magnet、整理及迁移的候选差异与已有测试逐项核对。
- 权威文档和升级要求核对；保持必须停掉全部旧 worker 的要求。
- 清理 V14 顶部已失效的“当前运行”描述，历史失败证据保留。
- 完成最终结论后才能把有效代码应用到 main 并提交；本记录不是合入许可结论。

## 通知与整理复核

读取 notify_service 的投递领取/回写、独立短事务重试和资源快照重新生成，以及 organize_service 的文件锁、计划修订预留、线程执行和条件结果回写。投递 attempt_token 领取和完成都比较持久状态；HTTP 在短事务之外，retry_on_lock 仅重试 SQL，不重发 HTTP。重新生成会旋转旧投递 token，并在快照返回/作废后/commit 前检查执行权；ri/rj、rm 和 ti commit 参数覆盖其回滚边界。

整理从 execute_plan 起持有共享计划文件锁，线程操作完成及结果回写仍在该锁范围；开始移动前检查队列执行权，移动后以计划 revision/owner 保证结果归属，不因队列失权中途放弃文件收尾。sa 与 ti organize_takeover 覆盖真实 Redis/PG/文件锁接管，返回 busy 属显式结果，不宣称自动重试策略已验证。可读性/架构上继续复用已有 plan lock 与 revision API；安全边界沿用实际源/目标验证；磁盘验证及操作在线程中执行。未发现本次新增阻断项。

迁移片段核对：attempt_token 与 magnet_resolve_attempt_id 的 ADD COLUMN 失败会中断迁移，不能静默继续旧所有权语义。完整迁移与默认数据复核仍需结合候选差异检查。

## Metadata、magnet、调度与迁移复核

已逐文件读取 metadata_search、fetch_service、fts、notification_build、magnet_resolve、torrent_inspect、scheduler、job_handlers、resources API、database、相关模型与配置相对 main 的差异。metadata 网络返回后锁行并 populate_existing，重新计算人工字段排除，季/合集变化拒绝旧候选；fetch 并发任务以 gather(return_exceptions=True) 收齐再传播失权，防止退出共享边界时仍有后台写入。FTS 作为可重建影子索引不能宣称与主库原子提交。

magnet 独立 attempt UUID 贯穿状态/失败计次/成功与 inspection 提交；资源行条件写锁覆盖 inspection 的最终提交。真实 PG 八项、录制单文件、多作品四项以及 td 写锁交错分别覆盖不同边界。HTTP torrent 使用内容摘要文件名与同目录临时文件原子替换，磁力路径按 attempt 分离；解析/文件 IO 移至线程，线程不接收 ORM 会话。取消后线程可能继续，不能宣称取消撤销文件副作用。遗留无引用缓存回收与海报残缺缓存已保留 P2，不隐含完成。

调度与 handler 不把 ExecutionOwnershipLostError 当网络/单项错误吞掉；内部 commit、sweep、进度、通知构建与资源发布关键边界有检查。安全关键迁移新增列失败直接中止启动；新表由模型注册创建，不迁移既有任务身份。真实 PG 迁移补验 sd、Turso/PG 较早约束证据与 th/ti 完整门禁均已完成。部署文档明确停止旧 worker 后升级，不承诺混合版本滚动安全。

五维结论：本候选提升状态归属与副作用边界的正确性，保持现有服务分层与 ORM/参数化查询，新增描述符/事务/文件机制各有明确用途；未引入外部依赖或认证放宽。网络之外的同步解析/文件操作已线程化，预留清理分页；未做海量负载性能宣称。未发现剩余阻断项。最终文档清理仅移除 B4 已失效的候选/待验收措辞，不改运行代码或测试。

结论：批准将本候选有效代码合入本地 main。th 3937 passed、97.38%；ti 3169 passed、88.46%，17 跳过有明确审计，实时提供者能力不在离线门禁结论内；P0/B9 已合入，M1 与其余 TODO 保持未完成。合入时按候选清单核验文件哈希，提交前运行仓库 lint，不再无理由重复完整门禁。
