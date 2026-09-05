# 作品单季化终态设计

> **文档状态：已实施（P2–P7 全部落地）。** 本文档描述作品模型改造的终态设计，与其余子文档
> （data-models.md、business-logic.md 等）口径一致、互为权威来源；P8（生产迁移 runbook 执行）为
> 运维动作，操作序列见 [db-migration.md](db-migration.md)「作品单季化迁移」。

## 一、目标与背景

### 身份粒度分裂问题

数据源身份粒度天然分裂：系列级源（wikipedia/tmdb/imdb）一个 id 覆盖全季，逐季源（bangumi/mal/anilist/douban）每季一个条目。当前 `TVSeries` 是系列级模型（多季挂 `seasons` JSON / Episode 行下），逐季条目与系列级作品之间无 id 桥、标题兜底被精确匹配 + 年份守卫双重拦截，导致同一部剧裂成两个作品且任何自动机制无法自愈（无职转生事故：wikipedia 建的系列级作品与 bangumi 命中 501963 另建的条目互不相认）。

### 终态目标

**一个 `TVSeries` 作品 = 恰好一季；系列（IP）级关系由 `WorkCollection` 承载。** 所有数据源统一映射到「季作品」粒度：逐季源条目 1:1 对应季作品；系列级源条目身份落在合集上，季作品用合成身份 `{系列id}#s{N}`。

已确认的范围决策：

- Agent 订阅**保持作品粒度**（订阅某一季），不做合集级订阅；多季系列由各季作品逐季订阅。
- 资源修订向导随之调整：TV 资源**先关联合集、再选合集下的季作品**两级关联；多作品合集包在文件关联步骤**强制每个关联作品都有完整文件清单**。

### 自愈论据

以无职转生事故回放：wikipedia 匹配建合集 + 其下 S3 作品；bangumi 命中 501963 另建 S3 作品——两者同季同年（2026），标题聚类（繁简归一后「无职转生 第三季」可对上）+ 年份守卫（同季首播年天然一致，守卫不再误伤）在次日 04:00 去重自动合并；即便标题没聚上，两个同季作品也归同一合集，可见且可手动合并。

### 生产规模实测（决定迁移可行性）

121 series / 13 声明多季 / 实际资源跨季的仅 6 部 / 396 movies / 128 collections / 2345 episodes / 1301 resources / 9 agent_works；合集包 128 season + 28 franchise + 23 multi_season + 3 未知；多季作品上 season=NULL 的资源仅 1 条。**数据量很小，迁移脚本复杂度可控。**

## 二、核心不变量

1. `TVSeries.season_number` 新增 `INTEGER NOT NULL DEFAULT 1`；作品行 = 该 IP 的一季。`season_number=0` 表示特典/SP（沿用 Plex Specials 约定，`resource_parser.py:739-741` 已有 season=0 先例）。
2. 每部 series 作品必须属于一个 `WorkCollection`（单季作品也套一个壳合集，保证匹配/关联代码只有一条路径）；Movie 的 `collection_id` 维持可空（电影合集逻辑不变）。
3. 合集内 `(collection_id, season_number)` 应用层唯一，数据迁移收敛后加部分唯一索引（`WHERE collection_id IS NOT NULL`）。
4. 季作品的 `title_cn/title_en/original_title` 存**基础剧名**（剥季号，维持 `strip_season_from_title` 写入约定）；季限定标题变体（「无职转生 第三季」「無職転生Ⅲ …」）进 `aliases`；展示名 = 基础名 + 季后缀（合集成员 >1 时拼「 第N季」/「Season N」）。
5. 作品 `seasons` JSON / `number_of_seasons` 两列退役（季作品只有一个季），不删列，改惰性孤儿（项目既有惯例，如 path_map/plex_section）；`number_of_episodes` = 本季集数；`start_date` = 本季首播（逐季源天然给出；系列级源经 TMDB season 端点或留 NULL 待刷新）。
6. `Episode` 保留 `(series_id, season, episode)` 结构与唯一键，`season` 恒等于所属作品的 `season_number`（去规范化，全部既有查询零改动）。

## 三、身份体系

### 注册表粒度元数据

`app/services/metadata_source_registry.py` 的 `SourceSpec`（:78-139）增加 `granularity: "series" | "season" | "movie"`：

