# 数据库迁移方案

为 `file_resources` 增加 nullable `magnet_resolve_attempt_id VARCHAR(36)`。轻量迁移按列存在性幂等执行，失败须阻止启动，禁止静默回退旧所有权语义；历史状态和缓存路径不改，旧行标识保持 NULL。升级前须停止全部旧版本 worker，迁移完成后再启动新版本。

RSSRipple 支持三种数据库后端，两两之间都有可复现的迁移脚本。本文是**迁移的权威操作手册**：迁移矩阵、各脚本的用法与行为、Docker 部署下的迁移步骤、以及数据安全不变量。后端选型与全文检索语义见 [conventions.md](conventions.md) 的 `DATABASE_URL` 一节。

| 后端 | URL scheme | 定位 |
|------|-----------|------|
| SQLite（旧版，aiosqlite） | `sqlite+aiosqlite:///` | 历史遗留，已废弃 |
| Turso（嵌入式，SQLite 文件格式兼容） | `sqlite+aioturso:///` | 单节点默认（`docker-compose.standalone.yml`） |
| PostgreSQL（分布式） | `postgresql+asyncpg://` | 分布式默认（`docker-compose.yml`） |

## 迁移矩阵

```
SQLite（旧） ──migrate_to_turso──▶ Turso ──migrate_to_postgres──▶ PostgreSQL
```

- **SQLite → Turso**：`scripts/migrate_to_turso.py`。Turso 读取 SQLite 文件格式，迁移是「一致性备份复制 + 删除 FTS5 对象 + 开启 MVCC」。
- **Turso → PostgreSQL**：`scripts/migrate_to_postgres.py`。按外键依赖序逐表复制，类型自动转换，保留全部 UUID 主键与时间戳。
- **SQLite → PostgreSQL**：分两步走（先 `migrate_to_turso`，再 `migrate_to_postgres`）。`migrate_to_postgres` 只接受 Turso URL——它需要 MVCC 模式下打开的 Turso 文件。
- **PostgreSQL → Turso**：不支持（无反向脚本）。PostgreSQL 是能力超集；需要「降级」回单节点时，从最近的 Turso 备份重建，或接受以全新库重新抓取。
- **作品单季化迁移**（`season_split_migration.py`）与后端矩阵正交：它是同一后端内的原地数据迁移（系列级作品行 → 季作品 + 壳合集），两个后端通用，见第 5 节。

## 1. SQLite → Turso（`scripts/migrate_to_turso.py`）

```bash
uv run python scripts/migrate_to_turso.py \
    --source data/rss_ripple_dev.db \
    --target data/rss_ripple_turso.db
```

行为：

1. 用 SQLite backup API 做一致性复制（正确处理 WAL，无需复制 `-wal` 边车）。
2. 删除所有 FTS5 虚拟表及其影子表（Turso 不实现 `fts5` 模块，且 FTS5 表切换到 MVCC 会破坏连接的 schema 视图）。
3. `PRAGMA journal_mode='mvcc'` 持久化开启并发写（MVCC 是文件级属性）。
4. 校验：打印可见表数量与关键表行数。

**不变量**：

- 源文件只读、从不修改；目标已存在时拒绝覆盖（除非 `--force`）。
- 迁移后把 app 指向新文件：`DATABASE_URL=sqlite+aioturso:///data/rss_ripple_turso.db`。

## 2. Turso → PostgreSQL（`scripts/migrate_to_postgres.py`）

```bash
uv run python scripts/migrate_to_postgres.py \
    --source sqlite+aioturso:///data/rss_ripple_turso.db \
    --target postgresql+asyncpg://rssripple:rssripple@localhost:5432/rssripple
```

行为：

