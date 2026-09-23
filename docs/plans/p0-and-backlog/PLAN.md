# RSSRipple 深度评审：复核结论与 P0 修复方案

> 本文记录 2026-09 系统设计深度评审的 P0 修复范围、数据影响分析与验证证据。
> 未完成项（P1–P3）见 [TODO.md](TODO.md)。
> 后续每批的必要性论证、严格集成门禁与补验缺口见 [VALIDATION.md](VALIDATION.md)。
> 本文是修复记录与依据，**不是**权威业务契约；行为变更已同步到 `docs/design/` 对应子文档。

## 当前收尾与下次续接（2026-09-20）

本次用户要求收尾 B9 并合入本地主干，再保留后续修复入口。P0 与 V1–V4 已通过完整验收；B9 的 ab 轮完整单元/API（97.85%）与隔离集成（89.93%）也已通过，测试项目已清理，B9 已从 TODO 删除。有效实现已提交本地 main（代码提交 `4c804ed`）；最终证据见 VALIDATION.md 的 B9 收尾节。

V6 身份接地已完成完整验收：单元/API 3540 passed（97.84%），完整集成 3136 passed（89.81%），应用退出与证据导出/清理完成。原 P0-4 两项已从 TODO 删除；证据见 [V6](V6-IDENTITY-GROUNDING.md) 和 VALIDATION.md 最终验收节。

V7 合集归属已完成完整验收：单元/API 3561 passed（97.82%），完整集成 3136 passed（89.83%），应用正常退出、证据导出和项目清理均完成。原 P0-6 四条待办已从 TODO 删除；D6 剧集/电影删除仍待处理。

[V8 D1](V8-COLLECTION-SEASON-INDEX.md) 与 [V9 D2](V9-UPGRADE-FOREIGN-KEYS.md) 已完成联合 bg 验收：完整单元/API 3612 passed（97.76%），完整集成 3136 passed（89.66%），应用退出、报告导出和项目清理均完成；2891 个冻结文件未变。D1/D2 已从 pending-only TODO 删除，有效实现已提交本地 main：`6ab0548`。

[V10 D3](V10-RETIRED-SEASON-FIELDS.md) 已通过完整验收：单元/API 3648 passed（97.75%），集成 3141 passed（89.70%），应用正常退出、报告导出和项目清理完成。D3 已从 TODO 删除，27 文件有效实现已提交本地 main：`6852cf0`。V11 D4/M4/M5 已完成完整门禁并合入本地 main：`11ee930`（单元/API 3746 passed、97.49%；完整集成 3142 passed、89.02%）。三项已从 TODO 删除。V12 D6 已完成完整门禁及离线工具补充验证，代码已合入本地 main：`7c99db9`，D6 已从 TODO 删除。下一批续接 [V13 B7](V13-CONSUMPTION-PROGRESS.md)：PG 晚提交和 metadata 完成晚于消费均已复现，尚未实施消费进度修复。


所有本地 Compose 测试必须显式唯一 `-p`；完整验证须包含应用正常退出、覆盖率合并/导出和项目清理。此前失败轮保留为失败证据，不能被后续定向通过覆盖。

## 1. 评审背景

对 `app/`（后端）、`frontend/src`、`docs/design/`、数据库迁移层与部署编排做多维度评审，
产出按严重度分级的缺陷清单。P0 定义为：未认证数据泄露 / 核心功能静默失效 / 核心不变量可被击穿。

评审初期判定 6 项 P0：

| 编号 | 问题 | 结论 |
|---|---|---|
| P0-1 | SPA catch-all 路径遍历（未认证任意文件读取） | 已修复 |
| P0-2 | 资源修订端点向"死队列"入队，定向运行永不执行 | 已修复 |
| P0-3 | LLM `inferred_season` 未对照季证据校验 | 已修复 |
| P0-4 | 维基 judge / TMDB ReAct 身份未接地 | 复核为 P1，已在 V6 修复并完成验收 |
| P0-5 | `FileResource` 工作 FK 互斥无 DB 约束 | 降为 P2，待办（§3 为历史数据依据） |
| P0-6 | 剧集必属合集的创建/删除路径可产生孤儿 | 降为 P1，V7 已修复并完成验收 |

## 2. 已完成的 P0 修复

### P0-1 SPA catch-all 路径遍历

- **实现**：`app/main.py:27` 新增 `_resolve_static_file(full_path, *, base=None)`，对解析后的候选路径做
  `Path.resolve()` + `is_relative_to(root)` 包含校验，仅当是根内真实文件才返回路径。
  `app/main.py:355` 的 `serve_spa` 改用该函数；检测到含 `..` 的越界请求返回 404 JSON，正常客户端路由回退 index。
