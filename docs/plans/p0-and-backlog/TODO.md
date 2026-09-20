# RSSRipple 深度评审待办（TODO）

> 来源：2026-09 系统设计深度评审；2026-09-12 按默认自托管 PostgreSQL＋Redis＋多 worker 场景复核。
> 编号保留作为追踪标识，**所在章节表示当前优先级**；已实现与历史证据见 [PLAN.md](PLAN.md)。
> P0/P1 与删除/整理路径为本次重点；其他未附复现的条目仍是待验证假设，不代表已全部证实。
> 本文件仅列出尚未完成的 P1–P3 项。**每条是一个复选框；完成并验证后直接删除该条**，规则见文末。
> 行号基于评审时工作树，可能随改动漂移；以现象与函数名为准。

## 优先级

| 级别 | 含义 |
|---|---|
| P0 | 已复现的文件丢失、未认证数据泄露、默认部署核心功能持续失效 |
| P1 | 生产默认部署下功能损坏、数据丢失/污染、安全弱点、性能悬崖 |
| P2 | 边界/一致性/可维护性缺陷，规模或异常路径下暴露 |
| P3 | 契约漂移、冗余、可读性 |

---

## P1

### 从 P2 提升

- [ ] dedup survivor 取最旧行，忽略数据完整度与人工保护（`metadata_dedup.py:430-440,557-565`）。

### 数据模型 / 持久化

- [ ] **P1-D3 退役列 `number_of_seasons` 仍被写入**：`app/services/metadata_dedup.py:460-463` 与
      `POST /series`、`PUT /series/{id}` 仍接受；`is_unsplit_legacy_series` 会据陈旧值误判单季化作品（历史主库快照 1 行非空）。
      **修复**：停止写入、从 `MANUAL_EDITABLE_FIELDS` 移除；存量清理须保留真正未拆季证据。实际 API → Episode 跨季污染已复现，见 [V10 方案](V10-RETIRED-SEASON-FIELDS.md)。
- [ ] **P1-D4 `PendingDecision` 无唯一约束 + check-then-insert**：`app/models/pending_decision.py:14-16`
      仅非唯一索引；`app/services/agent_service.py:503-548` 先查后插。队列已按 agent key 去重，普通运行不因 3 worker 必然并发；直接回填/API 并发仍可竞争。
      **修复**：与 P1-M5 合并设计覆盖度感知、无 NULL 歧义的决策键，再加 pending 部分唯一索引及 SAVEPOINT。普通 nullable 联合唯一索引不能阻止重复；两个真实 PG 连接已复现同槽双提交，M4/M5 也已用实际 pipeline 复现，续接 [V11](V11-DECISION-COVERAGE.md)。
- [ ] **P1-D6 删除路径泄漏身份袋 / 丢手工映射**：`DELETE /series|/movies` 不调用
      `delete_external_ids_for_work`（遗孤行导致命中即 miss 且无法重登记）；`resource_work_links` CASCADE
      静默丢手工映射。**修复**：删除时显式清理/转移。合集删除部分已在 V7 完整验收；此条仅保留剧集/电影删除与手工映射策略，见 [V7 方案](V7-COLLECTION-INVARIANTS.md)。
### 后台执行 / 调度 / 队列

- [ ] **P1-B4 Redis consumer lease 过期致重复执行**：lease 15s/heartbeat 5s（`app/services/task_queue.py:43-44`），
      事件循环阻塞超 15s 时 RUNNING 任务被恢复重入（`:483-548`）。**修复**：已有 consumer 心跳；治理阻塞并增加执行所有权/副作用幂等，延长 lease 只能缓解，同一事件循环再加心跳无效。
- [ ] **P1-B7 水位线按 `max(created_at)` 推进跳过并发资源**：`app/job_handlers.py:151-164`。
      提交更晚但 `created_at` 更早的并发/重试资源会被永久漏处理。**修复**：设计与提交可见性一致的消费进度/补偿；自增分配顺序不等于提交顺序，宽限期只能缓解。验收包含先分配后提交的交错事务。
### 元数据匹配

- [ ] **P1-M1 默认 MetadataAgent 路径未统一人工标题映射优先级**：默认分支未走
      `fetch_and_link_metadata` 的 ChannelRawTitleMapping；Agent 已有本地匹配、回退白名单及
      repository 的 season_hint 传递，不能宣称这些均失效。统一人工映射→本地匹配→Agent 的入口和测试。
- [ ] **P1-M2 单成员合集被当作可验证单季**：`app/services/metadata_service.py:1564-1575` 在
      `len(members)==1` 时直接链接，未校验 `verified_season_count`。**修复**：单成员不等于单季证据。
- [ ] **P1-M3 维基整体故障被缓存为 `not_found`**：judge 路径硬编码 `"error": None`
      （`app/services/metadata_wiki_judge.py:481/545`），`_classify_failure` 看不到瞬态信号。
      **修复**：区分部分语言失败与所有可信路径失败，只有瞬态整体失败不缓存；成功证据不因其他源错误被丢弃。同步检查 TMDB 搜索吞异常后缓存空列表的路径。
- [ ] **P1-M4 季包覆盖键忽略集数区间**：`app/services/agent_service.py:697-706`，半季包与整季包判重复。
      **修复**：覆盖键纳入 episode 区间。
