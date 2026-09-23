# V14：Redis 执行所有权（P1-B4）

## 当前验收状态：th/ti 均已完成

th 完整单元/API **3937 passed、15 skipped、6 warnings，2290.65 秒，退出 0，覆盖率 97.38%（23345 行，612 未覆盖）**。ti 完整集成 **3169 passed、17 skipped、88.46%**，应用正常退出、报告导出及隔离栈清理均成功。3086 文件最终哈希无变化。证据 `probes/queue-unit-th-result.json`、`queue-unit-th*`、`queue-integration-ti*`。

下方 th/ti“运行中”均为历史过程。当前无本批运行中的测试或隔离栈；代码仍在原型，尚未合入 main。下一步完成 [最终审查](V14-FINAL-REVIEW.md) 的其余模块与文档核对，再应用已验收候选。


## 最新 ti 完整集成已收尾

最终审查按模块记录于 [V14-FINAL-REVIEW.md](V14-FINAL-REVIEW.md)。队列原子所有权与下载预留/清理已完成本轮五维复核；其余模块仍待核对，本记录不代表批准合入。th 最后观察推进到约 98%，session 37774 尚未终态。

ti test-runner 退出 0：3169 passed、17 skipped、8 warnings，1885.85 秒。双应用 SIGINT 后 exited 0，四份覆盖率汇总退出 0，覆盖率 **88.46%（23345 行，2693 未覆盖）**；导出和 `rssripple-v14-final-ti` down --volumes 均退出 0，3086 冻结文件无变化。新增 descriptor 正式参数通过。证据 `probes/queue-integration-ti-result.json` 与同前缀日志/JUnit/coverage/skips。17 个跳过的测试名和原因与 sn 完全相同，sn 的逐项补验/缺口审计仍适用，未将其计为通过。

th 完整单元/API session 37774 尚在运行（最后观察约 94%），因此整批尚未验收，不能合入。仍需 th 终态及最终五维审查与文档收敛。


## 跳过项逐条审计

sn 17 项跳过按真实 JUnit 逐项映射到 `probes/queue-integration-sn-skip-audit.json`：2 项 Redis HTTP 同名测试在 sj 通过；磁力 terminal_retry_flow 的 tracker 422、failed→pending 与运行中 409 在 sb API 测试通过，失败耗尽及恢复成功在 sn 集成通过，但这不是同一 HTTP 全流程的等价证明。11 项频道 LLM workflow、1 项 metadata search/link 未在该门禁验证外部提供者路径，2 项 live magnet 属明确公网可选测试。后续不得将这些跳过写为通过，也不得为消除 skip 而放宽目标断言。th/ti 结束后须用新 JUnit 再确认最终名单。

## 当前冻结验收 th/ti

tg 独立 Redis 7 补验退出 0，两真实连接确认空列表自动移除、新描述符保留，专用容器清理退出 0。正式 `descriptor_driver.py` 已加入 queue_recovery 参数入口；这是受控命令交错，不是多进程租约验证。证据 `probes/queue-descriptor-tg*`。

当前候选冻结 3086 文件，清单 `probes/queue-final-th-source.json`，聚合补丁已刷新且 apply --check、修改文件 Ruff 通过。th 完整单元/API 正在 session 37774（`/tmp/rssripple-v14-unit-th.log`，JUnit/coverage 同前缀），门槛 95%。ti 唯一隔离项目 `rssripple-v14-final-ti` 启动 session 50800 已退出 0，完整 test-runner session 53982 已启动，日志 `/tmp/rssripple-v14-integration-ti.log`；结束仍须双应用 SIGINT 正常退出、四份覆盖率汇总 >=85%、导出证据和 down -v。运行期间禁止改候选 app/tests/scripts。两轮均未取得终态，不能记通过。


## 最新审查补修：processing 列表删除竞态

te 确定性红测证明 `_recover_orphaned_jobs` 的 LLEN→DELETE 存在丢描述符窗口：读到 0 后原消费者恢复并写入新描述符，随后 DELETE 擦除新任务，断言失败。证据 `probes/queue_processing_recovery_te.py`、`queue-recovery-te.log`，fakeredis 替身明确调度交错，不宣称真实 Redis 进程证据。

修复删除冗余 LLEN/DELETE；Redis 移除最后一个元素时自动删除列表键，恢复逻辑只应 LREM 已检查的旧描述符。新增恢复时消费者重新领取的回归及禁止整表删除的断言。队列两文件回归 tf 已完成：68 passed、1 warning，2.80 秒，退出 0；两个修改文件 Ruff 通过。证据 `probes/queue-recovery-tf.log/.xml`。候选代码已改变，sn/sb 为补修前证据；须补真实 Redis 同交错、重新冻结及完整门禁，再完成审查合入。不得把 sn 通过解释为当前候选已验收。


## 最新完整门禁：sn 已完成

补验 td（写锁先取得）已通过两项 LLM listing 无结果/成功矩阵：实际多作品 inspection 后 flush 并持有资源行锁，另一会话更新 attempt；用 `pg_blocking_pids` 确认真实等待后释放旧事务。最终独立会话确认新 attempt/pending 生效，同时 27 文件指派、2 作品链接及电影/剧集/合集各 1 已提交。录制种子 SHA 与 si/sn 相同，外部 metadata/LLM 为合成回复，运行候选未修改。退出及独立 tmpfs PG 容器清理均为 0；代码/哈希/结果见 `probes/queue_multiwork_lock_probe.py`、`queue-multiwork-td*`。这是受控数据库锁交错，不是公网或实际租约到期测试；真实租约已有独立证据。

sn 完整集成 **3168 passed、17 skipped、8 warnings，1935.37 秒，退出 0**；两个应用 SIGINT 后均 exited 0，四份覆盖率汇总退出 0，覆盖率 **88.47%（23347 行，2693 未覆盖）**。报告导出和唯一项目 `rssripple-v14-final-sn` 的 down --volumes 均退出 0；3085 个冻结文件哈希未变。证据见 `probes/queue-integration-sn-result.json`、同名前缀日志/JUnit/coverage/skip 清单。跳过项仍须结合既有专项补验证据解释，不能将完整门禁通过等同于全部验收缺口关闭。下一步完成最终五维代码审查与验收缺口核对，再决定合入；当前仍未合入 main。


当前状态：B4 候选已实现并积累专项证据，尚未完成整体验收、未合入 main。B7 已验收合入；下文早期“未实现/B7 门禁运行”均为历史过程记录。唯一续接原型为 `/tmp/rssripple-v14-dispatch-lx`，交付候选以 `probes/queue-dispatch-prototype.patch` 和 `queue-dispatch-source.json` 为准。

当前完整单元/API rg 轮已终态：3924 passed、15 skipped、6 warnings，覆盖率 97.38%，退出 0（1999.55 秒）。session 96790 已关闭；报告见 probes/queue-unit-rg*。下一步修复下表内部提交缺口，随后重新冻结验收；此轮通过不等于 B4 已完成。

最新续接：内部提交及线程化补修、真实整理接管证据已补；冻结 sb/sc 门禁已启动，3084 文件哈希见 probes/queue-final-sb-source.json。完整单元/API sb session 86184；唯一集成项目 rssripple-v14-final-sc，启动阶段 session 66608 已退出 0、全部服务健康，test-runner 已启动。运行期间不得修改冻结源码，先轮询已有句柄；本状态不代表门禁通过。

### 当前验收差距

sn 启动 session 61011 已退出 0、服务全部健康，完整 test-runner session **65660** 已启动，日志 `/tmp/rssripple-v14-integration-sn.log`。下方启动中描述保留为过程记录。

sn 新候选已冻结：`probes/queue-final-sn-source.json` 共 3085 文件；相对 sb 只有四个集成测试文件变化（缓存、Redis 夹具、多作品 driver 和参数入口），app/scripts/unit/API 源码哈希不变，单元/API 继续引用 sb 完整结果，不无故重复。多作品四矩阵正式作为第十个 queue_recovery 参数；权威业务文档更新领取保证与停旧 worker 升级要求，测试文档同步。全 app/tests/scripts Ruff 和聚合补丁 apply --check 通过。

新完整集成项目 **rssripple-v14-final-sn** 已启动，启动 session **61011**，日志 `/tmp/rssripple-v14-integration-sn-start.log`；必须先确认启动退出 0，再运行唯一 test-runner。本轮期间禁止修改候选；结束后无论成败均须 SIGINT 双应用、四份覆盖率合并、报告导出及 down -v。此前 sc 已失败并清理，不能将其覆盖率通过当本轮通过。

sm 已终态：两个完整相关集成文件 **66 passed，3.49 秒，退出 0**；两个文件 Ruff 通过。结果 `probes/queue-fixes-sm.log/.xml`。交付聚合补丁/哈希已刷新，根仓库 `git apply --check` 通过。七项旧失败已在定向文件回归中消除，但尚未取得新的完整集成门禁通过证据，B4 继续保留。

sc 后补修：仅修改原型两个集成测试文件，生产实现未改。缓存测试保留真实 to_thread、核验内容摘要路径及原字节、断言写失败确实触发且无残留；Redis 夹具补上恢复所必需的 active key，将未领取进度更新断言改为拒绝，并新增实际领取 handler 的 50%→100% 进度正例。独立 sl 四项缓存检查 **4 passed，6.72 秒**，日志/JUnit 为 `probes/queue-torrent-sl*`。sk 的真实线程诊断进程在 sandbox 中持续 ep_poll，SIGINT 无结果后明确 SIGTERM，退出 143；未当作通过。sl 在允许的宿主执行环境及 faulthandler_timeout=20 配置下完成，尚不推断 sk 的唯一根因。

两个完整相关集成文件正在 session **37726**，日志 `/tmp/rssripple-v14-fixes-sm.log`、JUnit `/tmp/rssripple-v14-fixes-sm.xml`。sb/sc 已收尾，原型从此不再等于 sb 冻结快照；运行实现没有变更，后续仍须刷新交付补丁并建立新完整集成验收。

sb/sc 最终结果（2026-09-23）：sb session 86184 退出 0，**3936 passed、15 skipped、6 warnings，1948.28 秒，覆盖率 97.37%**。sc session 47946 退出 1，**3159 passed、7 failed、17 skipped、8 warnings，1844.78 秒**；四份覆盖率汇总退出 0、**88.33%**，两个应用 SIGINT 后均 exited 0，报告已导出到 `/tmp/rssripple-v14-integration-sc-artifacts`，down --volumes --remove-orphans 退出 0。完整报告归档 `probes/queue-unit-sb*`、`queue-integration-sc*`，17 项跳过实际原因见 `queue-integration-sc-skips.json`。收尾复核 3084 个冻结文件哈希无变化。本轮不通过，不关闭 B4。

除上述缓存三项，sc 另失败 `test_task_queue_redis_paths.py` 的 enqueue_dedup_priority_and_status、orphan_recovery_requeues_dead_consumer_jobs、startup_recovery_runs_before_worker_loop、orphan_recovery_requeues_priority_job_at_front。前者在未领取状态调用 update_progress，后三者须复核夹具的 active key/身份是否满足实际协议；不得直接降低所有权保护使测试通过。独立 sk 修正候选仍在 session 80389，已确认进程存活且等待 ep_poll，无结果；主 sb/sc 已终态，不再轮询原句柄。

Redis HTTP sj 已收尾：30 passed、140.37 秒、test-runner 退出 0；memory/Redis 两应用收到 SIGINT 后均 exited 0，JUnit 已导出，down --volumes --remove-orphans 退出 0。证据 `probes/queue-http-sj.log`、`.xml`、`-exit.log`、`-cleanup.log`。这补足该文件的双后端 HTTP 参数及两项 Redis 状态持久化用例，不将单 Redis 应用接口测试说成多 worker 接管证据。

缓存测试修正候选仅写入 `/tmp/test_torrent_inspect_candidate_sk.py`，未改冻结原型；四项定向测试 session 80389，日志 `/tmp/rssripple-v14-torrent-sk.log`。原进程已只读确认存活，日志暂为空，继续轮询该句柄，不重复启动。

sc 已观察到 3 个集成失败（约 33%）：`test_fetch_success_persists_payload`、`test_fetch_oversize_aborts_without_retry`、`test_fetch_download_error_uses_retry_budget`，同属 `tests/integration/metadata/test_torrent_inspect_coverage.py`。静态检查发现 `_stub_httpx_counting` 的 `_fake_to_thread` 调用 `fn()` 丢失所有参数，线程化 mkdir 的 exist_ok/parents 因而丢失，使路径提前返回；成功用例仍断言旧 `rid-ok.torrent` 文件名。`test_fetch_write_failure_returns_none` 也可能提前返回而虚假通过。当前运行不修改冻结文件，待最终 traceback 确认并归档/收尾后，保留实际 to_thread 或正确转发参数、断言内容摘要路径，并增加写入失败确实被触发的证据。sc 即使覆盖率达标也不算整体验收通过，修正后需重新执行完整集成。

Redis HTTP 补验 sj 已启动：唯一项目 `rssripple-v14-http-sj`，配置 `/tmp/rssripple-v14-http-sj.yml`（归档 `probes/queue-http-sj-compose.yml`），独立 memory/Redis 应用分别使用不同 Turso 数据库，临时卷/内部网络、无宿主端口。启动 session 49688 已退出 0，四服务健康。完整 `tests/integration/http/test_task_queue.py` test-runner session **39066**，日志 `/tmp/rssripple-v14-http-sj.log`；先轮询该句柄，不重复启动。完成后须 SIGINT 两应用、记录退出状态、导出 `/app/data/redis-http-sj.xml` 和服务日志，再 down -v 清理。此补验不修改 sb/sc 冻结源码，运行中不视为通过。

sc 运行中跳过审计：约 9% 时已观察 13 项 SKIPPED，临时清单见 `probes/queue-integration-sc-skip-interim.json`，不是最终计数。`TestMagnetResolve::test_terminal_retry_flow` 在 300 秒内无终态而跳过，不算成功；workflow 的 11 项依赖 LLM 配置/返回结果，metadata link 也有条件跳过。最终必须读取 JUnit 的具体原因，并核对实际隔离替代覆盖；不得只凭总体退出 0 宣称这些分支通过。目前未更改跳过条件或冻结测试。

升级审查补充：`test_task_queue.py::TestRedisQueue::test_releases_unrecoverable_legacy_running_lock`覆盖无描述符旧 running 作业释放；`test_queue_execution_ownership.py::test_legacy_recovery_cannot_fail_replacement_job` 覆盖扫描后被替代的作业不被旧恢复标 failed。后者使用 fakeredis，不当作真实 Redis 多进程证据。旧 worker 的无条件回写不会因新 worker 增加 WATCH 而自动受保护，因此部署应先停止全部旧 worker，再启动新版本；不支持据本轮证据宣称新旧 worker 混跑安全。待合入时须将此升级要求及队列保证边界同步到权威业务文档，当前冻结源码/设计候选保持不变。现有“同 key 保证不会并发执行”的绝对描述也需改为当前有效领取唯一，已失权旧执行者依靠副作用边界保护。

2026-09-23 sd 补验：使用独立容器 `rssripple-v14-pg-sd`（PostgreSQL 16、tmpfs 数据目录、仅回环随机端口），显式执行此前因未提供 PostgreSQL 而跳过的三项 database migrations 测试及 decision migration 启动审阅测试，结果 **4 passed、1 warning，3.01 秒，退出 0**。测试使用合成旧结构/审阅数据，未连接生产数据库；原始日志、JUnit 及退出 0 的容器清理记录为 `probes/queue-pg-sd*`。冻结候选源码未修改；此补验不替代仍在运行的 sb/sc 完整门禁。

| 范围 | 已有证据 | 仍需完成 |
|------|----------|----------|
| 队列原子所有权 | 真实 Redis 进程接管、条件完成/恢复/进度专项 | 最终统一冻结后回归与旧版本升级核对 |
| 下载、Agent、metadata | 持久幂等、PG 17+6 项事务矩阵、录制种子专项 | 与最终候选同步的完整集成及门禁 |
| 通知投递/整理 | 真实通知进程接管；ri/rj、rm 重新生成回滚；sa 真实 Redis/PG/文件锁接管组合 | 最终候选完整集成门禁 |
| 回填与维护 | 抓取、FTS、维护及 sweep 失权专项；rk/rl、rm 旧集数协调 Turso/PG 提交回滚 | 最终组合回归 |
| magnet | PG 八项矩阵、实际 SIGKILL、录制单文件种子实际 inspection | 多作品链边界审查；不把网络替身当公网验收 |
| 事件循环阻塞 | 缓存/inspection 阻塞红绿测试；ry 真实 Redis 18 秒慢分析续租三次且仅执行一次 | 最终候选组合回归及剩余关键同步路径审查 |
| 最终门禁 | rg 单元/API 3924 passed、97.38% | 后续修复后重新冻结，单元/API ≥95%、完整集成 ≥85%、正常退出、证据导出、清理与最终代码审查 |

上述缺口按 B4 范围保留，不因局部绿测关闭。海报残缺缓存已另按 P2 列入 TODO，避免其无限扩大 B4 合入范围。

### 冻结期间的多作品路径审查（2026-09-23）