- **测试**：`tests/unit/test_main_static.py`（新增）。覆盖正常资产、index、`../`、嵌套 `..`、`/{绝对路径}`、
  缺失文件，以及路由级 `GET /%2e%2e/main.py → 404` 且响应不含源码。
- **依据**：原实现 `STATIC_DIR / full_path`，路由正则 `^/(?P<full_path>.*)$` 接受 `../`；该路径不在
  `/api/v1/*`、`/posters/*` 保护前缀内，绕过 `AuthMiddleware`（`app/middleware/auth.py:65-68`）。
  生产镜像把 `app/static` 打进（`Dockerfile:34`）。修复后越界请求不再泄文件。

### P0-2 资源修订端点死队列引用

- **实现**：`app/api/v1/resources.py:39` 顶层 `from app.services.task_queue import task_queue` 改为
  `from app.services import task_queue as task_queue_module`；5 处调用点（788/1026/1095/1110/1379）
  改为 `task_queue_module.task_queue.enqueue(...)`，运行时读模块属性。
- **测试**：更新 `tests/api/test_resources.py` 的 9 处 monkeypatch 目标；新增
  `TestReparseMetadata::test_uses_live_queue_singleton` 回归（导入后再替换 `app.services.task_queue.task_queue`，
  断言端点把任务投递给新队列）。
- **依据**：`main.py:126` 仅重绑 `_tq_mod.task_queue`，导入期已绑定的名字不会更新；
  原对象是从未 `start()` 的 `MemoryQueue`，任务永不消费且 active key 永久泄漏（后续 409）。

### P0-3 LLM 季号确定性校验

- **实现**：`app/services/metadata_agent.py:224` 新增 `_validate_inferred_season(meta)`，在生产路径
  （`:826`）与 eval 路径（`:916`）调用。规则：`content_type=tv`、`found`、`inferred_season` 非空且非 0（特典）时，
  必须落在 `matched_entity.seasons` 列表内或 `number_of_seasons` 范围内；否则清零并置 `season_ambiguous=True`。
  同时修正 `_apply_verified_season_default`：证据定季为 1 时清除 `season_ambiguous`（`:275`）。
- **测试**：`tests/unit/test_metadata_agent.py` 新增 `TestValidateInferredSeason`（8 项），
  覆盖合法季保留、幻觉季丢弃、无证据丢弃、特典 season 0 保留、丢弃后单季默认恢复等。
- **文档**：`docs/design/business-logic.md` 的 MetadataAgent 季号章节已同步。
- **依据**：`_apply_verified_season_default` 原先在 `meta.season is not None` 时直接返回，
  LLM 幻觉季号无确定性兜底，违反"季号绝不猜测"不变量。

### 原评审验证结果（历史记录，非本次统计）

- `uv run ruff check`：通过。
- `uv run pytest tests/unit -q`：**2770 passed / 3 skipped**。
- `uv run pytest tests/api -q`：**655 passed / 11 skipped**。

## 3. P0-5 / P0-6 的数据影响分析与范围修正

原评审对 PostgreSQL 主库（`rssripple-postgres-1`）提供的只读查询快照如下；2026-09-12 复核未访问生产库，不能据此断言当前数据仍相同：

| 指标 | 数值 |
|---|---|
| `file_resources` 总数 | 1609 |
| 工作 FK（series/movie/audio）同时 >1 的违规 | **0** |
| `collection_id` 与工作 FK 同时非空 | **199**（163 非 batch + 35 季包 + 1 franchise） |
| `tv_series` 总数 | 188 |
| `tv_series.collection_id IS NULL` | **0** |
| 悬挂 `collection_id`（指向缺失合集） | 0 |
| `(collection_id, season_number)` 重复 | 0 |
| `number_of_seasons` 非空（退役列） | 1 |

**P0-5 范围修正**：模型注释宣称"`collection_id` 非空则工作 FK 必须全空"与现状矛盾。
`batch_content_analysis.sync_resource_collection`（`app/services/batch_content_analysis.py:461-502`）明确在
season/multi_season 场景把关联作品的 collection 写到资源上；199 行全部指向有 series/movie 成员的合集。
因此该互斥约束**不应包含 collection 维度**，只应约束 series/movie/audio 至多一非空（历史快照 0 违规，实施前须重新扫描并验证后端迁移）。
模型注释需要更正。

