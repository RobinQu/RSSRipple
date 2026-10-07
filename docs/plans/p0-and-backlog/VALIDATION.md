# 分批修复与集成验收标准

目标：逐项处理 TODO.md，不以数量、覆盖率或旧绿色结果替代问题级验收。2026-09-12 起采用以下门禁；历史修复保留，但缺失的集成证据必须补齐。

当前状态（2026-09-13）：V0 完整集成发现的 17 项失败已分组修复，ZFS 整理及三项 HTTP 复验通过；V1 O3/O4 已同步主工作树并通过专项回归。合并后的完整单元/API 和隔离集成门禁正在运行，不标记 V0/V1 完成，不提前删除 TODO。下方按时间保留历史证据。

## 每批开始前的必要性与方案论证

1. 对照当前源码和权威设计验证条目是否仍成立，记录具体入口、触发条件、影响与可复现证据。区分缺陷、产品增强、设计使然、尚未证实的假设；不得为了清空清单而直接关闭假设。
2. 重新判级：默认部署可达性、损失是否不可逆、频率、已有保护、依赖关系。保留原 ID，解释调整原因。
3. 比较最小正确修复与替代方案，列出数据迁移、事务/并发语义、兼容性、失败恢复、回滚约束。若涉及既有人工编辑、合集归属或消费进度，不得牺牲这些不变量换取测试通过。
4. 在实施前固定验收输入、独立期望值与失败断言；实施后重新审查必要性及方案是否仍成立。每批记录实际命令、环境、通过/失败/跳过数和未覆盖范围。

## 严格集成门禁

| 维度 | 必须满足的标准 |
|---|---|
| 数据来源 | 优先复用版本化真实脱敏快照/torrent/HTTP cassette；记录 case ID、源证据哈希、采集时间和审核状态。没有原始 RSS/媒体字节时明说，不能以重建输入声称历史抓取/真实媒体通过。 |
| 独立答案 | 依据权威规则、已审核源证据或直接读取文件字节确定期望；禁止把当前 DB/runner actual 复制为 expected。真实数据变异故障须注明变异字段。 |
| 被测边界 | 至少穿过两个真实生产模块；持久化问题必须经过真实事务和数据库；文件问题必须操作临时文件树并核对字节及残留；只调用 helper 或 mock 被修复模块不能充当集成凭证。 |
| 外部边界 | 仅替换真实系统边缘（下载器 RPC、媒体服务器、录制 HTTP）；注明替代。离线语义回放拒绝未录制请求及旁路联网，禁止异常被业务吞掉后假通过。 |
| 正反例 | 每项包含触发原故障的负向例、合法成功例、幂等重试；涉及竞争则用确定性屏障/故障注入，验证全部副作用与失败后的 DB 状态，不能仅断言 HTTP 200。 |
| 数据库 | 迁移/FK/唯一性/事务竞争须同时验证 Turso 与隔离 PostgreSQL，包含新装及升级路径；单后端通过不得宣称双后端完成。 |
| 分布式 | worker 调度、队列/所有权问题须 PostgreSQL＋Redis＋至少三个独立 worker；API 与 worker 分进程，按执行证据证明持续运行/恢复，而非仅检查 enqueue mock。 |
| 安全 | 认证默认开启的正反例、越界目标哨兵保持不变；临时目录/独立服务，不对生产数据构造破坏性故障。 |
| 隔离 | 所有测试 Compose 使用唯一 -p，数据库/卷/网络独立，结束清理本项目；不允许用生产库跑迁移或修复测试。 |
| 退出 | 本批必要回归和关联集成全部通过；不允许新增无理由 skip/xfail。覆盖率门禁保持单元/API ≥95%、完整集成 ≥85%；单个专项不宣称达到完整套件门禁。 |

现有全量门禁耗时较高：每个小修改运行相关回归；每个完整实现批次结束运行对应 CI 门禁。测试失败必须定位并修复，不为适配当前实现弱化断言。JUnit、语义差异报告及覆盖率写入独立可写报告目录，固定语料只读。警告必须披露；与本批有关的未释放连接、后台任务或线程警告视为未通过，需要修复。

## 批次顺序与证据台账

| 批次 | 范围 | 必要性 / 方案 | 状态 |
|---|---|---|---|
| V0 | P0-1/2/3/7/8/9 集成补验 | 原修复单元/API 已通过，但真实语料与破坏性路径、认证边界、实际队列消费的联合证据不足。先补证据，发现缺陷则重开修复。 | 进行中 |
| V1 | P1-O3/O4 路径越界 | 输入清单及媒体服务扫描可能越出卷；先复现，再统一路径校验与失败原子性。 | 已复现，待实现前确认异常语义 |
| V2 | P1-O1/O2/O5、B4 | 执行所有权、重规划与文件语义一致性；依赖 PostgreSQL 竞争验证，不能只加进程锁。 | 待重新论证 |
| V3 | P1-B9/B7、D4、M4/M5 | 定向请求/消费进度/覆盖决策的持久化幂等，需交错事务与实际 worker 验证。 | 待重新论证 |
| V4 | 其余 P1 数据、metadata、通知、安全 | 逐项按可达性重排，身份接地、人工保护、合集归属及登录防护分别定验收。 | 待重新论证 |
| V5 | P2 | 按安全/一致性→边界功能→规模性能→产品增强排序；性能条目先测量。 | 待重新论证 |
| V6 | P3 | 契约/可维护性逐项核实，确认设计使然的条目附证据关闭。 | 待重新论证 |

### V0 本轮论证

- 真实语料沿用 `metadata_corpus_v2`（采集 2026-09-09），不重新导出生产数据。只有 confirmed review 可作为作品语义期望；pending 只用于输入格式/安全不变量测试。
- 整理操作验证沿用真实文件名、源 torrent 哈希及独立审核的季集，媒体内容使用显式声明的确定性测试字节，避免复制真实大体积媒体；原始清单字节数与测试文件大小分别记录。
- 文件安全集成必须走 completed Task→冻结通知→规划落库→执行→任务清理判据；不能把单元 helper 重命名后当集成。
- P0-3 同时复跑既有两组完整源回放；正常场景通过只证明兼容性，越界季号仍须独立负向集成覆盖。
- 本轮真实语料基线：`CORPUS_REPORT_DIR=/tmp/rssripple-p0-corpus pytest tests/integration/metadata_corpus -q --no-cov --junitxml=/tmp/rssripple-p0-corpus/junit.xml`：1584 passed，6.47 秒。包含两个完整审核场景，不代表其他 pending 资源已审核。
- 待补：认证开启的 P0-1 HTTP 边界、P0-2 真实队列消费、P0-3 负向 DB 关联门禁、P0-7/8 真实文件清单整理集成、P0-9 自动刷新及 worker 故障恢复。前轮五项调度通过不替代这些缺口。


V0 已补证据：

- `tests/integration/security/test_p0_static.py`：真实 AuthMiddleware＋生产静态路由/挂载＋临时哨兵文件，5 项通过；ASGI 无 lifespan，仅替换目录配置。合法 asset/SPA 必须成功，防止未注册路由导致假阴性。
- `tests/integration/organize/test_p0_real_manifest.py`：12 项通过，真实案例 ID 为 `f79ef2eb-02d5-42d3-80dc-70dd3c1d733b`；源 torrent SHA-256 校验与独立审核期望入测试，真实文件名/清单大小与合成媒体大小分别记录。
- 上述合并运行：17 passed，7.94 秒，无警告；JUnit `/tmp/rssripple-p0-integration.xml`。首轮 JUnit record_property 警告已通过兼容的 suite properties 修正。
- 真实 MemoryQueue→生产 handler→AgentRun 的 P0-2 补验首轮 1 passed，1.50 秒；测试 seed 的单季合集不变量已完善，并纳入本轮最终合并回归。
- V0 尚未完成：P0-3 负向持久化证据、P0-2 Redis 分进程消费、P0-9 刷新/worker 故障恢复，以及完整集成门禁仍待补；现有专项结果不得用于关闭整轮补验。


V0 本轮合并结果：`CORPUS_REPORT_DIR=/tmp/rssripple-v0-final .venv/bin/python -m pytest tests/integration/metadata_corpus tests/integration/organize tests/integration/security -q --no-cov --junitxml=/tmp/rssripple-v0-final/junit.xml`：**1826 passed，0 skipped，1 warning，57.23 秒**。警告来自既有 `test_worker_entrypoint.py::test_main_invokes_asyncio_run` 未 await `_run`，新增 18 项单独运行无警告。Ruff 和 diff whitespace 检查通过。本轮未修改生产实现，未重跑前轮 25 分钟全量单元/API；该历史 98.27% 不作为本轮完整集成覆盖率结果。V0 的剩余缺口与 V1–V6 待办继续有效。


### V0 续轮：负向持久化暴露 P0-3 残留

重新论证：只检查 ResourceMetadata/cache 不足以证明“季号绝不猜测”。新增真实 ReAct 图→工具 finalize→validator→repository→DB 测试（模型响应故障注入、原始标题沿用已捕获案例）确认：inferred_season=99 且缺季数证据时，validator 正确清空，但新实体 upsert 的历史默认 S1 仍创建并链接作品。因此 P0-3 的持久化修复重新打开，须补齐后再验收。

方案比较：直接修改所有历史 upsert 默认会混入广泛兼容迁移；仅检查缓存不能保护 DB。选择显式传递 season_ambiguous 到 upsert，在证据仍无法定季时先复用/创建合集并返回 None，绕过历史默认与单成员猜测；已有可靠解析季号/单季证据仍合法，其他历史迁移路径维持自身契约，另列 P1-M2 跟进。无 schema 改动。测试同时核对资源 FK、季作品集合和缓存，重跑应幂等；联网守卫曾捕获被吞掉的 Bangumi 动画验证外连，现以已审核的动画频道属性配置隔离该无关分支，守卫保留。


续轮实现与证据：

- repository 将 season_ambiguous 显式传给季作品 upsert；无可靠季号则复用/创建合集并返回 None，阻止默认 S1 复活。涉及 business-logic/per-season-works 权威文档已同步。
- 新增 `test_p0_season_persistence.py` 四个场景：合法 S1、越界 99＋多季证据、越界 99＋无季数证据、越界 99＋已验证单季回退；每场景检查实际 FK/作品行/cache，再 force_refresh 重试及缓存重放。仅模型响应为故障注入，真实 LangGraph/ReAct 与 repository 不替换，禁止未计划联网。
- 新增集成＋metadata_service/per_season_upsert：198 passed，5 warnings，63.07 秒。repository＋完整真实语料＋新集成：1626 passed，5 warnings，36.26 秒，JUnit `/tmp/rssripple-p0-season.xml`。补完缓存重放后新四项复跑：4 passed，3.05 秒。警告为现有 create_react_agent 弃用和 pytest fixture 弃用，无外连守卫/连接清理错误。
- 完整单元/API 95% 门禁已启动，日志 `/tmp/rssripple-v0-followup-unit.log`，JUnit `/tmp/rssripple-v0-followup-unit.xml`，覆盖率 `/tmp/rssripple-v0-followup-coverage.xml`；结果尚未完成，不引用旧门禁代替。
- V0 仍需 Redis 分进程修订消费、自动刷新/worker 故障恢复及完整集成门禁。P1-M2 的一般单成员兼容路径仍独立待修，不将本次显式模糊标记修复扩大成全路径均已消除猜季的结论。

### V0 2026-09-13：分布式修订消费与故障恢复

必要性：内存队列通过不能证明默认 Redis 部署；单个调度器恢复不能证明三个独立进程重启后的对账。采用独立 PostgreSQL/Redis/Web/三 worker 测试栈，生产 handler 不替换，唯一项目名 `rssripple-v0-redis-20260913-b`；固定语料只读挂载，原始标题来源与完整 SHA-256 检查写入驱动。

- `scheduler_revision_e2e` 已通过三次 Web 修订→Redis→worker→AgentRun 落库，旧资源消费、水位线不变、无下载副作用均断言。资源故意未挂作品，测试不宣称完成语义匹配。
- `scheduler_recovery_e2e seed/mutate` 已通过故障前抓取与自动刷新、三个 worker SIGKILL 后退出码均为 137、停机期间 Web 配置提交。恢复阶段已通过：新间隔下连续抓取、重新启用刷新产生新的 done job（空作品集 processed=0）；恢复后实测三个 worker 均为 running。
- 前轮完整单元/API 原会话仍在运行，日志持续推进；未重启或用旧结果替代。

### V1 前置必要性复核（尚未实施）

调用链、方案取舍、异常边界与集成矩阵已细化至 [V1-PATH-SAFETY.md](V1-PATH-SAFETY.md)。实现须等待正在运行的 V0 全量门禁结束，确保报告与被测源码版本一致。

2026-09-13 用独立临时目录再次复现 O3/O4：`payload.files=[{"name":"../outside.mkv"}]` 被 `_collect_files` 选为下载内容；服务器路径 `/media/../outside` 经绑定解析保留 `root_subpath=../outside`，最终库根越卷。哨兵内容未修改，未执行移动。证据与被测源码 SHA-256：`/tmp/rssripple-v1-repro.json`。两项保持 P1，不因当前 P0 测试通过降级。

方案论证方向：复用统一相对路径语法校验，加解析后的卷/下载根包含检查，覆盖符号链接；只做字符串 `..` 检查不足以覆盖链接逃逸。拒绝不安全清单时应让整个计划在选文件/执行前失败；扫描写库须先校验全部 Location，防止半批成功。补合法嵌套路径、非法清单混合、种子目录名、符号链接、现有绑定路径，以及失败后文件哨兵/数据库状态断言；最终范围待实现前检查调用方异常语义。


本轮分布式最终结果：revision 三次修订全部 PASS；recovery 的 seed、mutate、check 全部 PASS（共四条阶段断言）。测试项目 `rssripple-v0-redis-20260913-b` 已清理容器、网络及临时数据。运行说明与边界见 `docs/testing/scheduler-integration.md`。本轮只新增驱动与只读语料挂载，未改生产实现；Ruff/diff 检查通过。V0 的 Redis 消费、自动刷新调度和三 worker 重启证据已补，完整单元/API 门禁继续使用原进程；完整集成覆盖率尚未运行，不标记 V0 完成。

### V0 完整隔离集成门禁（2026-09-13，运行中）

原 `docker-compose.test.yml` 会读取宿主 `.env` 并挂载 `./data`；独立项目名不能隔离该宿主目录。因此新增 `docker-compose.integration-isolated.yml`，固定测试凭证、内部网络和项目独立命名卷。构建依赖使用当前 `uv.lock`，不改锁文件；应用、测试和脚本只读挂载。复跑说明见 [isolated-integration.md](../../testing/isolated-integration.md)，不使用原配置的清理宿主数据命令。

项目 `rssripple-v0-full-20260913-c` 已启动完整离线集成套件，日志 `/tmp/rssripple-v0-full-integration.log`；运行时没有外网、生产密钥或生产卷。源码及依赖哈希记录 `/tmp/rssripple-v0-full-source.json`。测试结束后需正常停止应用，合并四份 coverage 严格执行 85% 门禁，导出报告并清理项目；当前尚无最终结果。完整单元/API 继续原会话，未重启。

首轮环境错误已定位：新配置误设置 `MAGNET_RESOLVE_ENABLED=false`，`test_conflict_while_in_progress_or_done` 实际返回 422 “Magnet metadata resolution is disabled”。确认失败后主动停止：1 failed、61 passed、KeyboardInterrupt，284.07 秒；不计为门禁结果。已恢复套件所需的 magnet 功能，不修改断言；首轮项目清理后使用新项目 `rssripple-v0-full-20260913-d` 空库重跑，详细日志 `/tmp/rssripple-v0-full-integration-d.log`。

完整单元/API 原会话现已结束（退出码 0）：**3471 passed、14 skipped、16 warnings，1453.66 秒，98.27%（20554/20915 行）**，95% 门禁通过。JUnit 与 coverage XML 为上文 followup 路径。补修后的生产源码未再修改；尚待完整集成 85% 门禁及其失败分析完成。

### V1 O3 临时副本修复（2026-09-13）

V0 完整集成原会话持续运行，因此 O3 实现在 `/tmp/rssripple-v1-path-work` 进行，未修改主工作树被测 app/tests；哈希复核无变化。新负向回归先证明父路径及绝对路径会选中卷外哨兵并产生 pending 计划，再实施完整输入校验和执行前源边界复查。第一版整理服务＋完整整理集成 333 passed；最新线程边界调整后的 API＋服务＋新集成 141 passed。方案、来源、各报告与限制见 [V1-PATH-SAFETY.md](V1-PATH-SAFETY.md) 末节；O3/O4 均未从 TODO 关闭，待同步和完整门禁。

O3 待同步变更清单及双方哈希：`/tmp/rssripple-v1-source-state.json`；可评审补丁：`/tmp/rssripple-v1-source-patch.diff`（6 个文件）。同步前须检查主工作树基线哈希，避免覆盖后续修改。最新 Ruff 检查通过。

V0 完整集成仍在原会话运行，已出现 `TestMetadataAgentMock::test_agent_links_canned_work` 失败。初步核查发现 `mock_llm._finalize_result` 的 TV 成功响应既无 season，也无可验证季数；HTTP 用例却要求自动创建并关联季作品，与补修后的不猜季契约冲突。须待最终失败堆栈确认，再为成功夹具提供明确季号证据并保留缺证据的负向断言；不能恢复默认 S1 或把失败改成 skip。当前未修改运行中的夹具，不计为集成门禁通过。

### V0 完整集成结果与修复续轮

- 项目 `rssripple-v0-full-20260913-d` 最终 **3039 passed、17 failed、17 skipped、6 warnings，1570.93 秒**。应用均 SIGINT 正常退出 0，四份 coverage 合并 **18755/20915 行＝89.67%**、85% 数值门禁退出 0；因测试失败，整轮失败。报告和原始数据 `/tmp/rssripple-v0-full-artifacts-d/`，日志 `/tmp/rssripple-v0-full-integration-d.log`、`/tmp/rssripple-v0-full-coverage-d.log`。
- 14 个整理失败共同报 EINVAL，独立干净容器直接调用确认 native renameat2 不可用；镜像与 daemon 都是 x86_64，Docker driver 为 ZFS，不是测试污染。仅提高测试等待时间或调整断言不能解决。
- 普通文件增加 no-replace link 发布回退，校验 identity/size/mtime 与双方 inode 后删源名称；发布后未删源可由已有幂等恢复。原生其他错误照常传播，安全链接也不可用及目录不允许降级。源与目标仍受原完整内容比较保护；未宣称解决并发目录替换或断电持久化。
- 同一 ZFS 环境完整 organize：**241 passed、5 warnings，28.49 秒**，`zfs-organize.xml` 已导出；最新单元文件安全/执行器：**85 passed、1 warning，0.31 秒**，`/tmp/rssripple-p0-zfs-unit-final.log`。权威 file-organization.md 已同步。该项目已清理全部容器、网络及临时卷。
- 两项 mock 失败确认在“无证据时期待创建季作品”；成功响应现明确提供合成单季证据，同时新增 `(inferred=None,count=None)` 不关联及 `(None,1)` 单季正例，保留全部越界季号回归。真实语料金标未改。
- `test_runs_history_annotations` 用 rows[0] 错当成指定运行；started_at 的 DB 默认值只有秒级，可能同秒并列。测试改为同时核对入队 job_id、返回 run_id 对应历史行，保留 matched/dispatched 正断言，不改生产历史排序。
- 新独立项目 `rssripple-v0-recheck-20260913-e` 正在跑上述三项 HTTP＋六项季号回归，日志 `/tmp/rssripple-p0-recheck-e.log`。仍待全量门禁重跑，旧 98.27% 不能作为新发布兼容实现的完整单元门禁结果。

续轮最终专项结果：e 项目 **9 passed / 6 warnings，112.28 秒**，三项 HTTP 失败及六项季号场景均通过；报告 `/tmp/rssripple-p0-recheck-e.xml` 已导出，项目容器/网络/卷已全部清理。

