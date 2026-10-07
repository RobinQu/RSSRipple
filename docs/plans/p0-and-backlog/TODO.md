# RSSRipple 深度评审待办（TODO）

> 来源：2026-09 系统设计深度评审；2026-09-12 按默认自托管 PostgreSQL＋Redis＋多 worker 场景复核。
> 编号保留作为追踪标识，**所在章节表示当前优先级**；已实现与历史证据见 [PLAN.md](PLAN.md)。
> P0/P1 与删除/整理路径为本次重点；其他未附复现的条目仍是待验证假设，不代表已全部证实。
> 本文件仅列出尚未完成的 P1–P3 项。**每条是一个复选框；完成并验证后直接删除该条**，规则见文末。
> 行号基于评审时工作树，可能随改动漂移；以现象与函数名为准。

> 2026-10-07 主干复核：当前 P0（P0-1/2/3/7/8/9）及 B9 的提交 `4c804ed` 已包含在本地 `main`；原 P0-4、P0-6 降级后的修复 `7fd8121`、`e535e1f` 也已包含。原 P0-5 仍按 P2 保留，V27 尚未验收合入，不能宣称原始 P0 编号全部关闭。最新核验基线 `b650e61`，运行代码与测试无未提交差异；工作区另有 V25 验证证据。未抓取或推送远端。详情见 [PLAN.md](PLAN.md#p0-主干复核2026-10-07)。

> B7/B4/M1/M2 已验收合入，详见 V13–V16；M3 也已通过 x/aa 完整门禁并合入 `2274768`，见 [V17](V17-WIKI-TRANSIENT-FAILURE.md)。S2 已通过 p/q 合入 `f343b7b`，S3 已通过 m/n 合入 `b0e2ce1`；S1 也已验收合入 `6bf8c1c`，下一步推进剩余问题；S1 的必要性与严格出站验证方案见 [V20](V20-OUTBOUND-POLICY.md)。候选已补 batch/LangChain/OpenRouter/Wiki 路径及真实 HTTP feed 回放；ah 单元/API 4078 项、97.11%；ak 完整集成 3280 项、88.33%，均零失败；已完成最终审查与本地合入。

## 优先级

| 级别 | 含义 |
|---|---|
| P0 | 已复现的文件丢失、未认证数据泄露、默认部署核心功能持续失效 |
| P1 | 生产默认部署下功能损坏、数据丢失/污染、安全弱点、性能悬崖 |
| P2 | 边界/一致性/可维护性缺陷，规模或异常路径下暴露 |
| P3 | 契约漂移、冗余、可读性 |

---

## P1

当前已复现的 P1 均已验收；V25 完整证据和终审见 [V25](V25-DEDUP-METADATA-PRESERVATION.md)。剩余待办按以下 P2/P3 继续复核。

## P2





### 从 P1 调整（原编号保留）

- [ ] **P1-D5 热 FK 缺索引**：`file_resources.series_id/movie_id/collection_id`、（`episodes.series_id` 已被联合唯一索引前缀覆盖，不重复添加）、
      `agent_works.*`、`pending_decisions.*`、`download_tasks.file_resource_id`、`agent_runs.agent_id`、
      `webhook_deliveries(status,next_attempt_at)` 等无 `index=True`（SQLite 不自动索引 FK）。
      **修复**：先对实际查询做 EXPLAIN/规模验证，再补缺失索引迁移，避免只据 `index=True` 判定。
      V28 已核对真实双库七表目录：episode 联合唯一前缀可用、pending 的 agent/key 仅为部分索引。16000 行明确合成规模、录制标题的 movie FK 查找在两库从全表扫描变为索引扫描，结果 16 个 ID 不变；测试项目已清理。尚未确定其余查询族和最终索引/迁移，未改运行代码，见 [V28](V28-HOT-FK-INDEXES.md)。
      d 双库 15 类查询矩阵通过；扩展分布在 PostgreSQL 通过，但 Turso 暴露无索引宽行分页正确性问题（下列独立待办）。webhook 已比较少量 pending 与大量未来重试两种分布，索引选型仍需后续验证，不能将探针通过当作迁移验收。
- [ ] **P1-D7 `lazy="selectin"` 过度加载**：`Channel.file_resources`（`app/models/channel.py:102-113`）、
      `Agent.*` 七大关系（`app/models/agent.py:75-117`）使列表/校验路径拉全量关联。
      **修复**：记录列表 SQL/关联规模，按需显式加载；异步 ORM 禁止靠隐式 `select` 懒加载兜底。
- [ ] **P1-B5 队列无重试/退避/死信**：失败即终态（`app/services/task_queue.py:264-271,610-654`），队列 API 只读。
      **修复**：先补副作用幂等，再按任务类型加有限重试/退避；DLQ 为增强，不是所有任务的修复前提。
- [ ] **P1-F1 `WorkMetadataRefreshModal` 未国际化**：整个 modal 硬编码中文
      （`frontend/src/components/WorkMetadataRefreshModal.tsx`，被 `SeriesDetail`/`MovieDetail` 使用），
      en-US 用户看到中文。**修复**：接入 `useTranslation` 并补 locale key。

### 海报缓存发布

- [ ] **P2 未引用种子缓存回收**：代码复核未找到通用 torrent 缓存回收，删除资源及取消/崩溃后可能留下未引用文件；B4 的内容摘要/attempt 路径会增加旧版本残留。先提供引用与临时文件盘点，再设计有界回收，必须保护数据库引用、在途写入和历史路径；禁止仅凭文件年龄删除。本项为生命周期缺口，尚未量化生产占用。

- [ ] **P2 海报写入失败留下残缺缓存且后续直接命中**：`download_and_cache_poster` 直接写最终路径，缓存命中只检查存在。V14 rh 在真实临时目录注入写入 4 字节后 OSError，第一次返回 None，第二次却返回残缺文件 URL（预期 62 字节）。应以同目录临时文件完整写入后原子发布，失败清理临时文件，并验证已有缓存的无效内容处理；同时避免同步磁盘写入阻塞事件循环。与 B4 的多作品元数据副作用相关，但严重度按 P2，不能将该复现扩大为已证明旧任务覆盖新元数据。 V26 已以真实录制 JPEG/本地 HTTP 复现 6 失败、1 成功对照；独立原子发布/完整性收据候选组合回归 181 项通过，权限补修及 HTTP 截断/短写/fsync/权限失败恢复后专项集成 27 项通过（i 原始退出码未知，保留报告，不代替完整门禁）；V25 已合入 `bad6742`，预组合 l 的 264 项证据保留；V26 现已正式重基，10 文件/5987 输入冻结，n/o 完整双门禁运行中，仍未合入，见 [V26](V26-POSTER-PUBLICATION.md)。

### P0-5（现 P2） `FileResource` 工作 FK 互斥 DB 约束

- [ ] 为 `series_id/movie_id/audio_work_id` 至多一非空增加 DB 约束（原评审快照 0 违规；实施前重新检查，不代表无需 DDL 迁移）。
      2026-10-07 重新扫描既有录制子集 559 条：工作 FK 违规 0，工作与 collection 合法共存 1 条；三工作 FK 字段均完整。该数据导出于 2026-09-04，不代表当前生产状态，仍按 P2 约束硬化处理。证据见 [录制预检](probes/resource-work-fk-recorded-preflight.json)。
      V27 双库直接 SQL 全组合基线 32 failed/32 passed，确认无约束；新旧库全组合及故障回滚 134 项、缺列升级/表重建 2 项、真实服务路径 12 项通过。扩大 i 225 通过/1 个假连接失败，修正后 j 全迁移文件 50 项通过；已补原生 CHECK 漂移、NOT VALID 与双库迁移写入保护 7 项；新 V25/V26 预组合 o 的全部工作 FK 与屏障升级验证 159 项通过（含两项真实表重建后并发关联保护），专用项目已清理；仍待父批验收、正式重基与最终完整门禁，未合入，见 [V27](V27-RESOURCE-WORK-FK.md)。
      **不要**包含 `collection_id` 互斥（见 [PLAN.md](PLAN.md) §3，`sync_resource_collection` 刻意共存）。
      **实现**：`app/models/file_resource.py` 加 `CheckConstraint`；PostgreSQL 用
      `ALTER TABLE ... ADD CONSTRAINT`（幂等查 `pg_constraint`）；SQLite/Turso 用
      `BEFORE INSERT/UPDATE ... WHEN <冲突> BEGIN SELECT RAISE(ABORT,...); END` 触发器（零表重建）。
      **验收**：迁移幂等测试 + 直接写入双 FK 被拒（两后端）。**同步**：更正 `file_resource.py` 过期的
      collection 互斥注释与 `docs/design/data-models.md`。


### 数据 / 持久化

- [ ] **Turso 无索引宽行倒序分页结果错误**：本地锁定 `pyturso 0.8.0rc2` 的真实 ORM 表，8192 条较宽决策按 `created_at DESC LIMIT 20` 应返回已知序号 16–35，无索引及强制扫描却返回 7904–7923；升/降序索引对照均正确。64 行及 8192 行窄记录对照正常；输入显式合成，未证明生产发生，暂按 P2。需定位引擎/排序条件、补独立预期顺序回归，并验证受影响生产查询；不能只用索引前后集合相等或单一路径索引作为全局修复结论。失败与控制证据见 [V28](V28-HOT-FK-INDEXES.md) d–k。V29 已用真实录制候选的实际 API 复现旧版本 2 失败，新版本 4 项通过；旧库升级另暴露 FTS 格式不兼容，仅重建派生索引后两次启动与六表哈希检查通过。已形成旧格式探测/事务重建候选，77 项扩展回归及 8 项含真实进程退出的升级测试通过；锁文件仅升级 pyturso。仍需正式父批重基、独立环境与完整门禁，尚未合入，见 [V29](V29-TURSO-PAGINATION.md)。
- [ ] 时间戳全为 naive `DateTime`，PG `func.now()` 受会话时区影响；与 Python `utcnow()` 混用（`app/database.py:199-203`）。
- [ ] 轻量迁移 ~1100 行无版本跟踪/down path，`additions` 追加易漏（P1-D2 即此失效），表重建手抄列易漂移。
- [ ] `search_text VARCHAR(4096)` 与无界别名拼接 writer 不一致（`app/services/work_search_events.py:64-72`）。
- [ ] `WorkExternalId` 无 FK/无 `updated_at`，删除不清（与 P1-D6 相关）。
- [ ] `AgentWork` `ondelete="SET NULL"` 与 XOR `CheckConstraint` 矛盾（`app/models/agent_work.py:14-33`）。
- [ ] `Episode.season` 与父作品 `season_number` 无约束（`app/models/episode.py:14-25`）。
- [ ] `WorkCollection` 唯一约束对 NULL `external_id` 不生效（shell 场景）；`Channel.required_metadata_fields` 可空与"强制"契约矛盾。
- [ ] `channels` 无 URL 唯一约束；`downloader_instances`/`media_server_instances` 无 name/url 唯一；
      `media_server_bindings` 无 `(server_id, prefix)` 唯一（最长前缀匹配歧义）。
- [ ] JSON 列 fresh `JSON` vs 迁移 `JSONB`、`search_text` fresh `VARCHAR(4096)` vs 迁移 `TEXT` 漂移。
- [ ] PG btree 行大小风险：`ResourceFileAssignment(file_path 1024)`、`Library(server_path 1024)` 等 CJK 多字节索引。

### 后台

- [ ] `AgentRun(status=running)` 崩溃后无回收/reaper。
- [ ] 失败 dispatch 累积重复 error `DownloadTask`（`app/services/agent_service.py:61-72,928-941`）。
- [ ] 周期任务先消费 throttle 再入队，入队失败丢整个周期（daily ~24h，`app/services/scheduler.py:274-296`）。
- [ ] interval job 仅 1s `misfire_grace_time` （实际默认 coalesce=True），事件循环阻塞即跳过 tick（`app/services/scheduler.py:43-131`）。
- [ ] `scan_since=None`/大批回填无界扫描（`app/job_handlers.py:119-137,182-205`）。
- [ ] refresh 输入无界、无 per-job timeout，4 个 slot 可被长任务独占（`app/api/v1/works.py:70-97`、`app/config.py:119-122`）。
- [ ] DB lock 重试中间件重放非幂等 handler（`app/database.py:150-181`）。
- [ ] `SubmissionGuard` 进程内、可选、验证失败烧 token（`app/services/submission_guard.py`）。
- [ ] `_WORK_METADATA_LOCKS` 无界增长（`app/services/fetch_service.py:65-80`）。
- [ ] `MemoryQueue` 状态无界、`clear()` 不清 `_active_keys`、shutdown 丢在途（`app/services/task_queue.py:190-235`）。
- [ ] 每次 fetch 载入全频道 GUID（`app/services/fetch_service.py:701-704`）；重复并发插入 `IntegrityError` 中止 job。
- [ ] SSE 批分析 `while True` 无超时、generator 异常不发 `event: error`（`app/api/v1/resources.py:1216-1246`）。

### 元数据

- [ ] 季包候选合并后不重生成 LLM pick（`app/services/agent_service.py:506-526` 死比较）。
- [ ] `ResourceMetadata.season_ambiguous` 已经 from_dict 读取，但对最终派发门禁的作用需验证，不能直接作为死字段删除（`metadata_repository.py:448`）。
- [ ] 跨源刷新未强制"内容以主源为准"（`metadata_service.py:1349-1445`）。
- [ ] 跨类型去重/别名传递闭包可能误并（`metadata_dedup.py:201-219,264-299,678-846`）。
- [ ] `_has_conflicting_identity` 忽略 series 级 source 冲突（`metadata_service.py:581-632`）。
- [ ] 电影标题兜底未归一化/无年份守卫/无歧义检查（`metadata_service.py:2037-2056`）。
- [ ] FTS 候选无 `ORDER BY` 且硬上限 30，召回无保证（`app/services/fts.py:169-183`）。
- [ ] `similarity_score` 实际为 bigram Dice/Levenshtein，与 `filter-dsl.md` thefuzz 描述不符；不能宣称普遍退化为子串匹配
      （`app/services/text_normalizer.py:139-195`、`filter-dsl.md:69`）。
- [ ] 多处整表扫描作为常规回退（`metadata_service.py:407-459`、`cluster_work_binding.py:527-533`、
      `metadata_title_index.py:96-162`、`metadata_dedup.py:615/635/693`）。
- [ ] 维基 slug id 与数字 pageid 不收敛（`metadata_source_registry.py:240-300`）。
- [ ] Douban URL 正则混淆书/影/音（`metadata_source_registry.py:143-176`）。
- [ ] `metadata_cache.title VARCHAR(512)` < 原始标题上限 1024。

### 安全 / API

- [ ] `ChannelUpdate` 未接受 `status`，PUT 提交 inactive/active 被静默忽略；补明确的暂停/恢复入口及前端交互，且须定义在途抓取的状态写回语义（本次三 worker 回归确认）。

- [ ] 500 响应回显 `str(e)`（`app/api/v1/resources.py:625-628,1081-1085`、`downloaders.py:298-301`）。
- [ ] `confirm` 决策无 `pending` 状态守卫可重复派发（`app/api/v1/decisions.py:158-224`）。
- [ ] `POST /downloaders/{id}/test` 可用存储凭证打任意 URL（`app/api/v1/downloaders.py:326-353`）。
- [ ] **产品增强/威胁模型待定，非单独漏洞结论**：`GET /volumes/dirs` 枚举任意绝对路径（`app/api/v1/volumes.py:94-129`）。
- [ ] 无安全响应头（HSTS/CSP/X-Frame-Options 等）、无 `TrustedHostMiddleware`。
- [ ] 用户 `regex` DSL 无超时，ReDoS 可占用 worker（`app/services/filter_engine.py:189-196,366-370`）。
- [ ] **产品增强/威胁模型待定，非单独漏洞结论**：webhook 无签名、允许明文 HTTP、payload 含内部绝对路径（`app/services/notify_service.py:466-485,161-207`）。
- [ ] `/openapi.json`、`/docs` 未认证（`app/main.py:208-217`、`app/middleware/auth.py:65-68`）。
- [ ] `ApiKey` 无过期/作用域/轮换；env `API_KEY` 不可撤销；`/auth/status` 用非常量时间比较。
- [ ] **产品增强/威胁模型待定，非单独漏洞结论**：单管理员模型：任何凭证即全权；外部消费者 API key 可改 LLM/源凭证、建删下载器。
- [ ] `GET /channels/{id}/field-values` 的 `%`/`_` 未转义（`resources.py:535-548`）。
- [ ] `BatchDecisionRequest.action` 未用 `Literal`；`ResourceAssociationUpdateRequest.fields` 为无类型 dict。
- [ ] `AUTH_ENABLED`/`API_KEY`/`DEV_MODE` 未写入 `.env.example`。

### 前端

- [ ] `ResourceEditWizard.tsx` 2531 行单体；`AgentDetail.tsx` 1571 行，建议拆分。
- [ ] 异步加载无取消，陈旧响应可覆盖新数据（`AgentDetail.tsx:201-311`、`Dashboard.tsx:142-143`）。
- [ ] 任务操作错误被吞（`AgentDetail.tsx:351-362`）。
- [ ] 无 catch-all/404 路由（`App.tsx:29-76`）。
- [ ] API client 无 timeout/abort/retry（`frontend/src/api/client.ts:52-97`）。
- [ ] 过滤器字段目录三处手同步（`types/index.ts:556`、`FilterBuilder.tsx:46`、`filterUtils.ts:156`）。
- [ ] 无 `jsx-a11y`，44 处 icon-only 按钮缺 `aria-label`，可点击 div 无键盘支持。
- [ ] 剪贴板回退不一致（`AgentDetail.tsx:638-651` 总报成功；`ChannelDetail.tsx:88-95` 无回退）。
- [ ] 向导服务端错误→步骤路由依赖中文字符串匹配（`ResourceEditWizard.tsx:1289-1294`）。
- [ ] 手动表单校验，保存前可跨步进入非法状态（`ResourceEditWizard.tsx:1233-1306`）。
- [ ] 401 丢弃目标地址（`client.ts:61-66`）。
- [ ] `AgentUpdate = AgentCreate` 语义错误（`types/index.ts:706`）。
- [ ] `useApi` 死代码且 `unknown[]`（`hooks/useApi.ts`）。
- [ ] `window.innerWidth` 渲染期读取，resize 不响应（`AgentDetail.tsx:1392`）。

### organize

- [ ] copy 独占临时文件的崩溃遗留回收、movedir 跨盘崩溃恢复仍缺失；普通复制异常已清理本次临时文件，不能再按旧的半成品 dst 行为描述。
- [ ] 配置变更同步重建所有 open plan（磁盘/网络 IO 阻塞请求，`organize_service.py:656-666`）。
- [ ] 可选组正则误伤 `[SubsPlease]` 类标题（`organize_template.py:167-174`）。
- [ ] op 状态回写按 `(op_type, src)` 脆弱；plan/DB 转换非原子（`organize_service.py:794-803`）。
- [ ] `_resolve_manifest` 规划期回写 `resource.torrent_file`（`organize_service.py:198-207`）。
- [ ] 移动/复制无 fsync 持久化屏障（`organize_executor.py:154-226`）。
- [ ] `schedule_auto_execute` fire-and-forget 任务无强引用（`organize_service.py:683-702`）。
- [ ] 模板 sanitizer 忽略 `\` 与 Windows 保留名（`organize_template.py:93-105`）。

---

## P3

- [ ] 离线 HTTP 集成门禁依赖外部能力而条件跳过：sn 的磁力终态重试等待 300 秒后 skip，11 项频道 LLM workflow 和 1 项 metadata search/link 因未配置提供者 skip。应使用录制输入与明确的本地提供者响应，建立不依赖公网且必跑的端到端用例；现有 API/服务组件覆盖不能替代完整串联。两项 live magnet 仍保留独立可选公网验收。逐项证据见 V14 跳过审计。

- [ ] SSE 错误格式与 `error-handling.md:60` 契约不符（无 `event: error`）：
      `app/api/v1/channels.py:438-462`、`app/api/v1/resources.py:1192-1246`。
- [ ] 未文档化错误码 `CONFLICT`/`BAD_REQUEST`/`FETCH_ERROR`/`EMPTY_FEED`/`NOT_PENDING`/`LLM_NO_PICK`（`app/main.py:150-161` 等）。
- [ ] 500 日志缺 `request_id`/user 上下文，无 request-id 机制（`app/main.py:191-196`）。
- [ ] 统一响应信封漂移：部分错误响应缺 `meta`（`resources.py:614-617,1176-1180`）；`/health` 使用文档规定的独立响应，不属漂移。
- [ ] 冗余索引：`MetadataCache` 单列索引与唯一约束重复；`SubtitleGroupMapping.normalized_key` 既 unique 又 index。
- [ ] `WorkExternalId.external_id VARCHAR(128)` 与 `TVSeries/Movie.external_id VARCHAR(100)` 不一致。
- [ ] `MetadataCache.generation` 的 `Mapped[int]` 可推导 SQL 类型；仅评估 server_default 需求；`Episode.season` 无 server_default。
- [ ] `WorkCollection.search_text` 未纳入通用 backfill，需验证独立迁移是否已覆盖；不进 FTS 是明确设计（`fts.py:604-619`）。
- [ ] `AgentSuggestion.resources`/`PendingDecision.candidates`/`AgentRun.matched_resource_ids` JSON id 列表无清理。
- [ ] `create_channel` 防重复 token 可选（`channels.py:100-107`）。
- [ ] `OTPRequest.code` 无长度/格式校验（`app/schemas/auth.py:6-9`）。
- [ ] `MetadataAgent` 冲突解决 `temperature=0.1` 非确定；`_ensure_genre` 每作品额外 LLM 调用。
- [ ] `metadata_cache` 逻辑代数切换后旧行处理与 `generation` 维护。

---

## 待办清理规则

1. 每条是一个 `- [ ]` 复选框。**完成并验证后直接删除该条**，不保留"已完成"记录；本文只保留未完成项。
2. 仅当修复改变了不变量/对外契约（API 契约、数据模型、错误码、filter 语义等）时，才在
   `docs/design/` 对应权威子文档留一行说明；纯内部修复不留痕。
3. 每个修复应附带能长期防回归的测试；**测试即长期凭证**，无需在本文保留历史。
4. 若某项被拆分为多个子任务，直接在本文件展开为新复选框，不新建文档。
5. 若某项经确认**不需要修**（如设计使然），删除该条并在必要时把结论写入对应 `docs/design/` 文档，
   不在本文保留"已否决"记录。