1. 在目标库建全量 schema（`Base.metadata.create_all`）+ `pg_trgm` 扩展与 `ix_<table>_search_text_trgm` GIN 索引（`_ensure_pg_trgm_indexes`，各步 `_best_effort` 容错）。
2. 按 `Base.metadata.sorted_tables` 的**外键依赖序**逐表复制——先父表后子表，因此无需关闭外键约束。
3. 逐行经 SQLAlchemy Core 走「ORM 表定义」复制，类型在两端自动转换：
   - Turso 的 JSON 存为 `TEXT` → 目标反序列化后写 `JSONB`；
   - `BOOLEAN` 的 0/1 → `true`/`false`；
   - `DATETIME` 字符串 → `timestamp`；
   - 主键/外键 UUID、`created_at`/`updated_at` **原样保留**（schema 先建好，ORM 默认值不会触发）。
4. 跳过 `fts_outbox`（Turso 专属变更日志，PostgreSQL 恒为空）。FTS 边车（`<主库名>_fts.db`）也不迁移——PostgreSQL 无边车，搜索走 `search_text` + pg_trgm。
5. 收尾回填 `search_text` 空值，保证 GIN 索引首查即完整。

**不变量**：

- **迁移前必须停掉 app**：Turso 文件是单进程独占锁，运行中的 app 会锁住源文件；同时避免 app 在迁移期间向目标库写入。
- 源文件从不修改；目标必须为空（或 `--force` 重建——`drop_all` + `create_all` 后重新复制）。
- 用 `--force` 重复执行是幂等的（每次都从源重建目标，不累积重复数据）。

## 3. 迁移结果校验（`scripts/verify_search_parity.py`）

```bash
uv run python scripts/verify_search_parity.py \
    --source sqlite+aioturso:///data/rss_ripple_turso.db \
    --target postgresql+asyncpg://rssripple:rssripple@localhost:5432/rssripple
```

对两个后端跑同一批搜索入口（`search_series_fts` / `search_movie_fts` / `search_audio_work_fts` 与 `match_*_by_title`），比较：

- **表行数**：逐表对比，任何一张表行数不等即迁移丢数据。
- **候选集**：223 条查询（CJK / 英文 / 单字 / 大小写 / 子串）的候选 ID 集合。
- **排序匹配结果**：`match_*_by_title` 的 (entity, score) 结局。

**判定**：`RESULT: PASS` 要求「0 表行数不一致 + 0 个 pg-only 召回回归」（PostgreSQL 永不漏掉 Turso 能命中的候选）。以下差异是**预期且良性**的，不计入 FAIL：

- `turso-only`：Turso 的零散 bigram 假阳性（如 `tensei` 命中 `kensei`），PostgreSQL 更精确。
- `turso parse-error`：含 `:`/`'`/`[`/`(`/`^`/`"` 的查询 Turso 抛解析错误返回空，PostgreSQL 正确处理。
- ranked-match 的 `hard-diff`/`tie`：分数刚过阈值的模糊匹配（`limit=30` 截断次序）与重复作品平分——来自既有的候选截断非确定性，与后端无关。

注意：校验时要停掉 app，否则运行中的调度器会持续向目标库写入新行，让「表行数」出现「目标比源多」的假阳性（那几行是迁移**之后**新产生的，不是丢失）。

## 4. Docker 部署迁移

### 4.1 默认栈已切换为分布式

- **`docker-compose.yml`（默认）**：PostgreSQL + Redis + 一次性 `migrate` 服务 + app（web）+ 3 worker。首次 `docker compose up` 会启动一个**全新的空 PostgreSQL**——它不会、也无法自动读取你在单节点时代积累的 Turso 数据。
- **启动 DDL 集中在 `migrate` 一次性服务**（`python -m app.migrate`，跑完即退出）：web/worker 设 `DB_MIGRATE_ON_STARTUP=false` 跳过自身启动迁移，并 `depends_on: migrate (service_completed_successfully)`——worker 的长事务不再与启动 DDL 竞争锁。PostgreSQL 分支的启动 DDL 另带 `lock_timeout=5s` + 有限重试（SQLSTATE 55P03 快速失败重试，约 3 分钟上限），避免一个排队的 ALTER 把全库读请求堵在锁队列里。本地/单进程开发默认 `DB_MIGRATE_ON_STARTUP=true`，行为不变。
- **`docker-compose.standalone.yml`**：Turso 单节点（无 PostgreSQL/Redis），数据仍在 `app-data` 卷的 `rss_ripple_turso.db` 里。