### V0/V1 合并完整门禁（运行中）

O3/O4 合并专项 416 passed、metadata misc 59 passed，路径修复已在核对基线后同步主工作树。前端 tsc/Vite 构建与相关 ESLint 通过，权威 file-organization/api-endpoints/frontend 文档同步。源与依赖共 418 个文件哈希固定在 `/tmp/rssripple-v01-source.json`。

- 完整单元/API：日志 `/tmp/rssripple-v01-unit.log`，JUnit `/tmp/rssripple-v01-unit.xml`，coverage `/tmp/rssripple-v01-coverage.xml`，命令内强制 `--cov-fail-under=95`。
- 完整隔离集成：新项目 `rssripple-v01-final-20260913-f`，日志 `/tmp/rssripple-v01-integration-f.log`；完成后仍需正常停应用、汇总四份 coverage、85% 门禁、导出报告和清理该项目。
- 两者均尚未完成，不引用前轮专项或覆盖率替代。待最终结果再关闭 V0 补验及 TODO O3/O4。

续报：完整单元/API 原会话现已退出 0，**3481 passed、14 skipped、16 warnings，1481.01 秒，20680/21076 行＝98.12%**，95% 门禁通过。JUnit、coverage 仍为上述 v01 路径。完整隔离集成仍在原会话，尚未产生最终测试与合并覆盖率结果；不能关闭 V0/V1。

### V2 必要性复核：PostgreSQL 并发复现

V0/V1 完整门禁保持原会话运行，418 个被测文件哈希复核无变化。在独立临时副本与项目 `rssripple-v2-probe-20260913-g` 验证 O1/O2/O5：两个真实进程重复完成同一计划；规则提交窗口将原 hardlink 计划以 move 执行并删源；过期重规划使 done 计划包含 pending op。最终脚本退出 0，使用合成媒体，不冒充真实语料验收；修复尚未实施。具体方案、可复跑脚本和持久证据见 [V2-PLAN-OWNERSHIP.md](V2-PLAN-OWNERSHIP.md)。该项目的 PostgreSQL 容器、临时数据库及网络已全部清理，退出码 0。

### V2 所有权原型验证

新的独立项目 `rssripple-v2-lock-20260913-h` 确认 DB 断连释放 advisory lock 时文件线程仍可存活，故否定仅靠该锁恢复 running 的方案。共享文件锁原型通过实际后端终止、SIGKILL 后真实 hardlink 恢复及重复取消保护。接入临时副本 execute_plan 后，PostgreSQL 双进程只执行一次；10 项现有服务回归与新增取消/独立 DB 会话/真实文件断言均通过。结果、补丁和部署限制已记录 V2 文档。h 项目已全部清理（退出 0）。

这些结果属于临时副本，未同步主工作树；O1/O2、取消 API、配置版本及迁移还未完成，O5 也不关闭。V0/V1 两道完整门禁仍在同一原会话运行，未重启；当前源码哈希保持不变。新增本地文档与复现工具通过 Ruff，差异无空白错误。

### V0/V1 完整集成最终结果及历史测试修正

- f 项目最终 **3087 passed、1 failed、17 skipped、8 warnings，1656.82 秒**，测试退出 1。两应用 SIGINT 后均退出 0；四份 coverage 合并 **18904/21076 行＝89.69%**，85% 数值门禁退出 0。因测试失败，整轮不通过。报告及测试 DB `/tmp/rssripple-v01-artifacts-f/`，应用日志 `/tmp/rssripple-v01-app-f.log`，coverage 日志 `/tmp/rssripple-v01-coverage-f.log`。f 的全部容器、网络、卷已清理。
- 唯一失败为 `test_runs_history_annotations`。SQL 日志显示对应 run `249c85ff-f0ed-4f5f-b99e-c10a79fd8f2a` 扫描 18 条、duplicates_skipped=18、matched=0；代码明确先去重再记 matched。上一轮按 run_id 选择历史记录修复了定位，但没有修正共享 Agent 已派发全部资源这一前置条件。
- 测试改用独立新 Agent 验证首次完整扫描、matched_resources 与 dispatched 标注，保留 job_id/run_id 对应；新增第二次扫描 matched=0、duplicates_skipped≥1、dispatched=0 的幂等断言。生产计数和派发语义未改。新独立项目 `rssripple-v01-history-20260913-j` 正在重跑整个 HTTP coverage2 模块，日志 `/tmp/rssripple-v01-history-j.log`，完成后再跑完整集成。只修改集成测试，不需要重复已通过且源码相同的单元/API 门禁。

续报：j 项目整个模块 **72 passed、1 skipped，428.93 秒**，退出 0，报告 `/tmp/rssripple-v01-history-j.xml`；容器、网络及卷已全部清理。完整集成在新项目 `rssripple-v01-complete-20260913-l` 运行，日志 `/tmp/rssripple-v01-integration-l.log`。418 个文件的哈希 `/tmp/rssripple-v01-source-l.json`，相比 f 轮仅 HTTP coverage2 测试改变；生产源码及单元/API 未变，保留其 98.12% 通过结果。l 轮仍需完整测试、应用正常退出、四份 coverage 合并及 85% 门禁，当前尚未完成。

V2 临时副本已接入重建/分类/执行 CAS、file_op 快照、取消共享锁与提交前门禁。最新服务/API **130 passed / 1 warning，51.84 秒**；独立 PostgreSQL 验证三项原竞争已满足安全断言，并拒绝活动 worker 期间 Web delete_data 请求。进程关闭时同时取消外层/内部任务的两项测试也通过。详细证据及尚未完成的迁移/真实清单/部署验证见 V2 文档；未同步生产，不替代完整门禁。m 项目已清理，l 轮 418 个源码哈希再次核对无变化。

V2 续轮补齐执行 202 的共享锁忙碌反馈、旧 running 模式缺失拒绝及冻结字段响应，整理 API **51 passed，21.13 秒**。临时前端显示冻结处理方式，tsc/Vite 与 ESLint 通过。完整旧 schema/FK 链两次实际轻迁移在 Turso CONCURRENT/MVCC（1 passed，0.58 秒）与 PostgreSQL 均通过，未知历史模式未被写成 move，配置版本未重置。n 项目已清理；仍待锁目录错误配置、真实清单集成、权威文档及完整门禁。所有改动留在临时副本，l 轮原会话继续运行。


### V0/V1 完整门禁通过（2026-09-13）

- 项目 `rssripple-v01-complete-20260913-l` 完整集成 **3088 passed、17 skipped、8 warnings，1685.41 秒**，测试退出 0；日志 `/tmp/rssripple-v01-integration-l.log`。
- 两个应用 SIGINT 后均正常退出 0，再合并四份 coverage；**18907/21076 行＝89.71%**，85% 门禁退出 0，日志 `/tmp/rssripple-v01-coverage-l.log`。原始 coverage、XML 和隔离测试数据库已导出到 `/tmp/rssripple-v01-artifacts-l/`；项目全部容器、网络及卷已清理，退出 0。
- 生产及单元/API 源码与已通过的 **3481 passed、14 skipped、98.12%** 门禁一致；l 轮仅修正历史 HTTP 测试的独立 Agent 前置与重复扫描断言。对应 418 个文件哈希见 `/tmp/rssripple-v01-source-l.json`。
- 至此关闭 V0 补验、V1 O3/O4，按 pending-only 规则删除 TODO 中两条记录。真实清单、路径负例、文件字节/清理副作用及部署集成测试继续保留。V2 O1/O2/O5 仍未关闭，其临时副本的专项不能代替后续完整门禁。


### V2 主工作树完整门禁（p 轮，未通过）

V2 版本 CAS、冻结 file_op、共享执行/取消锁、锁域注册和两后端迁移已同步本地。真实清单 15 场景、独立 PG 并发/取消/迁移验证通过；扩大专项 533 passed/2 failed 后，两项迁移问题修复并以 52 passed/3 skipped 补验。自动执行暴露的 Turso SAVEPOINT 残留快照已有确定性红/绿回归，修复仅回收冲突连接且保留原异常。细节见 [V2-PLAN-OWNERSHIP.md](V2-PLAN-OWNERSHIP.md) 最新节。

- 固定源码/依赖/前端构建产物共 702 个文件：`/tmp/rssripple-v2-source-p.json`，测试期间不修改。
- 完整单元/API：`COVERAGE_FILE=/tmp/rssripple-v2-unit.coverage .venv/bin/python -m pytest tests/unit tests/api -q --cov=app --cov-fail-under=95 --cov-report=term-missing:skip-covered --cov-report=xml:/tmp/rssripple-v2-unit-coverage.xml --junitxml=/tmp/rssripple-v2-unit.xml`，日志 `/tmp/rssripple-v2-unit.log`。
- 完整隔离集成：项目 `rssripple-v2-complete-20260913-p`，配置 `docker-compose.integration-isolated.yml`，日志 `/tmp/rssripple-v2-integration-p.log`。仍需完整测试、两应用正常退出、四份 coverage 合并及 85% 门禁、报告导出与项目清理。
- 前端构建/相关 ESLint、全仓 Ruff、差异空白检查已通过；此前 PG o 项目已全部清理。当前两道完整门禁尚无最终结果，O1/O2/O5 仍保留 TODO，不能沿用 P0/V1 覆盖率作为 V2 通过证明。


### V3 并行必要性验证（未同步）

V2 完整单元/API 与隔离集成原会话均已实际轮询确认运行中；本轮未修改被测源码。独立临时副本验证 P1-B3 坏任务会阻断已有投递：正确终态 done 的红测失败；逐任务事务＋持久退避原型的新故障场景和已有流水线 **9 passed，5.89 秒**。测试保留已审核清单来源，注入坏 RPC 与 flush 后异常，检查回滚、正常新任务、既有投递和到期恢复。范围、补丁及未完成标准见 [PLAN.md](PLAN.md) 的 V3 续轮；不能把原型通过当作主工作树修复完成。


### V2 p 轮最终结果与事务修复（2026-09-13）

固定 702 文件的完整门禁已结束，不能关闭 O1/O2/O5：

- 单元/API：**3492 passed、14 skipped、2 teardown errors**，1605.86 秒；覆盖率 **20947/21373＝98.01%**。两错误来自 `test_schedule_auto_execute_*` 全局 create_task mock 覆盖数据库夹具清理；改为局部 monkeypatch context，已定向通过 2 项。
- 集成：**3090 passed、2 failed、17 skipped、1 teardown error**，1707.25 秒。两应用正常退出 0，四份 coverage 合并 **19159/21373＝89.64%**；覆盖率数值达标不能抵消测试失败。报告及测试数据库在 `/tmp/rssripple-v2-artifacts-p/`，JUnit `/tmp/rssripple-v2-integration-p.xml`。项目 `rssripple-v2-complete-20260913-p` 容器、网络及卷已清理。
- 媒体库更新失败：附带重规划在响应会话刷新 Library，响应线程可能触发关系懒加载；重规划回滚也会使响应对象失效。改用独立会话处理已提交配置。新增 rollback 故障测试在旧实现下 **1 failed**（`/tmp/rssripple-v2-library-rollback-red.log`）；单独的正常路径测试未能复现原错误，不作为红测证据。
- 合集派发失败：FTS 预搜索 drain 吞掉冲突后，元数据匹配继续使用已失效的调用方事务。drain 改为独立事务，只消费已提交 outbox，不提交调用方修改。故障注入确认旧实现出现 PendingRollbackError；修复后另一个会话仍只能看到原已提交标题。
- Turso 回收同时暴露通用 async adapter 无 terminate 方法。方言显式失效改调用 close；保持 has_stop=False，避免在 GC 无 await 上下文强行关闭。真实 SAVEPOINT 冲突回归增加物理连接已关闭断言，并继续验证其他连接不被回收及后续提交可见。
- 缓存夹具保存原 cached function 引用，清理不再依赖 monkeypatch finalizer 顺序。以上初步专项 **20 passed、1 warning，1.12 秒**，`/tmp/rssripple-v2-boundary-green3.xml`；扩大专项及新完整门禁尚待最终结果。

### V3 追加验证（仍为临时原型）

- 全整理集成＋通知/调度单元原轮 **434 passed、2 teardown errors**，370.94 秒（同 create_task 清理问题），`/tmp/rssripple-v3-organize-notify-full.xml`。强化调度断言及两项清理修复后定向 **6 passed、101 deselected**，2.30 秒；没有把定向通过视作原整轮通过。
- PostgreSQL 独立进程并发退避、实际旧 schema 升级、通知先提交/失败标记后提交的 SQL 屏障竞态已通过；另验证失败记录 INSERT 前任务被删除触发真实 FK 错误，后续正常任务仍创建通知。脚本及结果见 `probes/notification_build_pg_probe.py`、`probes/notification-build-pg-result.json`、`probes/notification_storage_pg_probe.py`、`probes/notification-storage-pg-result.json`。
- q、r 专用项目均已清理。V3 尚未同步主工作树，B3 保留待办。

V2 事务修复扩大回归 **187 passed、1 warning，61.60 秒**（`/tmp/rssripple-v2-boundaries-expanded.xml`），已将 8 个实现/测试文件同步主工作树；权威约定同步独立事务边界。需要在此版本重新跑完整门禁。


### V2 s 轮重新启动（运行中）

- 源码清单 `/tmp/rssripple-v2-source-s.json`；完整单元/API 日志 `/tmp/rssripple-v2-unit-s.log`，JUnit `/tmp/rssripple-v2-unit-s.xml`，覆盖率 `/tmp/rssripple-v2-unit-coverage-s.xml`。
- 隔离项目 `rssripple-v2-complete-20260913-s`，完整集成由既有 test-runner 容器执行。启动时 Compose 使用了自动停止依赖的选项，随后仅终止宿主 Compose 客户端以保留应用 coverage；客户端退出 137 不是测试结果，容器未停止或重启。以 `docker wait rssripple-v2-complete-20260913-s-test-runner-1` 写入 `/tmp/rssripple-v2-integration-s-exit.txt` 为准；完整日志最终从容器导出。
- 测试结束后仍需两个应用 SIGINT 正常退出、四份 coverage 合并与 85% 门禁、报告导出及项目清理。当前不能报告整轮通过。

V3 已在含 V2 事务修复的临时副本重新启动整理集成＋调度/通知/整理单元扩大专项，日志 `/tmp/rssripple-v3-organize-notify-corrected.log`，JUnit `/tmp/rssripple-v3-organize-notify-corrected.xml`，尚待最终结果。V4 Agent 事务项已以真实 NOT NULL 失败复现，仍仅为必要性验证，方案与后续验收见 PLAN.md 最新节。两项均未改变 s 轮被测源码。


### V3 修正基线扩大专项完成

通知隔离原型在 V2 事务修复基线上：完整整理集成＋通知/调度/整理单元 **434 passed、8 warnings，329.32 秒**，`/tmp/rssripple-v3-organize-notify-corrected.xml`，测试退出 0。此前两项 create_task 清理错误已消失；保留现有未 await 测试协程等 warning，不把其说成零警告。结束后补同步真实 Turso 连接关闭断言及不在本次选择内的 metadata 缓存夹具；这两项是已通过 V2 专项的测试修正。B3 仍待同步主实现、权威文档及完整门禁，不能仅据扩大专项删除。

### V4 Agent 候选事务原型

已在独立临时副本实现后台每候选组独立事务、API 回填每组 SAVEPOINT；后台先完成选择事务，计数在候选事务成功后增加。不可恢复的 API 外层连接故障上抛。真实 NOT NULL 红测修复后通过，并扩展为后台及未提交新 Agent 两场景，确认回填不隐式提交 Agent。完整 Agent 服务测试 **108 passed、1 warning，92.25 秒**，`/tmp/rssripple-v4-agent-service.xml`。此为原型，尚需真正 COMMIT 失败、真实清单/RPC 与调用方端到端验证。


V4 PostgreSQL 延迟外键真实 COMMIT 故障：两组 flush 均成功，第一组 commit 报 ForeignKeyViolation，第二组持久化；最终任务数和 dispatched 都为 1、errors 为 1，父会话 Agent 仍可读。旧实现同脚本退出 1，末尾为 prepared state InvalidRequestError；原型退出 0。日志 `/tmp/rssripple-v4-agent-commit-pg{,-red}.log`，可复跑脚本 [agent_commit_pg_probe.py](probes/agent_commit_pg_probe.py) 与 [结果](probes/agent-commit-pg-result.json)。最初两类种子前置失败（field_mapping 缺失、单元 helper 的 aware parsed_at）已修正为合法 PG 夹具，不计入事务回归证据。专用 `rssripple-v4-agent-20260913-t` 已清理容器及网络。

原型补丁 [agent-transaction-prototype.patch](probes/agent-transaction-prototype.patch) 仍未同步；剩余关键门禁是下载器外部副作用恢复、后台及 API 回填水位线策略、真实清单与实际调用方集成。


### V4 水位线、回填原子性与真实下载器续验

- 实际 `_handle_run_agent` 复现：候选组故障后状态 failed，但旧逻辑仍推进水位线。原型仅在无 RunResult.errors 时推进，下一增量运行补派失败组、跳过已提交组。相关 3 场景 **3 passed、1 warning，1.57 秒**，`/tmp/rssripple-v4-watermark-green.xml`。首次前置元数据不完整时没有进入 dispatch，不计为红测；补齐系列年份/is_anime 后才确认水位线失败。
- 实际 API 新建/编辑回填红测 **2 failed，2.81 秒**：保存仍返回 201/200。原型在候选处理出现内部持久化错误时抛标准 INTERNAL_SERVER_ERROR 500，使外层保存事务回滚；不推进水位线，不提交部分配置/任务。正常保存、完整 Agent API 与恢复专项合计 **49 passed、1 warning，66.35 秒**，`/tmp/rssripple-v4-agent-api.xml`。这不改变已持久化 error 下载任务的既有重试路径；RPC 错误被该路径接管时不是内部事务失败。
- 已审核真实清单 `f79ef2eb-02d5-42d3-80dc-70dd3c1d733b` 的原始种子 SHA256 校验后提交到独立 Transmission。首次真实 RPC 接受后注入实际 NOT NULL 更新错误，DB 无任务且水位线保留；第二次真实增量 job 的 RPC 复用同一 torrent ID，最终 daemon 1 torrent、DB 1 task，连接 peer 与收到媒体字节均为 0。脚本 [agent_rpc_retry_probe.py](probes/agent_rpc_retry_probe.py)、[Compose](probes/agent-rpc-compose.yml)、[结果](probes/agent-rpc-retry-result.json)，日志 `/tmp/rssripple-v4-agent-rpc-retry.log`。网络 internal，种子元数据真实、系列身份为受控种子，不代表再次验证自动识别。
- 复跑真实 RPC 脚本前，在含 V4 补丁的临时副本启动 `docker compose -p rssripple-v4-agent-20260913-u -f <agent-rpc-compose.yml> up -d --wait transmission`；脚本严格检查该项目及空 daemon，通过 Docker inspect 获取内部地址。不要连接业务下载器。测试库由脚本生成独立 `/tmp/rssripple-agent-rpc-*/probe.db`。
- 当前 V4 仍未同步主工作树；API 回填外部 RPC 已接受后的端到端重试、扩大后台/队列回归、权威契约和完整门禁尚待完成。定向/指定时间运行的持久补偿仍需与 B9 的待处理请求设计衔接，不能用增量水位线回归宣称四种模式全部恢复。

真实 RPC 专用 u 项目已确认清理完成：容器、网络、三个卷全部删除，退出 0；s 轮完整门禁原会话继续运行。


### V4 API 回填的真实 RPC 回滚后重试

[agent_api_rpc_retry_probe.py](probes/agent_api_rpc_retry_probe.py) 在新建/编辑两个实际 HTTP 路径分别验证：真实 Transmission 接受后注入数据库 NOT NULL 失败，首次保存 500 且配置、任务、水位线回滚；随后同一提交成功（201/200），两个 RPC 返回同一 torrent ID、每 Agent 仅一条任务。脚本同时复验增量恢复，使用同一已审核原始种子及 SHA256；daemon 总计仍只有一个 torrent，媒体接收为 0。[结果](probes/agent-api-rpc-retry-result.json)，日志 `/tmp/rssripple-v4-agent-api-rpc-retry.log`，退出 0；复用上节专用 Compose，u 项目第二次运行后已全部清理，退出 0。

