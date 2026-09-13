# V2：整理计划版本与执行所有权

状态：2026-09-13，临时副本已接入 O1/O2/O5 的版本 CAS、执行快照、共享所有权及取消门禁，服务/API 与 PostgreSQL 竞争专项通过；真实清单、完整迁移和部署验证仍未完成。尚未同步生产，不关闭 TODO。V0/V1 完整门禁运行期间未修改其被测源码。

## 必要性和优先级

三项共同破坏“同一版计划只能由一个执行者按该版语义执行”的约束，合并处理，维持 P1。默认 PostgreSQL＋多 worker 部署确实存在跨进程入口；内存锁和本进程的 `_executing_plan_ids` 不能证明另一个 worker 已退出。优先于索引和界面优化。

| 项目 | 当前代码路径 | 复现方法及判定 |
|---|---|---|
| O1 | `replan_open_plans` → `_rebuild_plan` 读取 pending，在线程中规划后替换 ops，没有再次核对版本和状态 | PostgreSQL 独立会话：暂停已经完成计算但尚未落库的重规划；另一会话完成真实 hardlink 执行；随后释放重规划。核对新会话中的 plan/op 状态和审计 |
| O2 | `update_organize_rule` 先 commit 再重规划；`execute_plan` 读取当前 rule.file_op | 在同样的 commit-before-replan 窗口把 hardlink 改为 move；原操作行未变，执行却删除源文件。不是禁止用户有意切换模式，而是禁止旧计划与新模式混合 |
| O5 | `running` 且本地 set 不含 id 时允许重放 | 两个独立 Python 进程共用真实 PostgreSQL 和临时文件，首个进程在实际执行器入口暂停，第二个进程进入并完成；释放首个后也完成，产生两条 execute 审计 |

O1 的具体状态组合受 ORM 写集影响：旧对象的 pending 再赋 pending 不一定发 UPDATE；可能保留另一个会话写入的 done，却替换成全新的 pending ops。不能只断言 plan 最终变回 pending。

复现媒体为明确标注的 300 字节合成文件；沿用整理测试的合成通知，不能称为真实下载语料。数据库、ORM、规划器及文件执行器均真实执行；仅下载器后置清理/恢复入口被替换，避免访问外部服务。线程屏障控制交错，不替换文件执行结果。正式验收还必须接入 V0 已审核真实清单和 Task 流水线。

## 方案约束

1. **冻结执行语义及版本。** `OrganizePlan` 增加单调递增 revision 和明确的 file_op 快照；ops、通知 payload、所选库、类别、模板派生结果与 file_op 在同一事务产生。执行不再依据实时 rule 推导 move，规则被删也不能把 hardlink 默认为 move。
2. **配置更新使旧版失效。** 规则、库及影响路径的卷配置变更必须与配置版本/待重建标记一起提交，覆盖新增、排序、禁用和删除造成的 first-match 变化。pending/failed 继续按当前配置重建；执行抢占同时核对配置版本和计划 revision，不能趁配置已提交、重建尚未完成的窗口执行旧 ops。已开始的版本保留执行语义；配置更新不会半途改写它。
3. **原子提交重建。** 规划计算可以在事务外进行；最终提交使用 `id + revision + 可重建状态 + 配置版本` 条件更新。只有条件命中才删除/插入 ops、写快照及审计；全部同一事务。竞争失败丢弃计算结果，再读取当前状态，不覆盖 done/running/cancelled。人工 classify、通知 regenerate 也必须走相同门禁。
4. **原子抢占与结果归属。** 执行用条件 UPDATE 抢占 pending/failed 并记录不可复用的 owner token 与执行 revision。零行命中不得进入文件执行器。结果回写、失败回写、审计与 Task 后置动作均核对 token/revision；不能让陈旧 ORM 实例覆盖新 owner。
5. **取消先取得所有权。** API 目前可能在另一进程执行期间清理甚至删除下载数据。取消的状态抢占必须发生在任何删除/RPC 前，且活动执行一律拒绝；Web 进程本地 set 不作为依据。批量入口、自动执行和直接服务调用必须遵循同一规则。

