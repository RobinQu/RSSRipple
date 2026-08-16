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

## 数据安全不变量（总结）

1. **迁移永远复制、不改源**：两个脚本都只读源文件。
2. **迁移前停 app**：Turso 单进程锁 + 避免目标库被并发写入。
3. **`--force` 是幂等重建**：目标从头按源重建，重复执行不累积。
4. **迁移后必校验**：`verify_search_parity.py` 的「0 表行数不一致」是数据完整性的硬性证明。
5. **目标为空或 `--force`**：`migrate_to_postgres` 拒绝向非空目标插入（避免主键冲突与重复）。