V4 已开始扩大 Agent 服务/后台 handler/队列/全部 Agent API/原 P0 资源修订实际队列消费回归，日志 `/tmp/rssripple-v4-agent-expanded.log`，JUnit `/tmp/rssripple-v4-agent-expanded.xml`，尚待结果。V3 五份权威文档更新已在临时副本准备，并纳入可评审原型补丁，尚未同步主工作树。


### V2 s 轮结果与 v 轮单元/API 补验

s 轮完整集成 **3092 passed、17 skipped、8 warnings，1706.67 秒**，test-runner 退出 0。两个应用 SIGINT 后均退出 0；日志已完整导出 `/tmp/rssripple-v2-integration-s.log`，正在合并覆盖率，尚需报告导出与项目清理。

s 轮完整单元/API **3493 passed、2 failed、14 skipped、6 warnings，1697.90 秒**；**20950/21381＝97.98%**，测试退出 1。两项 manual_search_metadata local 测试只 flush 未提交的新作品，却要求独立 FTS 边车立即可见；实际手工搜索针对已保存作品且标注 No persistence，不能恢复搜索隐式提交调用方状态。仅将两项种子数据准备改为 commit，保留原搜索结果断言；本地搜索定向 **7 passed、199 deselected、1 warning，3.11 秒**（命令的 -k 同时排除了 FTS 测试，不称为完整 FTS 回归）。

完整单元/API v 轮在 `/tmp/rssripple-v2-ownership-work` 重启，确认其 app/unit/API 与主工作树只有上述一个测试文件不同，固定 293 个源文件到 `/tmp/rssripple-v2-source-v.json`。日志 `/tmp/rssripple-v2-unit-v.log`、JUnit `/tmp/rssripple-v2-unit-v.xml`、覆盖率 `/tmp/rssripple-v2-unit-coverage-v.xml`，要求 ≥95%；不修改仍在验证的源文件。待通过后同步这两项测试前置；此时仍不关闭 O1/O2/O5。

V4 扩大 Agent/后台/队列/API/P0 修订消费回归 **237 passed、1 warning，148.25 秒**，`/tmp/rssripple-v4-agent-expanded.xml`，退出 0。尚未同步主工作树。


V2 s 轮集成收尾完成：四份 coverage 合并 **19172/21381＝89.67%**，85% 门禁退出 0；原始 coverage、XML、隔离数据库导出 `/tmp/rssripple-v2-artifacts-s/`，JUnit 独立保存在 `/tmp/rssripple-v2-integration-s.xml`。全部 s 项目容器、网络及卷已清理，退出 0。v 轮完整单元/API 仍在冻结副本运行。

V3/V4 已核对原型基线哈希并同步主工作树（18 个实现/测试/文档文件），同时同步两项本地搜索测试种子的 commit；补充回填失败 API、后台事务与水位线权威契约。Ruff 与差异空白检查通过，开始主工作树组合专项 `/tmp/rssripple-v34-combined.xml`。尚未完成组合版本完整门禁，不关闭 B3、Agent 事务或 V2 整理 TODO；v 轮的冻结副本不会被主工作树修改影响。


### V3/V4 组合主工作树门禁 w（运行中）

组合专项 **13 passed、1 warning，20.10 秒**（`/tmp/rssripple-v34-combined.xml`），覆盖通知坏任务、持久退避迁移、原 P0 资源修订队列消费、Agent 回滚/增量恢复、API 回填原子性及 FTS 调用方事务保护。全仓 Ruff 和差异空白检查通过。

- 冻结 712 个源码/依赖/前端文件：`/tmp/rssripple-v34-source-w.json`。
- 完整单元/API：`/tmp/rssripple-v34-unit-w.log`、JUnit `/tmp/rssripple-v34-unit-w.xml`、coverage `/tmp/rssripple-v34-unit-coverage-w.xml`，强制 ≥95%；独立海报缓存 `/tmp/rssripple-v34-posters-w`。
- 完整集成：项目 `rssripple-v34-complete-20260913-w`，依赖 `up -d --wait` 已健康；用 `run -T --no-deps --name rssripple-v34-complete-20260913-w-test-runner-1 test-runner` 执行，日志 `/tmp/rssripple-v34-integration-w.log`。本轮不自动停止应用；测试结束后需 SIGINT、四份覆盖率合并 ≥85%、报告导出及完整项目清理。
- V2 的 v 轮完整单元/API 仍在独立冻结副本运行，用于关闭前一批验证；不能把不同版本的专项直接当作组合门禁通过。B3、Agent 事务以及 V2 整理条目当前仍保留 TODO。


V5 P1-B9 的确定性红测已确认：真实 API 修改落库并返回 200，MemoryQueue 的已有 agent key 拒绝新请求，真实 handler 完成后仅原 total_resources=0 的运行记录。1 failed，2.86 秒，`/tmp/rssripple-v5-busy-rerun-red.log`；设计与尚未完成标准见 V5-REQUEST-REPLAY.md。该复现不把队列最终空闲当作已消费，也不以睡眠超时推断请求丢失；同时结合当前无持久请求或后续分发机制的实现确认原因。

V5 持久请求存储原型已验证真实事务回滚、版本确认、新 UUID 防误删与 Agent FK 级联：1 passed，0.64 秒，`/tmp/rssripple-v5-request-persistence.xml`。尚未接入运行链路或调度器，忙碌请求红测未转绿；具体范围及补丁见 V5-REQUEST-REPLAY.md。


V5 已接通三端点事务请求写入、作业版本确认及 5 秒周期分发的临时原型：旧 P0/三端点/交错版本扩大 8 passed（15.92 秒）；真实写入后异常回滚和队列不可用后新 worker 对象接手 4 passed（7.33 秒）；存储退避上限/新修订保护 2 passed（4.12 秒）。这些是不同选择集，不相加冒充完整一轮。证据、尚未验证场景和更新补丁见 V5-REQUEST-REPLAY.md。主工作树 w 轮源码保持不变。


### V2 v 轮最终结果：测试通过，覆盖率门禁未通过

v 轮 **3495 passed、14 skipped、6 warnings，1813.82 秒**；覆盖率 **19246/21381＝90.01%**，95% 门禁退出 1。核查发现该临时副本缺少 `.coveragerc`，未启用项目的 `concurrency = greenlet,thread`；大量线程中执行的 API 行未被采集。此轮测试通过有效，但覆盖率数据不足以验收，不能改写成完整通过。已补复制配置用于后续运行，不追溯修改本轮数据或靠合并另一版本的覆盖率凑门禁。当前 w 轮在主工作树使用已冻结的正确 `.coveragerc` 并包含全部 V2 修复/测试，将以完整组合门禁继续验收。

V5 实际 job 返回错误及抛异常两路径的退避、到期恢复、其他 Agent 进度，以及暂停/恢复和换频道清理测试合计 **7 passed，12.82 秒**，`/tmp/rssripple-v5-request-lifecycle.xml`。异常退出遗留 AgentRun running 的既有 crash/reaper 条目仍独立保留，未据此测试宣称解决。正在专用 x 项目执行 PostgreSQL＋Redis 多进程、升级和 worker 替换验证。


## V2/V3/V4 组合版本最终验收（2026-09-13，w 轮）

本地实现已通过完整门禁：单元/API **3500 passed、14 skipped**，覆盖 **21016/21461（97.93%）**，超过 95%；集成 **3096 passed、17 skipped**，合并覆盖 **19282/21461（89.85%）**，超过 85%。运行前后 712 个被测文件哈希一致。V2 v 轮配置遗漏导致的覆盖率失败仍保留为历史失败，此次使用正确 `.coveragerc` 的完整组合版本完成验收。

已验证并从 pending-only TODO 删除：Agent 候选事务失败恢复、P1-B3 通知毒任务隔离，以及 P1-O1/O2/O5 整理规划版本、操作语义与跨进程执行所有权。权威设计文档及回归测试已同步。B9、B4、B7 和其余待办继续保留；这些结果不表示所有问题已解决。

原始单元日志/JUnit/覆盖率：`/tmp/rssripple-v34-unit-w.log`、`/tmp/rssripple-v34-unit-w.xml`、`/tmp/rssripple-v34-unit-coverage-w.xml`；集成日志/JUnit：`/tmp/rssripple-v34-integration-w.log`、`/tmp/rssripple-v34-integration-w.xml`；四路原始覆盖率、合并数据与 XML 已导出到 `/tmp/rssripple-v34-artifacts-w`。两个应用正常退出后完成覆盖率合并；唯一项目 `rssripple-v34-complete-20260913-w` 的容器、网络及临时卷均已清理。机器可读摘要见 [验收结果](probes/v234-complete-w-result.json)。未部署生产环境。


## V5 合入本地前的续轮验收（2026-09-13）

必要性继续成立：队列 active-key 去重不能保存当前作业选完资源后的修订；仅延迟入队或进程内集合无法覆盖 broker 故障与 worker 重启。原型保持资源编辑＋持久请求同事务、id/revision 条件确认，并维持 Agent 当前规则与定向水位线语义。

扩大回归 **319 passed、8 skipped、1 warning，164.50 秒**，报告 `/tmp/rssripple-v5-expanded-y.xml`；首次因临时副本缺 API 测试模块而收集失败，补齐后才计通过。新增实际 APScheduler 触发及两个端点真实兄弟资源修复 **3 passed，3.00 秒**，`/tmp/rssripple-v5-scheduler-z.xml`。这些测试使用真实 Turso、队列及 HTTP/调度链路，兄弟资源元数据为明确合成夹具。

[真实 RPC 驱动](probes/agent_request_rpc_probe.py) 用已审核 `f79ef2eb-02d5-42d3-80dc-70dd3c1d733b` 的原始种子，校验 SHA256；专用内部网络 Transmission 接受后注入真实 NOT NULL 失败，首次数据库无任务但请求持久退避，未到期不重试，到期通过分发器＋实际 MemoryQueue＋生产 job 重试。两次 RPC 复用同一 torrent ID，最终一个 daemon torrent、一个 DB task，请求清空且水位线不动，媒体接收 0 字节。[结果](probes/agent-request-rpc-result.json)，原始日志 `/tmp/rssripple-v5-request-rpc-aa.log`。种子真实，作品身份为合成前置数据；不声称验证了 metadata 识别。项目 `rssripple-v5-request-20260913-aa` 已全部清理。

驱动会创建独立 `/tmp` Turso 库，要求名为 `rssripple-v5-request-20260913-aa` 的新空 Transmission 专用项目；用 [Compose](probes/agent-rpc-compose.yml) 加显式 `-p` 启动，设置 `PYTHONPATH=.` 从含本地 app/tests 的仓库运行驱动，最后同项目 down -v。不得复用业务下载器。

按 code-review-and-quality 检查事务原子性、旧版本竞争、异常恢复、依赖及边界；未新增依赖。10 个实现/测试文件经主工作树基线哈希比对后已同步本地，新增模型、业务/API/迁移和测试权威文档已更新，相关 Ruff 全部通过。完整门禁尚待执行，B9 继续保留 TODO；此前“仅独立原型”描述为历史阶段。

主工作树同步后专项 **19 passed，10.97 秒**，报告 `/tmp/rssripple-v5-main-targeted.xml`；Ruff 与 `git diff --check` 通过。ab 轮完整单元/API 开始运行，结果未定；源码快照 `/tmp/rssripple-v5-source-ab.json`。


V5 ab 完整单元/API 与隔离集成均已开始：日志 `/tmp/rssripple-v5-unit-ab.log`、`/tmp/rssripple-v5-integration-ab.log`，项目 `rssripple-v5-complete-20260913-ab`；两者仍运行，未计验收通过。冻结快照 436 个文件未变。等待期间独立开展 V6 元数据身份接地，六项真实代码红测复现、原型六项转绿；必要性、方案与剩余严格验收见 [V6-IDENTITY-GROUNDING.md](V6-IDENTITY-GROUNDING.md)，未合入或关闭 TODO。

V6 扩大原轮终态：**235 passed、2 failed、1 warning，71.11 秒**（`/tmp/rssripple-v6-grounding-expanded.xml`）。两项失败的夹具在 found=True 时未提供身份；按原测试目的补上搜索证据中的 `wikipedia:en:1`，保留页面异常诊断和候选上限断言后，身份＋Wikipedia 专项 **33 passed、1 warning，3.35 秒**（`/tmp/rssripple-v6-grounding-corrected.xml`）。未将失败原轮改称通过；仍需数据库身份袋、真实语料、其他身份入口及完整门禁。原型补丁与哈希已更新。

B9 ab 完整单元/API 已结束，退出 0：**3500 passed、14 skipped、6 warnings，1475.15 秒**；覆盖 **21066/21528（97.85%）**，95% 门禁通过。原始报告 `/tmp/rssripple-v5-unit-ab.xml`、`/tmp/rssripple-v5-unit-coverage-ab.xml`；完整集成仍运行，尚不关闭 B9。


## B9 最终收尾（2026-09-13，ab 轮）

本地 B9 实现完成验证并已合入本地主干（代码提交 `4c804ed`）；已按 pending-only 规则从 TODO 删除 P1-B9。资源编辑与持久请求同事务、忙碌队列后的补发、旧版本确认保护、错误退避、兄弟资源、暂停/删除/频道变更、实际调度与跨进程恢复均有专项证据；真实种子＋Transmission 接受后数据库失败的到期重试复用同一 torrent ID，未下载媒体。

| 门禁 | 结果 | 覆盖率 | 退出状态 |
|---|---|---|---|
| 完整单元/API | 3500 passed、14 skipped、6 warnings；1475.15 秒 | 21066/21528 = 97.85%，超过 95% | 0 |
| 完整隔离集成 | 3114 passed、17 skipped、8 warnings；1705.85 秒 | 19361/21528 = 89.93%，超过 85% | runner 与覆盖率报告均为 0 |

两份 JUnit 的 failures/errors 均为 0；436 个被测源文件运行前后哈希一致。全仓 Ruff 与差异空白检查通过。两个应用 SIGINT 后均正常退出 0；原始四路覆盖率、合并数据和 XML 已导出到 `/tmp/rssripple-v5-artifacts-ab`，JUnit 为 `/tmp/rssripple-v5-unit-ab.xml`、`/tmp/rssripple-v5-integration-ab.xml`；日志为 `/tmp/rssripple-v5-unit-ab.log`、`/tmp/rssripple-v5-integration-ab.log`、`/tmp/rssripple-v5-coverage-ab.log`。唯一项目 `rssripple-v5-complete-20260913-ab` 的容器、网络及临时卷全部清理。

机器可读结果见 [B9 验收摘要](probes/agent-request-complete-ab-result.json)。之前记录的运行中状态为历史，不再代表当前状态。此次收尾不关闭 B4、B7、AgentRun 崩溃回收或元数据身份接地；剩余 P1 21 项、P2 79 项、P3 13 项，共 113 项。下次入口见 PLAN.md 开头和 V6-IDENTITY-GROUNDING.md，V6 仅保存方案、复现与原型补丁，未合入运行代码。


## 2026-09-19：身份袋与缓存必要性续验（独立原型）

主干仍为 `02865b2`，B9 已完成；本轮实现仅在独立 V6 副本。新增真实 Turso 的生产 `process → upsert → WorkExternalId` 集成，外部来源与 LLM 明确使用合成数据。Wikipedia 首轮 **2 failed、2 passed，2.71 秒**：合法主身份与非法主身份行为正确，但伪造 alt_external_ids 和 generation=6 的旧成功缓存均实际写入非法身份。补修后连同已有专项 **37 passed、1 warning，5.79 秒**。扩展 TMDB 后又复现其伪造别名落库（**1 failed、7 passed，4.04 秒**），修正后两源四场景及既有专项 **41 passed、1 warning，7.64 秒**。

方案因此补充：Wikipedia 仅保留选中证据的语言别名（证据为空也必须清掉模型别名），来源/页面 URL 来自证据；TMDB 当前工具不提供跨源身份，清掉模型 alt_external_ids/Wikipedia URL，规范 primary source/id；缓存代际从 6 升至 7，旧成功缓存重新走来源校验。合法身份重复处理保持同一电影与唯一身份袋行。该缓存变更仅使旧结果失效，不自动修复历史已落库的污染身份；不得把它说成已清理存量污染。

报告分别为 `/tmp/rssripple-v6-persistence-red.xml`、`/tmp/rssripple-v6-persistence-green.xml`、`/tmp/rssripple-v6-tmdb-persistence-red.xml`、`/tmp/rssripple-v6-both-sources-green.xml`。公开 Wikipedia API 的匿名只读录制尝试返回 HTTP 403，未生成成功录制，不能当作真实来源验收。现有数据库证据足以继续修复，不构成整项阻塞。

扩大 MetadataAgent 单元/集成回归已开始：`/tmp/rssripple-v6-expanded-20260919.log`，尚未有最终结果。仍需 Wikipedia ReAct 回退接地、合法跨语言别名正例、畸形 entity 边界、PostgreSQL 实际落库、真实来源语料及完整门禁。原型补丁与哈希已更新；两个身份接地 TODO 不关闭。

扩大回归已结束：**294 passed、1 warning，71.55 秒，退出 0**（`/tmp/rssripple-v6-expanded-20260919.xml`）。另行 Wikipedia ReAct 必要性测试 **2 failed、1 passed、1 warning，0.77 秒**（`/tmp/rssripple-v6-wiki-react-red.xml`）：已观察身份正例通过，但未知 pageid 与错误语言版本仍被回退图接受。该红测已保存进原型补丁，尚未修复；不得将 294 项通过解释为完整接地完成。下一轮先统一 judge/ReAct 的 Wikipedia 身份选择与别名清理，再补失败工具、页面 URL/语言和合法跨语言别名的正反例。主工作树运行代码仍为已验证 B9，未合入本原型。


## 2026-09-19：Wikipedia ReAct 与三入口双库续验

必要性来自上轮两个实际回退图红测。原型现已让 judge 和 ReAct 共用按 `(lang,pageid)` 选择身份与可信别名的函数；ReAct 仅接受成功 Wikipedia 工具响应，并从返回的 Wikipedia 页面 URL 确定语言版本。旧裸 ID 仅能匹配唯一语言身份；同页搜索/详情多次出现不构成歧义，保留详情类别和可信 langlink 身份，清掉模型别名、来源及 URL。初步 **44 passed、1 warning，7.27 秒**，`/tmp/rssripple-v6-react-green.xml`。

补正常跨语言别名、同页多观察、跨语言同号歧义、错误工具状态/失败响应/无语言 URL/假 Wikipedia 域名及畸形 entity 测试后，扩大回归 **308 passed、1 warning，65.45 秒**（`/tmp/rssripple-v6-react-expanded.xml`）。旧 `_run_react` 成功测试原本只有 `external_id=x` 且无工具证据，已补真实形状的 search 调用/响应，并保持 finalize 提取和诊断断言；没有放松接地约束。

数据库矩阵扩大到 Wikipedia judge、真实 judge→ReAct 回退和 TMDB ReAct 三入口，分别检查合法身份、非法主身份、伪造别名、可信别名和旧缓存；Turso **15 passed，6.91 秒**（`/tmp/rssripple-v6-all-paths.xml`），独立 PostgreSQL **15 场景通过、退出 0**。同一生产 process/upsert/cache/身份袋路径验证无非法身份、合法关联稳定、重复执行不重复建档；TMDB 工具无跨源身份，trusted_alias 场景仍只保留其主 ID。