**关键认知**：从单节点切换到分布式栈时，旧 Turso 数据仍在 `app-data` 卷里（命名卷跨 compose 文件共享），但新栈的 app 连的是空 PostgreSQL。要让旧数据回来，必须手动执行第 2 节的迁移。

### 4.2 把单节点（Turso）数据迁入分布式栈

在仓库根目录执行（脚本经 `docker compose run` 在 compose 网络内运行，`postgres` 主机名可解析；`app-data` 卷把 Turso 文件挂进容器，源路径为容器内的 `/app/data/rss_ripple_turso.db`）：

```bash
# 1. 停 app（避免迁移期间写入目标库；也释放 Turso 文件锁）
docker compose stop app

# 2. 迁移：--force 用源数据整体重建目标 schema。即使目标看似"空"，app 首次
#    启动也已写入 app_settings（TOTP 秘钥等），必须 --force 才能整体覆盖、
#    恢复 Turso 里的原始凭证与全部业务数据。
docker compose run --rm app \
  uv run --no-project python scripts/migrate_to_postgres.py \
  --source sqlite+aioturso:///data/rss_ripple_turso.db \
  --target postgresql+asyncpg://rssripple:rssripple@postgres:5432/rssripple \
  --force

# 3. 重启 app
docker compose start app
```

`POSTGRES_USER`/`POSTGRES_PASSWORD`/`POSTGRES_DB` 走 compose 默认值 `rssripple`，如你在 `.env` 改过则相应替换 `--target`。

### 4.3 迁移后校验（可选，容器内）

```bash
docker compose run --rm app \
  uv run --no-project python scripts/verify_search_parity.py \
  --source sqlite+aioturso:///data/rss_ripple_turso.db \
  --target postgresql+asyncpg://rssripple:rssripple@postgres:5432/rssripple
```

> 校验前同样确保 app 已停（见第 3 节的行数假阳性说明）。

## 5. 作品单季化迁移（`scripts/season_split_migration.py`）

作品单季化（per-season works，终态设计见 [per-season-works.md](per-season-works.md)）的 schema 部分（`tv_series.season_number`、`work_collections.aliases/search_text/manually_edited_fields` 加列）由 `_apply_light_migrations` 随启动自动完成，无需操作；本节是把**存量系列级 TVSeries 行拆分为季作品**的一次性数据迁移 runbook（per-season-works.md 的 P8）。

### 5.1 脚本用法

```bash
# 0. 迁移前抓行数快照（供 verify 的行数守恒对比）
uv run python scripts/verify_season_split.py --write-snapshot counts.json

# 1. dry-run（默认）：完整动作清单在一个事务内 staging 后整体回滚，
#    输出每部作品的建合集/拆季/子表去向供人工核对
uv run python scripts/season_split_migration.py            # 可加 --limit N 先试跑几部

# 2. 核对无误后写入
uv run python scripts/season_split_migration.py --apply

# 3. 校验（只读；退出码 0=全部通过）
uv run python scripts/verify_season_split.py --snapshot counts.json
```

行为概要（每部 legacy TVSeries 按 created_at 升序）：建/取壳合集（沿用既有 `collection_id` → 合集袋反查 → 归一化基础标题 get-or-create `series_group`）→ 系列级身份袋行重指向合集袋 → 判定季集合（`seasons` JSON ∪ Episode 行 ∪ 资源 season 值）→ 多季拆分（最小季复用原行，其余季新建季作品并袋合成身份 `{主id}#s{N}`；逐季源 id 默认留锚点季，资源证据一致指向他季时随之搬家；季首播日缺失时用本季 Episode 最早 `air_date` 离线补齐）→ 子表按 season 分量重指向（无法定位的 season=NULL 资源挂合集待确认）→ AgentWork 不动（订阅保持作品粒度，汇总行列出建议补订阅的季作品）→ `--apply` 收尾 `backfill_search_text` + 创建部分唯一索引 `uq_tv_series_collection_season`。