仅增加 `UPDATE ... WHERE status IN (...)` 不足以关闭 O5：它阻止初次双抢占，却没有定义进程崩溃、线程仍执行而协程被取消、DB 断连和恢复重放的边界。

## 崩溃恢复：实施前必须验证的决策

不采用“心跳超时即可接管”的方案：原 owner 可能只失去 DB 连接，文件线程仍能删源；数据库 fencing token 只能阻止陈旧结果回写，不能撤销文件副作用。也不能仅因 Web 的本地 set 为空就恢复 running。

首个原型评估 PostgreSQL 会话 advisory lock：实际 `pg_terminate_backend` 后，另一个连接已能抢占该锁，但原 Python 进程及暂停的文件线程仍存活，确认不能单独据此恢复。锁需覆盖文件线程及后置动作，并与重建、取消一致；Turso 单节点也必须正确处理协程取消后线程继续运行的情况。

会话锁本身仍不能证明断连后的文件线程已经停止，因此不能仅凭锁重新可取得就自动接管 running。若不能建立可靠的执行者终止证据，恢复须保守保留 running 并提供可诊断的受控恢复流程；不能静默永久卡住，也不能为了保留旧自动重放行为引入双删源。恢复接口、运维步骤及状态契约在原型验证后定稿，再同步权威设计，当前不宣称已有完整解法。

当前原型改用持久共享文件锁作为文件执行生命周期的保护：默认 Compose 的 Web/worker 均挂载同一个 `app-data:/app/data`，可使用独立的 `data/organize-locks` 目录。每个计划的锁文件永久保留且不得被日常清理、unlink 或替换，否则同名不同 inode 会产生两个 owner。路径末端不跟随符号链接，文件必须普通文件；锁键用计划 id 的 SHA-256，避免非法 id 变成路径或改变既有 NOT_FOUND 行为。目录打开、锁获取与释放在线程中执行。

整个受保护协程通过 shield 等待完成，包含文件线程、DB 回写、Task 后置动作及锁释放；重复取消不能使外层提前释放锁，最终仍向调用者返回取消。硬终止进程后由内核关闭文件描述符，后续执行者取得锁后才可按幂等协议恢复。DB 连接丢失不会主动释放这份文件锁。

此方案新增明确的部署要求：所有执行者与取消入口必须使用**同一份持久、支持跨进程排他锁的存储**。仅配置相同目录字符串不能证明不同主机共享同一 inode；不支持可靠锁的远程文件系统不能宣称安全。正式实现前需补默认 Docker 命名卷多容器验证、错误配置拒绝与运维说明；仍保留 DB revision/owner token/CAS 作为持久状态的一致性门禁。当前共享文件锁原型不代替 O1/O2 修复。

## 严格验收标准

| 场景 | 必须断言 |
|---|---|
| 真实 PostgreSQL 双进程抢占 | 仅一个进程进入实际执行器；另一个返回明确冲突；每个执行 revision 仅一份完成审计，Task 后置动作不重复 |
| 重建/分类/通知更新与执行交错 | 两种先后次序各覆盖；版本一致、plan/op 状态一致；旧计算不能覆盖终态 |
| hardlink→move、规则删除、库/卷改址 | 配置提交至重建完成窗口也不得混合旧 ops 和新语义；完成重建后的合法新配置可执行 |
| Web 取消与 worker 执行 | 拒绝活动计划，源、目标及下载任务不变；包括 delete_data=true；非活动取消正常成功 |
| SIGKILL、DB 断连、协程取消 | 使用实际进程/连接故障，证明旧执行者不会与恢复者重叠；未确认停止时不进入恢复文件操作 |
| 部分文件已发布后重试 | 保留 P0 内容比较与 no-replace 不变量，不覆盖不同内容；验证源内容、目标内容、剩余操作及 Task 状态 |
| 迁移 | 新装与升级、Turso 与 PostgreSQL；重复执行幂等；历史缺 file_op/revision 的计划不得猜成 move，需重建或明确拒绝 |