[PostgreSQL 驱动](probes/identity_pg_probe.py) 与 [结果](probes/identity-pg-result.json) 已保存；日志 `/tmp/rssripple-v6-identity-pg-ac.log`。驱动会清空指定测试库，必须在已应用原型且无业务 .env 的独立副本运行，显式 `IDENTITY_PROBE_DATABASE_URL=postgresql+asyncpg://organize_test:organize_test@127.0.0.1:<临时端口>/organize_test`、`PYTHONPATH=.`；仅接受回环地址及专用库身份。使用 [已有测试 Compose](probes/agent-request-replay-compose.yml) 的 postgres 服务并指定唯一 `-p`。本轮 `rssripple-v6-grounding-20260919-ac` 容器及网络已全部清理。

这批数据为合成来源/LLM＋真实数据库实现，不称为真实来源录制。匿名 Wikipedia REST 页面身份录制接口也返回 HTTP 403；尚未取得成功的来源录制。原型共 9 个实现/测试文件，补丁与 `/tmp/rssripple-v6-grounding-state.json` 已同步，主工作树运行代码未改。下一轮继续审查畸形工具数据边界和可用真实来源回放，完成权威文档同步及完整 95%/85% 门禁后才能关闭两项 P1；上轮“ReAct 尚未修复”是历史状态。


## 2026-09-19：来源 HTTP 实测暴露并修正字段契约

畸形来源数据验证先复现 10 项失败（0.15 秒）：非标量类型、超长数字身份、非对象页面及畸形可选别名/类别。补修后扩大回归 **326 passed、1 warning，83.01 秒，退出 0**，`/tmp/rssripple-v6-final-expanded.xml`。PostgreSQL ad 轮三入口五场景 **15 项通过、退出 0**；项目 `rssripple-v6-grounding-20260919-ad` 已清理。这两项证据早于下述 HTTP 字段修正，不当作修正后的全量结果。

必要性复核：旧 HTTP 正例使用无效 TMDB key 令来源搜索失败，却仍要求模型的 `mock-exa-*` 身份落库，与接地要求冲突。独立原型新增测试专用 TMDB 协议端点和 mock-LLM 应用入口，只重定向来源 HTTP 目的地，仍运行生产适配器、ReAct 和身份校验。TV/movie 模型结果改选来源返回的合成 ID；原 TMDB 广播剧正例改为拒绝无来源依据的音频身份，不能借 TMDB 视频证据创建音频作品。未新增生产环境配置或依赖。

真实 loopback HTTP→生产 `_execute_search_tmdb`→身份校验测试发现：生产搜索返回 `content_type`，详情返回 `media_type`，此前原型仅接受后者。修正测试设置和断言字段后，**2 failed、4 passed，0.82 秒**（`/tmp/rssripple-v6-source-http-red.xml`），两个合法搜索身份被错误拒绝。修复为兼容两个接口的类型字段、拒绝互相冲突类型；来源 HTTP＋身份边界＋Turso 持久化联合回归 **62 passed、1 warning，7.48 秒，退出 0**（`/tmp/rssripple-v6-source-http-green.xml`）。这说明工具消息合成用例不能替代生产适配器验证。

来源内容仍为明确标注的合成数据，HTTP 和数据库执行真实；尚无成功录制的 Wikipedia/TMDB 来源 API 响应。21 个原型文件已写入可应用补丁并通过 `git apply --check`。容器 HTTP 专项使用唯一项目名 `rssripple-v6-http-20260919-ae`，日志 `/tmp/rssripple-v6-http-ae.log`；最终结果待记录。仍需该应用级验证、权威设计文档及完整覆盖率门禁，主工作树运行代码未改，两项 P1 不关闭。


## 2026-09-20：容器 HTTP 专项完成

ae 轮 **4 failed、2 passed，119.51 秒**：来源候选响应中的标题干扰旧模拟模型按消息全文选作品；另有 genre 用例在 fetch 前清掉测试 TMDB key。af 轮修正这两项后 **2 failed、4 passed，117.20 秒**：TV 资源已关联，但旧断言使用系列级 `tmdb:900001`，单季作品实际按既有契约保存为 `tmdb:900001#s1`。仅修正持久化作品断言，手工搜索仍断言系列级身份。

ag 轮使用全新数据库后 **6 passed，117.47 秒，退出 0**（`/tmp/rssripple-v6-http-ag.xml`）：TV 关联、未命中、手工在线搜索正负例、电影关联、TMDB 无依据音频拒绝、genre 钳制全部通过。ae/af/ag 三轮独立容器、网络及测试卷均已清理，报告已导出。机器可读证据见 [HTTP 结果](probes/identity-http-results.json)。不把修正夹具后的通过视为真实提供者准确性验证。

权威 business-logic/data-models/integration-inventory 文档已在独立副本准备，原型累计 24 文件；全 app/tests Ruff 和补丁应用检查通过。下一步按 code-review-and-quality 复审最终补丁及测试独立性，复验修正后的 PostgreSQL，检查主工作树基线后同步，执行完整单元/API ≥95% 和隔离集成 ≥85% 门禁。完整门禁前不关闭两项 P1，也不把当前原型宣称已合入 main。公开来源录制仍缺失，保留明确的数据真实性限制；已有作品错误身份清理不在本轮验收声明内。


## 2026-09-20：V6 已同步工作树，ai 完整门禁运行中

按 code-review-and-quality 复核正确性、可读性、模块边界、安全与性能：接地函数不发网络、不访问 DB，复用现有 Wikipedia ID 解析；无新依赖，来源集合受上游候选/工具预算约束。测试专用 HTTP 入口仅用于 mock-LLM 服务。数据库矩阵的 TMDB 消息改为生产搜索候选实际 `external_id/content_type`；复验 Turso/HTTP/边界联合 **62 passed、1 warning，7.53 秒**（`/tmp/rssripple-v6-review-ah.xml`），PostgreSQL **15 场景通过、退出 0**，ah 专用项目已清理。来源 HTTP 测试的 genre 全局缓存也改为逐例恢复，避免影响其他测试。

24 个实现/测试/权威文档文件均核对主工作树原始哈希和原型最终哈希后同步；全仓 Ruff、差异空白检查通过。当前是未提交的本地工作树变更，main 已提交历史仍为 B9，不能称 V6 已验收或已提交。旧原型未合入的记录为历史阶段。

冻结 490 个 app/tests/scripts/config 文件：`/tmp/rssripple-v6-source-ai.json`。完整单元/API（95%）日志 `/tmp/rssripple-v6-unit-ai.log`，JUnit `/tmp/rssripple-v6-unit-ai.xml`；完整隔离集成日志 `/tmp/rssripple-v6-integration-ai.log`，项目 `rssripple-v6-complete-20260920-ai`。两道门禁正在运行，必须等最终退出码、导出 JUnit 并对集成应用优雅退出后汇总覆盖率（85%）；在终态前不修改冻结源码、不重启测试、不关闭 TODO。运行句柄保存 `/tmp/rssripple-v6-gates-ai.json`。本轮未推送远端。


V6 ai 完整单元/API 已结束，退出 0：**3540 passed、14 skipped、6 warnings，1557.00 秒**，覆盖 **21159/21627（97.84%）**，95% 门禁通过。JUnit `/tmp/rssripple-v6-unit-ai.xml`，coverage XML `/tmp/rssripple-v6-unit-coverage-ai.xml`。完整隔离集成仍运行，尚不验收/提交 V6；保持冻结源码并续接既有进程。


## V6 ai 完整集成结果及 al 复验

ai 完整集成已终态：**3129 passed、7 failed、17 skipped、8 warnings，1728.31 秒，退出 1**。7 项集中于旧夹具：Wikipedia judge 用例要求保留无证据身份；6 个 P0 季号场景模型直接 finalize、没有来源工具证据，先被接地拒绝而到不了季号校验。两个应用 SIGINT 正常退出 0 后，覆盖率汇总退出 0，**19413/21627（89.76%）**；测试失败意味着本轮不能验收。完整 JUnit/原始覆盖率在 `/tmp/rssripple-v6-artifacts-ai`，机器结果为 probes/identity-complete-ai-result.json，ai 项目已清理。

只修改两份集成测试：无证据 Wiki 身份改为明确拒绝；P0 用例让实际 ReAct 图先调用 search_tmdb，返回合成的合法身份，再 finalize 原故障注入季号。仍使用原捕获标题、保留所有 DB/cache/再次运行/非法季不落库断言，并断言工具确被调用且不出网。相关 **27 passed、6 warnings，4.13 秒**（`/tmp/rssripple-v6-fixture-green.xml`）。实现、单元/API 源码及配置与 ai 哈希完全一致，因此不重复已通过的 3540 项单元/API 门禁。

26 文件补丁已更新，两份修正已同步工作树。完整集成改用新项目 `rssripple-v6-complete-20260920-al`、日志 `/tmp/rssripple-v6-integration-al.log`，正在运行。冻结源码 `/tmp/rssripple-v6-source-al.json`；仍需终态结果、应用优雅退出、覆盖率汇总和清理，不关闭 V6 TODO。

## V8 D1 必要性与首轮原型（2026-09-20）

真实 Turso 的生产启动路径四项红测确认新装/升级库均允许同合集重复 S0/S1（4 failed，8.88 秒）。仅 ORM 索引后新装通过、升级仍失败（2 passed、2 failed，8.84 秒），因此方案补充轻量迁移幂等 DDL。独立副本最终 **5 passed、1 warning，10.37 秒，退出 0**；包含重跑启动、合法其他季/其他合集/legacy NULL 父项及冲突升级后人工编辑作品完整保留。方案、逐轮 JUnit 摘要和可恢复补丁见 [V8](V8-COLLECTION-SEASON-INDEX.md)。数据为合成，真实执行临时 Turso；尚无 PG 或完整门禁结果。

冲突历史库在原型中拒绝启动而不自动猜测合并，发布前仍须完成诊断/恢复流程与 API 409 竞争处理。D1 保留 TODO，未将原型应用 root。V6 al 的 490 个冻结文件、V7 an 的 493 个冻结文件本轮核对均未改变；两道门禁的原进程仍运行，未重启。

## V8 双库升级与写入竞争续验

Turso 包含已有 S3 作品/Episode/身份袋的升级保留回归 **6 passed、1 warning，11.04 秒**。独立 PostgreSQL ao 项目完成真实新装/已有数据升级的各两个进程同时启动，均退出 0；S0/S1/S3 重复插入拒绝，S5 双事务竞争观察到实际阻塞后按 23505 拒绝第二写入，只保留首个作品。冲突旧库失败后所有作品行不变。probe 退出 0，ao 已清理；详见 V8 及 probes/collection-season-pg-result.json。尚需 API 409、部署恢复和完整门禁，不关闭 D1。

## V6 最终验收（2026-09-20，al 轮）

身份接地的完整验收已完成：单元/API ai **3540 passed、14 skipped、6 warnings，1557.00 秒，21159/21627（97.84%）**；完整集成 al **3136 passed、17 skipped、8 warnings，1724.08 秒，19424/21627（89.81%）**。两轮 runner 和覆盖率门禁均退出 0；ai 单元/API 对应的实现及单元/API 源码未变，仅两份集成夹具补足来源证据后重跑完整集成。

490 个冻结文件哈希一致；两应用 SIGINT 后退出 0。JUnit/原始四路覆盖率/合并 XML 全量导出 `/tmp/rssripple-v6-artifacts-al`，JUnit failures/errors 均 0，al 项目已 down -v 清理。全仓 Ruff、差异空白检查通过，按 code-review-and-quality 复查了主/别名身份边界、工具失败拒绝、语言/类型匹配、缓存 generation=7、真实持久化以及测试替身与生产接口形状的一致性，无新增依赖或未解决的合并阻断项。

已从 pending-only TODO 删除原 P0-4 的 Wikipedia/TMDB 两项，权威契约已同步。实际 process/upsert/身份袋、Turso/PG、HTTP 来源适配与 P0 季号回归有证据；来源响应为标注合成数据，已捕获真实标题继续回放，不能称新增了在线成功录制的 Wikipedia/TMDB API 数据。缓存升级不清理历史非法身份。机器摘要见 probes/identity-complete-al-result.json。V7/V8 与其他待办仍未完成。

V6 有效代码、测试与权威契约已提交本地 main：`7fd8121`。尚未推送远端。

## V7 单元/API 终态与 ap 完整集成（2026-09-20）

an 完整单元/API **3561 passed、14 skipped、6 warnings，1465.99 秒，21261/21735（97.82%）**，退出 0；JUnit failures/errors 均 0，493 个冻结源码/配置文件哈希不变。最终复审了 API 创建/删除/解绑事务、跨页资源映射、启动批次回填、预加载关系与跨合集父锁顺序；无新增依赖或未解决的合并阻断项，全仓 Ruff、差异空白检查通过。

13 个实现/测试/权威文档文件先核对原型与当前主工作树基线，再同步到 root，尚未提交。完整集成新项目 `rssripple-v7-complete-20260920-ap`，日志 `/tmp/rssripple-v7-integration-ap.log`，状态 `/tmp/rssripple-v7-gates-ap.json`，冻结 `/tmp/rssripple-v7-source-ap.json`。保留 TODO，等待 runner 终态、应用优雅退出、覆盖率汇总/导出与清理。之前“仅独立副本”及“V6 仍运行”的记载为历史阶段；V6 已提交 7fd8121 并完成清理。

V8 两项 API 冲突红测确认未处理 IntegrityError（2 failed，2.74 秒）。独立副本同步 V7 基线后，在有序父锁下检查目标季槽，明确 409 且保留来源合集/身份袋/别名。API＋迁移 **8 passed，4.51 秒**；既有合集 API＋新冲突 **26 passed，10.95 秒**。仍须 API/后台并发和恢复流程，不关闭 D1。新补丁以当前 V7 未提交工作树为基线，主工作树 ap 冻结源码未变。

## D1 API 检查后的竞争写入（2026-09-20，aq）

真实 PostgreSQL＋生产合集 router/异常处理器的确定性交错复现：API 检查 S1 空闲后暂停，另一事务把目标合集已有 S2 成员改为 S1 并提交，再恢复壳吸收。第一版返回 **500 INTERNAL_SERVER_ERROR**；数据库拒绝重复并回滚，作品/来源合集/身份袋/别名虽保住，但 API 契约错误。此写入不改 collection_id，不能依赖父 FK 锁消除竞争。

原型把壳吸收/直接挂载的完整变更放进 SAVEPOINT，特定 `uq_tv_series_collection_season` 或 Turso 对应列唯一约束才转换为 409。相同交错改为 **409 DUPLICATE_SUBMISSION**，来源合集、incoming 归属、身份袋、目标别名与作品行数全部保留，probe 退出 0。另以实际身份袋唯一约束故障注入确认非季号异常仍抛出，不被误报为季冲突；连同既有合集 API 和迁移回归 **33 passed、1 warning，15.27 秒**。之后仅修复测试 import 排序，Ruff 通过。

驱动见 probes/collection_season_api_pg_probe.py；红/绿机器证据见 probes/collection-season-api-pg-result.json。所有数据明确合成，真实 SQL/HTTP ASGI/事务执行；`rssripple-v8-api-20260920-aq` 项目已清理。待补两个实际 API 同时抢占空槽、metadata/迁移扩大回归、冲突升级预检/恢复步骤、权威契约和完整门禁。离线旧迁移有碰撞合并行为，不能未经人工保护审计就推荐它自动修复所有冲突库。

## 双 API 抢槽、只读预检与迁移扩大（2026-09-20，ar）

两个真实 HTTP ASGI 请求向同一目标挂载不同壳合集的 S1：第一请求检查后暂停，第二请求的 pg_blocking_pids 明确非空；放行后 **201/409**，只有第一作品进入目标。失败方作品/壳/身份袋/资源归属保留，成功方资源与身份随之迁移，目标别名只合并成功方。驱动退出 0，证据为 probes/collection-season-pair-report-pg-result.json，ar 项目已清理。

在既有只读校验器增加 `--collection-conflicts-jsonl PATH` 独立模式，完整导出冲突成员 ID、合集、季号、主身份、标题与人工保护字段；不调用启动或创建索引。Turso 两项真实测试 **2 passed，0.88 秒**，覆盖 103 个冲突成员与合法对照，并记录全部 SQL 均为 SELECT。真实 PostgreSQL CLI 干净数据退出 0、冲突数据退出 1，完整 ID/保护字段与数据库行保持；不把预检退出 1 记为失败测试。

五份权威契约在独立副本准备，包含部署前备份/维护副本预检、冲突先留在旧版本人工核对、通过已支持解绑建壳的版本纠正归属，再预检/完整 verify/双启动；不推荐旧迁移自动合并保护作品。尚需实际恢复流程演练。

季拆分、合集与 franchise 服务扩大首轮 **71 passed、1 failed，23.12 秒**；唯一失败在兄弟列表测试夹具把同合集两作都默认 S1。只将夹具明确为 S1/S2，保留原排除/列表断言，正在复跑相同选择集，日志 `/tmp/rssripple-v8-migration-green.log`。最新原型 13 文件（含五份契约），仍未应用 root；V7 ap 继续冻结运行。

V8 相同迁移/合集/franchise/预检选择集复跑终态：**72 passed、1 warning，22.63 秒，退出 0**。仅修正兄弟列表夹具的明确季号，未弱化约束或断言。后续先审计 metadata 写入与演练冲突库恢复，再执行完整门禁；V7 ap 原句柄继续运行。

## metadata 并发、恢复演练与 at 门禁（2026-09-20，as）

metadata 扩大初轮 **236 passed、1 failed，65.46 秒**；日期回退夹具先放 S0 再创建另一 S0，与新约束冲突。改为在已有特典日期与无日期 S1 的合集创建 S2，仍断言不能借用特典日期，未弱化日期逻辑。

真实 PG 双 upsert 红测发现同一合集空季槽两个 matcher 同时新建时，一成功一 IntegrityError。成员解析改为先 FOR UPDATE 刷新已有父合集再查询/创建，标题回退复用合集同样进入该路径。绿测观察到第二 backend 阻塞，两个事务成功返回同一作品 ID，数据库恰好一行。metadata 服务、单季 upsert、repository、身份修复扩大 **237 passed、1 warning，61.54 秒**。该锁可能覆盖既有海报未命中时下载（既有 30 秒超时），同合集并发会等待；不宣称消除了所有 metadata 并发身份问题。

恢复演练：PG 合成同季冲突与人工编辑数据，实际预检 CLI 退出 1；子进程加载独立 V7 app 调用其解绑端点，为明确指定作品建新壳；预检转 0，V8 连续两次 create_tables 成功且索引存在。原两个作品 ID/人工标题/保护字段/Episode/身份袋/资源关联完整保留。驱动与红绿结果见 probes/collection_season_metadata_pg_probe.py、collection_season_recovery_pg_probe.py、collection-season-metadata-recovery-pg-result.json；as 项目已清理，未操作真实业务数据。

独立副本补齐缺失支持文件，未覆盖原型；全 app/tests/scripts Ruff 通过。最新原型 16 文件，六份权威文档已准备。完整单元/API at 已启动，日志 `/tmp/rssripple-v8-unit-at.log`，句柄 `/tmp/rssripple-v8-gates-at.json`，冻结 `/tmp/rssripple-v8-source-at.json`（496 文件）；不改动冻结源码。尚需终态、最终复审及完整集成，D1 不关闭。

## V7 ap 终态与 au 复验（2026-09-20）

ap 完整集成 **3135 passed、1 failed、17 skipped、8 warnings，1660.44 秒，退出 1**。唯一失败是 HTTP 合集测试在剧集解绑后仍断言 collection=None，与本批“不产生孤儿”的契约冲突；其他 API 功能步骤已通过。应用 SIGINT 正常退出 0，覆盖率汇总退出 0，**19526/21735（89.84%）**；因测试失败不验收。完整证据 `/tmp/rssripple-v7-artifacts-ap`，机器摘要 probes/collection-complete-ap-result.json，ap 项目已清理。