**校验脚本覆盖**：①全部子表/身份袋无悬空 FK；②行数守恒（`--snapshot` 对比迁移前快照；必须守恒的表任何差异即失败，合法增长/碰撞收缩的表只报 delta）；③每部 TVSeries 必属合集、`(collection_id, season_number)` 唯一、`Episode.season` 恒等于作品 `season_number`；④`search_text` 无空值；⑤降级版派发等价检查（每条已匹配资源挂载在作品/合集/links 之一，默认 warning，`--strict` 升级为失败）。

### 5.2 安全不变量

- **先备份**：PG 用 `pg_dump`；Turso 停 app 后复制 db 文件 + wal 边车。
- **跑前停 app**：Turso 是单进程独占文件锁；同时避免迁移期间并发写。**脚本单向、不可逆**——没有反向迁移，回滚只能靠备份。
- **dry-run 先行**：默认不落库，人工核对动作清单后才 `--apply`。
- **幂等**：重跑跳过已迁移作品并收敛（同 IP 多条 legacy 行坍缩进同一合集时，冗余行被合并吸收而非制造重复季成员）。
- 迁移后启动 app，`_apply_light_migrations` 与启动回填（search_text 空值、FTS 边车 drain/对账）自动收敛。

### 5.3 Docker 操作序列

```bash
docker compose stop app worker                     # standalone 栈只停 app
# 备份：PG → pg_dump；Turso → 复制卷内 db 文件 + wal
docker compose run --rm app \
  uv run --no-project python scripts/verify_season_split.py --write-snapshot counts.json
docker compose run --rm app \
  uv run --no-project python scripts/season_split_migration.py            # dry-run 核对
docker compose run --rm app \
  uv run --no-project python scripts/season_split_migration.py --apply
docker compose run --rm app \
  uv run --no-project python scripts/verify_season_split.py --snapshot counts.json
docker compose up -d
```

### 5.4 迁移后立即元数据刷新（year 门控）

拆分出的**非锚点季作品 `start_date` 为 NULL**（仅锚点季保留原值；Episode `air_date` 离线推导只补有逐集数据的季）。Channel 必选字段 `year` 由作品 `start_date` 派生，为空的季作品会拦下其新资源（进 Channel 文件资源待确认）。因此迁移完成后应立即对这些季作品执行一次元数据刷新（作品模块逐个/批量「刷新元数据」，或开频道级定期刷新），把本季首播日期补齐；同样建议按迁移汇总行的清单为相关 Agent 补订阅其余季作品。



1. **迁移永远复制、不改源**：后端迁移两个脚本都只读源文件；作品单季化迁移是唯一的原地写迁移——单向不可逆，必须先备份、停 app、dry-run 核对后 `--apply`（见第 5 节）。
2. **迁移前停 app**：Turso 单进程锁 + 避免目标库被并发写入。
3. **`--force` 是幂等重建**：目标从头按源重建，重复执行不累积。
4. **迁移后必校验**：`verify_search_parity.py` 的「0 表行数不一致」是数据完整性的硬性证明。
5. **目标为空或 `--force`**：`migrate_to_postgres` 拒绝向非空目标插入（避免主键冲突与重复）。
字幕组列表兼容迁移：`scripts/subtitle_groups_eval.py` 默认只读审计数据库中的联合发布值；`--export tests/fixtures/subtitle_groups.json` 生成与单测共享的真实样本，`--validate` 离线验证解析，`--apply` 为 `file_resources.subtitle_groups`/映射表回填并迁移 Agent、OrganizeRule、频道 field_mapping 中的旧 `subtitle_group` 规则。旧列保留为兼容镜像，迁移可重复执行。


整理所有权升级新增 Plan 六个快照/版本字段及 OrganizeConfiguration 单例（含 lock_domain），轻迁移在 Turso MVCC 初始化后确保单例，重复执行不重置 revision。历史 file_op 保留 NULL，pending/failed 执行前重建，running 未知模式拒绝执行。此组关键列失败须中止启动；迁移和启动前应停止旧版本整理执行者，所有 Web/worker 一起升级，禁止不遵守共享锁的旧进程与新版本混跑。保留原共享锁目录，数据库和锁域恢复必须一致。