si 将独立探针扩为四项：LLM listing 无结果/有效混合 TV+movie 结果 × attempt 保留/替换，全部通过、退出 0。有效 listing 正例额外断言恰有一条 `source=llm` 且 movie_id 非空的文件指派，证明成功电影绑定实际执行；两个替换用例均断言五表新增行全为零。sh 首轮由于探针未启用 LLM 配置而被此断言捕获，日志保留；si 仅补测试替身配置，冻结运行实现未改。日志与退出 0 的独立容器清理记录见 `probes/queue-multiwork-si*`。本轮补足 sg 的 LLM 成功绑定分支，但正式集成入口与写锁先取得的交错仍待完成。

sg 独立探针已验证多作品实际 inspection 回滚，代码为 `probes/queue_multiwork_probe.py`，只从冻结原型导入运行实现，未修改冻结文件。专用 PostgreSQL 16 容器 `rssripple-v14-pg-sf` 使用 tmpfs 与回环随机端口。正例持久化 27 条文件指派、2 条作品链接、1 Movie、1 TVSeries、1 WorkCollection；在首次 inspection 写入前由独立会话替换 attempt 后，旧解析实际执行同一多作品路径，但上述五表新增行全部回滚，替换 attempt 和 pending 保留。sg 退出 0，日志及容器删除退出 0 记录见 `probes/queue-multiwork-sg*`。sf 首轮因 metadata 替身缺少 ambiguous 字段而失败，已保留 `queue-multiwork-sf.log`，不算运行实现缺陷。

边界：真实录制种子、真实 PostgreSQL 和实际 inspection/作品 upsert/链接代码；网络磁力解析由复制录制字节替代，metadata 身份为合成值，LLM listing 返回 None，因此未证明成功 LLM 电影绑定子分支或写锁先取得的交错。探针尚未纳入正式参数化集成入口，B4 仍待完整门禁与最终审查。

se 输入筛选已执行：对 v1 全部录制 torrent 使用实际 `parse_torrent_files` / `analyze_torrent_files`，得到 29 个 franchise 候选；选择 SHA256 `912c2bd9bd70a9556cfaa974cd29d0f1748c05e26dbf6ae87453418a7f801ef1`（Cowboy Bebop，26 集 TV + 一部电影，27 个主视频、两个簇）。来源 case、原始输入、审阅状态、完整解析路径已保存 `probes/queue-multiwork-se-input.json`。此结果确认夹具可进入目标分支，尚不是回滚测试通过；不得把生产候选标签当身份真值。

`magnet_resolve._attempt_loop` 在实际 inspection 返回后，以资源 ID、attempt ID、done 状态做条件 UPDATE，失败则回滚外层事务。`maybe_inspect_torrent` 还会调用 `refine_batch_content`、`link_franchise_pack` 和 `bind_hint_clusters`；检查这些模块及 metadata/collection service 未发现自行 commit 或另开 committed_session 的调用，当前可见写入沿用传入会话。该静态检查只支持事务边界判断，不代替真实并发断言。

现有 `test_inspection_discards_changes_after_attempt_replaced` 将整个 inspection 替换为标题修改，PG magnet 矩阵也复用此测试。因此它证明资源字段不会被旧 attempt 覆盖，但未直接证明多作品的文件指派、作品链接、作品及合集创建全部回滚。后续补验应使用录制多文件种子，保留实际 inspection/链接实现，仅替换外部 metadata/LLM 响应；在首次写入前切换 attempt，再执行旧解析，独立数据库会话断言新 attempt 保留且旧解析新增的关联/作品/合集均未提交，并以未切换 attempt 的正例证明实际路径执行。如写锁已先获得，应验证新 attempt 等待并按提交顺序生效，避免构造同进程互等的假死用例。sb/sc 冻结期间不修改被测文件，这项缺口继续保留至实际补验完成。

## 必要性证据

独立项目 rssripple-v14-lease-kq、真实 Redis 7、两个进程、生产 RedisQueue，保持默认 15 秒租约和 5 秒心跳。合成 handler A 用同步阻塞 22 秒模拟事件循环饥饿。父进程观察其租约实际过期且 A 仍存活，再启动 B。B 恢复并执行同一 job；副作用标记顺序 B、A，最终结果被过期 A 覆盖为 owner=A。探针退出 0 表示成功证实缺陷，不是修复通过。无真实下载或用户数据，不能据此声称所有 handler 都会产生重复不可逆副作用。

探针和原始结果：probes/lease_reentry_probe.py、probes/lease-reentry-kq-result.json。

## 方案约束

仅延长租约或在同一事件循环增加心跳不能消除已复现机制。必须为每次 claim 建立执行代次/所有者，Redis claim、恢复、完成、失败和取消回队均以当前所有权为条件原子更新。旧执行者不能覆盖新结果、删除新 active key 或重复回队。Redis 状态 fencing 本身不能撤销 handler 已开始的外部操作，须逐类审核关键数据库提交和 RPC 的幂等/所有权边界，不能把状态 CAS 当成 exactly-once 执行。

先治理已证实的阻塞点，同时保留对进程长暂停、网络分区的所有权保护。恢复后旧处理应能检测失权；新的队列 generation 不应与 Agent 发布进度 generation 混用。兼容旧作业升级与停机恢复必须有明确规则。

## 严格验收要求

- 重跑上述真实租约到期交错，证明旧完成不能改变当前作业与去重键；失败/取消同样覆盖。
- 两个恢复者竞争、旧作业终态后同 key 新 job、心跳短暂失败、SIGKILL 后恢复与优雅停机回队均需真实 Redis 多进程证据。
- 关键副作用按 handler 逐类验证：复用已捕获 torrent 的下载器幂等证据，覆盖旧 worker 在 RPC 前/后失权，不宣称通用撤销能力。
- 队列入队顺序、并发容量、优先队列、HTTP 状态和进度查询兼容；完整 unit/API >=95%、唯一项目完整集成 >=85%，应用正常退出、导出和清理后才关闭 B4。

下一步先设计原子 claim/ack 的数据模型与旧作业升级规则，列出副作用边界，写失败/取消/同 key 新 job 的负向测试；不在尚未证明的方案上直接关闭 TODO。

专用 Redis 项目 down -v 已退出 0；探针 Ruff 和文档 diff whitespace 检查通过。未修改运行实现。

## 原子所有权协议草案（待实现与负向验证）

当前 `_run` 开始时不检查 msg.job_id，结束时 pipeline 无条件写状态并删除 active key；`update_progress` 仅检查 hash 存在，`clear` 无条件删除。同 key 的新 job 也可能受旧执行影响。恢复虽检查 job_id/status，但检查与恢复 pipeline 分离，消费者租约也在另一读操作中检查。仅给最终成功写入加条件不能覆盖这些路径。

| 操作 | 必须原子验证的状态 | 成功变化 | 失权行为 |
|------|------------------|----------|----------|
| 入队 | active key 空闲 | active/job hash/持久 descriptor 一起建立 | 保持已有作业 |
| claim | descriptor job_id 等于 hash 和 active、状态 queued | 设置新的执行 token 和 consumer、running | 不运行 handler，只清理本次无效 descriptor |
| 心跳 | 当前 consumer/执行仍有效 | 更新其租约，不复活被替代执行 | 取消/阻止后续受保护操作 |
| 恢复 | 原 consumer 租约缺失且 hash 仍属于该执行 | 撤销旧 token、queued、重新发布一次 | 不改新所有者 |
| 进度/完成/失败 | job_id + 执行 token + consumer 均匹配 | 仅更新自己的 hash；终态条件释放 active | 不覆盖新状态，不删除新 active |
| 优雅取消 | 仍拥有执行 | 单次回队并保留当前 job 的去重键 | 不再回队旧消息 |

候选实现使用 Redis 原子脚本或 WATCH/MULTI 事务封装上述边界，不能把“先 GET 再 pipeline”称原子。执行 token 是每次 claim 的随机代次，区别于逻辑 job_id；每次租约恢复必须撤销旧 token，即使逻辑 job_id 不变。进度写入需要传播当前执行上下文，禁止通过全局当前 key 推断所有权。清除/重试管理操作需明确是否撤销执行，避免静默改变已有 API 行为。

升级策略先要求同版本 worker 停启，兼容旧 queued descriptor（claim 时补 token）；旧 RUNNING 无所有权记录必须通过确定的恢复路径处理，不能继续允许旧 writer 无条件完成。此策略须与现有 startup legacy recovery 测试和部署文档对齐后定稿。

## 副作用审核范围

`register_all_handlers` 当前注册 17 类作业。队列状态修复必须共用同一协议，业务侧按以下范围逐一验证，不能由合成标记复现推广为已修复全部 handler：

- `run_agent`：候选持久化、下载提交 RPC、AgentRun/水位线确认；B7 的 generation 是用户范围保护，不是队列执行所有权。RPC 已接受后的重试仍需 infohash 幂等与持久任务关联。
- `fetch_channel`、作品刷新、重解析及 backfill：资源创建/绑定/发布事务，旧执行结果不能覆盖新人工编辑；远程只读抓取和模型调用可重复但需审计代价。
- `download_notifications`、`refresh_resource_organize`、`sync_progress`：webhook 投递、文件整理和完成状态提交的事务外副作用，核对现有租约、快照、文件内容/no-replace 保护，不能仅靠 Redis token。
- 清理、去重、FTS 和 magnet 作业：核对幂等、写入所有权和对象删除后的结果应用；连接检查与分析输出也要阻止旧进度覆盖新 job。

B7 的 km/kn 门禁仍使用原冻结实现，本草案未修改运行代码。下一步先为旧完成/失败/取消及同 key 新 job 的状态污染写独立负向测试，再实现原子队列协议和关键副作用边界。

## 六种状态污染负向用例（kr）

在独立 /tmp/rssripple-v14-ownership-kr 增加正式测试文件，生产 RedisQueue 方法配 fakeredis、两个消费者；确定性暂停旧 handler，分别让新消费者恢复同 job 或 clear 后运行同 key 新 job。旧 handler 成功/失败/取消六种组合均改变新执行状态，6 failed、1 warning，0.18 秒、退出 1；失败点均为当前状态不应变化的断言。此结果为必要性红测，非修复通过；实际租约与多进程证据仍以 kq 为准。

测试补丁、日志/JUnit 和源码哈希保存在 probes/queue-ownership-*。未修改 B7 原型或冻结门禁，运行实现未变。下一步在 V14 副本实现 claim token 与有条件的完成/取消，随后把同 key 并发、进度和恢复事务全部纳入同一协议；不能只让这六项变绿便关闭 B4。

## claim/完成事务原型（ks）

仅在 V14 副本实现：claim 用 WATCH/MULTI 原子检查 job_id/queued/active/consumer lease，写入每次执行独立 token 与 consumer；完成、失败和取消回队检查同 job/token/consumer/running/active/有效 lease 后才更新。失权路径不修改当前状态，也保留 descriptor 供恢复清理。取消有效执行时清 token 再单次回队。无 handler 的失败也经过同一所有权路径。

六种负向测试加完整既有 test_task_queue：61 passed、1 warning，2.77 秒、退出 0。补丁与源码哈希保存在 probes/queue-ownership-prototype.patch 和 queue-ownership-ks-result.json，日志/JUnit 同前缀。运行代码未合入 main，也未触碰 B7 原型或门禁。

该结果仅支持局部状态保护：旧 recovery 读检查/写事务分离、进度无所有权上下文、enqueue 非整体原子和业务副作用仍未解决。还需复跑真实 Redis 双进程，明确旧 handler 副作用是否仍可重复；不能用 61 项通过宣称已修复 B4。下一步先统一恢复/进度/入队原子协议并审查执行上下文传播。

## 进度所有权上下文（kt / ku / kv）

新增两项旧 handler 写进度交错，kt 两项失败，证明只保护最终状态不足。原型用 ContextVar 传播不可变 queue/key/job_id/token 执行上下文，claim 成功后设置，finally 恢复；Redis update_progress 原子检查当前运行身份及租约，只允许拥有执行的同队列同 key handler 更新。不能将无执行上下文的任意调用当作可信当前执行。

ku 62 passed / 1 failed（2.72 秒）：旧测试在 producer-only 队列外部直接写进度，按新契约被拒绝。将该测试改为真实 handler 写入、另一消费者实例读取，保留原内容断言，并补无上下文写入不能覆盖；kv 完整队列回归 63 passed / 1 warning，2.80 秒、退出 0。旧进度覆盖红测仍保留。修改文件哈希、补丁、三轮日志/JUnit 均已保存。

当前只保护队列状态和进度，仍未保护业务提交/RPC，也未重写恢复的原子边界；B4 继续待办。B7 km/kn 冻结副本未变化，门禁仍运行。

## 恢复与续租交错（kw / kx）

kw 负向用例在恢复外层确认租约消失后、读取 job hash 时让原消费者重新续租；实际恢复仍将活跃任务改 queued（1 failed，0.14 秒）。原型新增原子 descriptor 恢复：WATCH job/active/lease/processing list，事务内重新核对租约缺失、job 与执行 consumer 一致、状态可恢复、active 相符和 descriptor 仍在原列表，再撤销旧 token/consumer、queued、推回队列并移除原 descriptor。续租、终态或其他所有者变化均使恢复失效/重试。

kx 新旧队列测试 64 passed、1 warning，2.72 秒、退出 0。红绿日志/JUnit 与结果已保存；当前补丁和源码清单统一为 queue-ownership-prototype.patch / queue-ownership-current-source.json，可用 /tmp/refresh_v14_ownership.py 刷新。入队原子性、恢复锁条件释放、真实 Redis 复验与业务副作用所有权仍待完成，未关闭 B4。B7 两个完整门禁仍运行，冻结副本未改。

## 入队持久性边界（ky / kz）

ky 在 pipeline 执行前注入故障，原实现先 SETNX active 再事务建 job/descriptor，遗留无实际作业的 active key，负向测试失败。隔离补修先 WATCH active，再同一 MULTI/EXEC 创建 active/hash/descriptor；先完成 payload 序列化，避免序列化错误先留下占位。queued 状态清空执行 token/consumer，下一 claim 独立分配。

kz 完整队列回归 65 passed、1 warning，2.69 秒、退出 0，保持优先队列和重复入队行为。仅覆盖事务执行前失败，不把 Redis 执行后响应丢失描述为事务回滚；未知响应仍依靠 active/job_id 查询与幂等。补丁、当前源码哈希、红绿日志/JUnit 已保存，尚需真实 Redis 并发和故障复验、恢复锁条件释放及业务副作用保护。

## 恢复锁释放竞争（ll / lm）

必要性复核：恢复 finally 原先 GET 比较 consumer_id 后 DELETE，在两次命令之间锁过期并被其他恢复者获取时，旧调用可删除后继锁。同一 consumer 的多次获取也不应共享所有权令牌。ll 确定性测试在读取后替换为 successor-token，原代码删除后继锁，1 failed、0.14 秒、退出 1。

隔离原型改为每次获取生成 UUID token，释放时 WATCH 锁、校验 token、MULTI/DELETE/EXEC；WatchError 重读后退出，不误删后继锁。取消时继续依赖 TTL，不新增取消路径 Redis 等待。lm 完整队列单元回归 66 passed、1 个既有弃用警告，2.74 秒、退出 0。日志/JUnit 与补丁/源码哈希已保存。此证据来自 FakeRedis，仍需要真实 Redis 多进程验证；业务副作用保护、旧消息升级路径及全量验收尚未完成，B4 不关闭。

## 真实 Redis 双进程修复复验（ln）

唯一项目 rssripple-v14-lease-ln，Redis 7，本机随机端口，两个独立 Python 进程，原 15 秒租约/5 秒心跳未改。A 阻塞 22 秒，父进程确认租约消失且 A 存活后启动 B；两进程正常退出。starts=[A,B]、effects=[B,A]，最终状态 owner=B，证明旧完成不能覆盖接管结果，同时明确证明副作用重复仍存在。探针和结果分别保存为 probes/lease_ownership_probe.py、queue-lease-ln-result.json。测试使用合成标记而非真实业务；项目 down -v 退出 0。B4 不可仅凭状态保护关闭。

## 旧消息升级恢复原子性（lo / lp）

必要性：旧 RUNNING 无 descriptor 的启动清理在 hgetall 后无条件写 FAILED/delete active，若同 key 新作业在读写之间替换，旧扫描可破坏新作业。lo 确定性替换测试复现新 queued 被改 failed，1 failed、0.13 秒。

原型新增 WATCH job/active 的条件清理，重新检查 expected job_id、running、无 message/执行 token；只有 active 仍属于旧 job_id 才释放去重键。lp 全队列回归 67 passed、1 个既有弃用警告，2.66 秒、退出 0；变更 Ruff 通过。补丁与当前哈希已刷新，原始红绿日志和 JUnit 持久保存。仍待真实恢复/取消竞争矩阵、业务副作用边界及完整门禁。

## 下载发起前的执行所有权（lq / lr / ls）

实际 create_and_submit_task 负向用例在 handler 暂停时替换其 execution_token，旧执行恢复仍调用一次 RPC 替身，lq 1 failed、0.24 秒。原型增加 require_execution_ownership，通过 Redis MULTI 一致读取 job hash、active 和 consumer lease，要求完整运行身份匹配；无 Redis 上下文的手动 API/MemoryQueue 保持既有行为。业务入口在创建任务前检查，检查失败在 RPC 异常捕获范围外传播，不伪装为普通下载失败。