仅修改该 HTTP 断言：返回 200，剧集属于不同于旧目标的新合集，读取新合集验证成员恰好原作品。保留电影最终可无合集、重复解绑 404、错误 work_type 422 等断言。与 ap 冻结文件相比唯一变化为 tests/integration/http/test_coverage_supplement.py；实现及单元/API 源码不变，沿用 an 的 3561 项、97.82% 门禁。新项目 `rssripple-v7-complete-20260920-au`，冻结 `/tmp/rssripple-v7-source-au.json`，后续从 `/tmp/rssripple-v7-gates-au.json` 续接。原型扩为 14 文件。

V8 at 正在运行，保持其 496 文件冻结；副本仍含上述旧 HTTP 断言，须在 at 终态后、启动其完整集成前同步这一个修正，不要在运行中覆盖。

## V9 D2 必要性与三类 schema 矩阵

真实 Turso 首轮新装/升级对照 1 passed、1 failed：升级两次后 TVSeries 无 collection FK，非法父 ID 可实际写入。七个轻迁移 FK 对照 8 passed、8 failed，9.43 秒，新装通过、升级均缺约束。独立原型仅给新列附 REFERENCES/ON DELETE；扩展既有列缺约束后 16 passed、7 failed，31.71 秒，明确存量问题未解决。可恢复补丁、哈希、范围及带子表数据/双库/回滚验收见 V9-UPGRADE-FOREIGN-KEYS.md。未修改 root 或 V8 冻结源码，不关闭 D2。

## Turso 重建方式与模型接入验证

实际后端拒绝 ADD CONSTRAINT（near CONSTRAINT syntax error）。真实 DML 后 foreign_keys=OFF 可读取为 0，但 BEGIN CONCURRENT 中执行重建 DDL 失败，首轮 3 failed、4.63 秒。显式 BEGIN 后 DDL 可执行，然而该版本 foreign_key_check 不返回行，第二轮 3 failed、4.28 秒；不能把无行结果当成无悬空关联。改为明确 LEFT JOIN/非空父键检查后，正常提交、末尾故障回滚、悬空数据拒绝三项 **3 passed，4.07 秒**；CASCADE/SET NULL/RESTRICT 子表均保留，旧列、自定义索引/触发器与回滚后原 schema 均经断言验证。

独立原型新增 schema_foreign_keys.py：Turso 在 schema 阶段提交后、孤儿回填前开单独显式 BEGIN；根据 ORM 目标/删除动作核对实际 FK，预检悬空数据，复制原始 CREATE 定义和实际列，保留显式索引/触发器，外键关闭状态确认后同事务替换，finally 恢复外键。已有但动作/目标不符的 FK 明确拒绝，不猜测改写。只读 schema 检查正常路径不重建。

实际新装/缺列/既有列缺 FK 模型矩阵加重建原语 **26 passed、1 warning，36.53 秒**。随后增加带 Episode/FileResource/身份袋/人工保护及历史额外列、额外唯一索引的模型升级测试，正在运行（/tmp/rssripple-v9-fk-populated.log，句柄见 /tmp/rssripple-v9-fk-state.json）。当前实现仅接入 Turso；PG、新旧关联删除行为、故障注入与完整门禁仍须完成，不关闭 D2。初次独立能力脚本遗漏启用 MVCC 的设置错误已修正，不计为产品缺陷。

带实际模型数据的扩大终态：**27 passed、1 warning，38.06 秒，退出 0**。重复启动后原作品 ID/人工标题/保护字段、Episode、资源、身份袋及额外历史列/唯一索引完整保留。该结果不替代 PG 或完整门禁。


## V7 最终验收（2026-09-20，au 轮）

完整集成 **3136 passed、17 skipped、8 warnings，1688.63 秒，退出 0**；两个应用 SIGINT 正常退出 0，覆盖率汇总退出 0，**19524/21735（89.83%）**。单元/API 沿用未变实现的 an 轮 **3561 passed、14 skipped，97.82%**。493 个冻结源码/配置文件全部哈希不变，JUnit failures/errors 均 0，证据导出 `/tmp/rssripple-v7-artifacts-au`，隔离项目容器、网络、卷已清理。机器结果见 [au 验收摘要](probes/collection-complete-au-result.json)。

按 code-review-and-quality 复审创建/删除/解绑事务、预加载 ORM 关系、资源映射分页、父锁顺序、启动幂等回填及跨进程竞争；无新增依赖。合集成员/身份袋/手工文件映射保留及 PostgreSQL 并发有真实数据库证据；测试数据为明确构造的关系数据，完整集成继续包含捕获标题和真实 torrent 清单，未宣称新增在线来源录制。

原 P0-6 的四条 API/回填待办已验收，从 pending-only TODO 删除。D6 仅合集删除部分完成，剧集/电影删除与人工映射策略继续保留；D1/D2 原型仍未验收。历史 ap 失败与运行中记录保留，不代表当前验收状态。


## at 失败修正与扩大回归（2026-09-20）

at 完整单元/API **3564 passed、8 failed、14 skipped，1563.65 秒，97.77%**，不能验收，见 probes/collection-season-complete-at-result.json。六项涉及同合集重复季号的旧夹具；去重继承合集是真实写入顺序问题，须在事务内先 flush 删除重复行，再继承合集。Wikidata 原正例的两作品改为明确 S1/S2，同时补同季真实 DB 负例：apply/dry-run **2 failed，1.41 秒**（此前错误返回 linked）。补修后遇占用返回 ambiguous，拒绝前不更新标签，实际写入先锁已有目标合集。

保留原断言目的：Bangumi stale S1 从独立壳合集开始，继续验证修正到 S2/人工保护；手工资源映射不删除；人工日期案例仍有可用正季日期而必须保持不变。七个相关测试文件扩大回归 **206 passed、1 warning，56.31 秒**，`/tmp/rssripple-v8-writepaths-av.xml`。V7 的 HTTP 解绑断言已在 at 终态后继承。需要重跑完整门禁，206 项不能替代验收。


## av 完整复验启动

V7 已提交本地 main `e535e1f`。V8 23 个实现/测试/权威文档文件经主工作树基线哈希比对后同步，未提交；全仓 Ruff 与差异检查通过。root 的 496 个源码/配置文件冻结于 `/tmp/rssripple-v8-source-av.json`。完整单元/API 与隔离集成 av 正在执行，句柄与日志见 `/tmp/rssripple-v8-gates-av.json`；终态前不得修改 root app/tests/scripts/config。at 失败仍有效，TODO D1 保留。


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


## V8 av 终态与 bc 集成复验

av 完整单元/API **3574 passed、14 skipped、6 warnings，1622.13 秒，21291/21764（97.83%）**，退出 0。完整集成 **3119 passed、17 errors、17 skipped、8 warnings，1660.27 秒，退出 1**；全部 17 个错误发生在 season_model 共用的迁移前生产录制数据加载阶段，新 ORM 自动创建唯一索引导致历史重复季槽无法载入。两个应用正常退出 0，覆盖率汇总退出 0，**19517/21764（89.68%）**。证据 `/tmp/rssripple-v8-artifacts-av`，机器摘要 probes/collection-season-complete-av-result.json；av 项目已清理，因测试错误不验收。

仅修正 tests/integration/season_model/conftest.py：初始化历史库时移除新索引，完整加载原始生产录制数据；实际单季迁移后明确断言唯一索引已重建。同时将加载过程纳入 finally，失败也恢复全局测试 engine/factory。没有改写录制数据、跳过测试或删除旧断言。定向完整 season_model **17 passed，25.66 秒**（`/tmp/rssripple-v8-season-fixture-bc.xml`），Ruff 修正局部 import 排序后全仓通过。

av 冻结文件仅这一个集成夹具变化，应用/单元/API 源码不变，复用已通过的 3574 项门禁；原型累计 24 文件。新完整集成项目 `rssripple-v8-complete-20260920-bc`，冻结 `/tmp/rssripple-v8-source-bc.json`，句柄 `/tmp/rssripple-v8-gates-bc.json`。V9 bb 独立副本仍冻结，其同名集成夹具须在 bb 终态后继承再启动完整集成；不能在运行中覆盖。


## 写入防护原型与存量证据（2026-09-20）

独立副本新增请求级退役字段拒绝（包括 null），移除创建/更新 schema 的 number_of_seasons，但保留只读响应兼容；seasons 也明确拒绝，其他未知键沿用原契约。MANUAL_EDITABLE_FIELDS 移除退役计数；去重不再从重复行继承它。已有真正旧作品的计数/seasons 不自动清空，其他字段与人工保护保留。

实际 API 的两个原始红测、12 组退役请求无副作用、合法单季 Episode 写入，以及已有 API/去重扩大回归 **64 passed、1 warning，37.37 秒**（`/tmp/rssripple-v10-write-guards-green.xml`）。同时保留旧幸存行 count=3/seasons 证据且不新登记退役人工字段。八文件原型（含三份权威文档）仅在独立副本，未应用 root，仍缺存量方案和完整门禁。

只读审计仓库已有 `tests/fixtures/prod_works_v1.json`：65 个 TVSeries 中 27 个带非空 number_of_seasons，其中包含明确多季 JSON。这是迁移前录制夹具，不能与 TODO 所述另一份历史主库快照“1 行非空”混为一谈。禁止改写此录制数据来让清理测试通过；应据它建立真正旧多季、单季与缺证据样本。

清理还必须处理证据退化：work_verified_season 对 S1 可能依赖旧 count=1；直接清空可能把可验证的 S1 变成 unknown。下一步应设计显式确认且可审计的清理流程，保留原始证据、并发前置条件和真正多季分流，既不能猜季，也不能只做新写入防护就关闭 D3。


## V9 bb 单元/API 终态

完整单元/API **3611 passed、14 skipped、6 warnings，1626.49 秒，21340/21833（97.74%），退出 0**。JUnit failures/errors 均 0，2927 个冻结源码/支持文件哈希不变。随后仅继承 V8 bc 的历史迁移库集成夹具修正，应用/单元/API/配置不变；副本新清单 `/tmp/rssripple-v9-source-bd.json`。尚待 root V8 bc 完整集成终态，再核对基线同步 V9 并运行其完整集成；当前不关闭 D2。

## V8/V9 联合最终验收启动（2026-09-20，bg）

V8 bc 完整集成 3136 passed、17 skipped（1664.27 秒），应用退出、覆盖率汇总、导出及项目清理完成；最终复审新增真实 Turso 约束错误文本负例，最初 1 failed，补修后 4 passed（1.71 秒）。因此不能直接沿用此前完整门禁宣告验收。V9 bb 完整单元/API 3611 passed、14 skipped，覆盖率 97.74%。

V8 错误转换补修与 V9 的 11 个文件已逐项基线哈希核对后同步主工作区。Ruff 全部通过，2891 个源码及支持文件冻结于 `/tmp/rssripple-v89-source-bg.json`。联合完整单元/API（95%）与唯一项目 `rssripple-v89-complete-20260920-bg` 的完整集成（85%）运行中，持久句柄 `/tmp/rssripple-v89-gates-bg.json`。运行期间不修改被测源码；待测试终态、应用退出、覆盖率汇总、证据导出和清理全部完成后再决定验收。

P0/B9 合入核验：`git merge-base --is-ancestor 4c804ed main` 返回 0；本地 main 当前为 `e535e1f`，包含后续 V6/V7。尚未推送远端。

## V8/V9 bg 集成终态与 V10 bp 启动

V8/V9 bg 完整集成 **3136 passed、17 skipped、8 warnings，1716.83 秒，退出 0**；应用 app/app-llm 正常退出均为 0，四份覆盖率汇总退出 0，覆盖 **19576/21833（89.66%）**，85% 门禁通过。JUnit 的 errors/failures 均为 0，报告已导出 `/tmp/rssripple-v89-artifacts-bg`；测试项目 down --volumes --remove-orphans 退出 0。完整单元/API 仍运行，暂不验收提交或删除 D1/D2。

V10 从单元冻结副本复制完整测试目录 `/tmp/rssripple-v10-integration-bp`，逐项核实 2997 个源码/静态/夹具文件哈希一致（复制解引用 fixture symlink，确保容器内可读；原副本不变），以唯一项目 rssripple-v10-complete-20260920-bp 启动完整集成。句柄 `/tmp/rssripple-v10-gates-bp.json`；独立单元/API bh 同时继续运行。未读取生产配置/数据，V10 未应用主工作区。

## V8/V9 联合最终验收（2026-09-20，bg）

完整单元/API：3612 passed、14 skipped、6 warnings，2255.37 秒，退出 0；覆盖 21343/21833（97.76%），95% 门禁通过。完整集成：3136 passed、17 skipped、8 warnings，1716.83 秒，退出 0；覆盖 19576/21833（89.66%），85% 门禁通过。两个应用退出均为 0，覆盖率汇总/证据导出/项目清理全部完成，报告在 `/tmp/rssripple-v89-artifacts-bg`。冻结的 2891 个源码/支持文件终态哈希一致；Ruff 全库及 diff whitespace 检查通过。

最终复核覆盖：新装/升级唯一索引、旧冲突只读预检与失败保留、真实 PG API/metadata 抢槽、SAVEPOINT 内关联原子回滚、Turso 实际错误文本转换，以及七处 FK 的双库新装/缺列/已有列矩阵、真实 INSERT/UPDATE/ON DELETE、带关联数据原子重建、故障回滚、并发启动与只读孤儿报告。无新增依赖；权威模型/API/业务/迁移/单季化/集成清单已同步。历史失败轮保留，不以更改原录制数据规避失败。

D1/D2 验收完成，从 pending-only TODO 删除。当前生产库未执行迁移或数据修复；新约束遇到既有冲突/悬空关联会拒绝启动，须按只读报告及迁移 runbook 修复，不能自动删业务行。机器可读证据见 probes/database-invariants-complete-bg-result.json。V10/V11 及其他待办继续保留。

V8/V9 有效修复已提交本地 main：`6ab0548`。V10/V11 仅保存独立原型与证据，未混入本次运行代码。远端未推送。

## V10 bp 失败轮归档与 V11 Turso 事务边界

V10 bp 应用均正常退出 0，覆盖率汇总退出 0，18825/21841（86.19%）；测试为 3016 passed / 3 failed / 115 errors，因此不验收。报告导出 `/tmp/rssripple-v10-artifacts-bp`，项目清理退出 0。V10 bh 完整单元/API 已通过 3648 项（97.75%）；后续只修正集成夹具时可保留这份单元证据，若改变运行代码则须重跑。具体旧 HTTP helper 位于 `_http.py`、test_api_coverage2、test_api_coverage2_llm、test_decisions_flow，test_notifications 的同名字段属于提供者元数据输入，仍然合法，不应按作品写入字段删除；dedup 旧断言需改为禁止继承。原录制数据不得改写。

V11 新装模型与持久键原型通过 22 项；PG 同槽并发合并完整。Turso 原始调用因锁冲突失败，完整新事务通过 retry_on_lock 重放后恢复一行四候选，初始并发屏障及第三次 picker 重试均已观察。业务层尚未接重试/错误传播；升级迁移、历史决策处理与确认再验证仍缺。不能据存储原型测试关闭 D4/M4/M5。

## V10 bx 专项通过，bz 完整集成启动（2026-09-20）

重新确认必要性：写入退役季数字段会重新制造旧多季作品；不能为了恢复旧夹具而放开 API。共享及重复 HTTP helper 改为明确标注的合成 Bangumi 单季身份，保留已有合法身份；原始录制数据未修改。新增容器 HTTP 创建/更新拒绝测试，覆盖两个退役字段及显式 null，并检查拒绝后没有状态改变。dedup 断言改为禁止继承退役季数。

bx 受影响容器专项 **12 passed，216.54 秒，退出 0**；2998 个冻结文件未变，两应用 SIGINT 后退出 0，JUnit 已导出，测试项目已清理。原型现为 27 文件。运行代码未变，保留 bh 完整单元/API 3648 passed（97.75%）；完整集成使用新项目 `rssripple-v10-complete-20260920-bz`，句柄 26134，日志 `/tmp/rssripple-v10-integration-bz.log`，冻结副本 `/tmp/rssripple-v10-integration-bx`。完整测试、应用退出、覆盖率 ≥85%、导出与清理全部完成前不验收。

## V11 生产事务重试边界补验（2026-09-20）

后台 ask 多候选分支仅持久化决策，因此以全新 Session 重试整个候选事务；单候选下载分支不增加自动重试。请求事务遇到锁冲突向外抛出，交由现有请求事务边界处理。专项 **25 passed、1 warning，4.44 秒**，包含两个不同 Session 的重试、请求错误传播和下载分支不重复调用。

ca 探针直接并发调用生产 `process_resources(autocommit=True)`，没有探针外层 retry：两个实际 Turso 连接初次同时进入，生产边界发生一次重试，最终同一决策 ID、一条 pending、四个候选。唯一键及 pending 非空约束通过，历史非 pending 空键保持。数据为合成，LLM 用屏障替代；不宣称真实模型调用或跨进程验证。证据与可复跑脚本见 probes/decision-pipeline-turso-result.json、pending_decision_pipeline_turso_probe.py。旧 schema 升级、确认/AI/批量端点状态与覆盖复核、元数据合并后 rekey、完整门禁仍未完成，D4/M4/M5 不关闭。

## V11 旧决策只读审核原型（2026-09-20，cc）

新约束不能直接加到旧表：按实际候选覆盖重分组后，旧行可能需拆分，同槽旧行可能需合并。新增 decision_review 保留全部原始行及人工决定历史，按 agent 与规范化覆盖生成提案；缺失资源、未知覆盖、损坏候选列表和单候选组保留审核标记。报告含源决策 ID 与指纹，不执行删除、自动过期或派发，也不代表已授权迁移。后续 apply 必须锁定并重新审核，同时检查候选资格，不能仅依赖当前覆盖指纹。

**6 passed、1 warning，0.44 秒，退出 0**：包含实际 Turso 的旧表（没有 decision_key/decision_scope），SQL 观察器确认导出只执行 SELECT；混合槽拆分、重复槽合并、不同 agent 隔离、历史人工结果保留和候选作品改变使指纹失效。数据均为合成，原生产录制的 3 条 decided 历史不足以覆盖 pending 升级。原型现 14 文件，旧库 apply/schema 升级及确认端点保护仍未完成；未合入主干运行代码。V10 bz 完整集成仍运行，沿用句柄 26134，不重启。

## V11 确认入口覆盖漂移复现与初修（2026-09-20，cd–cf）

实际 Turso + HTTP 红测 **3 failed，1.70 秒**：创建同电影两候选决策并缓存推荐后，将被推荐资源改挂另一电影；confirm、ai-pick、batch AI 都错误成功并把决策标为 decided。下载派发由 AsyncMock 替代，不声称已真实下载。

新增共享 current_choice_error：检查 pending/有效期、至少两个不同且仍存在的候选、已存身份，以及每个候选的当前规范覆盖。手动和单条 AI 入口返回 409 INVALID_STATE，批量 AI 报该条失败；AI 计算后再复核一次。cf **12 passed、1 warning，3.21 秒**，覆盖三个入口的合法正例与漂移负例，以及六项只读审核测试；拒绝不派发、不改 decided 状态。

原型现 16 文件。这里不宣称已解决 TOCTOU：候选/Agent/决策锁顺序、等待模型期间状态变化、批量 SAVEPOINT 故障隔离、规则资格重验、旧库迁移和 rekey 仍待实施；现有旧测试的无键 PendingDecision 夹具也须按新契约逐项调整。主干运行代码未变，TODO 不关闭。V10 bz 原句柄 26134 继续运行。

## V11 批量事务失败隔离（2026-09-20，cg–ch）

真实 HTTP/Turso 红测 **2 failed，1.39 秒**：第一条的实际 NOT NULL flush 失败使整个 Session 进入 PendingRollbackError；另一场景 action 返回业务失败后，失败条目被改为 decided 的状态仍提交。必要性由数据库结果确认。

批量循环改为逐条 SAVEPOINT，失败返回通过局部异常回滚，成功计数只在 flush 和 SAVEPOINT 完成后增加。事先保存 ID，避免失败后读取过期 ORM 属性；锁冲突、外层事务失效和连接丢失继续向请求边界传播。组合 **14 passed、1 warning，4.31 秒，退出 0**，失败条目保持原始 pending/reason，下一条实际提交；同时复验三个入口的覆盖漂移/合法确认与只读审核。最后仅将局部异常类改名为 RejectedChoiceError 以满足 Ruff，检查通过。