通知生成失败隔离新增 `notification_build_failures` 表，由启动 `Base.metadata.create_all` 在 Turso/PostgreSQL 幂等建立，任务 FK 为 ON DELETE CASCADE、每任务唯一。升级不修改下载状态或既有通知；重复启动保留尝试次数和下次重试时间。回退旧程序可保留该表，但旧程序不会消费其中的退避状态。


Agent 定向补偿新增 `agent_resource_requests` 表，由模型注册后的 `create_all` 为新装/旧库创建，无存量请求回填。PostgreSQL 与 Turso 均使用非空 `(agent_id,resource_id)` 联合唯一键及两个 ON DELETE CASCADE 外键；重复启动保留 revision、错误与重试时间，不重置待处理请求。


### 启动时修复孤儿季作品

两个后端在 schema 轻迁移后执行合集归属回填，随后执行既有 FTS/search_text 回填。每批最多读取 100 个 collection_id IS NULL 的 TVSeries，保留原季号建立壳合集并同步已有作品关联资源；每批独立事务提交，重跑不再创建已修复作品的壳。

PostgreSQL 用 FOR UPDATE 串行处理同一批孤儿，多个启动者不会重复建壳；Turso 用既有 retry_on_lock 在新会话中重试锁冲突。回填失败回滚当前批次，不静默跳过。此回填不猜季、不合并同名作品；合集单季部分唯一索引由先前 schema 阶段独立建立，不能把孤儿回填当作 FK 等其他 schema 修复。

## 合集单季唯一索引升级预检与恢复

新装及轻量迁移均建立 `uq_tv_series_collection_season`（`collection_id, season_number`，`WHERE collection_id IS NOT NULL`），Turso/PG 行为一致。历史重复数据会使索引创建失败、启动中止；不会自动删作品、合并人工标题或猜测季号。

升级前保留数据库备份并停止自动写入任务，在维护副本使用目标版本的只读脚本，连接待检查数据库（无需启动 Web/worker）：

```bash
python -m scripts.verify_season_split --collection-conflicts-jsonl /tmp/collection-season-conflicts.jsonl
```

该独立模式仅 SELECT，流式导出每个冲突成员的完整合集 ID、季号、同槽作品数、作品 ID、标题、主身份和 manually_edited_fields。退出 0 表示没有冲突，1 表示有冲突；不能把文件生成成功当作可以升级。NULL 合集不在索引范围内，完整孤儿/关联检查仍用原 verify 模式。

若退出 1，保留当前可运行版本，不启动要求此索引的新版本。逐项核对报告与作品来源；同季不同作品应通过已支持“剧集解绑后建立壳合集”的版本进行明确解绑，保留原作品 ID、人工编辑及资源映射。确为重复身份的作品须人工确认合并策略及引用迁移，不能仅按最旧行选幸存者。旧 season_split_migration 的碰撞合并不是通用、无损的冲突修复命令。修复后重跑明细预检及完整 verify，比较备份快照中的作品、资源与关联；再在维护副本启动目标版本两次验证幂等，确认后才升级运行库。

若已经遇到启动失败，停止重启循环，保留日志及备份，用同一只读脚本输出冲突；回到前一可运行版本处理数据后再试。schema 阶段失败不会自动完成数据收敛；不要删除索引后强行继续运行。

合集挂载使用按 ID 排序的父锁并检查目标季槽；壳吸收/直接挂载的变更在 SAVEPOINT 内执行。检查后的竞争写入若触发该唯一约束则回滚完整变更并返回 409 DUPLICATE_SUBMISSION，其他数据库异常仍正常上抛。


### 轻量迁移的升级外键对等（D2）

