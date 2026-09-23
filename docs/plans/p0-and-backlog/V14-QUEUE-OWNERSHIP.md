# V14：Redis 执行所有权（P1-B4）

状态：必要性已复现，方案审查中，未实现、未验收。B7 的 km/kn 门禁继续运行，本轮不修改其冻结代码。

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