原型现 17 文件，action 为故障注入，不声称测试了真实下载 RPC 失败。PostgreSQL 批量错误、候选资格/锁顺序与并发状态、旧 schema 升级及 rekey 仍待补；D4/M4/M5 保持未完成。V10 bz 已越过磁力解析等待继续执行，原句柄 26134 保持有效。

## V11 当前订阅和过滤资格复核（2026-09-20，ci–cj）

新增实际 HTTP/Turso 红测 **6 failed、6 passed，6.64 秒**：只修改 Agent filter_config 或将 scope_channel_wide 改为 false（无订阅作品），原 confirm/AI/batch 仍接受旧决策。候选身份没有改变，因此仅检查覆盖度不够。

current_choice_error 现在重新加载 Agent.works，复用生产 _build_rule_set/_resource_matches_rules，并校验资源频道仍匹配 Agent；资源的作品、合集关系显式加载，避免 DSL 静默读不到关联字段。cj 组合 **20 passed、1 warning，8.03 秒，退出 0**，包括三入口的正常确认、覆盖漂移、过滤修改、范围修改及批量失败回滚/旧表审核。测试为合成候选，下载派发替换为 AsyncMock。

Channel 必填元数据变更的资格复验仍待补，当前规则复核也尚无锁保证；旧库迁移、rekey、并发状态与双库完整门禁继续保留。原型仅存独立副本与可应用补丁，未合入主干运行代码。V10 bz 继续运行至真实 feed 格式回放，未重启进程。

## V11 Channel 元数据门禁复验（2026-09-20，ck–cl）

频道在决策创建后新增必填 subtitle_group，三个确认入口仍绕过缺字段检查，红测 **3 failed、12 passed，8.72 秒**。正例的合成 Movie 明确提供 release_date 与 is_anime=False，保证其满足基础门禁；未改动真实录制数据。

current_choice_error 显式加载 Agent.channel，并复用 inspect_resource_confirmation(resource, current_required_fields)。组合 **23 passed、1 warning，9.69 秒，退出 0**：三入口缺字段均不派发、不改 decided，合法元数据仍可确认，已有范围/过滤/覆盖复核和逐条回滚继续通过。Ruff 通过。

资格复核已接入共享政策，但候选和决策在检查到派发之间仍需事务并发保护，不能据此关闭 D4/M4/M5。下一步重点验证模型等待期间另一事务改变 decision.status/candidates 的行为，再确定锁和推荐阶段边界；旧库迁移、rekey、双库扩大和完整门禁仍待完成。V10 bz 原进程继续执行 HTTP 端到端流水线。

## V11 AI 等待期间状态竞争（2026-09-20，cm–co）

独立 PostgreSQL 双连接已实际复现：_generate_llm_pick 替身在另一 Session 提交 status=skipped，旧请求仍 dispatch 一次并把最终状态覆盖为 decided。红测退出 1，原始结果与正向结果见 probes/decision-state-pg-result.json。模型/下载替身明确标注；并发提交是真实 PostgreSQL 操作，同进程两连接。

修复将模型选择保留局部变量，避免重读前 autoflush 旧对象；模型返回后按 Agent→PendingDecision 的顺序加锁并 populate_existing 重读，再检查候选集合/规范覆盖快照与当前资格。cn 复验退出 0：另一事务确已提交，dispatch_calls=0，ok=false，最终仍 skipped。临时 PG 项目已 down 清理，退出 0。已有 HTTP/Turso/审核组合 co **23 passed、1 warning，9.43 秒**，Ruff 通过。

本次只证明 AI 等待后不覆盖已提交的 skipped。手动确认和 skip 的统一锁、批量锁顺序、并发候选/作品/指派变更、下载幂等、旧库升级和 rekey 仍待完成；不能宣称整个确认路径并发安全。V10 bz 原进程继续执行 metadata HTTP 用例。

## V11 手动与 skip 终态保护（2026-09-20，cp–cq）

HTTP/Turso 红测 **3 failed、3 passed，3.12 秒**：confirm 已拒绝终态，但 skip 对 decided/skipped/expired 全返回成功并重写处理时间。历史记录无新键，仍须保留其原状态、人工结果与时间。

手动确认、单条 skip 和批量 skip 写入前复用 Agent→PendingDecision 锁顺序并重读；skip 仅允许仍 pending 的选择决策。组合 cq **29 passed、1 warning，11.75 秒，退出 0**，终态拒绝后保留原 decided_resource_id/decided_at，资格检查、正常确认与批量回滚继续通过。Ruff 通过，原型现 18 文件。

这轮不是双连接并发证明。复审还发现批量 AI 的外层事务在第一条写入后持续持锁，后续 _generate_llm_pick 可能在锁内等待。下一步须拆开批量推荐与锁定写入阶段，并验证模型等待期间另一事务仍可更新，以及写入阶段拒绝过时推荐；不能只把单条 AI 的锁后重读当作批量已完成。旧库迁移、资源/指派锁与 rekey 继续待办。V10 bz 已继续到队列 HTTP 测试。

## V11 批量推荐移出写锁阶段（2026-09-20，cr–ct）

批量先逐条计算不可变推荐（候选 ID 集、规范覆盖快照、picked_id），计算阶段不写决策；随后逐条 SAVEPOINT 中加锁重读和资格复核后派发。单条 AI 复用同样的 prepare/apply 逻辑，模型返回候选集外 ID 被拒绝。失败项保留独立错误，数据库事务失效仍向外传播。

实际 PG 对照探针在第二次模型回调用另一连接 UPDATE Agent，并设置 1500ms lock_timeout。拆分前代码放在独立副本运行，实际触发 LockNotAvailableError，只有第一条派发，退出 1；拆分后另一连接正常提交，两次模型调用、两条派发、零失败，退出 0。原始结果见 probes/decision-batch-model-lock-result.json，脚本 decision_batch_lock_pg_probe.py；均为合成资源，dispatch/model 替身，数据库锁和另一连接提交真实。cr HTTP/Turso 组合 **29 passed、1 warning，11.99 秒**。

本轮解决批量后续模型调用持有前项写锁的问题，不代表资源/作品/指派竞争、迁移或 rekey 已完成；还需双库扩大和完整门禁。V10 bz 已推进到 P0 季号持久化集成，继续使用原句柄 26134。

cs 独立 PostgreSQL 项目已清理，docker compose down 退出 0；可复跑脚本 Ruff 通过。

## V10 D3 最终验收（2026-09-20，bz）

完整单元/API bh：**3648 passed、14 skipped、6 warnings，97.75%（21349/21841）**，退出 0。完整集成 bz：**3141 passed、17 skipped、8 warnings，1669.61 秒，89.70%（19591/21841）**，测试与覆盖率汇总均退出 0。2998 个冻结文件未变；相比 bh 只有集成夹具改动，运行代码及单元/API 保持一致，因此继承 bh 完整门禁。两应用 SIGINT 后均退出 0，报告导出 `/tmp/rssripple-v10-artifacts-bz`，项目清理退出 0。

按 code-review-and-quality 复核 API 拒绝边界、清理事务/锁顺序、审阅指纹和幂等、未知季号不猜测、真实录制保留及合成身份标注；无新增依赖。27 个实现/测试/权威文档文件核对基线与最终哈希后同步本地 main 工作树，全仓 Ruff 和差异检查通过。D3 已从 pending-only TODO 删除。存量清理工具已验证，不等于已执行生产清理；生产数据库未改动。原失败 bp 证据保留，不能解释为成功轮。

机器摘要见 probes/retired-season-complete-bz-result.json。后续 V11 D4/M4/M5 仍为未验收原型，下一步先继承 V10 已验收基线并复验，再处理资源/指派并发、旧库迁移与 rekey。

V10 有效代码与权威文档已提交本地 main：`6852cf0`；尚未推送远端。V11 仅保存原型和证据。

## V11 继承 V10 与旧表约束安装原型（2026-09-20，cu–cx）

独立副本继承 main `7f8492a`（V10 代码 `6852cf0`），保留自身 18 文件；570 个非原型源码/测试/配置文件已同步，录制夹具未改。组合 cu **89 passed、1 warning，31.89 秒**，覆盖 V11 业务与 V10 退役字段/清理，全 app/scripts/tests Ruff 通过。

新增 decision_schema 安装器原型：未审核或重复键 pending 先失败并列 ID，不猜键、不修改候选/状态；只有已处理历史或已审核键时补列及 pending 唯一索引。PostgreSQL 设计为 CHECK，Turso 旧表用 INSERT/UPDATE 触发器补空键拒绝（保留未知历史列），新装模型仍有 CHECK。尚未接入启动或提供审核 apply。

cv 首次 **1 failed/1 passed** 暴露测试使用 BEGIN CONCURRENT，DDL 被真实 Turso 拒绝；改为迁移专用普通事务后 cw **1 failed/1 passed**，实际触发器正确拒绝，但抛 DatabaseError 而非 IntegrityError。cx 按实际异常语义验证 **2 passed、1 warning，退出 0**：INSERT/UPDATE 空键拒绝、同键唯一、离开 pending 后复用键、重复安装、历史列保留、未审核 pending 拒绝前后 schema/data 不变。

安装器仍须补 PostgreSQL、事务回滚、已有同名索引/约束定义检查，以及完整的审核数据迁移与启动接线；不得将空旧表升级通过宣称 D4 完成。原型现 20 文件，main 运行代码保持 V10 已验收状态。

## V11 旧库约束定义及双库 DDL 回滚（2026-09-20，cy–dc）

Turso 三项负例确认 IF NOT EXISTS 会静默接受错误同名定义：非唯一索引、仅 decided 的部分索引、空操作触发器，cy **3 failed/3 passed**。安装器现先检查列顺序、唯一属性、pending 谓词、触发器定义及 PG CHECK 定义；错误定义在任何 DDL 前被拒绝。Turso 故障注入确认安装后抛错可完整回滚新增列、索引、触发器并保留历史数据。

实际 PostgreSQL 首轮 da 失败于 asyncpg InvalidCachedStatementError：SELECT * 的结果形状跨 ADD COLUMN/回滚失效。修正为按已存在字段显式投影后 db 退出 0，验证 DDL 回滚、重复安装、INSERT/UPDATE 空键拒绝、未知历史列保留和错误索引拒绝。脚本与结果见 probes/decision_schema_pg_probe.py、decision-schema-pg-result.json；仅访问受保护的独立测试库，使用合成历史行。测试项目清理退出 0。最后 Turso dc **6 passed、1 warning，退出 0**，Ruff 通过。

安装器尚未接入 startup；已审核 pending 的数据变换/完整覆盖与键一致性校验仍待完成。空/已处理旧表的双库证据不替代混合历史决策的显式审核迁移，D4/M4/M5 不关闭。

## V11 显式审核数据迁移原型（2026-09-20，dd–de）

新增 DecisionMigration 事务档案与 apply_decision_review。执行要求 approved_fingerprint 精确匹配、supersede_pending=true；重新读取的审核指纹不匹配或仍有 unknown/missing/singleton 组则拒绝。使用当前重新计算的组，不信任用户修改的提案内容。仅旧 pending 保留原 ID 并变为 expired；历史非 pending 不变，新 pending 按等价覆盖创建且不继承缓存推荐；全部原始报告与新旧 ID 映射存入同事务档案。重复指纹返回已完成结果。

真实 Turso 旧表（无键列）合成数据：两个混合电影槽拆分并合并为两个等价选择，两个原 pending 保留，已决定历史不变；完整原始记录可从档案恢复。dd **3 passed、1 warning，1.25 秒**，包括成功/重复执行、后置故障导致新列/行变换/档案全部回滚、陈旧审核无修改拒绝。de 加上缺显式审批负例和 schema/review 回归 **17 passed、1 warning，2.48 秒**。模型/迁移权威子文档已在独立副本同步，原型现 26 文件。

该数据执行路径尚缺 PostgreSQL 验证、未知及单候选实际拒绝测试、多季/季包混合迁移、已审核键与覆盖一致性检查、CLI/启动接线及后续 rekey。数据库停写/备份仍为调用方前置条件；当前不向生产执行，不宣称 D4/M4/M5 完成。

## V11 审核迁移双库边界与离线 CLI（2026-09-20，df–di）

同一组实际数据迁移测试支持每测试独立 PG schema，固定临时账号和 loopback 地址限制；Turso df **8 passed、1 warning，2.82 秒**，实际 PostgreSQL dg **8 passed、1 warning，3.71 秒**。新增未知覆盖、缺失候选和单候选组均阻止整批执行，重新导出指纹不变、无档案写入；成功/回滚/幂等/陈旧及显式审批也在双库通过。PG 项目及 schema 已清理，退出 0。

新增离线 scripts.review_pending_decisions：只读导出不带批准字段、不覆盖已有报告；apply 要求 --writers-stopped/--backup-confirmed，在打开数据库前检查，服务仍要求报告显式指纹与 supersede_pending 审批。命令不调用应用 startup、不派发、不入队，Turso 去掉 CONCURRENT URL 参数并使用普通 BEGIN。权威迁移文档已同步命令与参数的责任边界。

CLI 首轮 dh **1 failed/8 passed**：父进程 dispose 后原生 Turso 文件锁仍使子进程无法打开。修正测试流程为独立种子进程退出后再启动 CLI，di **9 passed、1 warning，5.58 秒**，实际导出/缺确认拒绝/审核执行/另一进程幂等重跑通过。没有复制活数据库或绕过文件锁。原型现 27 文件，启动接线、多季混合覆盖、重指 rekey、剩余并发与完整门禁继续未完成。

## V11 季包/多季审核及持久键一致性（2026-09-20，dj–dm）

新增两种实际 Turso 旧表场景：单季包与 links-only 多季包，每种包含半范围 1–6 和完整范围 1–12，各两个版本，版本的关联/指派顺序相反。旧决策混合半包与整包；迁移后按真实覆盖分成两条选择，并调用生产 create_pending_decision 验证复用迁移生成的同一 ID，不另造键。单季平面 FK/季号和多季 NULL FK 均核对。dj 初轮 **2 failed/9 passed** 为旧表夹具加载 Agent 默认 selectin pending_decisions 时查询不存在的新列；改为 Agent 标量查询后 dk **11 passed、1 warning，6.26 秒**。

新增损坏键负例 dl **1 failed/6 passed**：非空 v1:wrong 与合法 scope 仍被安装器接受。提取 stored_choice_matches，按生产 choice_identity 重建版本化描述/摘要并比较；损坏或形状不匹配必须重新审核。dm 组合 **26 passed、1 warning，9.57 秒**，覆盖约束、迁移及生产创建，Ruff 通过。模型权威说明同步。

当前多季数据为合成，未声称原生产已观察混合 pending；PostgreSQL 的多季新增用例仍待复跑。下一步接入启动的安全拒绝路径，再处理作品重指 rekey 和资源并发。V11 仍为未验收原型。

## V11 启动安全接线与 PostgreSQL 多季补验（2026-09-20，dn–dp）

prepare_existing_decision_schema 在任何业务 startup/backfill 前检查旧表；旧 pending 未审核/键损坏/重复时失败，未创建表则由 ORM 新装约束处理。Turso 使用独立普通 BEGIN 的短 DDL 事务；PG 在启动 advisory lock 后、create_all 前检查。

实际 Turso CLI 进程验证：未审核启动退出 1 且重新导出指纹不变，离线审核 apply 后真正 create_tables 退出 0；连同既有迁移回归 do **58 passed、3 skipped、2 warnings，21.43 秒**。PG dn 初轮 **2 failed、8 passed、1 skipped** 发现手写旧表夹具把 decision_status 枚举建成 TEXT，生产 ORM 比较不成立；改回原枚举而非放宽实现。

PG dp **11 passed、1 skipped、1 warning，5.68 秒**：新增单季/多季迁移与生产键复用通过，实际 _create_tables_postgres 在审核前拒绝且原始指纹保持、审核后完整启动 DDL 分支成功。唯一 skip 是 Turso 专属 CLI 文件锁测试。临时项目清理退出 0，权威迁移文档同步。原型现 28 文件，仍须 rekey、资源/指派并发、既有测试夹具适配与两道完整门禁；未关闭 D4/M4/M5。

## V11 同类型作品合并 rekey 首轮（2026-09-20，dq–ds）

真实 Turso 生产 _merge_movie_group 红测 dq **1 failed，0.61 秒**：相同电影合并按旧槽位删掉一个 pending，四个候选丢为两个；旧实现同样会删碰撞的已处理历史。

同类型 series/movie 的重指函数移除 PendingDecision 碰撞删除，先锁受影响的 Agent，搬移全部关联后重算 pending 覆盖。同覆盖合并、不同覆盖拆分，稳定组保留 ID；变化旧 pending 保留为 expired，操作快照/替代关系存 DecisionMigration(operation=work_rekey)，不冒充人工审批。不带旧推荐，不自动派发；未知及 singleton 不产生可选 pending。

电影实际正例 dr **1 passed，0.60 秒**：四候选保留为一条新选择，两条人工已处理历史仍在。补后置异常回滚和重复 rekey 后，连同离线迁移 ds **13 passed、1 skipped、1 warning，8.94 秒**：失败恢复原四行且无档案，重复运行保持 pending ID/档案数量。唯一 skip 为 PG 专属启动测试。业务/模型文档在副本更新，原型现 32 文件。

尚未证明所有重指路径正确：当前受影响 Agent 发现仍按决策平面 FK，links-only 批量可能漏选；跨类型转换、P8、系列正例、实际 PG 锁竞争仍待补。不能用该电影专项关闭 M4/M5 或宣称完整 rekey 完成。

## V11 links-only 多季影响范围（2026-09-20，dt–du）

真实剧集合并负例 dt **1 failed、2 passed，1.51 秒**：S1 重复作品分别与同一 S2 组成两组候选，两个 PendingDecision 的平面 FK 均 NULL；合并后关联已重指，但仍保留旧的两条 pending，证实只查 decision.series_id 会漏选。新增夹具显式建立 WorkCollection，满足单季作品归属与唯一季槽。

lock_work_choice_agents 现在按 pending ID 游标每 100 行检查，候选资源 ID 同样每 100 个查询；同时匹配资源平面 FK、ResourceWorkLink 和 ResourceFileAssignment，找到受影响 Agent 后按固定 ID 顺序锁定。不会加载全量资源表。du 组合 **22 passed、1 skipped、1 warning，12.50 秒**：links-only 两组重建为一条包含四候选的决策，描述仅包含存活 S1/S2；电影保留历史/回滚/幂等、生产创建和离线迁移继续通过。业务说明同步。

本轮不证明跨类型/P8 或并发创建与影响范围扫描之间已完全串行化。后续先统一跨类型影响 Agent 的锁顺序，再接 rehome/cross-type/P8；D4/M4/M5 继续保留。

## V11 跨类型 rekey 验证（2026-09-20，dw/dy）

核对上一轮 dw 实际终态：13 passed、1 warning，5.68 秒。跨类型合并与定向 rehome 在修改关联之前统一发现源/目标作品涉及的 Agent，按 ID 顺序锁定；关联更新后在同一事务重建 pending 决策键，保留历史及候选档案。

新增电影转回剧集的两个边界场景：明确 S2E3 的源/目标候选合并；缺季号候选进入 unknown_coverage 档案，不猜季、不混入可派发决策。dy 组合回归 **15 passed、1 warning，6.86 秒，退出 0**，JUnit `/tmp/rssripple-v11-reverse-dy.xml`。测试使用标注合成数据、实际 Turso 数据库及生产合并服务，不能称为新增真实来源录制。dx 尝试在修正测试导入前主动终止（143），不作为通过证据。

原型与权威文档已保存到 decision-coverage-core-prototype.patch，尚未合入运行代码。下一步仍是 P8 迁移衔接、资源/关联并发修改与锁顺序的双库验证，再进行完整单元/API 和隔离集成门禁；D4/M4/M5 继续保留 TODO。