| 源 | TV 粒度 | 依据 |
|---|---|---|
| wikipedia | series | 页面通常系列级，wikitext 解析产出多季 seasons |
| tmdb | series（movie 为 movie） | `/tv/{id}` 单条目含全季 |
| imdb | series | 一个 tt 含全季 |
| bangumi | season | 一 subject = 一季（现有 `single_season_entry` 语义固化为粒度声明） |
| mal / anilist | season | 逐季条目 |
| douban | season | 剧集逐季一个 subject（电影为 movie） |

### 身份放置规则

- 逐季源 id（`bangumi:501963` 等）→ 袋在季作品上（`work_external_ids.work_type="series"` 不变）。
- 系列级源 id（`tmdb:82684`、`wikipedia:zh:8498329`、`imdb:tt…`）→ 袋在**合集**上：`work_external_ids.work_type` 放开第三个值 `"collection"`（该表无 CheckConstraint，仅 `external_ids.py:41` 的类型注解与 `find_work_by_external_id` 的查询分支要扩，无 schema 变更）。
- 季作品同时袋一个**合成季身份** `{系列级id}#s{N}`（如 `tmdb:82684#s3`），使「同一系列同一季」的重复匹配直接命中袋反查，无需标题。
- `canonicalize_external_id`（registry:253-300）增加 `#s{N}` 后缀透传规则（现状会吃掉 `/ season N` 尾巴，:278）。

### 匹配 / upsert 新流程

`create_or_update_series_from_external` 重写（metadata_service.py:772）：

1. 按注册表粒度分类 incoming 身份。
2. 逐季 id → 作品袋反查；命中即得季作品。
3. 系列级 id → 合集袋反查 → 得合集；季号来自资源解析（无季标记：合集可验证单季 → S1 作品；多季/未知 → 资源挂合集待确认，见「各子系统语义」）；按 `(collection_id, season_number)` 找成员作品，缺失则**惰性创建**（从父条目 seasons/episode_list 取该季数据建季作品；不物化未出现的季——合集详情页可仿 TMDB parts 先例按需展示未跟踪季）。
4. 袋全部落空时的标题兜底改为**两级**：基础标题（剥季）匹配合集（归一化 + aliases）；再在合集内按季号选作品；合集也没有才新建「合集 + 季作品」。现有 upsert 兜底会把「第三季」标题剥季后撞上 S1 作品（metadata_service.py:817-841），必须改为季感知。
5. web fallback（metadata_web_fallback.py）身份按其注册表粒度落袋（系列级 id 落合集袋），其余不变。

## 四、各子系统语义

### 退役与保留的机制

- **退役**：`metadata_episode_reconcile.py` 的跨季换算（`reconcile_episode`/`locate_absolute_episode`/`apply_episode_reconcile` 的绝对集号分支）；`resolve_missing_season` 由「resolve_missing_work」取代（代码保留同名别名兼容存量脚本调用）；`seasons_overwrite_allowed` 防退化 guard；`single_season_entry` 匹配期标记（变为固有粒度）；Agent 季兼容槽位（agent_service.py:703, 900-913）；`_batch_coverage_key` 的 multi_season 形态；organize `_check_multi_season_coverage`（organize_planner.py:865-922，由 `_check_multi_season_coverage_from_files` 取代——仅服务无权威文件关联的 legacy 快照）；必填字段 `season` 键与 `absolute_episode`/`episode_confidence` 键（required_fields.py:128-169）及 `tv_multi_season` 形态。
- **保留改造**：`absolute_episode` 字段保留为解析证据；绝对集号→季的定位改为沿合集成员（按 season_number 排序、各作品 `number_of_episodes` 累加）换算，仅服务于「无季标记但有绝对集号」的存量/新资源路由。
- **资源字段**：`FileResource.season` 保留（解析证据 + DSL 字段）；`batch_seasons`/`season_ranges` 保留为从关联季作品派生的冗余缓存（写入点在 links/assignments 变更处镜像，类似现有 season_ranges 派生）。
- **multi_season 包**：不再是「一个作品的多季」，而是「一个资源挂多个季作品」——落到现成的 `resource_work_links` 多作品关联（合集恰一作品镜像 FK、多作品清 FK 的既有不变量天然适配）。`batch_scope` 值保留 `multi_season`，覆盖度 key = 关联季作品 id 集合。