lr Agent/队列回归 178 passed、1 warning、127.28 秒。Ruff 指出新增异常命名缺 Error 后缀，改为 ExecutionOwnershipLostError 后 ls 68 passed、1 warning、2.71 秒，Ruff 通过。该检查不能撤销已发起 RPC，亦存在检查后失权窗口；下载副作用的幂等与持久关联、其他 16 类 handler、租约丢失后的取消治理仍待完成。此批 RPC 为替身，无真实媒体下载，不能据此关闭 B4。

队列核心补丁仍为 queue-ownership-prototype.patch。下载入口/测试/权威业务文档单独保存为 queue-download-ownership.patch，基于 V13 原型并依赖前述队列补丁；不是对当前 main 可独立应用的整批变更，避免把未验收 B7 运行代码混入 B4。配套 source.json 保存哈希。B7 lj/session 84551 与 lk/session 66950 仍运行，最新日志分别约 81%/6%，未改冻结源码。

## 下载准入完整身份矩阵与真实接管（lt / lu）

lt 将业务入口回归扩至八种情形：token/job_id/status/consumer/active 失配、lease 缺失、Redis 读取失败均在 db.add/flush 与 RPC 前停止，并验证具体异常类型；合法持有者正常创建并提交。完整队列相关回归 75 passed、1 warning、3.01 秒，退出 0，Ruff 通过。

lu 使用真实 Redis 7、两个独立进程、默认 15 秒租约/5 秒心跳，A 同步阻塞 22 秒，B 接管后完成；业务操作前调用生产 require_execution_ownership，A 恢复被拒，starts=[A,B]、effects=[B]、最终 owner=B，两个进程退出 0。此为合成标记测试，不是下载器端到端幂等验收。源码/结果/日志保存为 probes/lease_guard_probe.py 与 queue-guard-lu-*；唯一项目 rssripple-v14-guard-lu 清理退出 0。

阻塞路径初审：RSS feedparser 本已通过 asyncio.to_thread 执行，不应仅凭库为同步实现再次改造或宣称它阻塞 heartbeat。torrent 读取/解码和文件写入仍有同步调用，需测量与真实执行路径证据后决定治理；本轮没有未经论证扩改这些路径。下一步仍为业务副作用幂等/持久关联、其余 handler 与真实取消/崩溃恢复矩阵。B7 两项完整门禁继续原进程，未重启或修改冻结内容。

## 已接受 RPC 后的接管：真实数据重复落库（lv / lw）

独立项目 rssripple-v14-download-lv 使用真实 PostgreSQL 16、Redis 7、Transmission，内部网络无媒体来源。使用 confirmed case f79ef2eb-02d5-42d3-80dc-70dd3c1d733b 的原始 torrent，SHA256 7f2b6d96aeeb800355ca64985173449a91b4b76df85edeba6a105368e64dd813；通过生产 RedisQueue 与 create_and_submit_task，A 的真实 RPC 返回后暂缓将结果交给业务，强制租约失效，B 实际恢复同一 job 并完成落库，最后释放 A。这里是确定性模拟 lease 丢失，不是本次测得 CPU 阻塞。

lv 首次因 Channel 种子漏 field_mapping 在插入阶段失败，未进入 RPC；修正夹具后 lw 退出 0 表示缺陷已证实：rpc_ids=[1,1]、Transmission 仅 1 个 torrent、PG 持久任务 2 条且 torrent_id 都为 1。peers_connected=0、received_media_bytes=0。日志、驱动、Compose 和结果均存 probes/queue-download-l*、download_takeover_probe.py、compose.download-lv.yml。此调用实际下载创建入口而非整个 Agent handler，因此不能据此断言所有上层组事务都必定重复；它已证明共享下载创建入口本身不具备接管幂等性。

### 后续方案论证：持久派发身份

仅在 RPC 返回后再检查 token 只能缩小窗口，不能保证检查与 commit 之间不被接管；给 downloader/torrent_id 无条件全局唯一也可能误合并不同 Agent 的合法任务。候选方案是对队列逻辑作业与派发操作建立稳定身份（接管/重试保留、独立新操作区分），用数据库唯一性及冲突复用原子保证一条本地任务；任务参数必须冻结或校验一致。RPC 幂等仍需依靠下载器的 torrent 身份，并明确不同目录或 payload 不得悄悄共用。

实施前须审查 job_id 当前长度/重用风险、同一作业多个资源和不同 Agent 的身份边界、手动 API 无队列上下文的既有行为、取消后重新派发、数据库冲突时整组事务回滚，以及主库/队列跨系统提交失败。门禁必须包含 lw 变绿（1 条任务）、真实 COMMIT 失败重试、接管前后不同目录拒绝/隔离、两后端迁移及直接并发写入。不能以本轮新增检查或 Transmission 去重宣称 B4 完成。

lw 独立项目 down -v 已退出 0，PostgreSQL/Redis/Transmission 容器、专用卷与网络均已删除，cleanup_exit_code 已写入结果。

## B7 合入后的续接基线

本地 main 已含 B7 提交 edfceef。B4 原型仍在 /tmp/rssripple-v14-ownership-kr，运行实现未合入；可恢复队列核心补丁为 probes/queue-ownership-prototype.patch，下载入口/测试/业务文档补丁为 probes/queue-download-ownership.patch，两者现在均以含 B7 的主干为适用基线。继续前核对 source.json；不要把整个旧原型目录覆盖到主干，旧原型其余文件未包含 B7 最终配置/FTS 修复。

当前无运行中的测试或专用 Compose 项目。下一步先落定持久派发身份设计，再用已保存的 lw 真实数据重复落库探针验证；补齐真实取消/崩溃恢复矩阵及其余 handler 的副作用边界。V14 的局部绿测不替代新的完整单元/API 与集成门禁。

## 持久派发身份原型与真实接管转绿（lx–mc）

新原型 /tmp/rssripple-v14-dispatch-lx 从已验收 main 复制并应用 B4 补丁，含 B7 最终配置/FTS 修复。稳定身份选择 queue key/job_id/资源/Agent；新 job_id 使用完整 UUID hex（旧 queued ID 继续读取）。DownloadDispatch 用独立短事务预留任务 UUID 和冻结参数，RPC 完成后主业务事务锁预留行、按 UUID 插入一次并标记 settled。参数漂移拒绝、已保存状态不覆盖、删除任务保留 tombstone；手动 API 无上下文保持既有路径。两个事务之间无数据库写锁跨 RPC。保留/清理政策、跨系统故障及调用方无写锁前提仍需验收。

lx 两项身份/持久化数据库测试通过；ly 真实测试 DB 的准入矩阵与队列回归 77 passed、1 warning、7.49 秒。lz 使用相同已审核 torrent、新独立 PG/Redis/Transmission 栈，A RPC 接受后失去租约、B 接管先落库、A 再完成，rpc_ids=[1,1]、daemon_torrents=1、persisted_tasks=1；peers=0、媒体字节=0，探针退出 0，down -v 清理退出 0。驱动 download_takeover_reserved_probe.py、compose.download-lz.yml 与 queue-download-lz-* 已保存。

ma 原有 Agent/手动任务 API 131 passed、1 warning、55.20 秒、退出 0。随后原始 SQL 默认值检查 mb 发现字符串 'false' 在 Turso 被读成 True，1 failed、0.62 秒；改用 SQLAlchemy false()，mc 直接写入/身份/准入 11 passed、1 warning、4.78 秒。Ruff 通过。ma/lz 发生于默认值修正之前，不能描述为最终全量门禁。

**最新完整候选补丁**为 probes/queue-dispatch-prototype.patch，源码哈希 queue-dispatch-source.json，固定基线 f9d4a0a；已 git apply --check。该补丁包含并取代应用步骤上的旧 queue-ownership/queue-download 补丁，不应重复叠加。刷新工具 /tmp/refresh_v14_dispatch.py 固定 git 基线，防止后续主干变化丢失差异。权威模型/业务/迁移和测试清单在候选补丁内同步，未合入运行代码。当前全部测试均终态、无专用 Compose 项目。

下一步：真实 COMMIT 失败/崩溃恢复、不同目录/payload 的同身份拒绝、新操作/不同 Agent 不误合并、两后端旧库升级及直接唯一约束、任务删除与预留保留清理、其他 handler 副作用，之后才是冻结完整门禁和合入评审。

## 回滚、参数漂移与真实 COMMIT 故障（md / me / mf）

md 在真实测试库中先写入任务与 settled，再触发 NOT NULL 约束故障；rollback 后任务为零、独立预留仍存在且 unsettled，重试保持原 task_id 并仅一条任务。test_download_dispatch 4 passed、1 warning、1.81 秒。me 通过生产队列上下文与测试数据库分别改变 directory/payload/downloader，均在第二次 RPC 前抛参数变化错误，第一次任务保留；准入文件 11 passed、1 warning、4.67 秒。

mf 独立 PG/Redis/Transmission 栈使用同一 confirmed torrent，测试专用 DEFERRABLE INITIALLY DEFERRED 外键允许 flush，但令第一次 COMMIT 实际失败；随后在同一逻辑 Redis handler 的新事务中重试。rpc_ids=[1,1]，两次 proposed task UUID 完全相同，最终一任务、一 torrent，预留 settled=true，peers=0/媒体字节=0。退出 0、down -v 清理退出 0。驱动 download_commit_reserved_probe.py 和 queue-commit-mf-* 已保存；此是显式恢复测试，不是自动 handler 重试或独立进程崩溃恢复证据。

最新 aggregate patch/source 已刷新，仍未合入。下一步保持两后端升级/唯一约束、自动崩溃接管、预留清理政策和其他 handler 边界的验收要求；不能以本轮事务回滚通过缩小 B4 范围。当前所有测试已结束，无专用运行栈。

## SIGKILL 自动接管与独立预留连接（mg / mh / mi）

mg 独立 PG/Redis/Transmission 项目，两个独立 Python worker 进程；A 在真实 captured torrent RPC 接受后、任务落库前挂起，父进程确认预留已持久且无 DownloadTask 后 SIGKILL A（-9）。B 使用生产 RedisQueue 启动，默认 15 秒 lease/5 秒 heartbeat 未改，无人工删租约或手动调恢复回调；自然过期后自动接管并退出 0。同一 job_id、同一预留 UUID、rpc_ids=[1,1]，最终一任务一 torrent，媒体字节=0、peers=0。驱动 download_crash_reserved_probe.py、结果/日志 queue-crash-mg-* 已保存，项目清理退出 0。该测试为共享下载业务入口的专用 handler，不代表全部 17 类生产 handler。

mh 连接绑定边界负测确认：AsyncSession(bind=已有 AsyncConnection) 的新会话加入外层事务，外层 rollback 抹掉预留，1 failed、0.55 秒。reserve_dispatch 改为从 AsyncConnection.engine 取独立连接，原 AsyncEngine 路径保持；mi 身份/准入/参数漂移回归 16 passed、1 warning、6.42 秒，Ruff 通过。mg 在此修复前使用引擎绑定路径通过，不能充当最终所有路径全量验收。

aggregate patch/source 及候选权威文档/测试清单已刷新。当前所有进程终态，专用栈已清理。下一步两后端旧库建表与直接唯一约束、预留清理/保留、同资源不同 Agent 与新操作的集成隔离、剩余 handler 的副作用，之后冻结完整门禁。

## 两后端旧 schema 升级与直接 SQL 约束（mj / ml / mk）

新增正式集成 test_download_dispatch_schema.py 和共用 driver，旧 schema 为当前基线全部表但缺 download_dispatches，并保留 AppSetting 哨兵。生产 create_tables 第一次补表后插入预留，再第二次启动，旧哨兵和预留均保留；直接 SQL 对重复 operation_key、重复 task_id、NULL settled 分别触发 IntegrityError，默认 settled=False。

mj 旧库夹具未启用既有 Turso MVCC，在种子 INSERT 阶段失败，未进入迁移；修正初始化后 ml 正式子进程测试 1 passed、0.98 秒、退出 0。mk 同驱动在专用 PostgreSQL 16 上退出 0，全部断言通过；项目 rssripple-v14-schema-mk 清理退出 0。日志、JUnit 和 PG 结果保存为 queue-schema-*。Ruff 通过，正式测试/driver 已加入最新 aggregate patch 清单，Turso 场景纳入常规集成收集。此证据不涵盖任意更早的 schema、实验原型表升级或历史任务业务重写。

下一步优先定稿预留保留/清理：不能只按年龄删除仍可被接管的预留，也不能在任务删除后立即抹掉防重建记录。需要同时考察逻辑作业终态/缺失、现有任务及 Redis 不可用时保守跳过；现有 operation_key 是摘要，清理所需作业身份字段尚须设计。其余 handler 副作用验证继续保持范围，B4 仍未完成。当前没有运行中的测试或专用栈。

## 预留保留/清理协议原型（mm / mn）

必要性与方案：仅按年龄删除可破坏仍在排队/接管的稳定 UUID；永久保留全部预留又会随已删除任务增长。候选增加 job_key/job_id 和 created_at 索引；7 天以上、无对应任务、Redis 确认原作业退休三条件同时满足才删。queued/running、active 孤儿、异常状态或身份缺失保留；Redis 不可用停止后续清理。终态不回到 queued，重新入队使用全新完整 UUID；不得复用旧逻辑 job_id。

清理按 created_at/id 分页（500 条），数据库读取结束后再调用 Redis，删除时重新检查无任务且 settled 未改变。已有任务永不因此删除预留。接入每日清理原事务提交后的阶段，并沿用该会话数据库绑定；AsyncConnection 归一为其 engine 获取独立连接。

mm 队列/派发/保留矩阵 86 passed、1 warning、11.08 秒；随后补已有任务保留与 scheduler 接线，mn 33 passed、1 warning、9.67 秒，Ruff 通过。证据 queue-retention-mm/mn.*。此轮尚未做真实 Redis 清理竞争验证；新增列后必须重跑两后端 schema 验收。

最新 aggregate patch 已包含模型、清理服务、每日入口、测试及权威文档更新；当前没有运行测试或专用栈。继续真实清理/删除竞争、同资源不同 Agent/新操作隔离和其余 handler 边界，再进入最终全量验收。

## 当前 schema、真实清理竞争及操作隔离（mo / mp / mq / mr）

mo 新字段后的 Turso 正式升级测试 1 passed、0.88 秒、退出 0；mp 同驱动在独立 PostgreSQL 16 上重复生产 create_tables、保留哨兵/预留、直接 SQL 唯一/非空/默认值断言均通过。

mq 使用同项目真实 PG/Redis：三条退休/缺失/替换的孤立预留删除，queued/running 保留。另让实际 persist_dispatch_result 的事务先锁预留并写任务，清理尝试 DELETE；监测 pg_stat_activity 明确观察 wait_event_type=Lock，再释放 writer 提交。DELETE 返回 0，已落库任务和 settled 预留均保留。驱动 dispatch_cleanup_pg_redis_probe.py 与 queue-cleanup-mq-* 已保存。数据为合成持久化夹具，无 RPC。项目 rssripple-v14-schema-mp 清理退出 0。

mr 增加实际队列上下文/测试数据库的两种隔离：同逻辑作业内不同 Agent，及同 key 的新逻辑作业。下载器替身均返回同一 torrent ID，但分别保留两条不同任务 UUID 与正确 Agent 归属；新作业 ID 独立且为 32 hex。准入/漂移/隔离测试文件 13 passed、1 warning、5.80 秒，Ruff 通过。

aggregate patch/source 与候选权威文档/集成清单已刷新。当前无运行测试/专用栈；下一步集中审核其余 handler 的业务副作用与阻塞路径，不能把共享下载入口的覆盖等同于 B4 全部完成。

## 通知发送与结果确认的队列边界（ms / mt / mu / mv）

必要性：在真实队列上下文撤销 token，生产 deliver_due_deliveries 仍发送 HTTP；若发送过程中失权，仍将 pending 标记 done。ms 两参数红测 2 failed、1 warning、1.33 秒；使用 HTTP 替身，无外部消息发送。原型在 semaphore 内发送前及 commit_lock 内结果变更前调用 require_execution_ownership，失权不进入普通 HTTP 失败退避。mt 新旧通知回归 48 passed、19.08 秒。

新增异常路径暴露 gather 提前返回问题：一条失权错误向外传播时，其他 HTTP 仍在途，共享会话可能先被释放。mu 专门交错测试 1 failed、0.85 秒。改为等待所有在途协程结果后再传播首个异常；mv 49 passed、1 warning、19.90 秒，Ruff 通过。fixtures 复用改用模块别名消除 F811。日志/JUnit queue-notify-* 已保存，相关运行代码、测试、通知权威文档纳入最新 aggregate patch。

边界：无法撤销已经发出的 HTTP，两个执行之间的校验/提交窗口与远端幂等仍需专门验证，不将此称为 exactly-once。下一步为真实通知接管竞争和单条 delivery 状态并发更新；另外通知快照生成时的停种/文件获取及其余 handler 仍未完成审核。

文件整理源码复核：execute_plan 使用 registered_domain + async_plan_lock 的跨进程 flock，run_execution 经 owned_thread 在线程执行，finish_before_cancel 保持线程真实结束及结果确认前不释放锁；结果按 revision/owner CAS 写入。此为此前 P0 机制，本轮未改写，也不据源码阅读声称已完成队列接管组合测试。当前所有测试终态，没有运行中的专用栈。

## 通知结果覆盖复现（mw，未修复）