正式测试放入独立 organize 集成套件，至少一组使用已审核真实 torrent 清单（媒体字节仍为合成），另加清晰标注的故障注入。每项核心负例须先在旧实现失败，修复后通过；不使用单进程 asyncio 锁测试代替 PostgreSQL 双进程验证。

专项通过后执行完整单元/API ≥95% 与隔离集成 ≥85%，记录源码哈希、退出码、JUnit、coverage 和项目清理。仅当实现、恢复契约、权威文档及完整门禁均完成，才删除 TODO O1/O2/O5。

## 实施拆分

1. 保存三个原行为复现和源哈希，形成可重复的失败证据。
2. 在独立副本验证执行所有权与恢复原型，先排除不安全自动接管；产出进程故障测试。
3. 实施计划 revision/file_op、配置失效和两后端幂等迁移，统一重建/分类/执行/取消门禁。
4. 接入真实清单与完整流水线，更新 data-models、file-organization、api-endpoints 和测试文档，执行完整门禁。

## 已完成的复现证据

独立项目 `rssripple-v2-probe-20260913-g`、PostgreSQL 16、最终脚本退出码 0，三项原行为均确认：O5 两个不同 PID 各完成一次；O2 保留原操作行却 move 删源；O1 最终 done 计划包含 pending op。报告 [ownership-result.json](probes/ownership-result.json)，相关源码哈希 [ownership-source.json](probes/ownership-source.json)。这证明修复必要性，不是修复后的验收通过。初版脚本先修正了场景间数据库隔离，并根据实际 ORM 写集修正 O1 观测条件；最终从空测试表重跑全部三项。

复跑工具 [organize_ownership.py](probes/organize_ownership.py) 是原缺陷的行为断言脚本，不加入 pytest 验收集合。它会删除并重建所连接数据库的应用表，因此只能连接 [compose.ownership.yml](probes/compose.ownership.yml) 创建的独立临时数据库。使用唯一 `-p` 启动该配置，`port postgres 5432` 读取随机回环端口；在新临时目录复制当前 `app/`（排除 static）及该脚本，复制 `tests/unit/test_organize_service.py` 为 `seed_helpers.py`，引用当前 `.venv`，不复制 `.env`。在临时目录以显式测试 `DATABASE_URL` 运行脚本，保留输出及 `evidence-*`，最后对同一项目执行 `down --volumes --remove-orphans`。修复后应改用上文相反的安全断言，不能要求这个原行为脚本继续成功。

## 所有权原型结果

- 项目 `rssripple-v2-lock-20260913-h`：实际终止 PostgreSQL 后端，证明 advisory lock 释放而原进程存活；共享文件锁仍拒绝接管。SIGKILL 退出 -9 后，取得文件锁并通过真实执行器发布 hardlink。媒体为 1,048,577 字节合成数据，验证源目标同 inode。
- 两次调用 cancel 后仍持锁；文件线程和模拟结果收尾完成才释放，并保留调用者 CancelledError。原型工具 [ownership_lock_probe.py](probes/ownership_lock_probe.py) 与 [ownership_guard.py](probes/ownership_guard.py)，输出 [ownership-lock-result.json](probes/ownership-lock-result.json)。在上文临时副本及专用 PostgreSQL 中运行，脚本不清表，不访问实际下载器；不作为远程文件系统锁认证。
- 已将异步锁和完整生命周期保护接入临时副本的真实 `execute_plan`。PostgreSQL 双进程仅一份 execute 审计、竞争者在执行器前拒绝，输出 [ownership-service-result.json](probes/ownership-service-result.json)。已有执行服务回归 **10 passed / 66 deselected**，12.72 秒；新增真实服务重复取消测试 **1 passed / 76 deselected**，1.53 秒，独立会话确认最终 done、操作 done、源目标 hardlink。报告 `/tmp/rssripple-v2-ownership-unit.xml`、`/tmp/rssripple-v2-cancellation.xml`。
- 可评审临时补丁 [ownership-prototype.patch](probes/ownership-prototype.patch)，副本 `/tmp/rssripple-v2-ownership-work`，文件哈希 `/tmp/rssripple-v2-prototype-state.json`。**未同步生产**；取消 API、重建门禁、revision/配置快照及迁移仍未实现，不能用原型专项通过关闭 O5。原型必须先完成这些入口及部署验证，再进入完整门禁。

