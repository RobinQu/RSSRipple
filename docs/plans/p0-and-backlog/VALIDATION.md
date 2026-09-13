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