## V11 P8 新结构衔接（2026-09-20，ea–ec）

必要性：原 P8 只重指 PendingDecision.series_id，不重建持久键或按当前候选拆分。实际迁移红测 ea 为 **1 failed、1 passed，1.07 秒**：两个季各两个候选拆季后仍只有一个 pending；回滚正例通过。dz 首次失败为夹具漏填 reason，不是缺陷证据。

独立原型为 migrate_series 增加事务内决策重建：迁移前发现并锁受影响 Agent，子关联迁移完成并 flush 后重建，调用方继续拥有 commit/dry-run rollback。eb 组合 **17 passed、1 warning，7.72 秒**；ec 完整 P8 单元与 rekey **20 passed、1 warning，9.56 秒，退出 0**，JUnit `/tmp/rssripple-v11-p8-ec.xml`。既有 P8 空候选夹具改为 skipped 历史记录并新增状态保持断言；有效 pending 的两季分槽、四候选保留、旧记录过期、档案及回滚由新增测试验证。沿用既有作品标题形状，新增候选明确为合成数据，不是生产待决策快照。

当前仅验证已有键结构。下一步必须解决旧 pending_decisions 没有 key/scope 列时 P8 与审核迁移的顺序：未拆季资源不能提前通过身份审核，而现有 ORM 路径读取新列。需要真实旧表复现和离线迁移测试，不能以当前绿色结果宣称升级闭环。随后继续双库并发与完整门禁，D4/M4/M5 保留 TODO，原型不合入 main 运行代码。

## V11 无键旧表 P8 衔接（2026-09-20，ee–eg）

旧表实际复现缺 decision_key 列失败（ee）；ed 初次在测试准备的 Agent eager-load 阶段失败，不作为 P8 本体红测。实现按表列检测：有键结构继续同事务 rekey；无键结构仅使用旧字段重指决策作品引用，保留候选和状态，拆季后另行导出/批准审核，不自动批准或提前安装约束。P8 的 Agent 名称查询改为标量查询，避免带出新决策列。

扩大 ef 为 2 failed、30 passed、1 skipped：发现赋值查询结果形态回归，以及同会话审核沿用拆季前关系。修正后 eg **32 passed、1 skipped、1 warning，18.08 秒，退出 0**，JUnit `/tmp/rssripple-v11-p8-eg.xml`；跳过 PostgreSQL 专属启动测试。旧表测试确认四个混季候选在 P8 阶段原样保留、没有新增键列，随后审核产生两个正确季槽并归档原决策。数据明确为合成候选，实际 Turso 旧表及生产迁移路径。

原型及权威迁移说明已更新。尚需 PostgreSQL 旧表同场景、升级回滚、并发修改及完整门禁；D4/M4/M5 不关闭，未合入 main 运行代码。

## V11 旧表升级双库验收专项（2026-09-20，eh–ek）

在唯一隔离项目 `rssripple-v11-p8-20260920-eh` 的 PostgreSQL 16 上执行实际旧表迁移。新增同事务 SAVEPOINT 故障注入，断言拆季失败后决策指纹、四个候选资源的原作品归属/季号、作品数量恢复，随后重新拆季并审核成功。

PG eh 首轮 1 failed、11 passed、1 skipped，失败是合成夹具 parsed_at 带时区、目标列不带时区，发生在迁移前；按已有夹具约定清空无关字段后 ej **12 passed、1 skipped、1 warning，6.55 秒，退出 0**。唯一 skip 为 Turso 专用 CLI 测试。最终同代码 Turso ek **1 passed、12 deselected、1 warning，0.64 秒，退出 0**。没有修改生产配置或数据；测试数据明确为合成。容器、网络清理退出 0。机器摘要见 probes/decision-p8-upgrade-result.json。

此专项证明旧表 P8 → 重新导出 → 显式审核 → 新约束的双库衔接及 P8 回滚。尚不代替资源/链接/指派并发验证和完整测试门禁；下一轮优先补确认派发与关联修改交错事务。V11 继续为独立原型，D4/M4/M5 保留。

## V11 资源修改与派发竞争（2026-09-20，el/em）

两个真实 PG 连接复现：资格校验后、派发边界内，第二连接把候选 movie_id 清空成功，第一连接仍标记 decided。专项使用合成候选与模型/下载替身，不声称实际 RPC 完成。独立原型在最终确认校验中按资源 ID 排序取得 FOR UPDATE；AI 推荐准备保持无资源锁。红测 writer_blocked=false，补修后 writer_blocked=true，提交后同一修改成功；证明锁确实持有并释放。机器结果与可重跑脚本见 probes/decision-resource-pg-result.json、decision_resource_pg_probe.py。

确认资格、批量事务、终态保护 API 回归 em **23 passed，11.63 秒，退出 0**。隔离项目 rssripple-v11-resource-20260920-el 已清理，退出 0。尚需文件指派/链接/作品元数据并发以及跨路径锁顺序验证，后续完整门禁仍未运行；D4/M4/M5 不关闭。

## V11 文件指派竞争与电影包资格（2026-09-20，en–eq）

初次 en 在资格门禁前失败：批量覆盖 reload 使用 populate_existing 却未加载 movie 等关系，合法电影包缺元数据。补齐资格相关 eager-load 后 eo 双 PG 连接红测复现实际指派竞争：派发边界内将指派 movie_id 清空成功，决策仍 decided。

原型最终确认依次锁资源、作品链接、文件指派，各表按 ID 排序；ep 文件指派 UPDATE 被数据库 lock_timeout 阻止，确认提交后同一更新成功。模型与下载为替身、候选为合成数据，仅证明数据库边界。eq 扩展三类确认 API 的普通电影/电影包正反例及覆盖度单元 **46 passed、1 warning，17.24 秒，退出 0**。隔离项目已清理退出 0，结果与复跑脚本见 probes/decision-assignment-pg-result.json、decision_assignment_pg_probe.py。

下一步仍需关联插入/删除、作品元数据和交叉写路径锁顺序，不以已有 UPDATE 场景推断全部并发已覆盖。完整门禁未运行，D4/M4/M5 保留。

## V11 关联增删并发矩阵（2026-09-20，er）

必要性：此前仅验证已有指派 UPDATE，不能据此推断新关联插入或删除会阻塞。独立 PostgreSQL 16 项目通过实际确认函数，在下载调用边界让另一连接分别 INSERT/DELETE 文件指派、INSERT/DELETE 作品链接。四场景均观察到 SQLSTATE 55P03，确认提交后同一操作均成功；决策均 decided、派发调用一次。新增子记录由资源父行锁的 FK 检查阻止，已有记录删除由子行锁阻止。本轮无需修改运行实现。

顺序运行四场景退出 0，项目 `rssripple-v11-associations-20260920-er` 容器及网络清理退出 0。明确使用合成电影包和模型/下载替身，数据库及确认函数真实；不是实际 HTTP 编辑路径或下载器端到端证明。机器结果与复跑脚本见 probes/decision-association-matrix-pg-result.json、decision_association_matrix_pg_probe.py。

下一步检查作品元数据变化，以及实际资源编辑/作品合并的交叉锁顺序；完整门禁未执行，D4/M4/M5 继续保留，V11 不合入 main。

## V11 作品元数据竞争（2026-09-20，es–eu）

真实 PG 双连接红测：最终资格校验后清空电影 release_date 成功，第一事务仍 decided（es）。原型现在在资源/子关联锁之后，从稳定引用收集作品，按模型/主键顺序加共享行锁，再读取并锁定所属合集，随后重新加载资格证据。et 同一更新被阻止，确认提交后成功。

确认 API、批量事务、终态与审核 eu **44 passed、1 warning，21.01 秒，退出 0**。项目 `rssripple-v11-work-20260920-es` 已清理退出 0。候选与作品为合成，模型/下载调用替换，PG 连接与确认路径真实；专项只证明电影上映日期更新边界。机器结果/脚本见 probes/decision-work-pg-result.json、decision_work_pg_probe.py。

下一步需扩大剧集/合集、频道必填及订阅规则变化，同时验证实际编辑/合并路径锁顺序；本轮不宣称所有元数据竞争已解决。完整门禁仍未执行，D4/M4/M5 保留。

## V11 既有回归兼容性（2026-09-20，ev/ew）

扩大运行原有决策 API、metadata_dedup、agent_service：ev **118 passed、42 failed，66.058 秒，退出 1**。失败包括无键 pending、单候选冲突、缺少明确覆盖度却期待派发、旧去重删除行为；不能用此前专项绿测代替此扩大结果。逐项失败名称与首行错误已保存 probes/decision-expanded-compatibility-result.json。

先修正 API 夹具：明确建立合成电影作品、为两候选绑定同一身份并生成规范键；剧集序列化场景补合集、季号、上映日及人工集号证据；单候选防御测试保留单候选错误状态及 409 断言，但使用规范持久键。保留原列表/跳过/确认/真实 dispatch_download（下载器 mock）/批量/状态重置断言，没有绕过数据库约束或确认门禁。

全部原有与新增决策 API ew **58 passed，32.70 秒，退出 0**。仅说明其中 API 的 17 项旧失败已解决；metadata_dedup 3 项、agent_service 22 项尚待处理，不宣称剩余 25 项通过。下一轮从明确候选基数和覆盖度证据入手，必要时修复实现回归，不降低断言。V11 仍未通过完整门禁，D4/M4/M5 保留。

## V11 去重既有用例修订（2026-09-20，ex）

旧三项失败用例以缺失资源 ID、单候选或空候选构造无键 pending，且要求删除“冲突”决策；这既不符合新约束，也会容许候选静默丢失。改用明确合成的实际作品、资源及两候选规范决策。原 Episode/AgentWork/人工映射去重断言保持；决策改为验证两条源记录 expired 加一条四候选 pending、独立重指的新键与原候选相等、不同 Agent 的候选不串槽。

完整 metadata_dedup + decision_rekey **40 passed、1 warning，16.00 秒，退出 0**，JUnit `/tmp/rssripple-v11-dedup-ex.xml`。本轮仅修改测试数据及对应保留语义断言，未改运行实现。原 ev 去重 3 项失败已解决；Agent 服务仍有 22 项已知失败未解决，下一轮处理候选基数和批量覆盖证据。V11 未达到完整门禁，D4/M4/M5 保留。

## V11 候选基数与旧记录边界（2026-09-20，ey/ez）

必要性复核：PendingDecision 只用于至少两个不同候选，原字段/幂等/推荐跳过测试的单候选输入不再有效；同一季作品同时挂 S1/S4 也违反单季模型。正例改为两个真实资源，季号隔离用独立 S1/S4 作品，保留字段/原因/ID 幂等/候选合并断言。新增单候选及重复 ID 拒绝、数据库无决策写入的负例。

旧确认记录退役测试保留其畸形空候选输入和旧原因，但补规范键；正常冲突改为两个真实资源并断言未被误退役。旧未拆季覆盖用例改为“即使关系已加载且标题区间明确，也不能派发”，符合不猜季约束。未修改运行实现或降低基数校验。

ey 候选创建 **9 passed，3.91 秒**；最终 ez **12 passed、98 deselected、1 warning，5.10 秒，退出 0**，JUnit `/tmp/rssripple-v11-candidates-ez.xml`。原 Agent 服务 22 项失败中的 10 项已解决，余下 12 项集中于批量/links-only 覆盖；尚未再次声称全文件通过。下一轮补齐明确覆盖的正例，并保留未知覆盖拒绝。完整门禁未执行，D4/M4/M5 继续保留。

## V11 批量覆盖既有回归（2026-09-20，fa）

剩余 12 项原 Agent 失败复核：单季正例缺少范围、多季正例只填 batch_seasons 或 links，无法证明实际内容覆盖。正例补明确 1–12 区间；多季用独立季作品、links 及逐季 ResourceFileAssignment，顺序反转仍应归一。未知覆盖拒绝及修订后再运行用例保持，未给通用资源工厂自动补范围。

完整 Agent 服务文件 fa **110 passed、1 warning，39.39 秒，退出 0**，JUnit `/tmp/rssripple-v11-agent-fa.xml`。原 ev 42 项失败经 API、去重、候选基数与本轮覆盖测试分别解决；本结论不是完整单元/API 门禁。测试明确使用合成证据。权威 constraints.md 同步修正旧“季作品集合即覆盖度”定义，纳入区间与缺口。

下一步继续尚未完成的频道/订阅规则并发、实际编辑合并锁顺序与全量门禁，并同步其余权威契约及集成夹具。V11 仍为原型，不关闭 D4/M4/M5。

## V11 全范围诊断启动与规则竞争红测（2026-09-20，fb/fc）

已启动独立副本 tests/unit + tests/api 全范围诊断（--maxfail=20，尚非完整门禁），现有进程句柄 **7667**，日志 `/tmp/rssripple-v11-diagnostic-fb.log`，JUnit `/tmp/rssripple-v11-diagnostic-fb.xml`，尚无终态。冻结清单 `/tmp/rssripple-v11-diagnostic-fb-source.json` 含 521 个 Python 源文件，保存时全部哈希未变化。必须续查该句柄，不因观察超时重跑；运行期间不改副本源码。

只读审查与隔离 PG 双连接 fc 另复现两处尚未修复竞争：最终校验后 Channel.required_metadata_fields 新增字幕类型，或 AgentWork.filter_overrides 改为仅允许 4K，第二连接均可提交且第一连接仍派发、标记 decided。模型和下载替身，候选为合成，数据库真实。专项期望阻塞而失败（退出 1），不可计为通过。项目 rssripple-v11-rules-20260920-fc 已清理退出 0。脚本/机器证据见 probes/decision_rule_pg_probe.py、decision-rule-pg-result.json。

下一步：待 fb 终态后处理诊断失败，并补频道/现有订阅记录的规则稳定性及订阅增删的协作锁。Agent 现有 NO KEY UPDATE 不能阻止子订阅字段单独更新或新增；不能直接升级为 FOR UPDATE 而忽略资源修订持久请求 FK 与锁顺序。需用实际 API 交错验证修复，V11 不合入 main，D4/M4/M5 保留。

## V11 规则锁独立补修（2026-09-20，fd–ff）

原全范围 fb 句柄 7667 仍运行，为保留诊断源码，从其副本复制 `/tmp/rssripple-v11-rules-fd` 独立修改。最终确认对频道和现有订阅规则加共享锁；Agent 更新及订阅增删改 API 先取得既有 Agent NO KEY UPDATE 父锁，协调新增订阅，不升级锁强度而阻断持久请求 FK。

fe PG 五场景（频道/订阅直接 UPDATE，实际 create_work/update_work/delete_work API 函数）均在确认期间被锁阻止、提交后成功；模型/下载替身，数据合成，数据库与函数真实。项目已清理退出 0。不是完整 HTTP 并发证明，也不保证任意外部 SQL 订阅 INSERT 遵守协作锁。

fd Agent/决策 API 回归 **81 passed、3 failed，113.02 秒**，失败为旧运行记录展示用例无键/单候选夹具。独立副本补真实双候选与规范键、保留原展示断言后启动 ff 重跑：**句柄 36693**，日志 `/tmp/rssripple-v11-rules-ff.log`，JUnit `/tmp/rssripple-v11-rules-ff.xml`，尚无结果。

增量补丁为 probes/decision-rule-fix-prototype.patch（相对于冻结主原型，不可直接当作 main 补丁），前后哈希/五场景结果/进程信息见 decision-rule-fix-result.json；重跑脚本 decision_rule_api_pg_probe.py。待 fb 与 ff 终态后核验并归并，勿覆盖运行源码或重启同一测试。V11 仍未验收。

## V11 规则锁双向交错及原型整合（2026-09-20，ff/fg）

ff API 重跑终态 **84 passed，137.56 秒，退出 0**，JUnit `/tmp/rssripple-v11-rules-ff.xml`；此前 fd 的三项展示夹具失败已解决。fg 另覆盖相反顺序：实际模型等待回调中，另一连接提交频道必填、订阅过滤、实际订阅更新/删除 API 函数；四场景最终校验均拒绝、下载调用 0、决策保持 pending。该验证也证明模型准备期间这些写入不受确认锁阻塞。项目已清理退出 0。模型与下载均为替身，数据库和 API 函数真实。

比对四个增量文件的原/新 SHA-256 后，将当前原型目录切换为 `/tmp/rssripple-v11-rules-fd`，完整 decision-coverage-core-prototype.patch 已含规则补修。旧 `/tmp/rssripple-v11-decisions` 不再编辑，继续服务 fb 全范围诊断 **句柄 7667**；该诊断覆盖规则补修前快照，不能作为最终新原型门禁。当前原型没有合入 main。增量补丁保留历史，后续使用完整补丁及 state.json 目录为准。

下一步续查 fb 终态，按失败补齐其他夹具或实现；仍需资源编辑/作品合并交叉锁顺序、完整门禁与权威文档审查。D4/M4/M5 保留。

## V11 资源修订与持久请求锁顺序（2026-09-20，fh/fi）

必要性：确认持有 Agent 锁后等待资源，而真实 PATCH 先写资源再插入 AgentResourceRequest；如果父锁过强，会形成反向等待。fi 通过事件屏障让 PATCH 的实际 flush 先完成、确认实际 Agent/决策锁后才继续 request_channel_resources。编辑成功写 revision=1 请求并提交，确认随后读取 720p 并派发一次，最终 decided。两连接实际 PostgreSQL、真实 correct_parse_fields 和持久请求函数；只替换下载、缓存获取和入队唤醒。合成电影候选，10 秒只是测试失败上限，时序由事件而非睡眠决定。

fh 初次脚本 schema 导入路径错误，未进入交错；修正后 fi 退出 0。项目已清理退出 0。机器结果与可重跑脚本见 probes/decision-resource-edit-pg-result.json、decision_resource_edit_pg_probe.py。本轮无运行实现修改，验证既有 NO KEY UPDATE 与持久请求 FK 锁兼容。

fb 全范围诊断句柄 7667 仍运行，已到约 88%，出现新的失败/错误，尚无终态，必须沿用原进程。下一步先处理其报告，再补作品合并竞争与最终全量门禁；V11 仍未验收。

## V11 权威契约与验收清单同步（2026-09-20）

只修改当前原型文档：业务流程图和单季化契约统一覆盖度为作品/季号/区间，去掉“仅作品集合即覆盖”旧说法；API 契约补最终资格重校验、过期选择拒绝和批量 SAVEPOINT/外层失效处理；集成清单列明真实双库、真实数据回放与替身边界及最终覆盖率/退出/清理门禁。

重新核验 prod_works_v1.json SHA-256 为 d11651d2162ced23e8d919af0bff2d9f316e203234cc854909ba5f444a35ec32，与原捕获审计一致。559 资源、101 批量及三个已处理单候选历史决策，不可充当真实生产混合 pending 的证据。没有改写真实数据以适配断言。

旧快照 fb 诊断句柄 7667 仍在运行，约 93% 后继续推进，已有失败/错误，待最终报告逐项处理；未重启进程，也未修改其 521 个冻结源码。V11 尚未验收。

## V11 全范围诊断 fb 与夹具适配（2026-09-20）

原诊断进程已结束（退出 1）：3729 passed、9 failed、8 errors、15 skipped，1635.61 秒。521 个源码文件与冻结快照一致。该轮无覆盖率统计，不构成完整验收；失败明细见 probes/decision-full-diagnostic-fb-result.json。

当前独立原型已修复其中 3 个 Agent 夹具失败（此前 ff 轮通过）；本轮修复覆盖 helper 调用参数并移除重复指派构造，保留两组覆盖与候选集合断言，fj 轮 8 passed（3.54 秒，退出 0）。决策列表、缺失资源拒绝及 Dashboard 的决策键夹具适配后，fk 轮 9 passed、3 skipped（5.17 秒，退出 0）；跳过项不计作验证。缺失资源用例使用真实持久化的合成作品和两个不存在的候选，保留 409 与未派发断言。