**P0-6 范围修正**：主创建路径恒挂合集，历史快照无孤儿；`TVSeries.collection_id` 全部读取处对 NULL 安全
（`app/api/v1/resources.py:724`、`app/api/v1/works.py:309`、`app/services/metadata_service.py:1400/1716`、
`app/api/v1/agents.py:325-340`、`app/services/cluster_work_binding.py:343`）。真正缺陷是
`DELETE /collections`（`app/api/v1/collections.py:206-217`）把成员置 NULL 会制造孤儿，以及
`POST /series`（`app/api/v1/series.py:66-75`，前端 `seriesApi.create` 无调用点）可创建无合集剧集。
故建议以路径修复 + 幂等回填解决，DB `NOT NULL` 作为可选硬化，而非 P0 bug 修复。

## 4. 复现与后续

- 单元/API 测试：`uv run pytest tests/unit tests/api -v`。
- 迁移与集成验证必须用独立 Compose 项目名（`COMPOSE_PROJECT_NAME=<唯一名>` / `-p <唯一名>`），
  禁止触碰运行中的 `rssripple` 栈。
- 未完成项、验收标准与待办清理规则见 [TODO.md](TODO.md)。

## 5. 2026-09-12 复核与 P0-7 / P0-8 / P0-9 实施

基准：默认自托管 PostgreSQL＋Redis＋多 worker、单管理员、合法内网服务。
原清单 123 条不等于 123 个已证实缺陷；优先处理可复现文件丢失和默认部署核心功能持续失效。
原 P0-4 身份接地→P1，P0-5 FK 约束硬化→P2，P0-6 合集创建/删除路径→P1；其余升降级及范围更正在 TODO.md。

### P0-7 / P0-8：文件安全（合并实施）

- 临时目录复现：等大小异内容目标导致源删除；两个源同目标导致第二份内容丢失，原预检均通过。
- 已确认策略：完整分块比较源/目标内容，只有一致才允许 move 删源；比较前后检查文件身份、大小及修改时间。
- 同路径先检查存在与大小；同 inode 可直接确认；不同大小/异内容/读取失败/比较期间变化全部失败且保留源。
- copy 已有目标需内容一致；hardlink 已有目标需同 inode。move 仅目标存在时保留旧大小恢复判据（无法证明内容，不执行删源）。
- 优先使用 renameat2(RENAME_NOREPLACE)；不支持时普通文件用 link 原子发布、校验后删除源名称，安全链接也不支持则失败，不回退可覆盖 rename；跨盘 move/copy 写独占临时文件，完整验证后发布，最后删源。
- planner 全部操作组装后及 executor 读取旧计划后共用路径门禁：重复目标、写入其他源、路径循环及文件/目录角色冲突拒绝；保留合法正片先搬出、剩余 keep 文件后 movedir 流程。
- 完整比较在线程执行。无 REST/DB schema 变更；失败沿用 failed/PlanError/审计，禁止后续清理。

### P0-9：worker 调度对账（合并原 P1-B2）

- 每个 worker/all 进程启动对账，之后每 30 秒直接 DB→本地 APScheduler 对账；不能入共享队列或全局互斥。
- 仅列查询调度字段，分别差异更新 fetch/refresh；无变化保留 next_run_time；active/error 都调度，inactive/删除移除。
- 对账 coalesce=True、max_instances=1、misfire_grace_time=30；DB 失败保留现状，下轮重试；非法频道隔离。
- web API 不再更新本进程调度。创建频道 commit 后首次入队；启用/修改正常在下一轮对账生效，新增任务约 5 秒首次触发。
- 自动任务使用内部 scheduled 标记，执行前检查删除/停用/刷新关闭；手动抓取维持既有行为，不取消在途任务；ChannelUpdate 尚未暴露 status，暂停/恢复仅验证已提交数据库状态。
- 保留现有队列 key；不承诺 exactly-once，不扩展为通用重试/DLQ。

### 本次复核证据与后续验收

规划阶段：静态路径与季号校验 14 项通过；文件误删、目标冲突、payload 路径越界、扫描路径越卷及 nullable unique 失效均在隔离临时数据上复现。
TestClient/异步 DB 验证在受限环境挂起后中止；历史全量结果不可当作本轮结果。
本次专项验收覆盖异内容保源、目标竞争、EXDEV/复制中断、旧计划碰撞、合法 movedir；三个调度器增改停删、next_run_time 不漂移、DB 故障恢复、首次抓取后继续周期抓取。
分布式验收使用唯一 Compose 项目名；本次未访问或改动 rssripple 生产栈。

范围边界：P1 的跨进程执行所有权与规划版本一致性、P2 的断电持久化仍独立跟进。


### 实施验证（2026-09-12）