### 默认部署与配置版本续轮

- 默认共享卷验证：独立项目 `rssripple-v2-volume-20260913-i`，两个无网络容器共享同一 Docker 命名卷。第二个容器确认不能取得活动 owner 的锁；持锁容器经 SIGKILL、实际退出 137 后，另一容器取得锁并用真实执行器生成 hardlink（同 inode）。[结果](probes/ownership-container-result.json)、[驱动](probes/ownership_container_probe.py)、[Compose](probes/compose.ownership-lock.yml)。在临时副本内把驱动保存为 `container_probe.py`、Compose 保存为 `compose.lock.yml` 后运行；需要副本中已应用 ownership 原型的 `app/`，不直接在文档目录运行。i 的全部容器及命名卷已清理。
- 临时副本新增 OrganizeConfiguration 单例版本、Plan revision/config_revision/file_op/owner_token 字段及轻迁移草案。配置 ORM flush 和 bulk UPDATE/DELETE 在同一事务推进版本，覆盖规则、库、卷路径与下载器映射。显示备注和下载器在线状态不失效计划。首次种子写入必须晚于 Turso MVCC 初始化：已移除导致 `BEGIN CONCURRENT` 失败的 table after_create DML，改为迁移阶段显式初始化及配置写入时幂等确保存在。安全关键 Plan 列迁移失败必须中止，不能被 best-effort 吞掉。
- 配置事务断言：Turso **3 passed / 1 warning，1.12 秒**，JUnit `/tmp/rssripple-v2-config-revision.xml`；独立 PostgreSQL 项目 `rssripple-v2-config-20260913-k` 同三项断言全部通过，日志 `/tmp/rssripple-v2-ownership-work/config-pg.log`。覆盖正常更新、bulk 更新、回滚和非规划字段变化。测试数据为合成配置，无媒体真实性声明。
- 上述字段与事件尚未接入执行/重建 CAS；迁移重复执行、升级旧计划、并发配置写入、取消接口及完整服务回归仍待完成。该基础实现不改变 TODO 状态。最新临时补丁与哈希已更新，不可把上一版四文件补丁误当作当前全部改动。

### CAS、执行快照及取消入口接入

本节更新前面的实施状态，历史步骤保留供核对。

- 规划保存 file_op、needs_category 和配置版本。重建/分类先以 `plan.id + revision + pending/failed + 当前配置版本` 条件更新，命中后才在同一事务替换 ops、快照和审计；失配不改原计划。执行抢占记录新 revision/owner_token，完成或失败回写也需匹配该 token/revision，不能回写过期执行结果。
- pending/failed 的配置版本过期或缺旧快照时，执行入口先按当前配置重建并提交新版本，再从该版本读取 file_op；不是把新模式套在旧操作行上。执行前再次用配置版本 CAS，覆盖重建后再变更的窗口。running 恢复必须取得共享文件锁，并保留原 file_op；未知模式的旧 running 计划拒绝猜成 move。
- 增加 manual_destination 区分实际人工分类和规则被删后 FK SET NULL；后者不得被误认成“手工选择库，默认 move”。needs_category 也从计划快照读取。新 Boolean 默认值使用 SQL `false()`，避免 Turso 把带引号的字符串 `'false'` 读成真值；已有重路由、规则收紧及 hardlink 保种断言捕获并验证了该原型问题。
- Web 取消入口取得同一共享锁，刷新状态后 CAS 取消并提交，才允许清理 Task/数据。拒绝活动 owner 时任何清理入口均不调用。配置读取显式刷新 ORM identity map，防止新版本号配上旧规则对象。
- 线程执行与锁获取/释放使用原始 executor Future 并 shield 等待，解决 asyncio 关闭时同时取消外层调用与内部 owner Task 的情况；原始 Future 不属于 shutdown 的 Task 集合。外层仍在完成后返回 CancelledError。两种取消场景 **2 passed / 76 deselected，1.08 秒**，`/tmp/rssripple-v2-shutdown.xml`。
- 修正默认值后的服务＋配置事务＋整理 API 专项 **129 passed / 1 warning，51.14 秒**，`/tmp/rssripple-v2-state-api.xml`。PostgreSQL 项目 `rssripple-v2-state-20260913-m` 已验证：双进程只完成一次、活动 worker 时 Web delete_data 返回 409 且清理零调用、模式变更先替换旧操作行、重规划竞争后 plan/op 均 done。此处 Web 使用真实 ASGI 路由与 PG 依赖，清理入口用“若被调用则失败”的负向探针；不冒充完整部署 E2E。最终线程加固后的相同专项正在复跑，最终日志见 `/tmp/rssripple-v2-state-api-final.log` 与副本 `v2-state-final-pg.log`。