七处历史新增 FK 列必须与新装库一致：`file_resources.audio_work_id`、`tv_series.collection_id`、`movies.collection_id`、`downloader_instances.volume_id`、`libraries.media_server_id`、`libraries.volume_id`、`file_resources.collection_id`。新增列直接带 REFERENCES；已存在列须按 catalog 核对目标表/列和 ON DELETE，不能仅因列存在就跳过。目标与删除动作以 ORM 模型为准；错误的既有约束明确拒绝启动，不自动改成另一种关联语义。

升级前，在停止旧进程并备份数据库后，以目标 DATABASE_URL 运行只读预检：

```bash
PYTHONPATH=. .venv/bin/python scripts/verify_upgrade_foreign_keys.py --output /tmp/fk-orphans.jsonl
```

退出 0 表示这七处没有悬空关联；退出 1 时 JSONL 完整列出表、列、子行 ID、父键与目标。不调用 create_tables，不触发回填或其他 DDL；兼容尚无相关表/列的旧库。根据业务证据逐项修复或恢复父项后重跑，禁止凭季号/名称猜测重连或直接删除子行。它不替代 D1 同季冲突预检，也不表示所有其他 FK 均已审计。

PostgreSQL 在现有启动 advisory transaction lock 与有界 DDL lock_timeout 内补约束；先诊断悬空 ID，再由 ALTER ADD CONSTRAINT 在锁下验证完整数据；任一失败回滚本轮 schema 事务。Turso 已有列缺约束需表重建：在 schema 阶段完成后、孤儿作品回填前，以新的普通 BEGIN 执行，不能使用 BEGIN CONCURRENT；按原始 CREATE 定义保留历史额外列、唯一约束、显式索引和触发器。重建前预检悬空值，暂时关闭外键防止 DROP 触发子表 CASCADE/SET NULL/RESTRICT；所有换表处于同一事务，finally 恢复 foreign_keys=ON。失败恢复原表及其子行，不吞异常继续启动；已完成的先前轻迁移 schema 阶段可能仍保留，业务关联不会被猜测修补。

该修复不支持迁移期间其他程序写同一个 Turso 文件；遵循原有单进程独占/停机升级要求。数据量大时表复制与索引重建会占用额外磁盘和启动时间，应在数据库备份副本演练后安排升级窗口。

## 退役季数的人工复核清理

number_of_seasons 不再经创建/更新 API 或去重写入。已有值不能自动全表清空：真正未拆季作品仍需这些证据。维护工具不运行应用启动迁移，使用现有 DATABASE_URL。

1. 停止所有写入进程并备份数据库。运行 `uv run python -m scripts.retired_season_fields --export /tmp/season-review.jsonl`，只读导出全部非空计数作品及其完整关联证据、阻断原因和指纹；每批最多 100 个作品，单个作品证据不截断。保留原始报告。
2. 人工检查单条记录的合集、既有季号、旧 seasons、身份袋、Episode、资源及文件指派。只有确认已单季化时，复制完整记录到独立 JSON 文件，添加明确整数 confirmed_season，值须等于既有季号，不能从默认 S1 猜测。
3. 运行 `uv run python -m scripts.retired_season_fields --apply-review /tmp/reviewed-work.json`。单作品事务先锁合集再锁作品并检查当前证据指纹；过期报告、跨季 Episode、矛盾季声明/身份/资源/指派均拒绝，先走关联修复或 P8 拆季流程。
4. 成功仅将计数置空、移除其退役人工标记、添加明确季号的人工确认标记；保留 seasons、其他作品字段及关联。输出前后指纹及 changed；相同审核再次执行为 changed=false。任何失败整笔回滚。核对报告后再恢复写入。

行锁与指纹不能替代停写：不同关联的新增需要稳定维护窗口。工具不修改生产配置，也不自动修复有歧义的历史关联。真正未拆季证据优先于人工标记，清理不会为跨季污染猜测拆分方案。

## 待决策覆盖身份的审核迁移

迁移须停应用写入并备份，使用独立普通 DDL 事务（Turso 禁止 BEGIN CONCURRENT）。先只读导出审核报告，人工添加与报告 fingerprint 相等的 approved_fingerprint 和 supersede_pending=true。执行端重新读取当前证据并比较指纹，仅使用重新计算的分组；报告中未知、缺失或单候选组阻止迁移，须先纠正后重新审核。