- 文件判据/原子发布共用 `app/services/organize_file_safety.py`，planner 与 executor 均调用；`tests/unit/test_organize_file_safety.py` 覆盖异内容保源、完整比较、同 inode、缺源、读失败、比较期间变化、目标竞争、复制中断和整计划冲突。
- 频道调度差异更新放在独立 `app/services/channel_schedule.py`；scheduler 启动及每轮调用。API 提交后首次入队、handler 识别旧自动任务的回归已加入。
- organize 专项单元测试：155 passed；调度专项：13 passed。相关 API/服务/调度测试第一轮 259 passed；后续新增的独立事务与旧计划用例由最终全量回归覆盖。
- organize 集成：`pytest tests/integration/organize -q --no-cov`，224 passed，1 条既有 worker 测试 coroutine warning。修正了旧测试对大小相等/独立 inode 的错误成功假设，以及不再命中当前系统调用的故障注入。
- 三 worker 容器验收：`docker-compose.scheduler-e2e.yml` + `scripts/scheduler_e2e.py`，五项 PASS；三个 worker 日志分别确认 added/updated/removed，测试结束已清理该项目容器与临时数据库。使用项目 `rssripple-p0-scheduler-20260912-a`，独立临时 PostgreSQL/Redis、本地 feed、无外部 metadata。复用本地依赖镜像并只读挂载当前源码，未做发布镜像构建。
- 验收发现 ChannelUpdate 不接受 status，PUT 会忽略该字段，已补入 P2；因此暂停/恢复用独立测试库提交状态，未虚称 API 已支持。先延长间隔、等在途任务完成，再验证停用不会被陈旧定时任务重新抓取。
- 可重复操作见 [频道调度分布式回归](../../testing/scheduler-integration.md)。完整测试命令与结果如下。

复核边界：不新增依赖或 DB/REST schema；完整内容比较增加磁盘读开销。文件发布优先 Linux renameat2，后续容器验证增加了普通文件 link 发布兼容路径（详见下节）；目录 movedir 跨盘恢复、断电 fsync、跨进程执行所有权/重规划版本竞争仍按 TODO 的 P1/P2 独立跟进。


最终门禁：

- `.venv/bin/ruff check .` 与 `git diff --check` 通过。
- `.venv/bin/python -m pytest tests/unit tests/api -q --cov=app --cov-report=term-missing:skip-covered --cov-report=xml:/tmp/rssripple-p0-coverage.xml`：**3471 passed / 14 skipped / 16 warnings**，1548.85 秒。覆盖率 **98.27%**（20548/20909 行），随后 `coverage report --fail-under=95 --format=total` 门禁通过。
- 警告包含 pytest-asyncio fixture 弃用、既有未 await coroutine 和 Turso 测试连接/线程清理问题，未将它们表述为无警告通过。受限沙箱中的异步 DB 测试曾挂起，最终全量在获准的沙箱外临时测试数据库上完成。
- P0-7 / P0-8 / P0-9 已从 TODO 移除。剩余 P1 29 条、P2 79 条、P3 13 条；原编号仅作追踪，当前级别以章节为准。较低级别中标注待验证的假设仍需逐项复现，不能把本次测试通过当作这些问题均已排除。


### P0-3 负向集成发现的持久化补修

后续补验发现：仅清空不受证据支持的 inferred_season 不足以保护 DB，缺季数证据的新作品可被旧 upsert 默认成 S1。现将 season_ambiguous 传入 upsert，仍无可靠季号时挂合集待确认；新增模型边界→真实 ReAct→持久化/缓存的四场景及重试/缓存重放验证。详见 VALIDATION.md 续轮记录。

2026-09-13 新完整单元/API 门禁通过：**3471 passed / 14 skipped / 16 warnings，1453.66 秒，覆盖率 98.27%（20554/20915 行）**，命令直接含 `--cov-fail-under=95`，退出码 0。报告 `/tmp/rssripple-v0-followup-unit.xml`、`/tmp/rssripple-v0-followup-coverage.xml`；这是补修后的独立结果，不替换上方历史记录。完整隔离集成门禁仍在运行，未宣称 V0 全部完成。后续 O3/O4 实施方案见 [V1-PATH-SAFETY.md](V1-PATH-SAFETY.md)。

### 完整容器验证发现的发布兼容问题

2026-09-13 完整集成结果为 3039 passed / 17 failed / 17 skipped；覆盖率 89.67% 通过 85% 数值门禁，但测试失败，V0 未通过。Docker 使用 amd64＋ZFS 驱动，干净进程实测 renameat2(RENAME_NOREPLACE) 返回 EINVAL，导致 14 项文件整理失败。本地宿主专项结果不能替代该环境验证。