仍需完成：执行 202 入口的跨进程忙碌反馈与快照字段展示、共享锁目录错误配置的验证、升级/幂等迁移测试、真实清单与完整 Task 流水线集成、相关权威设计和完整门禁。尚不能关闭 O1/O2/O5；当前补丁留在临时副本。

最终续报：最新实现的服务/API 专项 **130 passed / 1 warning，51.84 秒**，退出 0，`/tmp/rssripple-v2-state-api-final.xml`；PostgreSQL 竞争/取消验证也退出 0，[结果](probes/ownership-state-result.json)、[驱动](probes/ownership_state_probe.py)。驱动在应用当前临时补丁的副本运行，沿用上文专用测试 DB 和 `seed_helpers.py`，会重建测试表，不可连接业务数据库。m 项目的容器、临时 DB 与网络已清理。

### 执行反馈、快照展示与旧库升级

- 执行 202 入口增加共享锁探测，活动 owner 返回 ALREADY_RUNNING，且不创建后台执行任务；实际执行仍独立抢占，探测结果不被当作长期所有权。旧 running 缺 file_op 返回 INVALID_STATE，不接受随后必失败或猜测模式的恢复请求。
- 计划列表/详情增加 revision、file_op、needs_category。已冻结计划的分类原因不再随当前规则模板漂移；仅 legacy 无快照行保留提示性回退。前端列表和详情显示计划的处理方式，详情说明移动删任务或复制/硬链接保种；复用现有中英翻译，未知模式显示“未知”。前端 tsc/Vite 构建与三个改动文件的 ESLint 通过，日志 `/tmp/rssripple-v2-frontend.log`。
- 新增执行反馈和冻结响应断言，完整整理 API **51 passed，21.13 秒**，`/tmp/rssripple-v2-execute-api.xml`。这是新增入口后的最新 API 专项，不与前文 130 相加。
- 迁移测试复制完整旧 schema，移除六个新增 Plan 列及配置版本表，并以 Core SQL 写入真实 FK 链 Channel→Resource/Downloader→Task→Notification→旧 running Plan（合成 payload，不访问下载器或媒体文件）。两次调用实际 `_apply_light_migrations`，验证历史状态/payload 不变、file_op/config_revision/owner_token 保持 NULL、布尔标志为 false、revision=0，且第二次不把配置版本 77 重置为 0。
- Turso 原始模式通过后，再以生产默认 CONCURRENT/MVCC 配置复跑：**1 passed / 1 warning，0.58 秒**，`/tmp/rssripple-v2-migration-mvcc.xml`。独立 PostgreSQL 项目 `rssripple-v2-upgrade-20260913-n` 执行相同旧 schema 与两次迁移断言，退出 0，日志 `/tmp/rssripple-v2-ownership-work/migration-pg.log`；项目已全部清理。测试在副本 `tests/unit/test_organize_migration.py`，已包含在临时补丁中。

剩余重点是共享锁目录错误配置的拒绝、真实已审核清单的完整通知/执行/清理链路及权威文档同步。相同目录名不等于共享存储，不能因默认命名卷测试通过就忽略自定义部署；需补锁目录身份注册与不同目录的负例，并明确活跃锁目录不可复制或替换。当前所有改动仍未同步生产，TODO 不关闭。


### 锁域注册、真实清单与完整整理专项（2026-09-13）