### Agent 订阅与派发

- `AgentWork` **不加 collection 目标态**——订阅单位仍是作品（季作品）。多季系列需要逐季订阅（已确认接受）；10 行上限不变。
- 派发判定（agent_service.py:116-118）对单集/电影逻辑不变（`resource.series_id ∈ work_by_series_id`），无需预载合集映射。
- **合集包派发扩展**：当前派发只看作品 FK——`process_resources` 对 `series_id/movie_id` 双空的资源直接进 unrecognized 桶（agent_service.py:792-794），franchise 包正是因此不派发（:822-823）。终态下 multi_season 包清 FK、仅存 links，必须扩展：合集包的范围判定与覆盖度 key 改读 `resource_work_links`（关联季作品 id 集合），单集/单季包路径不动。
- 冲突分组 key 简化为 `("series", series_id, episode)`（season 分量删除——季已编码在作品身份）；季兼容槽位逻辑（:703, :900-913）整体删除；multi_season 合集覆盖度 key 改为关联季作品 id 集合；PendingDecision 幂等键保留 season 列但恒等于作品的 season_number，哨兵 -1 不变。

### organize

- 缺集校验的期望集来源简化为「这一季作品的 number_of_episodes / Episode 行」（单季作品自身）；multi_season 包按关联季作品逐个复用单季校验。
- `{season}` 模板变量 = `work.season_number`（`Season {season:02d}` 目录层级不变，organize_template.py:41-69 零改动）；新增 `{collection}` 模板变量供目录上层（如 `{collection}/Season {season:02d}/...`）。

### 通知 payload v2

- `work.seasons` 快照删除、`episodes[]` 去 season 分量、`work` 增加 `season_number`（`collection` 摘要已存在，notify_service.py:67，变更面小）。
- payload 加 `version: 2`（`file_associations` 已有 `version: 1` 先例）。
- **破坏性契约变更**：文档注明版本字段与下游（vault-organizer）迁移指引。

### Filter DSL 与必填字段

- 资源级 `season` 保留；`series.collection`/`movie.collection` 不变；**不新增 `collection.*` 命名空间**（保持简单）。
- 必填字段：`season` 从基础必选/形态必填中移除（季号由作品身份承载）；TV 单集仍必填 `episode`；season 包必填 `episode_start/end`；多作品合集包必填 `resource_collection` 不变。

### 去重与手动合并

- 聚类键（metadata_dedup.py `_title_keys`:134-152）改为**只在同 season_number 作品间聚类**（不同季标题再像也不合并；同季不同年仍是 reboot 证据，年份守卫保留）。
- 修复现存 bug：`_repoint_series_children`/`_repoint_movie_children`/`merge_cross_type_duplicates` 三处补 `resource_work_links`/`resource_file_assignments` 的重指向（当前 dup 删除时被 FK CASCADE 静默删行，人工 manual 映射丢失；`rehome_series_as_movie`:60-88 已有正确实现可提炼共用）。
- 新增手动合并入口 `POST /works/merge`（body: `{survivor_id, duplicate_ids, confirm: true}`）：复用 `_merge_series_group`，仅限同 season_number，人确认即绕过年份守卫；前端在合集详情页提供「合并作品」按钮。这同时是「守卫误挡」场景的常规修复工具。

### 资源修订向导（TV 两级关联 + 合集文件清单完整性）

**两级关联（TV）**：`PUT /resources/{id}/associations` 与向导 UI 重构——

- TV 资源第一步先关联 **collection**（可选已有合集 / 新建合集），第二步在该合集成员中选择/新建季作品；提交结构变为 `{collection_id, works: [...], assignments: [...], ...}`，服务端校验「作品必须是该合集成员」（`work.collection_id == collection_id`，不一致 422）。
- 向导的作品搜索框默认按合集分组展示；选择系列级搜索结果时自动创建/复用合集。
- `POST /resources/{id}/analyze-batch` 的 LLM 建议同样输出合集→季作品两级结构。

**合集文件清单完整性（多作品资源强制）**：对 `resource_work_links` 关联 >1 作品的资源（multi_season/franchise），文件关联步骤新增服务端校验：