仍有 10 个原诊断失败/初始化错误待处理，集中于 scheduler、dashboard_extra 和 resources 夹具；不得关闭 D4/M4/M5。后续先适配有效候选与身份，保留专门的旧数据负向用例，再执行真实录制数据回放、并发审查与完整覆盖率门禁。当前运行代码未合入 main。

## V11 剩余诊断修复与真实录制回放（2026-09-20）

scheduler、dashboard_extra、resources 的旧夹具已适配：有效决策绑定同一作品的两个不同候选，过期/未过期决策使用独立身份，分页保留刻意构造的异常单候选记录作为负向用例。fl 轮三个完整文件 **163 passed、8 skipped、1 warning，71.26 秒，退出 0**。原 fb 的 17 个失败/错误至此均有后续专项通过证据，但尚未重跑全量门禁。

新增 season_model/test_decision_coverage_captured.py，使用未经改写的真实 prod_works_v1.json 工作图和实际 Turso 数据库。加载 101 个 batch，核验同作品同季、标题范围均为 1–7 的两个录制资源：完整七条文件指派得到精确覆盖；七条指派证据不足的资源保持 unknown，并进入 Channel 确认。保留 3 条历史 decided 单候选记录，验证政策查询不写库。fm 轮 **1 passed，退出 0**，报告 /tmp/rssripple-v11-captured-fm.xml。该快照没有 pending 决策，不能作为真实待决策迁移或并发证据。

当前独立原型及新增测试已保存到 combined prototype patch。后续仍需审查跨业务锁顺序、适配集成夹具并完成 ≥95% 单元/API 与 ≥85% 完整隔离集成门禁；D4/M4/M5 保留 TODO，未合入运行代码。

## V11 集成诊断与 rekey 空槽竞态（2026-09-20）

三个集成文件 fn 诊断退出 1：46 passed、22 failed，20.75 秒。失败涉及旧单候选/无候选夹具、已退役覆盖键预期及合并后决策历史语义；详见 probes/decision-integration-diagnostic-fn-result.json。scheduler 夹具已改为持久作品和两个真实候选，fq 完整文件 7 passed，2.95 秒，退出 0；仍有 21 个 fn 失败待处理。

**必要性补证：作品合并的 pending 发现存在空槽窗口。** 两个实际 PostgreSQL 连接以事件屏障控制：合并调用 lock_work_choice_agents 返回空集合后，另一连接通过生产 create_pending_decision 创建首条决策并提交；随后完成真实 _merge_movie_group。结果 pending.movie_id 已迁至目标，但 decision_scope.work_id 仍为已删除的源作品，canonical_after_merge=false，fp 探针按预期退出 1。初次 fo 在准备数据时因时间夹具错误退出，不计入竞态证据。使用合成作品/候选，跳过 LLM，无下载 RPC。独立 Compose 项目 rssripple-v11-discovery-20260920-fo 已清理（退出 0）。探针与结果见 probes/decision_rekey_discovery_pg_probe.py、probes/decision-rekey-discovery-pg-result.json。

**修复方案待实现和论证：** 现有“扫描 pending → 锁发现的 Agent”不足以协调首条决策。需要在发现之前建立与创建路径共享的事务级身份变更协调协议，并在创建等待结束后刷新候选、重新核对 canonical scope；仅重扫或仅锁当前结果都不能排除再次插入。锁顺序必须兼容已有 Agent→decision→resource→work 确认路径，避免把资源/作品锁提前导致交叉死锁。下一轮先设计该协议，再以创建先行、合并先行、LLM 等待期间合并、回滚及 links-only 多作品四类边界建立红/绿证据。当前仅证明缺陷，未宣称修复；V11 继续禁止合入运行代码。

## V11 身份协调原型与定向绿测（2026-09-20）

已在独立原型实现 PostgreSQL 事务级 advisory 协调：创建使用共享模式，作品 rekey 在发现 pending 前使用排他模式；try-lock 失败立即抛出可重试数据库锁错误，禁止持有业务锁时等待造成反向锁序。工作合并较少，当前以全局 pending 身份变更域协调，允许不同创建事务并行，但合并期间会短暂拒绝所有创建；该吞吐取舍须继续评审。Turso 仍依赖原生写冲突与完整事务重试。创建在 LLM 之后获取协调锁及 Agent 锁，再刷新候选，核对实际 canonical scope；旧身份不能落库。

fs 实际 PG 两连接绿测退出 0：合并扫描空集合后创建遭 coordination_busy；合并完成后旧 key 遭 stale_identity；按目标作品身份重分组重试成功且 scope/FK 一致。fu 三连接核验共享创建并行、合并排他、创建排他与 rollback 释放均通过。临时项目 rssripple-v11-identity-20260920-fs 清理退出 0。原 fp 红测继续保留，不覆盖失败事实。

fr 初次回归 15 passed、2 failed，失败源于测试通过正常创建接口构造历史未知 TV 集号决策；改为显式历史数据，并补正常创建拒绝断言后，ft 为 17 passed、1 warning、8.19 秒、退出 0。保留完整 rehome/cross-type 历史合并断言。扩大 fv 回归运行中，session 56604；请继承进程而非重启。

静态核验 job_handlers 的错误路径保留 last_consumed_at 并 defer 持久请求；API 回填 result.errors 会回滚外层事务。仍需实际创建先行/模型等待期间合并、完整边界重试与 links-only 多作品竞争测试，以及完整门禁，不能把当前定向绿测当作 V11 已验收。

fv 已终止（退出 1）：158 passed、2 failed、1 skipped、1 warning，68.11 秒。失败为 test_create_pending_decision_series_no_episode 与 test_create_pending_decision_legacy_3tuple_key；下一轮核验这两项旧身份预期，不再轮询 session 56604。

## V11 身份契约适配与完整单元门禁启动（2026-09-20）

复核 fv 两项失败后，未知 TV 集号改为明确拒绝且不创建决策；三元组 key 兼容测试保留，但资源明确携带所属季号，禁止用 key 默认值替候选猜季。batch 覆盖集成改用 ORM 指派、作品、精确区间；legacy 未拆季与仅链接无指派继续拒绝。历史确认退休用例保留异常单候选负向数据，正常组绑定真实候选。

fw 单元 Agent＋集成 Agent：151 passed、3 failed，50.11 秒，退出 1；剩余三项修复后 fx 完整 Agent 集成：44 passed，9.32 秒，退出 0。fy 完整 metadata_dedup 集成：17 passed，5.90 秒，退出 0；决策历史不再删除，合并后 2/4 候选组与候选保留断言通过，原作品/订阅/映射/指派迁移断言继续保留。fn 原 22 个失败（含此前 scheduler fq）及 fv 原 2 个失败均有后续专项通过证据，不等同全量通过。

已冻结 523 个 Python 源文件到 /tmp/rssripple-v11-gate-fz（manifest /tmp/rssripple-v11-gate-fz-source.json）。初始 fz 复制规则误排除 .coveragerc，已主动终止并取得退出 143，不能作为门禁；恢复原配置后启动 ga 正式完整单元/API ≥95% 门禁，session **20821**。日志 /tmp/rssripple-v11-unit-ga.log，JUnit /tmp/rssripple-v11-unit-ga.xml，覆盖率 /tmp/rssripple-v11-unit-ga-coverage.xml。请继承该进程，不编辑冻结副本、不因观察超时重启。当前 rules-fd 原型可继续独立评审；若实现变更，ga 只证明其冻结版本。尚缺剩余并发边界和完整隔离集成验收，V11 未合入 main。

## V11 完整集成启动与身份时序补证（2026-09-20）

独立完整集成 gb 已启动：项目 rssripple-v11-complete-20260920-gb，冻结目录 /tmp/rssripple-v11-integration-gb，2967 文件 manifest 为 /tmp/rssripple-v11-integration-gb-source.json。录制 fixtures 已复制为实际文件，避免容器内解析宿主绝对符号链接。四服务健康、启动退出 0，runner session **63542**，日志 /tmp/rssripple-v11-integration-gb.log。当前必须保持项目，等 runner 终态后再 SIGINT 两个应用、确认 exit 0、合并四份 coverage ≥85%、导出 data、校验冻结文件并 down --volumes；仅启动成功不算验收。

单元/API ga session **20821** 仍在运行；冻结副本 /tmp/rssripple-v11-gate-fz 的 523 个 Python 文件及 gb 的 2967 文件本轮均复验未变。两个门禁均无终态，不因观察超时重启。

gc 新增两个实际 PostgreSQL 连接时序：①生产 create_pending_decision 已写入但未提交时，合并遭协调冲突；创建提交后以新事务重试合并，保留旧 expired 决策并生成目标作品的同候选 pending；②模型等待期间允许真实作品合并提交，模型返回后旧身份被拒绝，新事务按当前身份创建成功，旧建议不复用。候选均保留。使用合成作品/资源和受控模型返回，探针退出 0、专项项目清理退出 0；见 probes/decision_identity_orders_pg_probe.py 与 decision-identity-orders-pg-result.json。本证据为函数级实际事务、手动新事务重试，不冒充完整任务处理器自动恢复。V11 继续待验收。

## V11 links-only 多作品身份竞争补证（2026-09-20）

gd 实际 PostgreSQL 探针扩展至没有直接 series_id/movie_id、仅通过 work_links 与 file_assignments 绑定两个电影的合集。四个时序均通过：普通电影/links-only 合集各自的创建先行、模型等待期间合并；创建先行的旧 pending 保留为 expired，目标 pending 保留两候选；模型等待的旧身份拒绝，重建后不复用旧推荐。探针使用实际生产创建/合并和两个事务连接，合成资源/模型，显式新事务重试，不能代表完整任务处理器自动恢复。退出 0；项目 rssripple-v11-links-20260920-gd 已清理退出 0。结果见 probes/decision-links-identity-orders-pg-result.json。

完整单元/API ga（20821）与完整集成 gb（63542）继续运行，无终态。下一步继续继承门禁进程并补实际 job-handler 恢复路径；V11 未验收。

## V11 实际任务处理器恢复（2026-09-20）

ge 实际 PostgreSQL 专项通过：直接调用生产 _handle_run_agent，使用真实 process_resources、作品合并、请求快照/退避/确认及水位线持久化。模型等待期间另一连接完成作品合并；首轮返回 changed identity 错误，消费水位线保持原值，两条请求 attempt_count=1 且 next_attempt_at 为 +30 秒，没有 pending 决策。观测退避落库后推进测试时钟 31 秒，再调用处理器；由处理器自行重新读取/分组资源，生成目标身份的一条 pending、保留全部候选、确认删除请求、推进水位线。AgentRun 状态为 failed→pending_decisions。未替换 process_resources 或 RunResult；仅控制模型返回和请求时钟，素材为合成作品/资源。直接任务调用不等同 broker/worker 崩溃恢复。

探针 decision_job_recovery_pg_probe.py 与结果 decision-job-recovery-pg-result.json 已保存，退出 0；独立项目 rssripple-v11-job-20260920-ge 清理退出 0。完整单元 ga（20821）和完整集成 gb（63542）仍未结束，继续继承这两个进程。V11 不计作已验收。

## V11 初步质量审查（2026-09-20）

按 code-review-and-quality 复核正确性、可读性、架构、安全与性能，详见 probes/decision-quality-review-result.json。当前 app/tests/scripts Ruff 退出 0。临时副本根目录还包含早期 scratch probes，全目录 Ruff 有 42 项问题，不能宣称整个副本 lint 通过；这些临时文件不在待应用文件列表中。迁移/在线 rekey 的 scope→flat fields 投影有小段重复，最终审查需评估收敛；尚未改变运行代码以免污染在跑门禁。

当前审查为阶段记录，非批准合入：ga（20821）完整单元/API 和 gb（63542）完整集成尚未终止。确认接口、迁移指纹/归档、精确覆盖、创建协调与实际处理器重跑的专项证据不能替代完整门禁。

## V12 D6 真实录制删除红测（2026-09-20）

在独立主干副本中运行 gh：2 failed，5.63 秒，退出 1。实际 HTTP DELETE 成功后，真实录制图中一部作品的 1 条人工链接、另一部作品的 26 条人工文件指派均消失。未改写录制 fixture，未接触生产数据库；详见 V12-WORK-DELETION.md 与 probes/work-deletion-necessity-result.json。本批仅补必要性和验收证据，尚未实现。V11 ga/gb 原进程继续运行。

## V11 完整单元/API ga 通过（2026-09-20）

原 session 20821 已结束，退出 0：**3746 passed、15 skipped、6 warnings，1718.46 秒**。XML 覆盖率 **21918/22482＝97.49%**，95% 门禁通过；冻结 manifest 的 523 个 Python 文件复验无变化。JUnit /tmp/rssripple-v11-unit-ga.xml，coverage /tmp/rssripple-v11-unit-ga-coverage.xml，机器摘要 probes/decision-unit-complete-ga-result.json。此前 fz 配置错误的终止轮仍保留为无效记录，不混算。

完整隔离集成 gb session 63542 仍运行（项目 rssripple-v11-complete-20260920-gb），尚未汇总应用覆盖率、应用退出码或清理，不能关闭 D4/M4/M5 或合入 V11。D6 的后续红测仅在独立 V12 主干副本，不属于 ga 源码。

## V11 gb 完整失败收尾、语料复审与 gk 复验（2026-09-20）

gb 原进程退出 1：3139 passed、3 failed、17 skipped、8 warnings，1709.78 秒。两个应用 SIGINT 正常退出 0/0；四份 coverage 汇总退出 0，20004/22482＝88.98%，但测试失败因此本轮不通过。证据已导出 /tmp/rssripple-v11-artifacts-gb，2967 文件冻结复验未变，项目清理退出 0。结果见 probes/decision-integration-gb-result.json。

两个 misc_services 确认正例缺少精确区间/指派，已显式补齐；所有未知覆盖负例保留。真实 initial_d_franchise 失败先补 runner 的 load_batch_coverage（避免未加载关系假阴性），gi 为 61 passed、1 failed；残余差异仍仅为 confirmation.kinds。独立核对既有黄金记录：Battle Stage 3 文件 work=None，原审阅文字同样明确来源证据不足、不得绑定，故 V11 的覆盖未知确认是正确语义。仅将该确认预期 [] 改为 [batch_coverage_unknown] 并更新审阅说明；完整作品/文件指派等预期结构逐项比对未变，全部原始录制文件 byte-preserved，未删案例。gj 相关完整文件 62 passed，5.73 秒，退出 0。证据见 probes/decision-corpus-policy-review-result.json。

新冻结目录 /tmp/rssripple-v11-integration-gk 与 gb 仅差上述三个测试/预期文件，app 运行源码与 ga 已通过版本一致。项目 rssripple-v11-complete-20260920-gk 四服务健康、启动退出 0；完整 runner **session 2584**，日志 /tmp/rssripple-v11-integration-gk.log，manifest /tmp/rssripple-v11-integration-gk-source.json。当前继承此进程，不再轮询已结束的 ga/gb。仍需 runner、应用退出、coverage、导出/清理全部成功，V11 才能验收。

## V11 完整集成 gk 通过（2026-09-20）

冻结副本完整集成 **3142 passed、17 skipped、8 warnings，1699.48 秒，runner 退出 0**。两个应用 SIGINT 后均退出 0；四份覆盖率汇总退出 0，**20013/22482（89.02%）** 达到 85%。导出退出 0，报告位于 /tmp/rssripple-v11-artifacts-gk；2967 个冻结文件哈希全部不变。gb 三处失败对应夹具/加载器/审核规则修订已在本轮全量通过；原始录制图未改写。此前 ga 单元/API 97.49% 的运行代码未变化。

尚需合入前质量复核及主工作树应用，D4/M4/M5 暂不从 TODO 删除；D6 独立原型不在本次验收范围。机器证据见 probes/decision-integration-gk-result.json。

## V11 本地主干接收

54 个运行代码/测试/权威文档文件与冻结验收副本逐字节一致，应用后全仓 Ruff、git diff --check 均通过。五轴复核无阻断项，代码已提交本地 main：`11ee930`；D4/M4/M5 从 TODO 移除。gk 项目清理退出 0。未推送远端，未执行生产数据库迁移；已有 pending 数据必须依照 db-migration.md 审阅并显式应用离线迁移，不能跳过启动保护。D6 原型不在该代码提交中，下一步见 V12。

## D6 完整门禁完成（hl / hn）

单元/API hl：3769 passed、15 skipped、6 warnings，1766.25 秒，退出 0，21995/22562（97.49%）≥95%，2957 文件未变。完整集成 hn：3144 passed、17 skipped、8 warnings，1712.08 秒，runner 退出 0；两个应用退出 0，coverage 汇总退出 0，20076/22562（88.98%）≥85%；3028 文件未变，导出与项目清理退出 0，证据 /tmp/rssripple-v12-artifacts-hn。

冻结后新增离线工具由 hx 8 项联合补测、hu PG 锁专项支持，前端 hm 生产构建通过。尚需最终变更集核对与主工作树应用，D6 暂不从 TODO 删除；B7 必要性红测为下一批独立证据，不在 D6 修复范围。

## D6 本地主干接收（2026-09-20）

22 个有效实现/测试/权威文档文件完成最终复核并提交本地 main：`7c99db9`。运行 Python 与 hl/hn 冻结副本一致；后增独立工具与 hx/hu 补充证据匹配，前端生产构建通过；应用到主工作树后全仓 Ruff 和 git diff --check 通过。D6 从 pending-only TODO 删除。历史孤儿需显式离线审核清理，本轮未操作生产数据、未推送远端。下一批 B7 见 V13，仍是未修复红测。


## 决策终态守卫待办复核（2026-10-07）

必要性复核基线 `bbf9310`：原 TODO “confirm 无 pending 状态守卫”已过时。`current_choice_error` 在任何候选身份/资源校验前检查 status，确认端点持锁调用；该守卫由已验收提交 `11ee930` 引入。与最近完整验收的 `7da83a2` 比较，确认路由和校验服务无差异。本轮不新增运行实现。

旧终态测试使用不存在的候选与缺失身份，因此 409 可能由其他校验触发，不能单独证明状态守卫。本轮补 [可重跑探针](probes/decision_terminal_revalidation.py)：实际 ASGI + 每例独立 Turso 文件库，创建合法两候选身份；decided 通过首次实际确认产生（RPC 替身收到一次调用），skipped 通过实际跳过产生，expired 显式写库。三个终态各重选相同/不同候选，检查准确的 409 原因、RPC 调用数不变及独立会话重读终态不变。a **6 passed，14.12 秒，退出 0**。只在 b 测试进程绕过状态检查，其余校验仍调用原函数；b **6 failed，14.04 秒，退出 1**，六项均因错误接受为 200 而失败。负向对照证明断言敏感，不能把 b 当作运行代码失败。

录制素材仅为固定 SHA 的 prod_works_v1 资源标题，候选等价关系/身份及状态显式合成；无真实下载，不声称覆盖 PostgreSQL 或并发确认。两个进程均已终止，临时引擎/文件由 fixture 清理。原始日志、JUnit 和源码哈希见 [复核摘要](probes/decision-terminal-revalidation-result.json)。仅移除被证据推翻的旧条目，不据此宣称所有派发幂等问题已完成。

按 code-review-and-quality 五轴复核：准确性由合法输入与六项负向对照支撑；可读性明确数据/替身边界；架构和运行代码无变更；无新增外部连接、密钥或依赖；仅新增离线探针，无运行性能影响。文档与证据变更通过 diff-check 和探针 Ruff；未把专项测试替代完整门禁，V27 冻结副本及其在跑门禁保持不变。

复跑命令（仓库根目录，文件数据库测试需允许本地原生引擎运行）：

```bash
PYTHONPATH=. DATABASE_URL=sqlite+aioturso:///:memory: .venv/bin/pytest -p tests.conftest -p tests.api.conftest docs/plans/p0-and-backlog/probes/decision_terminal_revalidation.py -q
# 负向对照：同一命令前另设 DECISION_GUARD_MUTATION=1，预期 6 failed。
```
