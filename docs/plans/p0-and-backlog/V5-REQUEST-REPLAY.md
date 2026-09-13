# V5：资源修订的持久定向重跑（设计中）

## 必要性与范围

P1-B9 已通过实际 API、MemoryQueue、`_handle_run_agent` 和数据库复现：以事件屏障暂停选完资源的增量作业，PATCH 两天前的已捕获标题资源成功；队列同 key 去重返回 None，作业结束仅一条 total_resources=0 的运行记录，修订资源不再入选。红测 1 failed，2.86 秒，日志 `/tmp/rssripple-v5-busy-rerun-red.log`；[复现补丁](probes/busy-agent-rerun-reproduction.patch)。标题来自已审核案例；为避免外部 I/O，资源明确设为未关联，验证的是请求消费，不是元数据识别或下载成功。

仅改队列 key 会允许同 Agent 并发派发；只在 except 重试无法处理正常返回 None；只保留进程内集合无法跨进程或重启恢复。维持同 Agent 队列去重，在业务数据库记录尚未消费的修订请求。

## 拟实施契约

1. 增加 AgentResourceRequest：UUID 主键；agent_id/resource_id FK CASCADE；二者联合唯一；递增 revision 和 UTC requested_at。upsert 只更新原行并递增版本。版本用于确认处理，不用时间比较代替。
2. 三个资源修订端点和 healed sibling 请求，在资源最终 commit 前写请求行；commit 后尝试入队。队列忙碌或暂时不可用时，已提交请求仍留在数据库。保持资源修订先 commit 再 enqueue 的不变量。
3. 独立周期分发器为有待处理请求的 active Agent 尝试 enqueue 同一 agent key。重复 tick、多个 worker 并发、enqueue 返回 None 均不删除请求。分发器在 worker/all 角色注册，启动后能恢复遗留请求；web 不依赖进程内定时器维持持久性。
4. Agent 作业选资源时读取该 Agent 的请求快照，保存 `(row_id, revision)`，将对应资源并入本轮候选并绕过消费水位线；显式请求仍按当前 Agent 规则与频道归属校验。
5. 成功完成消费后，在最终运行记录事务内按 row_id＋revision 删除已处理快照。运行期间又有修订使 revision 增加时，旧作业不得删除新请求。删除重建的新 UUID 也不可被旧快照确认（防止版本从 1 重新开始造成误删）。
6. 失败或崩溃不确认请求；后续分发可重试，已提交下载组走 V4 去重。为持续失败定义退避，避免每个 tick 热循环；退避不能阻塞其他 Agent。需要明确按请求还是按 Agent 记录失败状态，实施前以真实失败路径验证。
7. Agent/资源删除用 FK 清理；Agent 暂停时不派发、恢复后继续。Agent 换频道后不处理原频道资源，需明确过期请求清理。不能仅依靠 SQLite nullable UNIQUE 避免重复。
8. 队列空闲时仍可立即运行；忙碌请求允许在释放 key 后由下一次分发 tick 执行，不承诺当前红测里的即时跟随时序。绿测须显式驱动真实分发 tick，并验证请求在等待期持久存在。

## 严格验收

| 场景 | 必须检查的结果 |
|---|---|
| 三修订端点＋兄弟资源修复 | 资源与请求同事务落库，入队时其他会话可见；请求失败不能只存资源 |
| 当前作业已选完资源 | 修订接口成功，忙碌返回 None 后请求仍在，后续作业消费旧资源 |
| 当前作业尚未选择 | 请求可并入本轮且仅确认当时版本 |
| 两次修订交错 | 旧版本完成不会清除新请求；第二次修订最终得到处理 |
| enqueue 异常、web/worker 重启 | 无需再次编辑资源即可恢复；测试实际独立进程/持久数据库 |
| 两 worker 并发分发 | 仍只有同 Agent 一个 active job；请求不丢失、不重复确认 |
| 处理失败与退避 | 请求保留、其他 Agent 正常消费、到期可恢复，无高频热循环 |
| 删除、暂停/恢复、频道变更 | 无孤儿请求或错误跨频道派发 |
| 真实清单＋实际下载器 | 修订后读取新字段，已提交下载不重复，RPC 后 DB 失败可重试 |
| Turso/PostgreSQL 升级与重启 | 实际旧 schema 升级两次、唯一约束和 FK 一致，版本不重置 |

先完成确定性红/绿与上述跨进程场景，再验证四种运行模式的水位线、P0 修订消费回归和完整单元/API 95%＋隔离集成 85% 门禁。当前仅必要性红测已完成，未修改主工作树实现，不删除 B9。