现为普通文件增加 link 原子发布兼容路径，校验后删除源名称；目标竞争、链接不支持或文件身份变化均保留源并失败，不回退覆盖 rename。ZFS 容器完整 organize **241 passed / 5 warnings**；最新文件安全/执行器单元 **85 passed / 1 warning**。另两项 mock 用例缺少季号证据，已将成功夹具设为显式合成单季场景，并增加无 inferred/无季数时不关联的负向回归；运行历史断言改用返回的 run_id，避免秒级排序相同导致选错行。这三项 HTTP 失败及新增季号回归正在新空库复验。完整门禁仍须重跑，详细报告见 VALIDATION.md。


## V0/V1 验收完成（2026-09-13）

最新完整单元/API 3481 passed、14 skipped，覆盖率 98.12%；完整隔离集成 3088 passed、17 skipped，四份覆盖率合并 18907/21076＝89.71%。两道门禁及应用正常退出均通过，报告已导出，隔离项目已清理。V0 补验与 V1 O3/O4 已完成，TODO 已删除 O3/O4；源哈希与报告路径见 [VALIDATION.md](VALIDATION.md) 最后验收记录。V2 仍独立验证中。


## V3 续轮：通知生成失败隔离（P1-B3）

必要性已用实际链路确认：已审核猫与龙 S1E10 清单可先生成正常通知和 pending delivery；另一个 completed task 的下载器边界注入 `files=[None]` 后，原 tick 在生成快照时抛异常，已有正常 delivery 仍 pending，整理补扫也被跳过。正确终态是 done；修正早期测试误写 delivered 后，旧实现仍红（`/tmp/rssripple-v3-notify-red2.log`，1 failed，0.79 秒）。

独立副本 `/tmp/rssripple-v3-notify-work` 原型采用 task id 列表和逐任务 committed_session，保存成功通知 id 后回主会话重新加载；失败事务 rollback 后在另一事务记录 `NotificationBuildFailure`（task 唯一 FK、attempt_count、next_attempt_at、error_message、updated_at）。首次 30 秒，指数退避封顶 1800 秒；到期重试，不永久丢弃；成功删除失败记录，tick 清理与并发成功交错留下的记录。下载状态不被通知失败改写，禁止以空 payload 占住唯一通知键。

严格集成以已审核 case `f79ef2eb-02d5-42d3-80dc-70dd3c1d733b` 的标题、真实 torrent SHA/文件清单、独立 review 季集为正常输入，媒体字节明确合成。两种故障为边界坏清单及真实快照 flush 后注入异常，检查已有投递/整理不受阻、未提交写入回滚、失败状态持久化、立即下一 tick 不重试坏任务而处理新任务、时间到期且修正数据后恢复、原文件不变且无删除 RPC。加现有完整流水线 **9 passed，5.89 秒**（`/tmp/rssripple-v3-notify-pipeline.xml`）；补强来源断言后的两种新场景单独复验见 `/tmp/rssripple-v3-notify-final.xml`。

原型 [补丁](probes/notification-build-prototype.patch) 和双方哈希 `/tmp/rssripple-v3-notify-state.json` 已保存，**未同步主工作树**。仍需验证 PostgreSQL 并发失败/成功交错、原子计数和退避上限、两后端新装/升级幂等、异常记录失败及取消/删除任务边界，并同步通知/模型权威文档，完成专项与完整门禁。P1-B3 不关闭。主工作树 V2 702 个被测文件继续冻结，原两道门禁会话未重启。


### V3 重试、迁移与实际并发续验

- 独立 Turso 新测试验证 10 次错误计数与 30 秒起始、1800 秒封顶退避，到期成功清理，取消/删除任务不再生成或新增失败标记；旧库缺表→实际 create_all＋轻迁移、二次启动保留失败诊断与下一次时间、Task 删除 FK 级联。与真实清单两类故障共 **4 passed，7.00 秒**，`/tmp/rssripple-v3-boundaries-upgrade.xml`。
- 专用 PostgreSQL 项目 `rssripple-v3-notify-20260913-q`：两个真实进程各记录三次错误，最终唯一行 attempt_count=6、退避 960 秒；从缺表旧 schema 升级并重复迁移后状态不变。后续以 SQL 执行屏障暂停失败写入，让另一真实会话先提交成功通知，再完成迟到 INSERT，下一 tick 清除残留标记。此最终版已替代先前手工注入交错终态的较弱证据。[驱动](probes/notification_build_pg_probe.py) 与 [结果](probes/notification-build-pg-result.json) 已保存；PG q 项目全部清理，退出 0。
- 驱动仅允许显式 `DATABASE_URL` 指向回环地址、库名/用户/密码均为 `organize_test` 的专用临时 PG；会清空该库应用表。需在已应用本原型、含 tests 包辅助函数的无 .env 临时副本运行，不得连接业务库。媒体真实性仅由前述已审核清单测试证明；PG 数据是合成任务元数据。
- 原型补丁和哈希更新为 6 文件。扩大回归覆盖完整 organize 集成和现有通知/调度/整理服务；最初因临时副本缺 tests.api 辅助入口而收集失败，补齐后重跑，当前日志 `/tmp/rssripple-v3-organize-notify-full.log`，尚无最终结果。还需补失败标记自身写入异常、检查旧调度单测的实际断言强度、同步权威文档和完整门禁；仍未同步主工作树或关闭 P1-B3。