- 每个关联作品**至少一条 assignment**（不允许作品挂着但没有任何文件指派）；
- 该资源的全部文件都被指派（不允许残留双 FK 空的未指派文件——除非显式标记为「不指派」）；
- 每个作品的指派区间完整覆盖其应有集数（对齐 organize 缺集语义：显式 episode_start/end → 作品 number_of_episodes → 文件清单推导；断档给 warning、整段缺失 422）；
- 不满足时 PUT 422 并在响应里逐作品列出缺口，向导 UI 逐步引导补齐。

### 前端（除向导外）

- 作品列表默认按合集分组浏览（合集已是 `/works?view=collections` 浏览模式）；作品详情页为单季视图（季切换器跳转同合集兄弟作品）；作品编辑页移除 `number_of_seasons`、季号只读（身份属性）。
- Agent 编辑器的作品选择器不变（仍选作品=季），仅在 UI 文案上按合集分组展示候选项。

## 五、迁移方案

### Schema 轻迁移

按既有 `_apply_light_migrations` 约定（app/database.py:332-1372；`additions` 列表 + `_best_effort` SAVEPOINT + `app_settings` sentinel 一次性数据修补）：

1. `tv_series.season_number INTEGER NOT NULL DEFAULT 1`。
2. `work_collections` 加列：`aliases JSON`、`search_text VARCHAR(4096)`、`manually_edited_fields JSON`（合集升级为系列级元数据载体；rating/genre/is_anime 不上移——逐季源天然按季给这些值，保留在季作品上）。
3. 索引：`ix_tv_series_collection_season`（部分唯一索引留待数据迁移收敛后由迁移脚本创建，轻迁移不建）。
4. 作品 `seasons`/`number_of_seasons` 两列不删，改惰性孤儿。

### 数据迁移脚本 `scripts/season_split_migration.py`

遵循脚本约定：dry-run 默认 + `--apply`、`--limit`、BATCH commit、结尾汇总行；docstring 注明「跑前停 app（Turso 独占锁/防并发写）」。dry-run 输出完整动作清单（每部作品：建合集、拆几季、各季资源/集数/订阅去向）供人工核对后再 apply。

**步骤**（每部 TVSeries 按 created_at 升序处理）：

1. **建/取合集**：作品已有 `collection_id` → 沿用；否则按（wikidata QID / 系列级身份）查合集袋，再按归一化基础标题 get-or-create（`external_source="series_group"`，`external_id` NULL，franchise_service.py:117-141 同款标题幂等键）。
2. **身份搬家**：作品身份袋中的系列级 id（按注册表粒度判定）**重指向**到合集袋（袋的 `UniqueConstraint(source, external_id)` 使双袋共存物理上不可能，行移动而非复制）；`external_id/external_source` 主列 creator-wins 不动（保留兼容期主列查找通道），季作品另袋合成身份 `{主id}#s{N}`。
3. **判定季集合**：`seasons` JSON ∪ Episode 行 ∪ 资源 season 值；为空或仅 {1} → 单季路径：置 `season_number=1`、挂合集、完成。seasons JSON 条目即使 `episode_count` 为 NULL 也贡献其季号（声明即证据）；`(collection_id, season_number)` 冲突守卫：单季/跳过路径遇到合集该季槽位已被占（legacy franchise 合集常把多部单季作品收在同一合集）时，不制造重复成员——改挂新建的壳合集（失败安全，作为手动合并候选留在组织层）。
4. **多季拆分**：最小季（通常 S1）复用原行（`season_number` 置为该季，`seasons` 列截取对应项，`number_of_episodes` 改该季集数）；其余季各建新作品行（复制 rating/genre/is_anime/status/poster/description，`start_date` 仅锚点季保留原值、其余 NULL 待刷新，aliases 注入季限定变体），袋合成身份 `{主id}#s{N}`。**逐季源身份归属**（P6 增强）：拆分后袋中逐季源 id（bangumi/mal/anilist/douban）默认随原行留在锚点季；若该作品全部非合集资源的 season 一致且 ≠1（无职转生形态：合并行实为 S3），逐季 id 移到资源证据指向的季作品——否则同一条目的后续匹配无法袋命中正确的季作品。合成身份与不确定情形不动。**季首播日离线推导**：拆分后各季作品 `start_date` 为 NULL 时，用本季 Episode 行的最早 `air_date` 补齐（无证据仍 NULL 待刷新）——避免 Channel 必选字段 `year` 门禁拦下该季资源。同一 IP 多条 legacy 行坍缩进同一合集时，后处理行的季被既有成员吸收、冗余行合并删除（不违反 `(collection_id, season_number)` 唯一性）；脚本幂等，重跑跳过已迁移作品。
5. **子表重指向**（按 season 分量路由）：`episodes`（按 season）、`file_resources`（按 season；`season=NULL` 且有 `absolute_episode` → 沿合集成员换算定位；仍无法定位 → 清作品 FK 挂 `collection_id` + 落 Channel 待确认，生产实测仅 1 条）、`resource_file_assignments`（按 season）、`resource_work_links`（按其 resource 的 season）、`pending_decisions`（按 season）、`channel_raw_title_mappings`（从 raw_title/search_title_key 解析季号，解析不出 → S1）。
6. **AgentWork 按下载历史重指向**：订阅保持作品粒度。锚定在原行（最小季）的订阅，若该 Agent 的已完成下载集中在合集内另一季作品，则重指向到该季作品（完成数最多者优先，打平取最近完成时间、再取较高季；无历史则留在锚点）；被重指向离开的季不再出现在该 Agent 的补订阅建议中。runbook 列出受影响 Agent 与「建议补订阅的季作品」清单（未被该 Agent 订阅覆盖的季）。
7. **收尾**：对触碰行跑 `backfill_search_text`（绕过 ORM 钩子的写入必须显式补）；Turso 后端等 FTS drain/对账自动收敛；创建部分唯一索引；打印校验计数（见下）。

