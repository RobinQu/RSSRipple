# 数据库迁移方案

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

- **`docker-compose.yml`（默认）**：PostgreSQL + Redis + app。首次 `docker compose up` 会启动一个**全新的空 PostgreSQL**——它不会、也无法自动读取你在单节点时代积累的 Turso 数据。
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