### V2 完整门禁失败后的实施续轮

p 轮已结束且项目已清理；具体失败及覆盖率见 VALIDATION.md。必要性来自真实库更新 500 和合集未派发，而非推测：附带重规划和 FTS outbox 消费必须使用独立事务；冲突连接必须调用 Turso 实际支持的关闭接口。已同步主工作树，新增 rollback 红测、调用方未提交数据保护、真实物理连接关闭断言，扩大回归 187 passed。s 轮重新验证完整单元/API 95% 与隔离集成 85%；最终结果前不关闭整理 TODO。

V3 原型续轮已补 PostgreSQL 失败记录落库遇任务并发删除的真实 FK 故障，后续正常任务仍生成通知；q/r 项目均已清理。强化调度测试对真实调用链和副作用的断言后，6 项定向通过；此前扩大原轮 434 passed＋2 teardown errors 仍按未通过记录。更新的原型补丁包含调度测试，尚未同步主工作树，B3 留待完整验证。


### V4 Agent 事务失败：必要性复核（仅复现，未实施）

`process_resources` 的候选循环捕获 dispatch/commit 异常后未恢复会话，循环后的旧决策清理又访问同一会话。独立临时副本用两集合成资源及真实 `download_tasks.download_dir NOT NULL` 失败复现：首组 flush 失败后，第二组和末尾清理均进入 PendingRollbackError，整轮无法返回。日志 `/tmp/rssripple-v4-agent-transaction-red.log`，**1 failed、1 warning，1.88 秒**；复现补丁 [agent-transaction-reproduction.patch](probes/agent-transaction-reproduction.patch)。这是人工故障注入＋真实数据库约束，不能称为生产数据案例。P1 保留。

修复不能仅在 except 加 rollback：rollback 会使 Agent、候选资源、AgentRun 对象失效；当前成功计数又在 autocommit 前递增，commit 失败会产生虚报。两调用方语义不同：后台 `autocommit=True` 要保留已成功组；API 回填使用调用方未提交的 Agent/规则，不能改为独立会话后读不到新增订阅，也不能隐式提交整个请求。优先论证后台按候选组独立事务、回填按 SAVEPOINT 隔离；显式区分可恢复的组失败与必须上抛的连接/提交故障，计数仅在对应事务边界成功后更新。

验收至少涵盖：第一组真实约束失败而第二组持久化；真正提交失败而非仅 mock flush；API 新 Agent 回填保留未提交字段；回滚后不触发 MissingGreenlet；已提交组不丢失、计数准确；Transmission 已接受但 DB 失败时重跑不重复下载；失败资源的水位线/重试语义明确。需要真实清单正例、Turso 与 PostgreSQL、后台 job 与 HTTP 保存链路，再跑严格完整门禁。当前只有必要性红测，不删除待办。


V3 修正基线的扩大专项已结束：434 passed、8 warnings，329.32 秒。原型进入等待 V2 s 轮结束后同步与完整门禁阶段。V4 事务方案已有独立副本实现，Agent 服务 108 passed；后台与未提交回填的约束失败均通过，尚未替代提交失败、RPC 幂等及水位线验收。主工作树 s 轮源码保持冻结。


V4 已完成实际 PostgreSQL COMMIT 失败的红/绿验证：旧实现处于 prepared state，原型回滚失败组并提交第二组，计数准确；专用 t 项目已清理。复跑脚本、结果和实现补丁见 VALIDATION.md 最新节。仍需补齐失败资源水位线：后台当前即使 errors 非空也推进增量水位线，API 回填又忽略 RunResult.errors；仅事务恢复不足以保证失败资源后续可重试，不得据现有 108 项测试关闭 P1。


V4 续轮已补后台增量失败保留水位线、API 回填内部事务错误回滚整个保存，49 项 Agent API/恢复回归通过。真实 Transmission＋已审核种子验证「RPC 接受→DB 失败→下一增量 job」复用相同 torrent ID，未下载媒体。对应补丁、原始证据和剩余边界见 VALIDATION.md。当前仍为临时原型，不将增量恢复外推为定向、窗口及回填的持久补偿已全部完成。