### 安全与验证

- **前置**：完整备份（PG `pg_dump` / Turso 停 app 后复制 db 文件+wal）；脚本单向不可逆，文档明示。
- **校验脚本** `scripts/verify_season_split.py`（仿 verify_search_parity，只读、可随时运行）：①无悬空 FK（links/assignments/episodes/agent_works/pending_decisions/file_resources/collection_id/身份袋全量 join 检查）；②行数守恒（迁移前 `--write-snapshot counts.json` 抓快照，迁移后 `--snapshot` 对比——必须守恒的表（file_resources/agent_works/pending_decisions/channel_raw_title_mappings/movies）任何差异即失败，可合法增长（tv_series/work_collections/work_external_ids）或因碰撞去重收缩（episodes/links/assignments）的表只报 delta）；③每部作品 season_number 与合集成员一致性（每部 TVSeries 必属合集、`(collection_id, season_number)` 唯一、`Episode.season` 恒等于作品 season_number）；④search_text 无空值（tv_series/movies/work_collections）；⑤降级版派发等价检查（每条已匹配资源必须挂载在作品/合集/links 之一上，默认 warning、`--strict` 升级为失败）。退出码 0=全部通过。
- **Docker 操作序列**（写入 db-migration.md）：`docker compose stop app worker` → 备份 → `run --rm app python scripts/season_split_migration.py`（dry-run）→ `--apply` → verify → up。

## 六、测试策略

### 导出脚本 `scripts/export_work_fixture.py`（先于实现落地）

仿 `scripts/subtitle_groups_eval.py:163-165` 的「DB→JSON fixture」先例，只读生产库导出：

- **选集**（`--auto` 确定性选取 + `--series-id` 手动追加）：全部 6 部「资源实际跨季」的作品（含无职转生 303bca1f——本次事故的合并后形态，含其 bangumi 袋 id，是最有价值的回归样本）；全部 23 条 multi_season + 28 条 franchise + 抽样 20 条 season 包所属作品；电影侧 10 部有合集 + 10 部无合集；3 条 batch_scope 未知的包。
- **导出对象图**（保持原 UUID，fixture 库为空库无冲突）：channels（被引用的）、work_collections、tv_series、movies、work_external_ids、episodes、file_resources、resource_work_links、resource_file_assignments、agents + agent_works（被引用的）、pending_decisions、metadata_cache（按导出资源的 raw title 过滤——使匹配测试无需 LLM 即可确定性重放）、download_notifications（已完成任务的快照，供 organize 测试用）。
- 输出 `tests/fixtures/prod_works_v1.json`（JSON 入库，随仓库走；单文件预计 <5MB）。

### fixture 加载与集成验证（`tests/integration/season_model/`）