- 临时原型新增配置单例 `lock_domain` 与共享目录 `.domain` UUID 身份。首次使用以 no-replace 发布身份文件，再以条件写入注册数据库；后续缺文件、身份不符或符号链接均拒绝，不能静默创建第二套锁。取得计划锁时再次通过同一目录描述符核对身份。不能证明被复制到另一文件系统的同一 marker 仍共享 inode，因此禁止复制或替换活跃锁目录。
- PostgreSQL 独立项目 `rssripple-v2-domain-20260913-o` 双进程同目录退出 `[0,0]`；不同目录 `[12,0]`，12 为预期拒绝。日志在副本 `domain-pg.log`。迁移两次执行包含 lock_domain 的最新 PostgreSQL 断言通过（`migration-domain-pg.log`）；服务并发、规则变更、过期重规划与 Web 删除取消断言均通过（`v2-domain-final-pg.log`）。项目清理状态待本轮完成后记录。
- 最新配置/所有权/迁移/服务/API 专项 **136 passed、1 warning，52.89 秒**，报告 `/tmp/rssripple-v2-domain-scope.xml`。身份负例专项 3 passed；这些计数不叠加。
- 沿用已审核真实 case `f79ef2eb-02d5-42d3-80dc-70dd3c1d733b` 的猫与龙 S1E10 原始标题、torrent 哈希及文件清单；媒体字节为明确合成。真实通知→计划→执行→任务清理/保种共 **15 passed，9.92 秒**，`/tmp/rssripple-v2-real-manifest-final.xml`。新增配置变更场景核对新操作行、新版本及冻结 file_op，覆盖 move/copy/hardlink 轮换；不修改真实语料或金标。
- 首轮完整 organize **274 passed、2 failed、7 warnings，82.11 秒**，`/tmp/rssripple-v2-organize-full.xml`，不可计为通过。一个失败是 monkeypatch `_rebuild_plan` 未接收新增配置版本参数，已修正并单项通过；另一个是通知 tick 与自动执行并发时，最终所有权 CAS 会话读取旧 pending/revision=0，而独立会话读取已提交 running/revision=1。真实文件已执行，因此不能提高轮询时限或绕过 CAS 来通过测试；正追踪 Turso 连接池、SAVEPOINT 与写锁失败后的事务状态。

上述实现仍在 `/tmp/rssripple-v2-ownership-work`，待该失败解决、权威设计同步及完整门禁后才能关闭 O1/O2/O5。补丁与哈希以本轮最终导出为准，不把旧原型快照误当成最新实现。


### 自动执行修复与主工作树同步

- 最小真实 Turso 回归确认：SAVEPOINT 建立后另一连接提交，INSERT RETURNING 报 database is locked；失败连接 rollback、只读、再次 rollback 后，仍读不到后续提交。红测 `test_savepoint_lock_failure_does_not_reuse_stale_snapshot` 稳定得到 1 而不是 2（`/tmp/rssripple-v2-snapshot-red.log`）。显式 SQL ROLLBACK 也不能清理该残留读取快照。
- 现有 Turso 兼容层对写锁/写写冲突标记单个物理连接失效，`invalidate_pool_on_disconnect=False` 保留其他连接；保留原异常，不重放单条 SQL。通知 tick 捕获异常时显式 rollback，避免退出 committed_session 时提交失败事务。确定性回归和真实自动整理均通过：**2 passed，0.79 秒**，`/tmp/rssripple-v2-snapshot-green.xml`。
- 扩大整理/通知/调度/迁移/服务/API 回归：**533 passed、2 failed、3 skipped、9 warnings，238.56 秒**，`/tmp/rssripple-v2-organize-recovery-full.xml`。两失败为遗留配置字符串导致迁移查错后端目录，以及模拟 PG 连接缺 run_sync。迁移改查实际连接 dialect，模拟连接补同步执行并断言单例注册；受影响回归 **52 passed、3 skipped，15.84 秒**，`/tmp/rssripple-v2-migration-recovery-final.xml`。不把这一专项重跑说成原整轮通过。
- 最终 PG 并发断言再次通过，最新 [结果](probes/ownership-state-result.json)；旧库迁移从清空后的专用数据库建立真实旧 schema，新增前置列检查，Turso **1 passed，0.59 秒** 与 PG 两次迁移均通过（`/tmp/rssripple-v2-migration-final.xml`、`/tmp/rssripple-v2-final-migration-pg.log`）。首次复跑误用了前场景已有新 schema，前置不成立，未计通过。o 项目已清理全部容器和网络，退出 0。
- 锁域双进程 [驱动](probes/ownership_domain_probe.py) 与 [结果](probes/ownership-domain-result.json) 已保存。沿用本文件专用 PG/临时副本规则；禁止连接业务数据库。
- 核对主工作树 V0/V1 的全部源哈希无变化后，同步 34 个源码、测试、环境样例与权威文档文件。测试 autouse fixture 将锁目录置于每测试 tmp_path，不使用业务 data/。前端 tsc/Vite、相关 ESLint、全仓 Ruff、diff whitespace 通过；可评审补丁及 `/tmp/rssripple-v2-prototype-state.json` 已更新（main_worktree_applied=true，不代表部署）。