## 存储层原型进展

独立副本已增加模型、请求 upsert、只读版本快照和条件确认 helper。真实 Turso 事务测试 **1 passed，0.64 秒**（`/tmp/rssripple-v5-request-persistence.xml`），验证未提交写入回滚、重复输入不重复加版本、旧版本确认不删除新修订、删除重建后的 UUID 保护，以及删除 Agent 的 FK 级联。最初缺少种子 helper 必填参数，修正后才计通过。

[存储原型补丁](probes/agent-request-storage-prototype.patch) 尚未接入 API、worker 或调度器，也未实现退避；忙碌请求完整红测仍预期失败，不能把存储层通过视为 B9 修复。下一步先接入资源编辑原子提交和作业版本确认，再补周期分发、故障退避与两后端跨进程门禁。


## 运行链路原型进展

三修订端点均已在 commit 前写 active Agent 请求、commit 后 best-effort 唤醒；后台阶段一读版本快照并把请求资源并入选择，阶段二按版本确认或退避。周期分发器每 5 秒尝试唤醒，队列保持相同 agent key。消费失败按请求持久化 attempt_count/error_message/next_attempt_at，30 秒起指数退避、上限 1800 秒；新修订递增版本并清空旧退避，迟到旧失败不影响新版本。分发器清理频道不再匹配的请求、跳过 非 active（paused/error）Agent；这些生命周期路径尚待专门验证。

- 初步完整链路＋旧 P0＋存储：3 passed，6.14 秒，`/tmp/rssripple-v5-rerun-green.xml`。
- 三端点恢复及存储：5 passed，8.53 秒，`/tmp/rssripple-v5-three-endpoints.xml`。
- 三端点各覆盖「当前运行无请求快照」与「已选旧版本后又修订」，包括请求可见、旧作业未误清新版本和最终确认：8 passed，15.92 秒，`/tmp/rssripple-v5-revision-interleaving.xml`。
- 三端点在真实请求 INSERT 后注入异常，资源和请求同时回滚；broker 不可用时保存仍成功，替换为新 MemoryQueue worker 对象后分发并消费：4 passed，7.33 秒，`/tmp/rssripple-v5-request-failures.xml`。这不是独立 OS 进程重启验证。
- 存储与退避上限、新修订清空退避、旧失败不能延期新修订：2 passed，4.12 秒，`/tmp/rssripple-v5-request-backoff.xml`。

原型仍未同步主工作树。还需处理失败到期的实际 job 恢复、暂停/换频道/删除、兄弟资源修复、实际 PostgreSQL 并发和旧库升级、进程重启及 Redis、真实下载器、扩大 API/后台/调度回归与完整门禁。初次自动插入 handler try/except 时误包含相邻模块常量，已修正语法后才计绿测。


## V5 生命周期与跨进程续验（仍为独立原型）

生命周期测试扩大至 **7 passed，12.82 秒**：作业返回错误或抛异常后请求持久退避，其他 Agent 继续；到期恢复；暂停时保留、恢复后消费；换频道后清除旧请求。报告 `/tmp/rssripple-v5-request-lifecycle.xml`。

实际 PostgreSQL＋Redis 验证通过：两个写入进程累计六次修订只保留一行；两个分发进程与两个 worker 竞争仅运行一个作业；已选版本 7 后提交版本 8，旧作业确认不删除新修订；替换为新 OS worker 进程后成功消费版本 8。旧 schema 实际升级、重复迁移保留版本，以及数据库唯一性、非空键、资源/Agent 删除级联均验证通过。独立项目 `rssripple-v5-replay-20260913-x` 已清理。驱动与结果见 probes 下 `agent_request_pg_redis_probe.py`、`agent-request-pg-redis-result.json`、`agent_request_pg_schema_probe.py`、`agent-request-pg-schema-result.json`。数据为合成未链接资源，未调用真实下载器。

仍需兄弟资源修复覆盖、真实下载器故障恢复、调度注册和扩大 API/后台回归，以及完整门禁。AgentRun 异常后遗留 running 属现有独立待办，尚未修复。B9 原型未合并主工作树，不关闭 TODO。


## V5 合入本地前的续轮验收（2026-09-13）

必要性继续成立：队列 active-key 去重不能保存当前作业选完资源后的修订；仅延迟入队或进程内集合无法覆盖 broker 故障与 worker 重启。原型保持资源编辑＋持久请求同事务、id/revision 条件确认，并维持 Agent 当前规则与定向水位线语义。