- conftest 复用 tests/unit/conftest.py 的 per-test Turso 文件库机制，加载器**走 ORM 插入**（触发 before_flush 钩子，search_text/FTS 正常维护），禁用调度器/队列（tests/api/conftest.py 的 mock 先例）。
- 验证流程：
  1. **迁移不变量**：加载 fixture → 进程内调迁移核心函数 → 跑全部校验断言；重点断言 6 部多季作品拆分后资源/集数/订阅守恒、season=NULL 资源的合集停泊、无职转生样本的 bangumi 身份落位。
  2. **匹配收敛回归**：用 fixture 中的 metadata_cache 重放——同一剧集的 wikipedia 路径与 bangumi 路径资源收敛到同一季作品（或经一次去重后合并），直接防本次事故复现。
  3. **派发/organize 等价性**：迁移前后对同一批资源跑 Agent dry-run 与 organize 规划，断言决策集合与目标路径（含 `Season NN` 目录）一致。
  4. **向导校验**：多作品合集资源 PUT associations 的完整性校验用例（缺作品指派 422、断档 warning、两级关联不一致 422）。
  5. 进入 ci-strict 集成 job（unit/api 覆盖率门禁不变）。

## 七、实施阶段拆分

| 阶段 | 内容 | 产出 |
|---|---|---|
| P0 | 导出脚本 + 抓取生产 fixture | `scripts/export_work_fixture.py`、`tests/fixtures/prod_works_v1.json` |
| P1 | 设计文档定稿（本文档 + 各子文档修订 + AGENTS.md） | 文档先行评审 |
| P2 | schema 轻迁移 + 模型层（season_number、合集加列、身份袋 collection 类型） | 双后端启动无损 |
| P3 | 匹配/upsert 重写（注册表粒度、两级标题兜底、惰性建季作品、reconcile 退役/改造） | 单测覆盖 |
| P4 | 派发/organize/通知/必填字段/去重（含 links/assignments bug 修复 + `POST /works/merge`）+ associations 两级结构与完整性校验 | 单测 + API 测试 |
| P5 | 迁移脚本 + verify 脚本 | dry-run 在 fixture 库与生产库各跑一遍核对 |
| P6 | 集成验证（六节全部用例进 ci-strict） | 门禁通过 |
| P7 | 前端改造（合集分组浏览、单季详情、向导重构） | eslint + tsc -b + midscene 主流程 |
| P8 | 生产迁移 runbook 执行（备份→dry-run→apply→verify） | db-migration.md 已含步骤 |

依赖关系：P0 可立即开始（只读，不依赖任何代码变更）；P1 与 P0 并行；P2→P5 串行；P6 是 P5 的准出门禁；P7 可在 P3 完成后并行开始。

## 八、主要风险与已接受的权衡

1. **通知 payload v2 是破坏性契约变更**——外部 webhook 消费者（vault-organizer）需同步升级；文档与版本字段先行。
2. **两级标题兜底的误分组风险**（把季作品挂错合集）——失败安全（组织层可见可改），配合手动合并入口兜底。
3. **无季标记资源的路由**从「挂系列级作品」变为「挂合集待确认」，待确认量可能上升——靠绝对集号沿合集换算 + 合集单季证据缓解。
4. **逐季订阅**：多季系列每季需手动添加订阅（已确认接受）；10 行上限对多季系列更紧，后续若成为痛点再评估合集级订阅（届时 agent_works 加第三态即可，向前兼容）。
5. douban 粒度标记为 season 是启发式（其条目绝大多数逐季），误判时退化为多季作品挂合集——不破坏正确性。

## 九、设计依据（源码核验记录）

方案各节均已对照源码核验「可行性 / 简洁性 / 可迁移性」，关键实证如下。

**核心不变量**：

- `Episode` 既有 `UniqueConstraint(series_id, season, episode)` 且 `season` NOT NULL DEFAULT 1（episode.py:14-23；生产库 `\d episodes` 实证 `uq_episode_series_season_episode`）——保留该列、恒等于作品 `season_number`，全部剧集查询零改动。
- 剥季写入约定实证：create 路径 `strip_season_from_title`（metadata_service.py:951-952），季限定原形保留进 aliases（:957-966）——「基础名入主列、季变体进 aliases」的终态约定与现状一致，迁移时无需重洗标题。

**身份体系**：