- [ ] **P1-M5 links-only 多季包共享一个决策槽**：`app/services/agent_service.py:781-804,915`，
      不同多季包塌缩为同一 `PendingDecision`，可能派发错误包。**修复**：覆盖度感知的幂等键。

### 安全

- [ ] **P1-S1 普遍 SSRF**：RSS feed / `torrent_url` / poster / webhook / media-server / downloader /
      `wigolo_base_url`/`llm_base_url` 均无出站白名单与私网拦截，且多处 `follow_redirects=True`
      （`app/clients/rss_parser.py:32`、`app/services/torrent_inspect.py:143`、`app/services/metadata_service.py:311`、
      `app/services/notify_service.py:475` 等）。恶意 feed 可在正常抓取中触发。
      **修复**：外部 feed 派生 torrent/poster URL 按不可信输入校验地址、DNS 与重定向；管理员配置的下载器/媒体服务器/webhook 必须允许明确配置的内网服务，不能一刀切禁私网。
- [ ] **P1-S2 TOTP 登录无速率限制/锁定**：`app/api/v1/auth.py:53-75`，6 位码可取窗口 3，在线暴力可行。
      **修复**：失败计数 + 退避/锁定 + 全局限流。
- [ ] **P1-S3 TOTP 密钥每次启动写日志**：`app/main.py:90-93` 打印完整 `otpauth://...secret=`。
      **修复**：停止记录 secret，并提供首次绑定方式；现有设计依赖 provisioning URI 日志，需同步更新初始化契约。

---

## P2

- [ ] `resource_cleanup._stale_unresolved_where` 忽略 work-links/collection 状态（`resource_cleanup.py:39-64`）。

- [ ] **P1-S4 CORS 通配 + 凭证且中间件顺序错**：`app/main.py:220-226` `allow_origins=["*"]` +
      `allow_credentials=True`，Starlette 反射 Origin 携带 Cookie；Auth 中间件先于 CORS 执行，预检被 401、
      401 无 CORS 头。**修复**：拆分 P2 跨域配置（Origin 白名单/中间件顺序）与 CSRF 验证；Cookie 已为 SameSite=Lax，不能直接推导任意跨站读凭证数据。


### 从 P1 调整（原编号保留）

- [ ] **P1-D5 热 FK 缺索引**：`file_resources.series_id/movie_id/collection_id`、（`episodes.series_id` 已被联合唯一索引前缀覆盖，不重复添加）、
      `agent_works.*`、`pending_decisions.*`、`download_tasks.file_resource_id`、`agent_runs.agent_id`、
      `webhook_deliveries(status,next_attempt_at)` 等无 `index=True`（SQLite 不自动索引 FK）。
      **修复**：先对实际查询做 EXPLAIN/规模验证，再补缺失索引迁移，避免只据 `index=True` 判定。
- [ ] **P1-D7 `lazy="selectin"` 过度加载**：`Channel.file_resources`（`app/models/channel.py:102-113`）、
      `Agent.*` 七大关系（`app/models/agent.py:75-117`）使列表/校验路径拉全量关联。
      **修复**：记录列表 SQL/关联规模，按需显式加载；异步 ORM 禁止靠隐式 `select` 懒加载兜底。
- [ ] **P1-B5 队列无重试/退避/死信**：失败即终态（`app/services/task_queue.py:264-271,610-654`），队列 API 只读。
      **修复**：先补副作用幂等，再按任务类型加有限重试/退避；DLQ 为增强，不是所有任务的修复前提。
- [ ] **P1-B6 `PUT /agents/{id}` 绕过 AgentWork ≤10 上限**：`app/api/v1/agents.py:426-439` 无计数校验。
      **修复**：更新路径补上限校验（或 DB 层计数约束）。
- [ ] **P1-B8 reparse 409 卡住 `confirmation_ignored_at`**：`app/api/v1/resources.py:1023-1037` 先 commit
      标记再入队，入队失败返回 409 但标记不清；仅 job `finally` 清除。**修复**：区分已有任务（通常会 finally 清理）、入队异常及崩溃恢复；按任务所有权清标记，禁止先入队后提交。
- [ ] **P1-F1 `WorkMetadataRefreshModal` 未国际化**：整个 modal 硬编码中文
      （`frontend/src/components/WorkMetadataRefreshModal.tsx`，被 `SeriesDetail`/`MovieDetail` 使用），
      en-US 用户看到中文。**修复**：接入 `useTranslation` 并补 locale key。

### P0-5（现 P2） `FileResource` 工作 FK 互斥 DB 约束

- [ ] 为 `series_id/movie_id/audio_work_id` 至多一非空增加 DB 约束（原评审快照 0 违规；实施前重新检查，不代表无需 DDL 迁移）。
      **不要**包含 `collection_id` 互斥（见 [PLAN.md](PLAN.md) §3，`sync_resource_collection` 刻意共存）。
      **实现**：`app/models/file_resource.py` 加 `CheckConstraint`；PostgreSQL 用
      `ALTER TABLE ... ADD CONSTRAINT`（幂等查 `pg_constraint`）；SQLite/Turso 用
      `BEFORE INSERT/UPDATE ... WHEN <冲突> BEGIN SELECT RAISE(ABORT,...); END` 触发器（零表重建）。
      **验收**：迁移幂等测试 + 直接写入双 FK 被拒（两后端）。**同步**：更正 `file_resource.py` 过期的
      collection 互斥注释与 `docs/design/data-models.md`。


### 数据 / 持久化

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