必要性复核：最终 Redis 检查与数据库提交不原子。新增确定性交错测试，在旧执行第二次 guard 内用不刷新 ORM identity map 的直接 UPDATE 提交另一执行的 done，然后让旧执行处理 HTTP 失败。notify_max_attempts=1 时最终实际为 failed，期望 done：1 failed、3 deselected、1 warning、0.75 秒，退出 1。日志和 JUnit 保存为 queue-notify-mw.*；红测纳入候选 aggregate patch，当前候选不满足合入条件。

证据范围：真实测试数据库及生产投递入口，HTTP 替身、人工控制交错；不是独立进程/真实 Redis 或真实 HTTP 接管测试。它证明旧 ORM 快照能覆盖已提交成功结果，不证明接管调度的完整时序。

方案要求：发送前建立每条 delivery 的持久尝试身份，结果仅允许匹配该身份的条件更新；人工 retry 与 regenerate 必须使旧身份失效。仅追加 status=pending 条件可保护 done，却不能区分重置后再次 pending 的新一代，不能作为完整修复。须检查三个重置入口及快照更新的一致性，数据库锁不跨 HTTP；崩溃后的尝试恢复不能永久卡住 pending。实施后至少覆盖旧失败/新成功、旧成功/新重试、快照重新生成、进程崩溃恢复与并发 fan-out，两后端验证条件更新/迁移，再使用本地 HTTP 服务完成真实接管验证。继续保持至少一次投递语义，不能承诺远端恰好一次。

当前 mw 测试已终态，没有因此启动 Compose 栈。下一步是上述尝试身份协议的设计和实现，B4 仍保留待办。

## 持久投递尝试身份与快照失效（mx / my / mz）

方案实施在隔离候选中：WebhookDelivery 新增 nullable attempt_token；发送前按 id、pending、旧 token 条件更新为 UUID 并提交，结果按 id、pending、本次 token 条件更新。每条领取/确认使用短事务及共享会话锁，HTTP 不持有数据库写锁；崩溃保留 pending，后来读取可换 token 继续尝试。人工 retry 换 token，两个 regenerate 在更新快照同一事务中使全部关联 delivery 失效。旧 schema 新列为强制启动迁移，失败不能吞掉，部署须停旧 worker。

mx 原通知与所有权回归 50 passed、1 warning、20.88 秒，mw 覆盖缺陷转绿。my 新增三个实际 retry/regenerate 入口在途交错，2 failed、5 passed、3.83 秒：关系集合可能在 fan-out 之前缓存为空，遍历 existing.deliveries 漏掉需失效的投递。改为按 notification_id 直接 SQL 更新持久集合，保留该失败证据。

mz 同一集合加正式 Turso schema 集成 54 passed、1 warning、21.52 秒、退出 0；驱动从旧表删除 attempt_token，再由生产 create_tables 恢复 nullable 字段，并保留既有两次启动/派发约束验证。Ruff 通过。日志/JUnit queue-notify-mx/my/mz.*；aggregate patch 新增模型和 database.py，并同步通知、模型、迁移与集成清单文档。

限制与续接：目前通知 HTTP 为替身，交错由测试控制，不是真实 Redis/独立进程 HTTP 接管证明；新字段的 PostgreSQL 升级、尝试领取并发/进程崩溃恢复、真实 HTTP 接管仍须补齐。token 条件写入并不使远端 HTTP 恰好一次；领取后失权仍存在 HTTP 发出窗口。快照生成 RPC 与其余 handler 审核、B4 冻结完整门禁及合入评审均未完成。候选未合入 main。本轮全部测试终态，没有启动 Compose 项目。

## PostgreSQL 新列升级与真实 HTTP 交错（na / nb）

必要性：mz 仅证明 Turso 升级和 HTTP 替身路径，不能代替 PostgreSQL UPDATE/RETURNING 及真实 HTTP 在途交错。na 在独立 rssripple-v14-schema-na PostgreSQL 16 栈执行生产 schema 驱动，旧 webhook_deliveries 删除 attempt_token 后两次 create_tables，nullable 字段恢复、旧 AppSetting 与新派发预留保留、直接 SQL 唯一/非空约束均通过，退出 0。结果 queue-schema-na-result.json。

nb 使用同一隔离库、两个独立 AsyncSession 和 asyncio 本地 HTTP 服务，实际 httpx POST 两次：A 接受请求后挂起，B 领取新 token 并收到 200、确认 done，随后 A 收到 500。最终 done、attempt_count=0、error_message=NULL，token 保持 B；A 返回 skipped=1，B delivered=1，两个请求内容一致。驱动 notification_http_pg_probe.py 与 queue-notify-nb-result.json/log 已保存，退出 0。驱动随后仅做 Ruff 导入排序/格式化，Ruff 通过。

数据是合成通知快照，不冒充完整媒体通知验收或真实下载样本；此轮未使用 Redis、无独立 worker 进程、未模拟领取前同时读取或 SIGKILL。证明范围仅为真实 PG/HTTP 下旧结果无法覆盖新 token 的结果。schema-na 项目 down -v 退出 0，清理日志已保存；没有本轮遗留服务。

下一步：领取前同时读取的 CAS 竞争、领取后崩溃的自动恢复、Redis 独立 worker HTTP 接管；其余 handler 审核和最终完整门禁仍是 B4 必需项。不得据本轮通过关闭 B4 或合入候选。

## 同快照领取与 Turso MVCC 冲突（nc / nd / ne）

必要性：nb 的第二个会话在第一次领取后才读取，不能证明两方读取同 token 的竞争。nc 新增 barrier，两个独立会话都完成读取后才同时领取；真实 Turso 返回一方 Write-write conflict 而非 skipped，1 failed、7 deselected、0.74 秒。只有一次 HTTP，但失败方未能正常结束，因此保留红测。

候选改为批次读完先 commit，领取/确认各用独立短会话事务，复用 retry_on_lock 仅重试数据库条件 UPDATE（固定 id/status/token 和结果），不重发 HTTP。成功后 set_committed_value 同步已加载对象，避免下一次 flush 重放状态；失败重试不 rollback 其他在途协程共用的调用者会话。nd 通知及并发回归 54 passed、1 warning、22.31 秒，Ruff 通过。

ne 新隔离 rssripple-v14-schema-ne 栈重跑 PostgreSQL 旧表升级/约束与真实 loopback HTTP 两会话迟到失败测试，均退出 0；仍为两个实际请求、旧 skipped=1、新 delivered=1、最终 done/attempt_count=0。项目 down -v 退出 0，日志/结果已保存。HTTP 驱动允许显式 rssripple-v14-schema- 前缀的独立项目并验证容器标签；仍要求空投递表，不连接默认栈。

当前所有测试终态，本轮栈已清理。仍须 Redis 独立进程自动接管/崩溃恢复、其余 handler 和完整冻结门禁，B4 未完成、未合入。

## 通知独立进程自然接管（nf / ng）

必要性：两会话 HTTP 竞争不能证明 Redis 作业恢复、进程死亡/暂停及业务 token 组合正确。nf/ng 分别使用全新 rssripple-v14-notify-nf/ng 项目，真实 PostgreSQL/Redis、本地 asyncio HTTP 服务、两个独立 Python worker。生产 RedisQueue 默认 lease=15 秒、heartbeat=5 秒未改；未删除租约、未手动调用恢复方法。

nf：本地服务收到 A 的实际请求，父进程确认数据库 pending 且领取 token 已提交后 SIGKILL A（退出 -9）。B 自然恢复约 15.844 秒，退出 0。同一 job_id 最终 done；第二次 HTTP 与第一次内容一致，delivery 使用新 token，最终 done/attempt_count=0/error_message=NULL。

ng：A 发出请求后 SIGSTOP，B 在自然租约过期后接管并完成（约 15.842 秒），再 SIGCONT A 并让旧请求收到 500。A 明确捕获 ExecutionOwnershipLostError；两个 worker 退出 0，但旧 handler 失权失败，不能将其写成成功投递。最终 Redis 同一 job_id 仍 done，delivery 仍由 B 的新 token 确认，attempt_count=0。日志包含恢复事件和旧 handler 失权信息。

结果 queue-notify-nf/ng.json、日志、源哈希与清理日志已保存；驱动 notification_crash_pg_redis_probe.py / notification_pause_pg_redis_probe.py。五个关键文件的记录哈希在测试结束后核对不变，Ruff 通过。两项目 down -v 退出 0，所有子进程终态，本轮没有遗留栈。

证据边界：合成通知 payload，无实际媒体下载/通知快照生成验证；专用 handler 调用生产 deliver_due_deliveries，并非完整 scheduler tick 或所有 17 类 handler。真实 HTTP 接受了两次，符合至少一次语义，不宣称外部恰好一次。本轮未改生产候选代码。后续须将接管回归纳入正式集成入口，继续快照生成 RPC/organize/其余 handler 审核与最终冻结门禁；B4 尚未完成或合入。

## 正式通知恢复集成入口与强制门禁（nh / ni / nj）

必要性：nf/ng 仅有独立探针，无法保证后续完整门禁自动回归；既有集成配置没有 Redis，且其他套件会 monkeypatch sleep。候选新增 tests/integration/queue_recovery，driver 独立进程隔离 monkeypatch；参数 kill/pause 对应真实 SIGKILL 与 SIGSTOP/SIGCONT。每例创建 queue_recovery_UUID PostgreSQL 库并 finally 删除，专用 Redis DB 0/1 先断言为空。预检非空时禁止清理别人的键；成功取得空库后才负责清理。90 秒超时终止整个 driver 进程组。

候选 docker-compose.integration-isolated.yml 增加两个无宿主端口的专用 PG/Redis 服务，test-runner 依赖健康并配置 QUEUE_RECOVERY_REQUIRED=1 及显式测试地址。完整门禁缺服务地址必须失败；普通局部无服务运行可 skip，不能算完整验收。isolated-integration.md 与测试清单同步。

nh 在新 rssripple-v14-notify-nh 栈只启动两个专用服务，以宿主 pytest 执行正式入口，2 passed、34.59 秒。增加预检失败不 flushdb 的保护后，ni 使用同轮专用服务和重新创建的独立数据库再跑，2 passed、34.66 秒，Ruff 通过。nj 移除两个服务地址但保留 REQUIRED=1，预期 2 failed、0.08 秒、退出 1，证明门禁不静默跳过。报告、每例 worker 日志/结果及清理日志已保存。项目 down -v 退出 0，全部测试终态。

本轮未运行 test-runner 容器和全套集成，不能宣称候选完整 Compose 门禁已通过；子进程覆盖率未另行纳入原四份合并，覆盖率标准不降低。新增测试/driver/Compose/文档纳入 aggregate patch。后续继续通知快照 RPC、其余 handler 与完整冻结门禁，B4 未完成、未合入。

## 快照生成 RPC 失权边界（nk / nl）

必要性：投递 token 不覆盖快照生成的停种/文件列表 RPC 及自动文件映射写入。nk 对 before_pause / after_pause / after_files 三个阶段设置失权，生产 build_task_notifications 原来仍继续生成且不抛失权异常，3 failed、8 deselected、1 warning、1.68 秒。测试使用真实 Turso、下载器替身与受控 ownership guard，不是实际 Redis 租约或 Transmission 验证。

候选在 _build_snapshot 起始、停种前、停种后及文件列表返回后检查队列所有权，检查位于 best-effort RPC 异常捕获之外；在构建事务删除失败记录前再次检查。build_task_notifications 对 ExecutionOwnershipLostError 直接抛出，不登记普通故障；_record_failure 也在入口和退避更新前检查，失权向外传播。手动 API 无队列上下文时原 guard 为 no-op。

nl 通知回归与 notification_build_retry / notify_poison_task 两个既有集成文件联合 61 passed、1 warning、26.51 秒、退出 0，Ruff 通过；断言相应阶段后没有多余 RPC、没有快照、没有 NotificationBuildFailure。日志/JUnit 已保存，notification_build.py 纳入 aggregate patch，通知与集成清单同步。

范围限制：本轮未消除 guard 到 commit/RPC 的窗口，未验证真正的 Redis 失权与下载器组合。后续应将快照 RPC 边界接入实际 Redis 上下文验证，并继续 organize、fetch/metadata、清理等 handler 的副作用审核。现注册 17 类作业，入口在 app/job_handlers.py:788 起，不能以下载/通知两个入口代替全部覆盖。本轮所有测试终态，未启动 Compose，B4 仍未验收/合入。

## 整理执行准入的队列失权检查（nm / nn / no）

必要性：既有计划文件锁及 revision/owner_token 可保护正在执行的同一计划，但不阻止已失权队列 worker 启动尚未执行的计划。nm 使用真实临时源文件和实际 execute_plan，控制 guard 为失权，原代码仍执行 move；1 failed、1 warning、0.81 秒。失败原因是未抛失权异常，不能称为真正 Redis 接管复现。

候选在取得文件锁及进程执行锁后检查队列所有权；文件路径预检结束、reserve_revision 前再次检查。失权直接退出，不启动文件线程。已开始的执行保持原有文件锁、版本确认和取消收尾协议，不因 Redis 失权提前释放锁。

nn 与既有 organize_service 回归联合 79 passed、1 warning、34.95 秒。no 再覆盖预检期间才失权的交错，两个参数 2 passed、1 warning、1.13 秒：源内容保留、无目标文件、计划仍 pending。Ruff 通过。测试使用受控 guard 和合成文件，尚未验证真实 Redis 接管与文件锁的组合；检查到执行之间仍有窗口。

候选代码、测试和 file-organization.md 已纳入 aggregate patch，日志/JUnit queue-organize-nm/nn/no.* 已保存。全部测试终态，本轮无 Compose 栈。下一步继续实际队列/文件锁组合、通知快照真实 RPC 及其余 handler 审核，再进入冻结完整门禁；B4 未完成、未合入。

## 整理收尾阶段的计划锁复核（np）

必要性及方案复核：源码中“锁外”是离开进程内 _executor_lock，外层 execute_plan 的计划文件锁仍有效。若在文件移动完成后因 Redis 失权中断收尾，done 计划后续短路可能留下未完成的下载器清理，因此本轮不增加这种中断。注释改为明确两个锁的范围。

新增真实临时文件 move 交错测试：文件执行结束后受控 guard 标记失权，生产任务清理入口挂起；第二独立 AsyncSession 调用 execute_plan 被文件锁以“正在执行中”拒绝。释放后原执行完成 done，清理入口仅调用一次，源已移动且目标内容完整。np 三项所有权测试 3 passed、1 warning、1.55 秒，Ruff 通过。日志/JUnit queue-organize-np.* 已保存。

边界：队列失权为受控 guard，竞争者是同进程第二会话；fixture 无关联真实 torrent，本轮未发送下载器 RPC，不能声称真实 RPC 幂等或跨进程接管组合验收。候选同步权威整理文档与 aggregate patch，未合入。全部测试终态，无本轮 Compose 栈；下一步保持真实 Redis/文件锁组合及其余 handler 审核要求。

## 元数据刷新批次吞掉失权异常（nq / nr）

必要性：17 个注册作业入口核对发现，refresh_works_metadata 与 refresh_channel_works 共用的 _refresh_works_batch 将所有 Exception 变成单作品失败并继续；ExecutionOwnershipLostError 也被吞掉。nq 对批次前失权、第一作品刷新中失权两种情况复现，2 failed、1 warning、1.08 秒。使用真实测试库的两部电影和网络/guard 替身，不是外部元数据或实际租约复现。

候选在每部作品开始前检查所有权，并让失权异常直接向外传播；普通错误、超时继续保持原隔离语义。nr 两参数加完整 test_job_handlers.py 共 28 passed、1 warning、6.83 秒，Ruff 通过。失权前零刷新，刷新中失权只调用第一作品，不进入第二作品。证据 queue-metadata-nq/nr.* 已保存，app/job_handlers.py 与测试及业务文档进入 aggregate patch。

入口审核的后续顺序：先完成上述两种 refresh 的共享服务内部写入保护；metadata_search.apply_work_metadata 和 season-0 路径存在内部 commit，不能仅靠批次外层检查证明安全。随后继续 fetch_channel / reprocess_resource_metadata / backfill_metadata / analyze_batch_files；再核对 sync_progress / daily_cleanup / daily_dedup / check_downloaders / fts_drain / fts_reconcile 及两个 magnet 作业。run_agent 下载派发、download_notifications 和 refresh_resource_organize 已有局部证据，但各自仍需组合与完整门禁。此为审核顺序，不代表其余入口已验收。

当前测试均终态，无本轮 Compose 栈，B4 未完成/未合入。下一轮优先检查单作品刷新内部 commit 与人工字段保护的竞争，避免重复运行已通过的批次测试。

## 单作品元数据应用的失权边界（ns / nt）

必要性：refresh_work_by_source 查询之后会调用含内部 commit 的 apply_work_metadata，批次外层检查无法阻止查询期间已失权的 worker 写入。ns 用真实 Turso 电影和候选查询替身，查询返回时置失权，原代码未抛异常且提交修改，1 failed、2 deselected、1 warning、0.73 秒。

候选在单作品刷新/应用入口、异步预览后、海报处理后及应用 commit 前检查队列所有权；season-0 日期回填写入前也检查。失权向事务所有者传播并回滚。nt 所有权回归、test_metadata_search.py 与 test_job_handlers.py 共 52 passed、1 warning、15.54 秒，Ruff 通过。新回归断言查询期间失权后标题仍为 Original，身份袋没有写入。证据 queue-metadata-ns/nt.* 已保存。