V2 s 轮完整集成与覆盖率已通过并清理（3092 passed，89.67%）；s 轮单元两项未提交搜索夹具问题经定向验证修正，v 轮完整重跑在冻结副本进行。V3 通知隔离及 V4 Agent 事务/失败回填原型已按哈希同步本地，权威文档同步；下一门禁验证组合版本。各项仍按待验证保留 TODO。


### V5 忙碌 Agent 定向请求：必要性已复现

实际队列和修订端点在当前作业选完资源后会丢失后续定向请求，1 failed，2.86 秒。持久请求与版本确认的方案、淘汰的替代方案及严格跨进程验收见 [V5-REQUEST-REPLAY.md](V5-REQUEST-REPLAY.md)。仍为设计/红测阶段，当前 w 轮被测源码未修改。


V2 v 轮测试已全部通过（3495 passed），但副本遗漏 `.coveragerc` 导致不满足 greenlet/thread 覆盖率配置，90.01% 未过 95% 门禁；保持未验收状态，以已包含正确配置和全部修复的 w 轮完整组合结果继续验收。V5 生命周期 7 项通过，跨进程 PostgreSQL/Redis 验证进行中。


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

V6 有效代码、测试与权威契约已提交本地 main：`7fd8121`。尚未推送远端。

后续 P1-D2 已建立 [V9 外键升级矩阵](V9-UPGRADE-FOREIGN-KEYS.md)：独立 Turso 原型已补既有列 FK，并通过 27 项 schema/带关联数据/回滚验证；PG、完整启动回归和两道完整门禁仍待完成，不关闭 D2。


## V7 最终验收（2026-09-20，au 轮）

完整集成 **3136 passed、17 skipped、8 warnings，1688.63 秒，退出 0**；两个应用 SIGINT 正常退出 0，覆盖率汇总退出 0，**19524/21735（89.83%）**。单元/API 沿用未变实现的 an 轮 **3561 passed、14 skipped，97.82%**。493 个冻结源码/配置文件全部哈希不变，JUnit failures/errors 均 0，证据导出 `/tmp/rssripple-v7-artifacts-au`，隔离项目容器、网络、卷已清理。机器结果见 [au 验收摘要](probes/collection-complete-au-result.json)。

按 code-review-and-quality 复审创建/删除/解绑事务、预加载 ORM 关系、资源映射分页、父锁顺序、启动幂等回填及跨进程竞争；无新增依赖。合集成员/身份袋/手工文件映射保留及 PostgreSQL 并发有真实数据库证据；测试数据为明确构造的关系数据，完整集成继续包含捕获标题和真实 torrent 清单，未宣称新增在线来源录制。

原 P0-6 的四条 API/回填待办已验收，从 pending-only TODO 删除。D6 仅合集删除部分完成，剧集/电影删除与人工映射策略继续保留；D1/D2 原型仍未验收。历史 ap 失败与运行中记录保留，不代表当前验收状态。

## 本地 main 合入确认（2026-09-20）

已通过 `git merge-base --is-ancestor 4c804ed main` 核实：P0 与 B9 有效代码均已包含在本地 main 的提交 `4c804ed` 中。后续 V6 身份接地为 `7fd8121`，V7 合集归属为 `e535e1f`。原 P0-5 已复核降为 P2，继续保留 TODO，不宣称完成。本次再次核验上述提交以及 V8/V9 的 `6ab0548` 均为本地 main 的祖先；核验时 HEAD 为 `fccfe2b`，工作区干净。本地 main 比 origin/main 超前 7 个提交，尚未推送远端。V10/V11 仍为未验收原型，不属于已合入的运行代码。

## 下一批必要性证据（2026-09-20，V11）

[V11 决策覆盖度](V11-DECISION-COVERAGE.md) 已复现 M4 半季/整季键冲突、M5 两组跨季包在实际 pipeline 落入同一待决策，以及 D4 两个真实 PG 连接空槽并发插入两行。原型仅包含复现与设计，无运行代码改动；隔离 PG 项目 bi 已清理。先完成 V8/V9 与 V10 验收，再依此细化覆盖描述、历史迁移和确认端点的并发契约。

## V10 D3 最终验收（2026-09-20，bz）