- `SourceSpec` 是 frozen dataclass（registry:65-75），加 `granularity` 字段为纯声明式改动。
- `work_external_ids` 无 `work_type` 的 CheckConstraint（生产库 `\d work_external_ids` 实证只有 `uq_work_external_id`）——放开 `"collection"` 只需扩 `external_ids.py:41` 的 Literal 与 `find_work_by_external_id` 的查询分支（:140-168），无 schema 变更。
- `canonicalize_external_id` 吃掉 `/ season N` 尾巴的行为实证（registry:278），`#s{N}` 合成形式的透传规则确有必要。
- 合集标题 get-or-create 先例实证（franchise_service.py:117-141）：Python 侧归一化比较的全表扫描在 128 个合集的规模下完全够用；两级标题兜底直接复用此模式 + 合集袋反查优先。

**退役与保留**：退役清单全部有明确代码锚点（reconcile 全模块 metadata_episode_reconcile.py:119-386；季兼容槽位 agent_service.py:703,900-913；`_check_multi_season_coverage` organize_planner.py:865-922；必填字段 required_fields.py:128-169）；绝对集号沿合集成员换算所需数据齐备（成员作品按 season_number 排序 + 各自 number_of_episodes）。

**Agent 派发**（核验发现一处遗漏，已修正）：当前派发只看作品 FK，`series_id/movie_id` 双空 → unrecognized（agent_service.py:792-794），franchise 包靠此不进派发（:822-823）；终态 multi_season 包清 FK 后必须新增「合集包读 resource_work_links 做范围判定 + 覆盖度 key」的派发分支。冲突 key 简化与季兼容删除的锚点实证（:878, :886, :703, :900-913）。

**organize / 通知 / DSL**：

- 缺集校验期望集来源现状：`episode_start/end` → `work.seasons` → 文件清单推导（organize_planner.py:787-862）；终态变为季作品自身集数，属简化。
- `{season}` 模板变量实证（organize_template.py:41-69）。
- 通知 payload 实证（notify_service.py:60-73）：`seasons` 快照与 `episodes[].season` 确实存在；`collection` 摘要**已存在**（:67），v2 变更面比预想更小。
- DSL `series.collection` 求值点实证（filter_engine.py:440-447，链式 selectinload 要求不变）。

**资源修订向导**：`PUT /resources/{id}/associations` 已是「works + collection_id + assignments 原子提交」结构（resources.py:771-830；schema 在 schemas/file_resource.py:233-303，本就有 `collection_id` 字段）；校验框架实证：`resource_association.py` 的 `_validate_assignments`（:147-196）已有「同 (work,season) 区间重叠 422、断档 warning」——新增三条完整性校验可直接挂入该函数；两级关联一致性校验挂 `_resolve_works`（:73-104）。

**去重与手动合并**：

- 聚类键含 aliases 实证（metadata_dedup.py:134-152）+ upsert 把季限定标题塞进 aliases（metadata_service.py:957-966）——终态下「同名不同季」必然互聚，按 `season_number` 隔离聚类是必需修正，不是可选优化。
- links/assignments 重指向缺失实证：`_repoint_series_children`（:238-322）只覆盖 FileResource/AgentWork/ChannelRawTitleMapping/PendingDecision/Episode；`ResourceWorkLink`/`ResourceFileAssignment` 的 FK 是 CASCADE（resource_work_link.py:26-34、resource_file_assignment.py:33-38），dup 删除即静默丢行；正确实现存在于 `rehome_series_as_movie`（:60-88）可提炼共用。
- 合并入口 `POST /works/merge` 复用 `_merge_series_group`，无线上新机制。

**前端**：季语义触点清单实证：SeriesDetail.tsx:106,219-220（分集表 season 列、N季M集）、WorkEditPage.tsx:97,147-148（number_of_seasons 表单）、ResourceEditWizard.tsx（行级 season 输入 L52/L1223、批量套季 L446-452、缺季校验 L486-487）、ChannelDetail.tsx:278-281。向导重构是前端最大工作项，其余为展示调整。

**可迁移性总核验**：生产实测规模（121 series / 6 部实际跨季 / 2345 episodes / 1301 resources / 9 agent_works / 多季作品 season=NULL 资源仅 1 条）下，迁移脚本五步路由（合集→身份→季集合→拆分→重指向）每步都有既有先例（轻迁移机制、franchise get-or-create、dedup 重指向、backfill_search_text 收尾），无新增基础设施。