范围限制：候选网络和 guard 为替身；新用例只直接证明查询期间失权路径，不能视为每个新检查点或真实 Redis/PG 并发验收。既有人工字段保护测试通过不证明所有并发人工编辑时序。检查到 commit 的窗口仍存在；下一步须继续该窗口的必要性/方案论证及其他 handler 审核。metadata_search.py、业务文档进入 aggregate patch，B4 未完成、未合入。本轮全部测试终态，无 Compose 栈。

## 部分元数据修改的生产事务回滚（nu）

必要性：ns 的查询失权发生在数据修改之前，且测试手工 rollback，不能证明批次事务会撤销已经 flush 的部分写入。nu 新增海报请求返回时失权、真实 add_external_id 完成并 flush 后失权两个参数，调用生产 _refresh_works_batch → refresh_work_by_source → apply_work_metadata 全链路，异常由 committed_session 自动 rollback，测试不代替生产执行回滚。

以另一新事务读取，标题仍 Original、poster_url=NULL、WorkExternalId 无行；阶段记录证明分别到达 poster 与 identity_flush。全部所有权文件 5 passed、1 warning、2.21 秒，Ruff 通过，日志/JUnit queue-metadata-nu.* 已保存。未修改生产代码，仅扩展候选回归。数据为合成电影/候选，海报及失权 guard 为替身；无真实 Redis/网络或 PostgreSQL 竞争证据，不覆盖检查到 commit 窗口。

测试已终态，无 Compose 项目。下一步继续窗口方案及其他 handler 的审查，B4 不关闭、不合入。

## PostgreSQL 并发人工标题被刷新覆盖（nv，未修复）

必要性：队列失权检查不等于作品数据的并发保护。nv 在独立 rssripple-v14-metadata-nv PostgreSQL 16 项目中，让 apply_work_metadata 在海报 await 处暂停，另一真实 AsyncSession 提交 title_cn=Manually corrected 和 manually_edited_fields=[title_cn]，再返回自动刷新。最终 title_cn=Automatic replacement，但保护标记仍在；断言失败、退出 1。合成候选与海报替身，真实双数据库会话；无需队列失权即可发生，因此也适用于普通刷新并发。

探针 metadata_manual_pg_probe.py、queue-metadata-nv.json/log 与清理日志已保存；Ruff 通过，down -v 退出 0，全部进程终态。此是新确认的缺陷，候选尚未修复，不能用 nt/nu 的通过覆盖此失败。

下一步方案：把候选扩展与海报网络请求移到数据修改之前，网络结束后在短事务内重新读取最新作品及人工保护字段，并在持有作品行写保护时重新计算差异/应用；禁止跨海报或元数据网络调用持有数据库写锁。仅 db.refresh 后再 await 网络、仅时间戳比较或多加 Redis guard 都不足以封闭该竞争。需验证 PostgreSQL 行锁、Turso 冲突/重试，保留 override_manual_edits=True 的显式人工覆盖语义，以及身份袋/剧集同事务一致性。B4 与此人工保护缺陷均保持未完成。

## 人工标题覆盖候选修复与 PostgreSQL 转绿（nw / nx）

apply_work_metadata 将候选扩展、海报获取提前到自身字段修改之前；随后 UPDATE updated_at=updated_at 按作品 ID 取得写保护，populate_existing 重新加载作品，再检查身份、计算人工保护/差异并写入。没有跨这两个网络调用持有该写锁。PG 通过行锁串行化；Turso 必须由新事务处理旧 MVCC 快照冲突，尚待专项验证。

nw 所有权、metadata_search 与 job_handlers 回归 54 passed、1 warning、16.60 秒，Ruff 通过。nx 新 rssripple-v14-metadata-nx 项目重跑 nv 的真实双 PostgreSQL 会话交错，退出 0，最终 title=Manually corrected、manual_fields=[title_cn]，缺陷场景转绿。驱动仅改为显式 PROBE_PROJECT 前缀检查，旧 nv 驱动保持为历史证据；新版保存 metadata_manual_pg_fixed_probe.py。日志/JSON/JUnit/清理记录 queue-metadata-nw/nx.* 已保存，项目 down -v 退出 0。

尚未验收：Turso 并发冲突后的生产重试、显式 override_manual_edits=True 并发语义、候选扩展期间作品删除/季属性变化及 season-0 独立日期回填分支。不得以本次 PG 转绿删除 P1 待办；B4 也仍未合入。全部测试终态，本轮栈已清理。

## Turso 人工保护与自动刷新写冲突（ny / nz / oa）

ny 真实 Turso 双会话中，海报请求期间另一会话提交人工标题，当前写保护实现直接保留人工值并成功，1 passed、5 deselected、0.65 秒；没有观测到冲突，故不算重试证据，测试随后改名为 respects_concurrent_manual_edit。

nz 增加 override_manual_edits=False/True 两参数，分别保留 Manual/写入 Automatic，保护标记仍在，两者通过。另用 barrier 使两个刷新都完成网络阶段后同时写同作品，实际 Write-write conflict 被当作普通失败，1 failed、2 passed、5 deselected、1.61 秒。

候选为每作品刷新提取新会话尝试，复用 retry_on_lock，并以原有 120 秒上限包住全部尝试；重试前重新检查所有权。失权不重试，网络读取可能重复，未引入跨网络写锁。oa 全部所有权、job_handlers、metadata_search 回归通过，具体数量/时长见 queue-metadata-oa.log；Ruff 通过。日志/JUnit ny/nz/oa 已保存，业务文档和 aggregate patch 同步。

仍需 PostgreSQL 显式人工覆盖交错、season-0 日期回填、删除/季属性变化及完整门禁。P1 人工保护与 B4 均保持未完成；本轮测试终态，没有 Compose 栈。

## 特典日期回填的并发人工保护（ob / oc）

必要性：season-0 日期回填有独立 commit，不经过 apply_work_metadata。ob 使用真实 Turso、同合集常规季与特典，在原回填查询计算之后由第二会话提交人工日期及保护字段；旧分支仍应用旧回填，1 failed、8 deselected、1 warning、0.72 秒。

候选在计算后对特典行取得写保护、populate_existing 重读；仅季号仍 0、合集未变、日期仍空且未人工保护时应用。删除返回 not found，季号/合集变化跳过旧值。没有新增网络调用，批次沿用新事务写冲突重试。oc 所有权、metadata_search、job_handlers 合计 58 passed、1 warning、18.61 秒，Ruff 通过；回归验证人工日期保留、applied 为空。证据 queue-metadata-ob/oc.* 已保存。

边界：回填计算用真实查询，但交错入口由测试包装；尚未对该分支跑 PostgreSQL 交错，也未直接测试新增删除/季号/合集变化分支。本轮测试均终态、无 Compose 栈。业务文档和 aggregate patch 已更新，待办仍保留；下一步补这些分支及完整门禁，不将局部通过视为 B4 完成。

## 特典回填目标变化矩阵（od）

扩展同一真实 Turso 双会话回归为 manual_date/delete/season/collection 四参数。计算旧回填值后，第二事务分别写人工日期、删除作品、改为季 2、迁移到另一合集；生产批次分别保留人工值、返回未找到、跳过两个旧归属结果，均不写旧回填日期。4 passed、8 deselected、1 warning、1.95 秒，Ruff 通过，queue-metadata-od.log/xml 已保存。

本轮未修改生产代码，仅补齐 oc 新分支的定向证据；aggregate patch/source 刷新。此矩阵不覆盖普通 apply_work_metadata 的季号变化，也不代替 PostgreSQL 对应交错和完整门禁。当前测试终态、无 Compose 栈。下一步检查普通候选扩展期间的季号/合集变化，防止旧季候选被应用到重读后的新季作品；P1/B4 仍未关闭。

## 普通候选应用的作品归属变化（oe / of）

必要性：网络后重读人工字段不能证明候选仍属于相同季/合集。oe 真实 Turso 双会话在海报 await 处分别改季、换合集或删除作品，前两项继续应用旧标题与身份而失败，删除场景通过；2 failed、1 passed、12 deselected、1 warning、1.64 秒。

候选在扩展前保存季号/合集，取得写保护重读后比较；变化直接 apply 返回 409，后台刷新归类为 found=false/scope_changed=true/applied=[]，不误标 identity_conflict。of 全部所有权、metadata_search、job_handlers 64 passed、1 warning、21.05 秒，Ruff 通过。回归断言旧候选不改标题/海报、不写身份袋；记录 queue-metadata-oe/of.*。API/业务文档及 aggregate patch 已同步。

限制：仅本次调用内的变化，未覆盖跨请求 preview/apply 版本一致性；真实 PostgreSQL 对应矩阵仍待验证。本轮测试终态，没有 Compose 栈，P1/B4 均未验收/合入。

## PostgreSQL 元数据并发矩阵（og）

必要性：Turso 的通过不能替代默认 PostgreSQL 部署。og 新建 rssripple-v14-metadata-og 专用项目，确认无 movies 表后建当前 schema；逐项调用同一组测试断言，每项只重置本轮创建的夹具表。九项全部通过：人工标题 override=False/True；特典回填 manual_date/delete/season/collection；普通候选 season/collection/delete。退出 0，Ruff 通过，删除场景的预期 404 日志保留。

数据为合成作品/候选，真实独立 PostgreSQL 会话，海报/查询使用确定性交错替身；不宣称真实在线元数据验收。driver metadata_matrix_pg_probe.py、queue-metadata-og.json/log 与清理日志已保存。项目 down -v 退出 0，全部进程终态，无遗留测试栈。本轮没有改生产候选代码。

该驱动是独立探针，尚未进入常规集成门禁；下一步将可复用并发矩阵纳入正式入口，然后继续 B4 剩余 handler、完整冻结门禁和代码评审。P1 人工保护仍不删除，B4 未完成/未合入。

## 元数据并发矩阵进入正式集成入口（oh）

新增 tests/integration/queue_recovery/metadata_driver.py，去掉 Docker inspect 依赖，使用父测试创建的 queue_recovery_UUID 数据库；沿用空库预检与本轮夹具清理。现有测试入口扩为 kill/pause/metadata，函数名改为 test_queue_and_metadata_concurrency；metadata 子进程执行上述九项相同断言，父测试要求全部通过。保留 REQUIRED=1、独立进程组超时清理和 finally 删除临时库。

oh 全新 rssripple-v14-metadata-oh 项目只启动候选 Compose 的两个专用服务，以正式 pytest 入口联合运行：3 passed、36.42 秒、退出 0，Ruff 通过。包含两个实际 Redis worker 接管场景和九项 PostgreSQL 元数据交错；不等同全套 test-runner 容器门禁。日志/JUnit/每参数报告与清理记录 queue-metadata-oh-* 已保存，项目 down -v 退出 0，全部测试终态。

候选 driver、入口、隔离门禁文档和 aggregate patch 已更新。下一步回到 B4 剩余 handler 审核及最终完整验证；人工保护待办仍等待整批验收后清理，B4 未合入。

## 下载 RPC 返回后的所有权边界（oi / oj）

必要性：发起前检查不能覆盖慢 RPC 期间的接管；旧 worker 返回后原先仍可将成功或失败结果写入 DownloadTask 并 settled 预留。oi 在实际 RedisQueue 执行上下文中、RPC 替身返回前替换执行 token，成功/异常两个参数均复现，2 failed、13 deselected、1 warning、1.08 秒。测试使用 fakeredis 与真实临时 Turso，不属于真实 Redis/下载器集成证据。

候选在 RPC 成功或异常返回后、persist_dispatch_result 前再次 require_execution_ownership。失权交给调用者回滚，已提交的派发预留保持 unsettled，供新执行者重试；远端已接受的请求不能撤销，检查到提交间仍存在竞争窗口。新增断言确认不生成任务且预留未 settled。oj 下载所有权、持久派发与完整 agent_service 单测共 130 passed、1 warning、47.28 秒，退出 0；修改文件 Ruff 通过。初次沙箱内运行无输出，显式中断退出 130 后在隔离临时库环境重跑，未把中断计为红测。

日志/JUnit queue-download-oi/oj 已保存，业务文档及 aggregate patch/source 同步。代码审核另发现 process_resources 的通用异常分支可能吞掉失权后继续候选循环，_handle_run_agent 的失败补偿亦未核验所有权；下一步须建立停止后续候选、禁止旧执行确认请求/消费进度的回归，再处理该边界。本轮未关闭 B4，未合入运行代码，没有启动 Compose 服务；全量冻结门禁及真实 Redis/下载器组合验证仍待完成。

## Agent 失权异常传播与请求收尾（ok / ol / om）

必要性复核：ok 用两个不同集数候选、派发抛失权异常验证请求事务/后台独立事务两个模式；两者均吞掉异常继续处理，2 failed、110 deselected、1 warning、1.29 秒。ol 通过真实 handler 与临时 Turso 持久请求验证处理中失权：返回普通结果时错误确认请求，抛失权时错误增加延期计数，2 failed、7 deselected、2.00 秒。数据均为合成夹具，失权由确定性替身触发，不宣称真实 Redis 接管验证。

候选令 process_resources 显式重抛 ExecutionOwnershipLostError，入口/各候选开始/建议写入前检查；handler 在运行开始、确认请求/消费进度之前、正常收尾前检查。失权不走失败延期，普通异常执行补偿前也需检查所有权。om 完整 agent_service、下载所有权、持久请求失败恢复文件共 136 passed、1 warning、54.68 秒，退出 0；Ruff 通过。负向断言覆盖后续候选不执行、建议不写、请求不删除且 attempt_count 不增加；普通失败隔离与到期恢复原测试仍通过。

queue-agent-ok/ol/om 日志/JUnit 已归档，权威业务文档与 aggregate patch/source 同步。尚未直接覆盖失权时发布游标不前移，也未覆盖候选内部慢 LLM 后写 PendingDecision 的边界；下一步从这些实际写入点继续审查并补测试。检查与提交不原子，历史已提交候选不撤销，遗留 running 记录回收仍在 TODO。本轮所有测试终态、无 Compose 栈，B4 及元数据人工保护仍未验收/合入 main。

## 慢建议与候选保存点回滚（on / oo / op / oq）

必要性：候选开始检查早于建议网络请求，不能保护返回后 persist_choice。on 合成双候选、真实 Turso 请求保存点/后台独立事务两个模式，在建议替身返回时标记失权；虽最终抛失权，数据库仍有待决策，2 failed、112 deselected、1 warning、1.27 秒。候选增加 persist_choice 前检查及候选事务退出前检查，oo 三个完整回归文件 138 passed、1 warning、55.20 秒。

进一步在实际 persist_choice 完成 flush 后标记失权，op 四参数矩阵 3 passed、1 failed、112 deselected、1 warning、2.16 秒：后台独立事务的外层回滚未撤销内部保存点释放的插入。原因是 Session 的逻辑 begin 没有在 Turso 首个 SAVEPOINT 前建立物理事务。候选对全新的后台独立 SQLite/Turso 会话显式执行延迟 BEGIN；请求已有事务与 PostgreSQL 不改变。oq 完整 agent_service、下载所有权及请求失败恢复 140 passed、1 warning、57.76 秒，退出 0；四种建议/flush 失权与事务模式均不留下 PendingDecision，Ruff 通过。oo 开始后仅追加 op 测试参数，没有修改该次运行的生产代码；最终生产代码以 oq 为准。

数据仍为合成作品及建议替身；本轮没有真实 LLM、Redis 接管或 PostgreSQL 保存点交错验收。证据 queue-agent-on/oo/op/oq 保存，业务契约及 aggregate patch/source 更新。下一步补失权时发布游标不前移的直接证据及 PostgreSQL 候选事务矩阵；还需复核显式读事务对慢下载/并发重试的影响，最终冻结门禁不能省略。所有测试终态，无 Compose 栈；B4 未合入、未关闭。

## 发布游标回滚及 PostgreSQL Agent 矩阵（or / os / ot）

必要性：请求未确认不能证明发布游标不前移。新增真实 handler/消费逻辑测试，在 process_resources 完成后，或实际 acknowledge_publications UPDATE 后标记失权；独立观察会话断言 cursor、last_consumed_at 未变，原发布仍可见；恢复正常执行后同一资源消费成功，旧 running 记录仍留待独立回收。or Turso 两参数 2 passed、9 deselected、1.97 秒。

新增正式 queue_recovery 的 agent 参数及 agent_driver.py，在父测试创建的独立 PostgreSQL 库中复用上述两项、建议返回/flush 失权×两事务模式四项，共六项。os 首跑在夹具 INSERT 阶段失败：通用单测 helper 的 parsed_at 带时区，不符合现有 naive UTC 列；仅本回归显式改用 utcnow()，不修改应用时间处理。ot 同一专用项目中新建临时库，六项全通过，pytest 1 passed、3 deselected、2.82 秒；Ruff 通过。首次执行审批超时后重试获准，超时未启动测试，不作为失败证据。

本轮使用合成资源、真实数据库事务、确定性失权/建议替身；未声称真实 Redis 接管或真实在线元数据。专用 rssripple-v14-agent-os 已 down -v 退出 0。日志/JUnit、六项 JSON 及清理记录保存，正式测试清单/隔离标准及 aggregate patch/source 已更新。原型仍待慢下载与并发重试影响复核、其余 handler 审核、真实服务组合和完整冻结门禁；B4 未合入 main。

## 重解析 finally 的标记所有权（ou / ov / ow / ox）