完整单元/API bh：**3648 passed、14 skipped、6 warnings，97.75%（21349/21841）**，退出 0。完整集成 bz：**3141 passed、17 skipped、8 warnings，1669.61 秒，89.70%（19591/21841）**，测试与覆盖率汇总均退出 0。2998 个冻结文件未变；相比 bh 只有集成夹具改动，运行代码及单元/API 保持一致，因此继承 bh 完整门禁。两应用 SIGINT 后均退出 0，报告导出 `/tmp/rssripple-v10-artifacts-bz`，项目清理退出 0。

按 code-review-and-quality 复核 API 拒绝边界、清理事务/锁顺序、审阅指纹和幂等、未知季号不猜测、真实录制保留及合成身份标注；无新增依赖。27 个实现/测试/权威文档文件核对基线与最终哈希后同步本地 main 工作树，全仓 Ruff 和差异检查通过。D3 已从 pending-only TODO 删除。存量清理工具已验证，不等于已执行生产清理；生产数据库未改动。原失败 bp 证据保留，不能解释为成功轮。

机器摘要见 probes/retired-season-complete-bz-result.json。后续 V11 D4/M4/M5 仍为未验收原型，下一步先继承 V10 已验收基线并复验，再处理资源/指派并发、旧库迁移与 rekey。

## P0 主干复核（2026-09-20）

本次按用户要求重新核对本地 `main`，核验 HEAD 为 `7f8492a`：

- 当前 P0-1/2/3/7/8/9 与 B9 的代码提交 `4c804ed` 为 `main` 的祖先。
- 原 P0-4（复核为 P1）的 V6 修复 `7fd8121`、原 P0-6（复核为 P1）的 V7 修复 `e535e1f` 均为 `main` 的祖先。
- 上述三次 `git merge-base --is-ancestor <commit> main` 均返回 0；`git diff --name-only main -- app tests` 为空，运行代码和测试没有仅停留在工作区的未提交修复。验收结果沿用各批次已完成的记录，本次只核对合入状态，没有重新运行测试。
- 原 P0-5 已降为 P2，仍未完成，继续保留 TODO。V11 尚在验证，不计入已验收代码。

因此当前级别为 P0 的问题已全部合入本地 `main`，不需要重复合并。核验时本地比 `origin/main` 超前 9 个提交；本次没有推送远端。


## P0 主干复核（2026-09-23）

按用户要求再次核验，本地分支为 `main`，最新核验 HEAD 为 `9b9a5fa`。

- `git merge-base --is-ancestor <commit> main` 对 `4c804ed`（P0-1/2/3/7/8/9 及 B9）、`7fd8121`（原 P0-4）、`e535e1f`（原 P0-6）均返回 0，修复已在主干提交历史中，无需重复合并。
- `git diff main --stat -- app tests scripts` 为空；当前工作区没有尚未提交的运行代码差异。B7 已另行验收并合入 `edfceef`；B4 也已完成验收并合入 `7781a81`（祖先检查返回 0）；M1 仍为独立原型，未计入已合入代码。
- 原 P0-5 当前为 P2，尚未完成，继续保留在 TODO；“当前 P0 已完成”不表示所有原始 P0 编号均已修复。
- 本次仅复核 Git 合入状态，测试证据沿用各批次验收记录，没有重新运行全量测试。
- 本地 `main` 领先本地记录的 `origin/main` 21 个提交；上述三个修复提交均不在该远端跟踪引用中。未 fetch 或 push，本结论仅针对本地主干，不宣称远端已同步。


## V13 B7 验收（2026-09-23）

资源消费改为事务内发布序号与 Agent 游标，覆盖迟提交、metadata 完成、历史排除与队列补偿；完整单元/API 3807 passed（97.46%），完整集成 3152 passed（89.04%），两个应用正常退出、冻结哈希一致、隔离栈已清理。完整设计与负向/恢复证据见 [V13](V13-CONSUMPTION-PROGRESS.md)。现有数据库须执行审核迁移流程，未操作生产数据库。B4 的重复执行与下载任务持久幂等仍待修复，不属于本批合入范围。

## V14 B4 最终验收与本地合入

队列 claim/恢复/完成按执行身份条件更新，下载派发具有持久预留及结果身份，metadata/通知/磁力解析/整理各自保护提交与副作用边界。th 完整单元/API 3937 passed（97.38%）；ti 完整集成 3169 passed（88.46%）。应用正常退出、四份覆盖率汇总、报告导出、隔离栈清理与冻结哈希核对均完成。最终审查见 [V14-FINAL-REVIEW.md](V14-FINAL-REVIEW.md)。已验收 67 文件逐文件哈希匹配，仓库 Ruff 通过，有效修复已提交本地 main：`7781a81`；B4 从 pending-only TODO 移除。升级必须先停旧 worker；未部署或推送。M1 原型及剩余 TODO 未完成。