通过审核后，原 pending 行保留为 expired，等价覆盖组生成新的 pending 行，缓存推荐不继承；非 pending 历史不改动。原报告与新旧 ID 映射写入 DecisionMigration，并与增列、约束安装、数据变换同事务提交。重复审核指纹返回已完成结果，不重放数据变换。任何异常回滚整批。PostgreSQL 离线阶段锁住 Agent/决策/资源/作品/关联与指派表，避免审核与执行之间被改写。

当前执行服务及离线命令仍为待验收原型；启动约束检查已接入，须完成剩余 rekey/并发和完整门禁后启用。

离线命令（在已应用并验收本变更的版本中）：

```bash
python -m scripts.review_pending_decisions --export decision-review.json
# 审核后另存 approved.json，补 approved_fingerprint 与 supersede_pending=true
python -m scripts.review_pending_decisions --apply-review approved.json --writers-stopped --backup-confirmed
```

导出不会添加批准字段，不覆盖已有文件，也不调用应用启动迁移。apply 缺少两个前置确认参数时在打开数据库前拒绝；Turso 独立进程使用普通 BEGIN，必须先停止持有文件锁的应用/worker。参数仅记录操作员确认，不会替代实际停写或备份动作。成功返回新旧决策 ID 映射；同一批准指纹重跑返回 already_applied=true。

启动先审核现有 PendingDecision 表结构与持久键，再进行业务 backfill；发现未审核 pending、损坏键或重复槽时明确失败，不自动删除、过期或猜测候选。操作员按离线流程完成审核迁移后再启动。Turso 在独立普通事务中安装旧表约束；PostgreSQL 在启动 advisory lock 保护的事务中安装，失败回滚该启动事务。新装表由 ORM 的 CHECK 与 pending 部分唯一索引保证。

P8 在已有决策键结构的数据库上执行时，先记录受影响 Agent，再在作品与子关联迁移后按当前资源证据重建 pending，旧记录与替代 ID 归档且与迁移同事务；dry-run 回滚包含该档案。不同季的候选必须分槽，无证据候选不猜季。旧表没有 decision_key/decision_scope 时，停写并备份后先执行 P8；此阶段只读旧字段、重指作品引用，保留候选与状态，不安装决策约束。然后重新导出决策审核报告，人工批准后执行审核迁移，最后启动应用。审核导出重新加载当前资源关系，避免同一会话内沿用拆季前关系。该顺序已通过 Turso 与 PostgreSQL 的旧表测试；拆季异常后决策指纹、资源归属、季号及作品数量恢复，随后重新执行与显式审核通过。

### 显式清理孤儿身份（D6）

使用 `python -m scripts.review_orphan_identities --export review.json` 只读导出；工具不运行应用启动迁移，也不覆盖已有文件。只有 series/movie/collection 目标不存在的身份进入 orphans；未知 work_type 进入 blocked。审核后在 JSON 中增加 approved_fingerprint（等于导出 fingerprint）及 selected_ids（精确选择 orphan 行 ID）。保留原始审核文件和数据库备份。

停写后运行 `python -m scripts.review_orphan_identities --apply-review reviewed.json --writers-stopped --backup-confirmed`。同事务重新核对指纹和拥有者；PG 锁定身份袋及三类拥有者表，Turso BEGIN IMMEDIATE。行值变化、新出现拥有者或未知类型均拒绝；选中记录已全部不存在时返回 already_absent_ids。只删除审核选中行，不接管身份，不在启动或 API 中自动清理。CLI/Turso、真实录制负例及 PG 并发锁验证已通过；工具不自动运行。


### 资源发布进度迁移

新协议启用前必须停写并备份，不能混跑旧时间水位线发布者和新事件消费者。以下步骤针对原有正式版本没有发布进度表的数据库；早期实验表不属于受支持升级路径，`checkfirst` 不能替代实验约束升级。