必要性：reprocess_resource_metadata 无条件 finally 清除 confirmation_ignored_at，旧执行返回时可能破坏接管者仍在处理的标记。ou 初始四项均因合成夹具缺少必填 torrent_url 失败，不算行为复现；补齐后 ov 执行前/处理中失权两项失败，正常成功/普通失败两项通过，2 failed、2 passed、15 deselected、1 warning、1.98 秒。

候选在重解析开始、finally 清理前及清理事务退出前检查所有权；保留正常成功/失败清理语义。ow 所有元数据所有权与 job_handlers 回归 45 passed、1 warning、14.07 秒，退出 0；随后补“恢复当前执行后标记可清除”断言，ox 四参数 4 passed、15 deselected、1 warning、1.56 秒。Ruff 通过，queue-reparse-ou/ov/ow/ox 日志/JUnit 保存，业务文档与 aggregate patch/source 同步。

本轮真实临时 Turso，metadata 处理与失权使用替身，没有 PostgreSQL/Redis 该场景的直接证据。保护仅覆盖 handler 启动/收尾；fetch_service._process_resource_metadata 内部仍有多个 commit 与吞异常分支，下一步须审查并用部分写入回滚测试覆盖，不能将 finally 修复当成完整重解析保护。B8 的入队失败/永久崩溃回收仍单独保留。本轮进程终态，无 Compose 栈，B4 未验收/未合入。

## 资源元数据部分写入与失权传播（oy / oz）

必要性：_process_resource_metadata 的锁重试外层和独立事务内均吞普通 Exception，主匹配的 best-effort 分支也吞失权。oy 对 MetadataAgent/旧链接两条路径，实际修改资源 search_title 并 flush 后分别返回或抛失权；四项均未传播失权，4 failed、19 deselected、1 warning、2.40 秒。

候选在 semaphore、torrent 缓存、作品锁和主匹配返回边界检查所有权，发布事件前/后与海报事务提交前检查；主匹配、扩展、独立事务及外层锁重试显式传播 ExecutionOwnershipLostError。oz 完整所有权文件与 fetch_service 单测 79 passed、1 warning、34.48 秒，退出 0。新增四项用独立观察会话确认原字段未变且 ResourcePublication 为空；真实临时 Turso，元数据网络和失权为替身。Ruff 通过，queue-fetch-oy/oz 日志/JUnit 已保存，fetch_service 纳入 aggregate patch/source，业务文档同步。

边界仍需补验：发布事件已经 flush 后失权、元数据提交后海报失权、PostgreSQL 相同矩阵；元数据提交不能因后续海报失权撤销。上层三个 asyncio.gather 默认提前传播异常但不等待其余任务，频道 backfill 通用异常分支仍可能吞失权并继续收尾；下一步须先处理任务排空及频道状态提交边界，不宣称 fetch 全链路已安全。本轮测试终态，无 Compose 栈，B4 未验收/未合入。

## 元数据并发排空与频道收尾（pa / pb / pc / pd）

必要性：一个资源失权后默认 gather 提前抛出，兄弟任务仍活跃。pa 对真实频道/全局回填选择结果放入两个屏障任务，父任务在第二项仍等待时返回，两参数均失败，2 failed、23 deselected、1 warning、1.06 秒。三个元数据入口改用 _gather_metadata，return_exceptions 收齐后优先传播失权，再传播其他异常；不主动撤销已提交资源。pb 所有权与完整 fetch_service 回归 81 passed、1 warning、35.08 秒。

pc 另验证回填返回后失权/回填抛失权两种情况，频道函数均吞掉或忽略失权继续收尾，2 failed、56 deselected、1 warning、1.11 秒。候选在抓取开始、RSS 返回、最终状态更新前及各 Agent 唤醒前检查，并显式传播回填失权。pd 相同完整文件 83 passed、1 warning、36.31 秒，退出 0；独立观察会话确认失权后频道仍 running、last_fetched_at 未推进。Ruff 通过。

本轮真实临时 Turso、合成资源与空 RSS/异步屏障替身；新资源入口复用同一排空函数，但新资源并发及父任务取消场景未单独验证。默认事件循环中 50ms 等待用于断言父任务不会提前完成，finally 显式放行并等待兄弟结束，测试不遗留任务。queue-fetch-pa/pb/pc/pd 日志/JUnit、业务文档及 aggregate patch/source 已更新。下一步仍需发布/海报提交边界、PostgreSQL 元数据矩阵扩充、feed 资源写入中途失权与取消排空验证；尚不能宣称全部 handler 已保护。本轮所有测试终态，无 Compose 栈，B4 未合入/未验收。

## 元数据发布与海报分阶段提交验证（pe / pf / pg）

必要性：字段 flush 后回滚不能证明发布事件提交点及第二阶段海报事务正确。新增电影/剧集×发布 flush/海报返回两个失权点四参数，调用真实资源元数据函数、真实 publish_resource 和数据库，替换网络与失权信号。

pe 四项因夹具缺 created 发布被真实服务拒绝；补 created 后 pf 两项通过、两项因错误地将 created/metadata 视为一条记录失败。这两轮属于测试夹具/断言修订，不算生产缺陷。按实际 resource_id+kind 唯一契约修正后 pg 4 passed、25 deselected、1 warning、2.39 秒，退出 0，Ruff 通过。

独立观察会话验证：发布 flush 后失权，资源字段/作品关联及 metadata 发布回滚，仅原 created 保留；海报返回后失权，已提交的字段/作品关联与 created+metadata 发布保留，海报 URL 不变。测试没有真实媒体/海报下载，不宣称 PostgreSQL 或 Redis 接管证据。本轮无生产代码变更，queue-fetch-pe/pf/pg 日志/JUnit及 aggregate patch/source 已保存。下一步扩充 PostgreSQL 同断言及取消排空/新资源插入路径验证，完整门禁仍未进行；测试均终态，无 Compose 栈，B4 未合入。

## 父任务取消与子事务清理（ph / pi）

必要性：失权异常排空不等价于父任务收到 CancelledError 后正确清理。ph 通过真实 asyncio 取消和清理屏障确认 _gather_metadata 在子任务 finally 阻塞时不退出，放行后传播 CancelledError，1 passed、29 deselected、1 warning、0.13 秒。进一步将子任务改为真实临时 Turso 事务：插入 Movie 并 flush 后等待，取消触发 finally；父任务只有在清理和事务退出后结束，独立观察会话确认无残留 Movie。pi 1 passed、29 deselected、1 warning、0.61 秒，Ruff 通过。

没有修改生产代码；测试使用合成 Movie，不涉及网络。仅覆盖一次父取消及正常清理，不证明重复取消、数据库 rollback 自身失败或进程强杀；后者依赖数据库与既有进程级恢复验收。queue-fetch-ph/pi 日志/JUnit、aggregate patch/source 已保存。下一步继续 PostgreSQL 提交边界矩阵和新资源写入期间失权验证。本轮测试终态、无 Compose 栈，B4 未验收/未合入。

## PostgreSQL 资源事务纳入正式矩阵（pj）

必要性：Turso 提交边界不能替代默认 PostgreSQL。metadata_driver 复用新建的四项发布/海报断言和四项元数据字段 flush 后失权断言，连同原九项作品并发矩阵共 17 项；父门禁要求报告全部通过且数量匹配。agent 原六项保持不变，正式子套件共四参数。

新建专用 rssripple-v14-metadata-pj 项目，使用正式 pytest 入口运行完整 queue_recovery 子套件，4 passed、40.47 秒、退出 0。其中 SIGKILL/SIGSTOP 两项是真实 Redis 双进程与 loopback HTTP 接管，metadata 17 项及 agent 6 项是真实 PostgreSQL 数据库与确定性交错替身；资源/作品为合成数据，不宣称真实元数据源或下载内容。Ruff 通过。

日志/JUnit、四份参数 JSON 及清理日志 queue-metadata-pj-* 已归档，测试清单、隔离标准及 aggregate patch/source 同步。项目 down -v 退出 0，无遗留测试栈，全部进程终态。本轮无生产代码变更；下一步检查新资源写入期间失权和其余 handler，不把此子套件通过当成完整冻结门禁。B4 仍未验收/未合入 main。

## RSS created 发布提交前失权（pk / pl / pm / pn）

必要性：fetch 新资源逐条 commit 早于元数据及频道收尾检查，发布写入期间失权仍可能提交。候选增加每条入口及 publish_resource(created) 后、commit 前检查。新回归在真实发布与 flush 完成后标记失权，由调用方 rollback，独立会话断言资源与发布皆不存在。

pk 初测失败、pl 完整文件 1 failed/88 passed 的原因均为测试 RSS 没有可识别 enclosure，根本未触达发布；不能当作旧代码缺陷复现。补齐合成 magnet 附件后 pm 1 passed、58 deselected、1 warning、0.65 秒，且明确断言发布替身已执行；pn 再跑两个完整文件通过，数量/耗时见 queue-fetch-pn.log。Ruff 通过。本轮未在修正夹具上撤销保护重跑旧代码，必要性依据提交路径审查及实际边界回滚测试，不声称已有有效红绿对照。

证据 queue-fetch-pk/pl/pm/pn、业务文档与 aggregate patch/source 保存。数据为合成 RSS，未下载真实内容；仍未覆盖该入口 PostgreSQL 交错或真实 Redis 接管。无测试进程/Compose 栈遗留，B4 未验收/未合入。下一步继续其余 handler 审核，最终必须完成冻结全量门禁。

## 批文件分析进度与缓存边界（po / pp / pq）

必要性：批分析的文件清单/LLM 等待后原先没有所有权检查，最终写缓存。po 两参数失败；其中空清单后失权确实被忽略，流参数后来查明没有进入 LLM：100 字节合成文件被真实解析器过滤，故该参数初次失败不能当作流式失权复现。候选在开始、progress、流事件及两处缓存写入前检查所有权。

pp 完整 handler+metadata 所有权文件 57 passed、1 failed；失败即上述未触达流场景。测试改为明确的合成解析报告，保持真实 handler 控制流和 DB，替换解析/流/缓存边界。pq 58 passed、1 warning、20.27 秒，退出 0；新增断言确认两个失权入口均触达且缓存未写。Ruff 通过。前述“两条路径均复现”的即时描述应以上述证据限定为准，流场景没有在修正夹具后做旧代码红测。

queue-analysis-po/pp/pq 日志/JUnit、业务文档及 aggregate patch/source 已保存，test_job_handlers 纳入补丁。尚无真实 Redis/缓存交错，不保证检查与缓存写入原子；分析期间资源字段写入边界亦需继续审核。本轮全部测试终态，无 Compose 栈，B4 未验收/未合入。

## 下载进度同步的迟到 RPC（pr / ps / pt）

必要性：list_torrents 跨接管后旧同步仍可把任务取消，或将下载器记成 error。pr 因测试漏声明参数而收集失败，不计行为证据；修正后 ps 成功空列表/普通 RPC 失败两个场景均未传播失权，2 failed、29 deselected、1 warning、1.00 秒。一次自动审批超时后重试获准，未因此重复启动测试。

候选在同步入口、每个下载器开始、RPC 返回和 commit 前检查；RPC 错误分支先检查所有权再记健康状态，ExecutionOwnershipLostError 直接抛出。pt 完整 scheduler+job_handlers 59 passed、1 warning、14.69 秒，退出 0，Ruff 通过。独立真实 Turso 会话断言原任务 downloading/0.1 和下载器 disconnected/未检查时间保持不变；普通完成/暂停/缺失 torrent/故障恢复原有回归同样通过。

数据和 RPC 为合成夹具，没有真实下载器/Redis 接管直接证据，检查到提交仍有竞争窗口。queue-sync-pr/ps/pt 日志/JUnit、业务文档及 aggregate patch/source 保存，scheduler 单测已纳入补丁。下一步继续每日清理/去重、下载器检查、FTS、magnet 路径的所有权及独立 CAS 审核，最终全量门禁未完成。本轮所有测试终态、无 Compose 栈，B4 未验收/未合入。

## 下载器健康探测失权（pu / pv）

必要性：独立健康检查的 test_connection 同样可能跨接管返回，原先无所有权检查。pu 成功、否定响应、普通异常三参数均未传播失权，3 failed、31 deselected、1 warning、1.51 秒。候选在入口、每个下载器探测前、成功/异常后和事务退出前检查；失权直接抛出，不能被普通 RPC 异常处理记成 error。

pv 完整 scheduler 文件 34 passed、1 warning、9.67 秒，退出 0，Ruff 通过。独立临时 Turso 观察会话确认失权后 status=disconnected、last_checked_at=None 保留；原有正常成功/失败探测测试通过。数据与 RPC 仍是合成夹具，未覆盖真实下载器/Redis 接管或 PostgreSQL 相同交错。

queue-health-pu/pv 日志/JUnit、业务文档和 aggregate patch/source 已保存。健康检查仍使用原有单事务遍历，检查与提交不原子；跨多下载器网络等待的事务行为还需整体审核。本轮测试终态，无 Compose 栈。继续每日清理/去重、FTS、magnet 剩余路径及完整冻结门禁，B4 未验收/未合入。

## 每日清理/去重提交与失权传播（pw / px）

必要性：两类维护任务的外层事务缺少失权检查，best-effort 分支会吞失权。pw 清理/去重×内部返回/抛失权四参数均未正确传播，4 failed、34 deselected、1 warning、2.02 秒。清理沿用真实过期任务删除，扩展点 flush 后标记失权；去重测试在替身内删除同一合成过期任务并 flush，验证的是外层事务，不声称真实合并算法覆盖。

候选增加入口、提交前和后续派发预留清理前检查，失权显式传播。px 完整 scheduler 文件 38 passed、1 warning、11.52 秒，退出 0；独立 Turso 观察会话确认过期任务删除回滚，原有普通维护回归通过。Ruff 通过，检查 resource_cleanup/metadata_dedup 未发现内部 commit。

queue-maintenance-pw/px 日志/JUnit、业务文档及 aggregate patch/source 保存。没有真实 Redis/PG 该路径交错，不证明去重内部所有 await 后都及时停工，也不消除检查到 commit 竞争窗口。下一步继续 FTS/magnet 及剩余内部写入审查；完整冻结门禁未完成，B4 未验收/未合入。本轮测试终态，无 Compose 栈。

## FTS outbox 失权保留（py / pz）

必要性：drain 在主库删除 outbox 后等待 sidecar 写入，服务/调度器均吞普通异常。py 实际主库删除、索引替身返回后失权或直接抛失权两参数均未正确传播，2 failed、38 deselected、1 warning、0.99 秒。候选增加 drain 入口、删除前、索引返回后、调度器提交前检查，显式传播失权使外层事务回滚。

pz 完整 scheduler+fts 文件 78 passed、1 warning、20.33 秒，退出 0；独立 Turso 观察会话确认 outbox 保留，原有普通索引失败消费/reconcile 语义测试仍通过。Ruff 通过。测试为合成删除事件，sidecar 写入及失权为替身；不宣称真实双库/Redis 接管交错。PostgreSQL 不使用此 outbox 索引路径。

queue-fts-py/pz 日志/JUnit、业务文档及 aggregate patch/source 保存，fts.py 已纳入候选。sidecar 已完成的写入不能随主库回滚，后续重放需依赖其幂等语义；FTS reconcile 的分表写入尚待审核。全量门禁未完成，B4 未验收/未合入。本轮测试终态，无 Compose 栈。

## FTS reconcile 分表写入（qa / qb）

必要性：reconcile 独立提交多个 sidecar 表，主库回滚不能撤销它们。qa 使用真实临时 Turso 主库与 FTS sidecar，清空剧集/电影索引后执行真实 scheduler→reconcile→shadow_write，在首张表提交后标记失权；旧实现未传播，1 failed、38 deselected、1 warning、0.72 秒。

候选增加入口、逐表前后及写入前检查，并在服务与调度器显式传播失权。qb 完整 FTS+scheduler 79 passed、1 warning、20.57 秒，退出 0，Ruff 通过；直接查询 sidecar 确认剧集已写入一行、电影仍零行，写入调用仅一次。夹具作品为合成，失权信号为替身，不宣称真实 Redis 接管。

queue-fts-qa/qb 日志/JUnit、业务文档及 aggregate patch/source 已保存，test_fts 纳入补丁。检查到写入仍非原子，已提交索引保留；后续正常对账的原有测试通过。本轮测试终态，无 Compose 栈。继续 magnet 生命周期及剩余路径审核、最终完整冻结门禁，B4 未验收/未合入。

## magnet 领取入口与独立生命周期（qc / qd）

必要性：resolve_magnet_torrent 只负责 SQL 领取并创建 detached 解析任务，旧队列执行不应在失权后继续领取。qc 真实临时 Turso、libtorrent/解析替身证明旧入口未传播失权，1 failed、55 deselected、1 warning、0.66 秒；finally 排空可能启动的后台任务，不遗留进程内任务。

候选在领取事务前及 guarded UPDATE 后、提交前检查队列所有权。qd 完整 magnet 单测 56 passed、1 warning、8.81 秒，退出 0，Ruff 通过；新增断言确认未启动解析且资源状态仍空。原有成功/重试/缺失 libtorrent 回归通过。证据 queue-magnet-qc/qd、业务文档及 aggregate patch/source 保存，magnet 服务与单测纳入补丁。