扩大回归 **319 passed、8 skipped、1 warning，164.50 秒**，报告 `/tmp/rssripple-v5-expanded-y.xml`；首次因临时副本缺 API 测试模块而收集失败，补齐后才计通过。新增实际 APScheduler 触发及两个端点真实兄弟资源修复 **3 passed，3.00 秒**，`/tmp/rssripple-v5-scheduler-z.xml`。这些测试使用真实 Turso、队列及 HTTP/调度链路，兄弟资源元数据为明确合成夹具。

[真实 RPC 驱动](probes/agent_request_rpc_probe.py) 用已审核 `f79ef2eb-02d5-42d3-80dc-70dd3c1d733b` 的原始种子，校验 SHA256；专用内部网络 Transmission 接受后注入真实 NOT NULL 失败，首次数据库无任务但请求持久退避，未到期不重试，到期通过分发器＋实际 MemoryQueue＋生产 job 重试。两次 RPC 复用同一 torrent ID，最终一个 daemon torrent、一个 DB task，请求清空且水位线不动，媒体接收 0 字节。[结果](probes/agent-request-rpc-result.json)，原始日志 `/tmp/rssripple-v5-request-rpc-aa.log`。种子真实，作品身份为合成前置数据；不声称验证了 metadata 识别。项目 `rssripple-v5-request-20260913-aa` 已全部清理。

驱动会创建独立 `/tmp` Turso 库，要求名为 `rssripple-v5-request-20260913-aa` 的新空 Transmission 专用项目；用 [Compose](probes/agent-rpc-compose.yml) 加显式 `-p` 启动，设置 `PYTHONPATH=.` 从含本地 app/tests 的仓库运行驱动，最后同项目 down -v。不得复用业务下载器。

按 code-review-and-quality 检查事务原子性、旧版本竞争、异常恢复、依赖及边界；未新增依赖。10 个实现/测试文件经主工作树基线哈希比对后已同步本地，新增模型、业务/API/迁移和测试权威文档已更新，相关 Ruff 全部通过。完整门禁尚待执行，B9 继续保留 TODO；此前“仅独立原型”描述为历史阶段。

主工作树同步后专项 **19 passed，10.97 秒**，报告 `/tmp/rssripple-v5-main-targeted.xml`；Ruff 与 `git diff --check` 通过。ab 轮完整单元/API 开始运行，结果未定；源码快照 `/tmp/rssripple-v5-source-ab.json`。


## B9 最终收尾（2026-09-13，ab 轮）

本地 B9 实现完成验证，可合入主干；已按 pending-only 规则从 TODO 删除 P1-B9。资源编辑与持久请求同事务、忙碌队列后的补发、旧版本确认保护、错误退避、兄弟资源、暂停/删除/频道变更、实际调度与跨进程恢复均有专项证据；真实种子＋Transmission 接受后数据库失败的到期重试复用同一 torrent ID，未下载媒体。

| 门禁 | 结果 | 覆盖率 | 退出状态 |
|---|---|---|---|
| 完整单元/API | 3500 passed、14 skipped、6 warnings；1475.15 秒 | 21066/21528 = 97.85%，超过 95% | 0 |
| 完整隔离集成 | 3114 passed、17 skipped、8 warnings；1705.85 秒 | 19361/21528 = 89.93%，超过 85% | runner 与覆盖率报告均为 0 |

两份 JUnit 的 failures/errors 均为 0；436 个被测源文件运行前后哈希一致。全仓 Ruff 与差异空白检查通过。两个应用 SIGINT 后均正常退出 0；原始四路覆盖率、合并数据和 XML 已导出到 `/tmp/rssripple-v5-artifacts-ab`，JUnit 为 `/tmp/rssripple-v5-unit-ab.xml`、`/tmp/rssripple-v5-integration-ab.xml`；日志为 `/tmp/rssripple-v5-unit-ab.log`、`/tmp/rssripple-v5-integration-ab.log`、`/tmp/rssripple-v5-coverage-ab.log`。唯一项目 `rssripple-v5-complete-20260913-ab` 的容器、网络及临时卷全部清理。

机器可读结果见 [B9 验收摘要](probes/agent-request-complete-ab-result.json)。之前记录的运行中状态为历史，不再代表当前状态。此次收尾不关闭 B4、B7、AgentRun 崩溃回收或元数据身份接地；剩余 P1 21 项、P2 79 项、P3 13 项，共 113 项。下次入口见 PLAN.md 开头和 V6-IDENTITY-GROUNDING.md，V6 仅保存方案、复现与原型补丁，未合入运行代码。