**V2 已在本地主工作树实施，完整门禁仍在运行**：702 个源码/依赖/构建文件哈希 `/tmp/rssripple-v2-source-p.json`；完整单元/API 日志 `/tmp/rssripple-v2-unit.log`，JUnit `/tmp/rssripple-v2-unit.xml`，coverage `/tmp/rssripple-v2-unit-coverage.xml`，强制 ≥95%；完整隔离集成项目 `rssripple-v2-complete-20260913-p`，日志 `/tmp/rssripple-v2-integration-p.log`。待最终测试结果、应用正常退出、四份 coverage 合并 ≥85%、报告导出及项目清理后才关闭 O1/O2/O5。


### p 轮结论与后续修复

p 轮完整门禁未通过：单元/API 3492 passed＋2 teardown errors；集成 3090 passed＋2 failed＋1 teardown error。98.01%/89.64% 覆盖率达标仅代表数值，不代表验证完成。项目已清理。失败涉及重规划污染响应会话、FTS drain 失效调用方事务及夹具清理顺序；续轮同时修正 Turso 连接关闭兼容性。必要性来自完整运行的实际失败，方案将两项附带操作隔离到独立事务，并保留原业务提交与异常传播语义。红/绿证据和扩大验证状态见 [VALIDATION.md](VALIDATION.md) 最新节。O1/O2/O5 继续保留，待修复后完整门禁通过。


## V2/V3/V4 组合版本最终验收（2026-09-13，w 轮）

本地实现已通过完整门禁：单元/API **3500 passed、14 skipped**，覆盖 **21016/21461（97.93%）**，超过 95%；集成 **3096 passed、17 skipped**，合并覆盖 **19282/21461（89.85%）**，超过 85%。运行前后 712 个被测文件哈希一致。V2 v 轮配置遗漏导致的覆盖率失败仍保留为历史失败，此次使用正确 `.coveragerc` 的完整组合版本完成验收。

已验证并从 pending-only TODO 删除：Agent 候选事务失败恢复、P1-B3 通知毒任务隔离，以及 P1-O1/O2/O5 整理规划版本、操作语义与跨进程执行所有权。权威设计文档及回归测试已同步。B9、B4、B7 和其余待办继续保留；这些结果不表示所有问题已解决。

原始单元日志/JUnit/覆盖率：`/tmp/rssripple-v34-unit-w.log`、`/tmp/rssripple-v34-unit-w.xml`、`/tmp/rssripple-v34-unit-coverage-w.xml`；集成日志/JUnit：`/tmp/rssripple-v34-integration-w.log`、`/tmp/rssripple-v34-integration-w.xml`；四路原始覆盖率、合并数据与 XML 已导出到 `/tmp/rssripple-v34-artifacts-w`。两个应用正常退出后完成覆盖率合并；唯一项目 `rssripple-v34-complete-20260913-w` 的容器、网络及临时卷均已清理。机器可读摘要见 [验收结果](probes/v234-complete-w-result.json)。未部署生产环境。