重要边界：领取完成后的 detached 解析不能再依赖已结束队列租约；当前 _set_status 只按 resource_id 更新，回收与旧解析并存可能覆盖新状态。该竞争尚无本轮复现，下一步应验证并设计独立 attempt 身份，不能把本次入口检查当作完整 magnet 修复。还需审核 inherited ContextVar 对 detached 子调用的影响。无真实 libtorrent 网络/Redis 接管测试，本轮全部测试终态、无 Compose 栈，B4 未验收/未合入。

## magnet 旧解析覆盖接管结果（qe / qf，未修复）

必要性已取得直接证据：真实临时 Turso 两会话，在旧 resolve 调用期间由第二会话提交 replacement 的 done/缓存路径。qe 旧解析随后失败，将 done 改成 failed，1 failed、56 deselected、1 warning、0.67 秒。qf 扩为成功/失败两参数，旧失败覆盖状态，旧成功把 replacement 路径改回资源共享路径，2 failed、56 deselected、1 warning、1.12 秒。第二会话模拟接管结果，未执行真实超时回收/第二进程/libtorrent，不宣称全链路接管复现。

下一步修复方案：

- FileResource 增加 nullable magnet attempt UUID 与双后端幂等迁移；成功领取时分配，detached 任务显式携带，所有 running/retry/failure/done 更新按 resource_id+attempt CAS，旧更新返回未接受立即停止。
- sweep 回收与人工重试必须使旧 attempt 失效；错误计数更新也需同一身份条件，不能 ORM 重读后无条件覆盖。
- 解析写入 attempt 专属缓存路径，只有成功 CAS 后引用；旧执行不能写共享资源缓存文件。拒绝发布的文件仅清理本 attempt 所有文件，清理失败留可审计记录，不能删除新执行缓存。
- 领取事务提交后脱离原队列 ContextVar，以独立 DB attempt 管理生命周期；保留正在运行状态的时间戳/回收策略，另验证长时间等待 semaphore 不会错误接管。
- 严格验收须覆盖旧成功/普通失败/异常、重试计数、回收/人工重试、文件字节不覆盖、claim 后崩溃、双后端迁移及真实双进程交错，再跑全量冻结门禁。

本轮只新增负向测试与方案，没有提交不完整 schema 改动。aggregate patch 当前含上述两个有意失败的回归，不可当作绿色候选或合入 main。queue-magnet-qe/qf 已保存，TODO B4 同步具体缺口。下一轮从该 attempt 方案实施，不重复已完成入口验证；本轮测试终态，无 Compose 栈，目标继续进行。

## magnet attempt 字段与旧库迁移基础（qg / qh）

按 qe/qf 方案开始实施：FileResource 新增 nullable VARCHAR(36) magnet_resolve_attempt_id；加入轻量迁移的存在性检查，并明确该列迁移失败阻止启动。历史 NULL/running/done 不重置，不给旧任务生成虚假 attempt。状态 CAS、文件发布及 detached 上下文协议尚未接入，qe/qf 仍未修复。

新迁移测试从 Base 复制并移除该列创建真正旧结构，插入三种历史状态，连续两次完整轻量迁移，核验列及数据。qg 因临时引擎未启用 MVCC 导致初始 INSERT 失败，非迁移缺陷；按实际 Turso 配置启用 MVCC 后 qh 1 passed、1 warning、0.53 秒。没有操作生产库，PostgreSQL 同结构升级尚未验证。

queue-magnet-qg/qh 日志/JUnit、模型/迁移权威文档、aggregate patch/source 已更新；新增模型文件及迁移测试纳入补丁。候选明确仍不可部署或合入，下一步接入领取生成 attempt、全部状态条件更新与回收失效，随后补文件发布隔离。当前测试终态，无 Compose 栈，目标继续进行。

## magnet attempt CAS 与缓存隔离候选（qi / qj）

新领取生成 UUID 并传递至 detached run/attempt；_set_status 匹配 resource_id+attempt，失败计数也改为条件 UPDATE。sweep 回收/人工重试清空旧标识。完成 CAS 未接受时立即停止，不再检查/关联旧结果。原 qe/qf 替身现在为 replacement 明确设置新标识，符合新领取契约；qi 完整 magnet 58 passed、1 warning、8.83 秒。

缓存改为新领取 attempt 专属路径，拒绝发布仅删除自身文件，OSError 记录日志。旧成功测试实际写旧文件，第二会话已引用另一文件；断言新文件字节及引用不变、拒绝的旧文件已清理。qj magnet+迁移 59 passed、1 warning、9.98 秒，退出 0，Ruff 通过。

仍未完成：内部函数暂保留 NULL 标识默认参数供旧测试/旧形态调用，须收紧以防无身份写入；detached ContextVar 尚未清理；人工重试读取状态到清空标识的竞争、sweep 与长等待任务、成功后 inspection 的行锁边界、拒绝/异常孤儿文件与 claim 后崩溃均需追加验证。PG 迁移/真实双进程及全量门禁未运行，不能据此合入。queue-magnet-qi/qj 已保存，模型/业务/API 文档及 aggregate patch/source 同步。本轮全部测试终态，无 Compose 栈。

## magnet 禁止无身份内部写入（qk / ql）

去掉 _run_resolution/_attempt_loop/_set_status 的 NULL 默认参数；后两者在访问数据库/生成缓存路径前验证 UUID。删除共享文件名兜底。旧单元/集成边界测试改为显式准备 pending+attempt 夹具再调用，未放宽状态或结果断言；生产调用只有成功领取后的显式 UUID。

qk 完整 magnet 单元+集成覆盖文件 120 passed、1 warning、17.95 秒，退出 0，Ruff 通过；新增 ql 直接拒绝 None/空串/路径形式非法标识，三参数结果见 queue-magnet-ql.log，数据库状态保持未领取。测试使用真实临时 Turso，libtorrent/HTTP 替身，不作为真实网络验收。

证据 queue-magnet-qk/ql、业务文档及 aggregate patch/source 保存，集成覆盖文件纳入补丁。迁移历史 NULL 行仍只可重新领取新身份，不可直接恢复旧执行。下一步 detached ContextVar 与回收/重试的并发边界；PG 迁移及最终冻结门禁仍未完成。本轮测试终态，无 Compose 栈，B4 未验收/未合入。

## detached 解析的队列 Context 隔离（qm / qn）

必要性：asyncio.create_task 默认继承父队列租约上下文，父作业完成后可能使独立解析子调用错误失权。qm 用实际 launch→create_task→run、真实临时库领取，注入父所有权哨兵及独立 trace ContextVar，验证子任务仅丢弃队列所有权；旧实现失败，1 failed、61 deselected、1 warning、0.68 秒。首次自动审批超时后一次重试获准，未重复启动测试。

候选新增 independent_execution_context：copy_context 后仅将队列所有权置空；领取事务成功提交后创建解析任务时显式使用。qn 完整 magnet 单元/集成与队列所有权文件 136 passed、1 warning、19.83 秒，退出 0，Ruff 通过。断言子 attempt 匹配数据库、trace 保留、父 owner 未变；信号为 ContextVar 替身，不是真实 Redis 接管。

queue-magnet-qm/qn 日志/JUnit、业务文档及 aggregate patch/source 保存。后续仍需领取崩溃/回收/人工重试竞争、inspection 条件写入和 PG 迁移/真实进程门禁。Context 脱离不能用于无独立持久所有者的普通子任务。本轮测试终态，无 Compose 栈，B4 未验收/未合入。

## 人工重试与新领取竞争（qo / qp / qq）

必要性：API 的 ORM 身份映射可能仍持有 failed 状态，而另一会话已领取 pending 新 attempt。qo 通过真实临时 Turso 两会话复现旧请求仍返回成功，1 failed（0.78 秒）。候选改为按观察到的 attempt、status、torrent URL 条件 UPDATE；失配 rollback 并返回 409 INVALID_STATE，不入队、不覆盖新任务。仍保持成功重置先 commit 后 enqueue。

qp 的 74 项回归通过，新增测试在预期 409 后读取回滚过期对象导致 MissingGreenlet；测试改为提前保存 ID，经独立会话验证新 attempt、pending 状态及计数 2 均保留。qq magnet 单元/API 专项 75 passed、115 deselected、1 warning，17.27 秒，退出 0；Ruff 通过。首次 qp 自动审批超时未启动，一次重试后运行。

日志及 JUnit queue-magnet-qo/qp/qq、API 契约及 aggregate patch/source 已保存，补丁可应用检查通过。此轮使用真实临时数据库、模拟 libtorrent/enqueue，未覆盖真实 PG/Redis 进程竞争，也不是完整集成门禁。下一步仍为 inspection 条件写入、领取崩溃与 sweep 回收、PG 迁移及完整冻结验收。B4 保持待办，候选未合入 main。

## inspection 提交前身份校验（qr / qs）

必要性：首次检查 attempt 后，inspection 仍可能 await，期间新任务可以领取并更新数据。qr 在实际解析循环中使用两个真实 Turso 会话，由 inspection 替身交错提交新 attempt、pending 状态和新标题，再写旧标题；旧代码 1 failed、63 deselected、0.72 秒，明确观察到新标题被覆盖。

候选在 inspection 返回后、事务提交前，以 ID/attempt/done 为条件执行同值 UPDATE（no_autoflush），匹配时持有写锁直到提交，失配则回滚整个检查事务。qs 完整 magnet 单元、迁移及 integration/magnet/test_magnet_resolve_coverage.py 共 127 passed、1 warning、20.75 秒，退出 0。Ruff 与 aggregate patch 应用检查通过；日志/JUnit queue-magnet-qr/qs、业务契约和补丁 manifest 已保存。

此测试证明真实 Turso 上的数据库回滚，不证明真实 libtorrent/网络或 PostgreSQL 并发；inspection 的外部副作用也不能由数据库事务保证。下一步补 PG 的 inspection/人工重试/迁移矩阵，并核对 sweep 与领取崩溃恢复。最终冻结及完整集成覆盖率门禁仍未完成；B4 继续待办，未合入 main。本轮进程均终态，无 Compose 栈。

## PostgreSQL magnet 矩阵（qt / qu）

必要性：Turso 的事务证据不能替代默认部署 PostgreSQL。新增正式 queue_recovery/magnet_driver，使用独立子进程和随机 queue_recovery_* 数据库：执行旧 schema（无 attempt 列）连续两次迁移，验证 NULL/running/done 历史状态及 NULL attempt 均保持，再执行人工重试与 inspection 两项交错测试。数据库和会话真实，资源和网络/inspection 调用为明确标注的合成替身，不冒充真实种子验收。

qt 初版两个竞争用例：1 passed、4 deselected，1.27 秒；qu 加入迁移后完整三项矩阵：1 passed、4 deselected，1.50 秒，均退出 0。正式入口新增 magnet 参数并断言三项结果均通过；每轮 finally 删除专用数据库，子进程有 90 秒超时。Ruff 和聚合补丁应用检查通过。

唯一 Compose 项目 rssripple-v14-magnet-qt 已 down -v，退出 0。证据 queue-magnet-qt/qu.log、对应 XML、qu.json 与 qt-cleanup.log 已归档；正式测试、清单及 source manifest 已同步。第二轮复用 runner 的临时输出路径，首轮 XML 已先保存，最终 JSON 属 qu。此轮没有重跑其他 queue_recovery 参数或完整覆盖率门禁。下一步检查 sweep 回收、领取后崩溃和实际 inspection 外部副作用；B4 仍未验收、未合入 main。

## sweep 失权回滚与传播（qv / qw）

必要性：回收器也是队列作业，失权后不可继续清空 attempt 或把失权异常当普通 enqueue 失败。qv 三参数复现入口失权继续执行、回收 UPDATE 后失权仍提交、enqueue 失权被吞掉，3 failed、28 deselected，1.42 秒。候选增加入口/SQL 前后/逐项入队/返回检查，显式传播 ExecutionOwnershipLostError；回收 UPDATE 后失权通过事务异常回滚。

qw job_handlers 与 magnet_resolve 全文件 95 passed、1 warning、20.21 秒，退出 0；验证真实临时 Turso 回滚后保留 pending 和原 attempt。Ruff 通过（测试后仅调整导入顺序及注释），补丁应用检查通过。业务文档及 queue-magnet-qv/qw 日志/JUnit 已保存，无 Compose 栈。

同时修正文档中“活任务绝不会超过回收阈值”的错误断言：semaphore 等待可超时，安全依赖 attempt 写入隔离，不能把时间当死亡证明。PG sweep 并发、领取后真实进程崩溃、同进程 inflight 阻止重领取及实际 inspection 外部副作用仍待验证。B4 尚未完成最终验收，未合入 main。

## 同进程回收后重新领取（qx / qy）

必要性：数据库回收并不清除旧进程内 inflight 标记，可能持续阻止 replacement。qx 使用真实 Turso、实际 launch/create_task 和受控等待协程，在旧任务等待期间提交回收，再次 launch 被旧内存标记拒绝；1 failed、64 deselected，0.66 秒。

候选移除 resource ID 级 inflight 集合，统一依赖数据库条件 UPDATE 去重，semaphore 继续约束解析并发。原内存标记测试改为真实 running 行不可领取断言；恢复用例验证两次领取使用不同 attempt。qy magnet 全单元与 integration coverage 文件 127 passed、1 warning、21.69 秒，退出 0，Ruff 通过。E2E 夹具仅移除退役标记清理，本轮未运行 E2E 网络套件。

queue-magnet-qx/qy 日志/JUnit、业务契约及聚合补丁已保存，应用检查通过。仍需 PostgreSQL sweep/实际进程崩溃恢复、网络外部副作用和最终完整验收。此测试用受控协程，不宣称 SIGKILL 或真实网络证据。B4 未验收/未合入，无活动测试进程或 Compose 栈。

## PostgreSQL sweep 与同进程恢复矩阵（qz）

必要性：补齐 qv–qy 的默认部署数据库证据，并检测新增 magnet 修复对已有 queue_recovery 的回归。magnet_driver 增加 sweep 入口/SQL 后/入队失权三项及旧协程等待时回收重新领取，连同迁移和原有两项竞争共七项；正式入口强制断言结果数量与每项成功。

qz 完整 queue_recovery 五参数 5 passed、42.13 秒，退出 0：真实 Redis 通知 SIGKILL/暂停接管，以及 PostgreSQL metadata 17、agent 6、magnet 7 项矩阵均通过。后三者的失权/网络依赖仍为确定性替身，通知的进程信号证据不能外推为 magnet 进程恢复已验收。Ruff 通过。

日志/JUnit queue-magnet-qz、五份结果 JSON 与 qz-cleanup.log 归档，测试清单及隔离说明同步。唯一项目 rssripple-v14-magnet-qz 已 down -v，退出 0；子库均由测试 finally 清理。下一步补 magnet 领取后的真实进程终止/恢复及 inspection 外部副作用，再进入完整冻结门禁。B4 仍待办，未合入 main。

## magnet 领取后真实 SIGKILL 恢复（ra）

必要性：同进程协程替身无法证明领取事务提交后进程死亡可恢复。新增正式 magnet_crash 子进程：实际 launch 提交 pending/attempt 后输出身份并等待；父进程用独立 PG 会话验证持久状态，SIGKILL 后断言退出 -9。随后仅将该测试行更新时间调旧，实际 sweep 回收并验证入队，再实际 launch 新执行，验证新 attempt、done、缓存字节一致及可解析。

解析替身返回 metadata_corpus_v1/torrents/987a72c09d5b0c2e934fa5016cc4dda6427a80dc5a4594e284a06ccf966acdb2.torrent 的录制字节；结果 JSON 保存 SHA256。资源身份为合成，网络与 inspection 均为替身，不宣称公网 libtorrent 或 inspection 副作用验收。没有修改生产时间或数据库。

ra 正式 magnet 参数（八项矩阵）1 passed、4 deselected，2.46 秒，退出 0；Ruff 通过，录制字节成功解析。queue-magnet-ra 日志/JUnit/JSON 与 cleanup.log 已归档。唯一项目 rssripple-v14-magnet-ra down -v 退出 0，子进程已终止、子库 finally 删除。正式入口、测试清单与聚合补丁同步；仍需实际 inspection 副作用审查及完整冻结门禁，B4 尚未验收、未合入 main。

## 真实单文件 inspection 的持久化缺口（rb / rc / rd）

必要性：ra 的 inspection 替身仅证明缓存恢复，无法证明文件指派完整。移除该替身并检查独立会话中 file_assignments 的路径，rb 1 failed、4 deselected，2.53 秒：done 和缓存均成功，但指派为空。实际 inspection 用 hasattr 同步访问未加载关系，异步懒加载错误被静默处理。候选调用前显式 refresh file_assignments，保持原 inspection 逻辑。

rc 同一隔离项目、新建数据库运行八项 PG 矩阵：1 passed、4 deselected，2.39 秒，实际录制单文件种子指派成功持久化。rd 全 magnet 单元与专项 integration coverage 文件 127 passed、1 warning、21.07 秒，退出 0；Ruff 通过。恢复 JSON 明确改为实际 single-file inspection，仍模拟网络解析，不涵盖多作品 LLM 路径。

queue-magnet-rb/rc/rd 日志/JUnit、rc.json、rb-cleanup.log、业务契约和聚合补丁均保存，应用检查通过。唯一项目 rssripple-v14-magnet-rb 已 down -v，退出 0。本轮无遗留进程/栈。接下来需审查多作品 inspection 调用链的事务外副作用及其他 B4 未闭合边界，再冻结完整门禁；B4 保持待办，未合入 main。