1. 停止 web/worker 和其他数据库写入者，完成备份。
2. 使用目标数据库配置执行 `python -m scripts.review_publication_migration --prepare-schema --writers-stopped --backup-confirmed`，仅创建三个新表，可重复执行。
3. 执行 `python -m scripts.review_publication_migration --export review.json`。文件独占创建，包含资源 id/channel/created_at、Agent 旧时间、待消费 IDs 和 `excluded_or_ambiguous_resource_ids`。
4. 核对报告后添加 `approved_fingerprint`，值为报告的 `fingerprint`；执行 `python -m scripts.review_publication_migration --apply-review review.json --writers-stopped --backup-confirmed`。
5. 确认应用成功后启动新 web/worker。任何指纹变化都须重新导出核对，不沿用旧报告。

迁移按频道 created_at/id 排序建立 created 事件。非 NULL 旧时间的 Agent 以 <= last_consumed_at 的最大序号建立 baseline/cursor，保持严格大于旧时间的待消费范围；NULL 时间留待首次运行初始化。时间以下潜在遗漏与用户主动排除不可区分，工具只报告、不自动下载，后续可由用户 rules-preview 或指定时间扫描选择。

PostgreSQL 在参与表写屏障内重新核对报告并应用，Turso 使用 BEGIN IMMEDIATE。事件、计数器、进度与完成标记同事务提交/回滚；标记保存审核指纹，同一审核重复应用不改进度。无标记却已有发布数据、审核内容被改动、数据库资源或水位线变化均拒绝应用。

web/worker 在运行配置和调度启动前检查迁移状态；真正空库初始化 fresh 标记，旧数据无标记拒绝启动。有标记仍检查资源初始事件与旧 Agent 的匹配频道进度，防止遗漏迁移或混用旧写入者。DB_MIGRATE_ON_STARTUP=false 不跳过此门禁。

专项证据与发布验收状态见 V13-CONSUMPTION-PROGRESS.md；本节描述迁移协议，不表示已对生产数据库执行迁移。

### 下载派发预留表

新版本注册 DownloadDispatch，由常规模型建表创建 download_dispatches（唯一 operation_key 与 task_id）；不回填旧下载任务，也不对 downloader/torrent_id 加全局唯一。已存在任务继续保持原有身份。混合版本 worker 不支持执行所有权保证，部署需停启全部 worker。Turso 的 Boolean 默认必须使用 SQL 布尔表达式，不能用字符串 'false'；原型旧表不作为正式升级来源。两后端旧库启动、重复建表和唯一约束直接写入测试属于合入前必要门禁。

派发表旧库建表已在 Turso 与 PostgreSQL 通过同一驱动验证两次生产 create_tables：旧 AppSetting 和新增预留保留，直接 SQL 的 operation_key/task_id 唯一约束、settled 非空及 false 默认生效。验证只覆盖相对本基线缺派发表的旧 schema，不代表任意更早或实验 schema 自动可升级。

派发表包含可空 job_key/job_id 与 created_at 索引，正常派发填入身份供清理验证；缺身份记录不清理。尚未发布的旧实验表不属于支持升级来源。两后端升级、约束与完整门禁证据见 `docs/plans/p0-and-backlog/V14-QUEUE-OWNERSHIP.md`。

升级新增 `webhook_deliveries.attempt_token VARCHAR(36) NULL`；老行无需回填。该安全字段的添加失败必须中止启动，不允许 best-effort 吞错。升级须停掉旧 worker，不能让不检查 token 的旧进程继续写投递结果。

### OTP 额度表新增

本版本增加 auth_rate_limit_buckets，应用启动的 Base.metadata.create_all 在已有 PostgreSQL/Turso 库上幂等创建该新表，无历史计数需要回填。上线前须让所有 web 实例升级；旧版本不预留额度，混跑期间不能声称全局限流已生效。不会修改已有 TOTP/Cookie 密钥或使已签发会话失效。回退旧版本会丢失限流保护；不要以删表清除额度作为登录流程。