## 多作品副作用复核及完整回归启动（re / rf / rg）

按 code-review-and-quality 复核：torrent_inspect → batch_content_analysis/franchise_service/cluster_work_binding 的直接调用链没有自行 commit，数据库写入由调用方事务负责；但三条路径均可进入 metadata_service 的外部 upsert。download_and_cache_poster 按 URL 摘要命名并直接 write_bytes，属于数据库回滚之外的共享缓存副作用。当前只能确认该写入边界存在，尚无并发破坏复现，不能把单文件 rb–rd 成功外推为多作品路径已安全。

re 对整个临时目录执行 Ruff 时被未纳入交付补丁的 download_takeover_probe.py 三项格式错误阻断；rf 对正式 app/tests/scripts 检查通过。两份日志保留，不修改历史探针来美化证据。

启动 rg 完整 tests/unit + tests/api，--cov=app --cov-fail-under=95。命令在 /tmp/rssripple-v14-dispatch-lx 执行，活跃 PTY session 96790；日志 /tmp/rssripple-v14-unit-rg.log，预期 JUnit /tmp/rssripple-v14-unit-rg.xml、coverage /tmp/rssripple-v14-unit-rg-coverage.xml。当前已确认运行，尚无终态；必须先轮询同一 session，不得重复启动。此为累计候选回归，尚非 B4 最终冻结验收。下一步处理其实际失败/覆盖率结果，并继续共享缓存与事件循环阻塞治理。B4 未合入 main。

## 海报残缺缓存复现（rh）；rg 持续运行

本轮先轮询 session 96790，确认仍运行，日志从 7% 推进到 9%，尚无最终结论。为保持其源码稳定，未修改候选 app/tests。

独立探针 probes/poster_partial_write_probe.py 使用真实 TemporaryDirectory 与合成有效 SVG 字节、模拟网络返回；Path.write_bytes 被注入“写 4 字节后 OSError”。第一次缓存调用返回 None，第二次直接命中 4 字节残缺文件 URL（应为 62 字节），rh 日志明确复现。未联网、未写生产缓存，进程正常退出，临时目录自动清理。

必要性和优先级：这是可恢复的展示缓存损坏，列入 P2；并非已证明旧 attempt 覆盖新数据库数据，不能据此扩大 B4 的阻断范围。方案为同目录临时文件完整写入后原子发布、失败清理及已有无效缓存检测，磁盘工作移出事件循环。实现留待 rg 终态之后，避免运行中修改测试对象。完整门禁与 B4 验收仍未完成。

## rg 完整单元/API 终态

同一 session 96790 已正常退出 0；3924 passed、15 skipped、6 warnings，1999.55 秒。覆盖率 XML 为 22716/23328 行、97.38%，满足本轮 95% 阈值；JUnit 3939 tests、0 failures、0 errors，与日志一致。queue-unit-rg.log/xml/coverage.xml 已归档。测试期间候选源码保持不变；没有因观察等待重启。

15 个跳过项：test_database_migrations 的 PostgreSQL create_tables、legacy_shapes、legacy_delivery_columns_nullable 三项因本机 127.0.0.1:5432 不可用；test_decision_migration 的 PostgreSQL startup 专用分支一项；退役资源级 metadata API 十一项。前四项不能按通过计算，也不能用 magnet 的 PG 迁移矩阵替代不同 schema 分支；最终隔离 PG 验收需明确覆盖。

下一步优先补通知 regenerate_resource_notifications 与旧集数协调 reconcile_stale_raw_episodes 的内部 commit 失权负向测试，再修复并进入最终冻结。rg 证明累计代码回归通过，不覆盖尚未写出的缺陷断言，不构成 B4 完成。无活动测试 session 或测试 Compose 栈，B4 未合入 main。

## 定向通知重新生成提交保护（ri / rj）

必要性：regenerate_resource_notifications 在快照返回后更新 payload、作废投递 attempt 并自行 commit；入口下游的快照检查不能覆盖最后一次提交。ri 两参数（snapshot 返回/invalidated SQL 完成后失权）均未抛失权异常，2 failed、11 deselected，1.67 秒。

候选在入口、逐任务、快照应用前、最终 commit 前和计划生成前检查所有权；测试通过 committed_session 调用，失权后独立会话验证旧 payload 和投递 token 均保持，plan_for_notifications 未调用。rj 完整通知服务与队列通知所有权文件 59 passed、1 warning、24.34 秒，退出 0；Ruff 通过。

queue-notify-ri/rj 日志/JUnit、通知契约及聚合补丁保存。测试为真实临时 Turso 和确定性失权/快照替身，尚无该边界的 PG 矩阵，不宣称 Redis 与 SQL 原子提交。下一步旧集数协调提交保护及 PG 补充；rg 全量结果早于本次修改，最终验收必须覆盖新候选。B4 未验收/未合入，无活动测试进程或 Compose 栈。

## 旧集数协调内部提交保护（rk / rl）

必要性：协调函数在 backfill 开始前自行提交集数与发布记录，不能靠后续 backfill guard 回滚。rk 用当前单季模型（合集、season_number=4、number_of_episodes=24，无退役 seasons 写入）和人工历史锚点构造 raw 资源，分别在入口/实际 publish_resource SQL 后失权；两项均未抛失权异常，2 failed、59 deselected、1.39 秒。

候选在入口、逐个 publish 前和内部 commit 前检查所有权。通过 committed_session 调用，失权传播后独立会话检查 episode=18、confidence=raw、absolute=90 及发布记录总数均不变。rl 抓取与 job_handlers 全文件 92 passed、1 warning、33.67 秒，退出 0；Ruff 通过。

queue-fetch-rk/rl 日志/JUnit、业务契约与聚合补丁保存，应用检查通过。本轮为真实临时 Turso 和确定性失权替身；下一步将 ri/rk 新断言纳入 PostgreSQL 正式矩阵，并完成事件循环阻塞治理与最终门禁。B4 仍未验收/未合入，无活动测试进程或 Compose 栈。

## 内部提交 PostgreSQL 矩阵（rm）

必要性：ri/rk 的 Turso 回滚不能替代默认部署数据库。正式 queue_recovery 增加 commit_driver 和 commit 参数，四项复用相同断言：通知 snapshot/invalidated、协调 entry/published。每项清空独立测试库夹具，真实 SQL 更新后独立会话验证回滚结果；失权为确定性替身，未把检查与 SQL commit 声称为跨系统原子事务。

rm 1 passed、5 deselected，1.65 秒，退出 0；结果 JSON 四项全部成功，Ruff 通过。queue-commit-rm 日志/JUnit/JSON/cleanup.log 已归档，正式清单与聚合补丁同步。唯一项目 rssripple-v14-commit-rm down -v 退出 0，子库 finally 删除，无遗留运行任务。本轮未重跑其他五个 queue_recovery 参数或全量覆盖率。

下一步优先事件循环阻塞的具体路径治理及整理组合验证，随后冻结完整 unit/API 和 integration 门禁。rg 全量通过早于 ri/rk 修改，不能作为最终版本验收；B4 未关闭、未合入 main。

## 种子缓存事件循环响应性（rn / ro / rp）

必要性：fetch_torrent_file 虽将 HTTP 移入线程，mkdir、bencode 校验及 write_bytes 仍同步运行。rn 用录制种子和真实线程/Event，在三个操作内调度事件循环心跳并等待最多 1 秒；旧实现三项均无法执行回调，3 failed、107 deselected、3.33 秒。这是实际入口响应性证据，不是已量得生产磁盘延迟超过 15 秒。

候选将三个操作移至 asyncio.to_thread。ro 169 passed/6 failed（15.18 秒），原因是旧测试线程替身忽略传入参数，mkdir 缺 exist_ok 误失败；修改替身为 fn(*args, **kwargs)，保留原重试和内容断言。rp 全 torrent_inspect/magnet_resolve 单元 175 passed、1 warning、15.11 秒，退出 0。Ruff 通过。

queue-loop-rn/ro/rp 日志/JUnit、业务契约和聚合补丁已保存，应用检查通过。此处只覆盖 HTTP 缓存入口，其他同步 parse/analyze 路径仍需治理；取消协程不会终止已经运行的线程，旧线程向共享缓存路径发布的竞争需补负向测试后才能验收。下一步优先该文件发布边界，不能把响应性改进视为取消安全。B4 未合入 main，无活动测试进程或 Compose 栈。

## 取消后的缓存线程发布（rq / rr / rs）

必要性：to_thread 取消不终止真实线程。rq 使用两个可解析的合成种子和实际线程，在旧写入暂停时取消协程，新请求写入同资源缓存后释放旧线程；新缓存被覆盖，1 failed、110 deselected、0.28 秒。

候选将 HTTP 缓存最终路径改为 resource ID + 完整内容 SHA256，同目录唯一临时文件写完后 os.replace，finally 清理临时文件。不同内容互不覆盖，相同内容发布等价；保留 torrent_file 作为权威路径，既有文件可读。rr torrent/magnet 全单元 176 passed、1 warning、15.02 秒，退出 0；Ruff 通过。补 rs 写入 4 字节后抛 OSError，验证没有最终残缺文件或临时文件遗留，并重验取消竞争，两项通过（详见原始日志）。

queue-cache-rq/rr/rs 日志/JUnit、业务文档及聚合补丁保存。此证据为临时文件系统和 HTTP 替身，不是实际多进程网络下载；进程终止留下未引用缓存/临时文件的清理仍待确认，其他同步 parse/analyze 路径和完整集成仍未验收。B4 未合入 main，无活动测试进程或 Compose 栈。

## 缓存命中响应性与生命周期复核（rt / ru）

读取方以 torrent_file 为路径，复核未发现从缓存名反推资源 ID 的实现；配置注释同步新命名。未找到通用孤儿 torrent 缓存清理，已按 P2 加入 TODO，要求先盘点引用和在途写入，不能仅按年龄删除；尚未量化实际存储占用，不虚称已泄漏用户文件。

ensure_torrent_cached 的命中路径仍同步读取并解析。rt 真实临时种子+事件循环回调复现阻塞，1 failed、112 deselected、1.28 秒。候选将存在检查/解析/无效文件删除移入线程，ORM 字段修改保持协程内。ru 全 torrent_inspect/fetch_service 单元 174 passed、1 warning、28.60 秒，退出 0；Ruff 通过。

queue-cache-rt/ru 日志/JUnit、业务说明、配置注释和聚合补丁保存，应用检查通过。其他 inspection 分析入口仍需响应性核对，完整集成及最终冻结未完成；B4 未合入，无活动测试进程或 Compose 栈。

## inspection 读取和分析响应性（rv / rw）

必要性：maybe_inspect_torrent 再次同步读缓存并分析路径，前置缓存函数移入线程仍不足。rv 使用录制单文件种子，分别阻塞 parse_torrent_files/analyze_torrent_files，事件循环心跳均不能执行，2 failed、113 deselected、3.29 秒。

候选将该入口的 exists/unlink、parse 和 analyze 移至 asyncio.to_thread；线程仅接收路径/普通文件清单，ORM 字段及 session 仍留在原协程。rw torrent_inspect/magnet_resolve 单元与 magnet integration coverage 文件 242 passed、1 warning、23.87 秒，退出 0；Ruff 通过。此测试验证受控阻塞期间循环可响应，不证明纯 Python CPU 工作无 GIL 竞争，也未重跑默认租约的真实 Redis 接管。

queue-loop-rv/rw 日志/JUnit、业务契约及聚合补丁保存，应用检查通过。下一步核对其余关键同步路径并完成整理组合/真实队列响应性与最终冻结门禁。B4 未验收、未合入 main，无活动测试进程或 Compose 栈。

## 最新候选真实服务组合回归（rx）

必要性：ri–rw 修改通知内部提交、集数协调与线程化种子处理，必须确认没有破坏既有进程接管及 PG 事务保护。rx 完整 queue_recovery 六参数 6 passed、44.60 秒，退出 0。包括通知真实 Redis SIGKILL/暂停接管、PG metadata 17 项、agent 6 项、magnet 8 项（真实子进程 SIGKILL 和录制种子实际 inspection）、commit 4 项。

日志/JUnit、六份结果 JSON、候选 source manifest 与 cleanup.log 以 queue-recovery-rx 前缀归档。唯一 Compose 项目 rssripple-v14-recovery-rx down -v 退出 0，测试子库均 finally 删除。此轮只覆盖专门恢复子套件，不是全部 integration 或覆盖率门禁；受控慢解析心跳测试仍需与真实队列租约结合，整理文件锁接管组合仍未完成。没有修改生产数据库或推送远端，B4 未验收/未合入，无遗留测试任务。

## 真实 Redis 慢分析续租（ry）

必要性：线程/Event 局部测试不足以证明生产 Redis 心跳持续运行。正式 responsiveness_driver 用独立 RedisQueue 子进程执行录制种子的实际 inspection，分析函数受控 sleep 18 秒；另一真实消费者运行并尝试恢复，父进程独立采样默认 15 秒租约 TTL。未缩短租约或扩大心跳容忍窗口。

ry 1 passed、6 deselected，18.65 秒，退出 0。观察到三次续租，最低采样 TTL 10981ms，全部正值；执行计数 1，子进程正常退出。睡眠模拟慢分析，不是生产性能基准，也不证明任意纯 Python CPU 循环不会争用 GIL。该参数纳入正式集成入口，要求至少两次续租及执行恰好一次。

queue-loop-ry 日志/JUnit/JSON/cleanup.log、测试清单及聚合补丁保存。唯一项目 rssripple-v14-loop-ry down -v 退出 0；测试 Redis 清空、子库删除，无残留进程。下一步整理文件锁与真实队列接管组合及最终完整冻结门禁；B4 未验收/未合入 main。

## PostgreSQL 整理所有权矩阵（rz）

必要性：原整理所有权单元测试仅覆盖 Turso，先补默认数据库与真实文件系统证据。正式 organize_driver 三项复用 before_lock、after_validation 和 started_cleanup 断言；临时目录内创建合成媒体字节、下载和目标库，独立锁目录；旧操作开始后失权仍持锁直到任务清理完成，竞争会话立即收到“正在执行中”而非等待。

rz 1 passed、7 deselected，1.36 秒，退出 0，JSON 三项均成功，Ruff 通过。验证执行前失权不移动文件、开始后的目标字节一致且清理仅调用一次。所有权失效为注入信号，尚未覆盖真实 Redis 跨进程抢占；不把此结果当作最终整理接管验收。

queue-organize-rz 日志/JUnit/JSON/cleanup.log 与正式测试清单、聚合补丁保存。唯一项目 rssripple-v14-organize-rz down -v 退出 0，测试子库和临时文件自动清理，无活动测试任务。下一步真实队列接管与计划锁组合，随后完整冻结门禁；B4 未合入 main。

## 真实队列接管与整理文件锁组合（sa）

必要性：rz 的注入失权不能证明进程暂停后的真实接管。新增 organize_takeover_driver：独立旧消费者实际移动文件，在清理入口持有计划锁时 SIGSTOP；父进程等待默认租约自然失效，再启动另一消费者。接管执行收到“正在执行中”，明确记录 busy；恢复旧进程完成整理后，旧队列完成不得改变 replacement 的完整状态。再入队新 job ID，计划 done 幂等短路，目标字节一致。

sa 正式参数 1 passed、8 deselected，17.02 秒，退出 0。真实 PG/Redis、跨进程锁和信号，媒体内容为合成。专门测试 handler 将 busy 转为显式结果，并由测试重入，不能据此宣称生产 handler 自带重试或 exactly-once 外部调用。Ruff 已检查新增 driver。

queue-organize-sa 日志/JUnit/JSON/cleanup.log、权威测试说明和聚合补丁保存。唯一项目 rssripple-v14-organize-sa down -v 退出 0，子进程结束、子库和临时文件清理。关键组合证据已补齐，下一步对最终候选做审查、冻结并启动完整单元/API 和隔离集成门禁；尚未以局部通过关闭 B4，未合入 main。

## 冻结验收 sb / sc 启动

正式 app/tests/scripts Ruff 通过，聚合补丁可应用，主工作树运行代码无未提交差异。对原型 app/tests/scripts（含离线夹具，排除 pyc）及 pyproject/uv.lock/.coveragerc/隔离 Compose 共 3084 文件记录 SHA256，probes/queue-final-sb-source.json。启动后不修改这些文件。

sb：完整 tests/unit tests/api，--cov=app --cov-fail-under=95；session 86184，日志 /tmp/rssripple-v14-unit-sb.log，JUnit /tmp/rssripple-v14-unit-sb.xml，覆盖率 /tmp/rssripple-v14-unit-sb-coverage.xml。尚无终态。

sc：唯一项目 rssripple-v14-final-sc，启动日志 /tmp/rssripple-v14-integration-sc-start.log，启动 session 66608 正常退出 0，app/app-llm/test-server/transmission/PG/Redis 均健康。完整 test-runner session 47946，输出 /tmp/rssripple-v14-integration-sc.log，尚无终态。结束后即使测试失败也要导出 JUnit/覆盖率数据，SIGINT 两应用并确认正常退出，汇总四份覆盖率（阈值 85%），导出全部报告，再 down -v。不得提前清理 gate-data 卷。

最终对照冻结哈希；任何测试失败或后续代码变更均需重新确定验收基线，不能沿用局部绿测关闭 B4。此阶段未合入 main、未推送远端。
