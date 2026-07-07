<p>
  <img src="docs/assets/rssripple-banner.svg" alt="RSSRipple - RSS 订阅下载器" width="596">
</p>

[English](README.md) | **中文**

RSSRipple 是一个面向 TV / 番剧 / 电影资源的 RSS 订阅下载器。它抓取 RSS 源，按每个频道的字段映射规则解析每条资源，将资源关联到本地元数据作品库，通过 Agent 过滤后，把匹配的种子推送到 Transmission —— 打通从订阅到下载的完整闭环。

## 亮点

- **端到端管线** — RSS 抓取 → 字段映射解析 → 元数据关联 → Agent 过滤 → Transmission 推送。Agent 运行为增量模式（`last_consumed_at` 水位线）；规则变更走 rules-preview / 回填流程，历史资源不会被静默自动派发。
- **LLM 辅助 Feed 分析** — 把 RSS 源指给 RSSRipple，LLM 会自动生成 `field_mapping` 规则，可在 UI 中调整后再保存。
- **统一元数据 Agent** — LangGraph ReAct agent 清洗标题、推断集数/季数，并只使用一个选定的数据源（`exa` / `jina` / `tmdb` / `wikipedia`）搜索。结果以 `TVSeries` / `Movie` 缓存到本地，避免重复查询。
- **Filter DSL** — 布尔查询，支持嵌套 `and` / `or`、字段操作符、按作品覆盖，以及对合集（`is_batch`）和多值字幕语言（`zh-CN`、`zh-TW`、`ja`、`en`、`multi`）的一等支持。
- **Transmission 集成** — 多下载器实例、必填默认目录、可选的按 Agent 子目录、带持久化目标路径的重试、实时进度同步。内置 `mock` 下载器用于测试。
- **React 仪表盘** — 频道、资源、Agent、待决策项、下载任务、作品库、下载器，一个界面全搞定。

## 快速开始

### 1. 配置环境变量

```bash
cp .env.example .env
# 至少设置：LLM_API_KEY、LLM_BASE_URL、LLM_MODEL
# 可选元数据源：EXA_API_KEY / JINA_API_KEY / TMDB_API_KEY
```

### 2. 用 Docker Compose 启动

```bash
docker compose up --build
```

这会同时启动应用 **和** 一个 Transmission 实例：

| 服务 | 地址 | 用途 |
| --- | --- | --- |
| RSSRipple | http://localhost:9001 | Web UI |
| API 文档 | http://localhost:9001/docs | OpenAPI / Swagger |
| Transmission | http://localhost:9091 | 下载后端 |

默认使用 SQLite + 内存队列；数据持久化在 `./data/` 下。

### 3. 手动运行

```bash
uv sync
cd frontend && npm install && npm run build && cd ..
uv run uvicorn app.main:app --reload --port 9001
```

## 获取 API 凭证

RSSRipple 需要一个 LLM 和至少一个元数据源。按需申请 key 后填入 `.env`。

| 服务 | 申请地址 | 环境变量 | 是否必需 |
| --- | --- | --- | --- |
| LLM（OpenAI 兼容） | [OpenRouter](https://openrouter.ai/keys) — 或任意 OpenAI 兼容服务商 | `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` | 是 — feed 分析、元数据 agent、建议 |
| Exa Agent Search | [dashboard.exa.ai](https://dashboard.exa.ai/) | `EXA_API_KEY` | 可选 — 默认元数据源 |
| Jina Search + Reader | [jina.ai/api-dashboard](https://jina.ai/api-dashboard/) | `JINA_API_KEY` | 可选 — 中日韩覆盖较好 |
| TMDB | [themoviedb.org/settings/api](https://www.themoviedb.org/settings/api)（申请 v3 key） | `TMDB_API_KEY` | 可选 — 影视 ID 匹配最佳 |
| Wikipedia | — | — | 无需 key（免费 `wikipedia` 库） |

一个元数据源只有"启用开关开启 **且** 凭证已配置"时才在 UI 中可选。开关：`EXA_ENABLED` / `JINA_ENABLED` / `TMDB_ENABLED` / `WIKIPEDIA_ENABLED`。`local` 源无需凭证 — 仅本地 DB 匹配。

## 配置

常用变量（完整列表见 [AGENTS.md](AGENTS.md) 的「其他约定」小节）：

| 变量 | 说明 |
| --- | --- |
| `DATABASE_URL` | SQLAlchemy 数据库 URL |
| `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` | OpenAI 兼容 LLM，用于 feed 分析、元数据 agent、建议 |
| `EXA_API_KEY` / `JINA_API_KEY` / `TMDB_API_KEY` | 元数据源凭证 — 按需配置 |
| `QUEUE_BACKEND` | `"memory"`（默认）或 `"redis"`（需 `REDIS_URL`） |
| `POSTER_CACHE_DIR` | 海报缓存目录，挂载到 `/posters` |

## 开发者指南

### 本地开发

compose 文件会监听 `./app` 并热重载 Python。前端改动**不会**热重载 — 在 `frontend/` 下运行 `npm run build`，或 `docker compose build app` 把新 bundle 重新打包进镜像。

### 测试

**单元 & API 测试**（快速，本地 SQLite）：

```bash
uv run pytest tests/unit tests/api -v
```

**集成测试**（docker-compose）— 两个 profile：

单机（SQLite + MemoryQueue）— 快速，无外部依赖：

```bash
rm -rf data/ && mkdir -p data   # 残留的 SQLite 文件在 `down -v` 后仍会保留
docker compose -f docker-compose.test.yml run --rm test-runner
# 单个模块：
docker compose -f docker-compose.test.yml run --rm test-runner \
  uv run pytest tests/integration/test_channel_workflow.py -v --tb=short
```

分布式（PostgreSQL + Redis，两个 app 副本）— 验证多实例队列去重：

```bash
docker compose -f docker-compose.test-distributed.yml run --rm test-runner
```

需要持久网络客户端的测试（E2E、种子生命周期）在两个 profile 中都被排除；Redis 专用的队列测试在单机模式下自动跳过。

### 贡献

分支命名遵循 [Conventional Branch](https://conventionalbranch.org/) v1.1.0。工作流见 [CONTRIBUTION.md](CONTRIBUTION.md)，完整分支规范见 [AGENTS.md](AGENTS.md)（分支与协作规范小节）。

### CI/CD

GitHub Actions 负责持续集成与持续交付：

- **CI Fast Gate**（`ci-fast.yml`）— feature/fix 等开发分支及其 PR：lint + 单元/API 测试。
- **CI Strict Gate**（`ci-strict.yml`）— `main`、`develop`、`release/**` 及其 PR：lint + 单元/API + 集成测试。
- **Docker Publish**（`docker-publish.yml`）— 推送到 `main` 或打 `v*` 标签时，构建多架构（`linux/amd64` + `linux/arm64`）镜像并发布到 `ghcr.io/robinqu/rssripple`。标签：`main` → `:latest`、`:main`、`:sha-<短哈希>`；`v1.2.3` → `:1.2.3`、`:1.2`、`:1`。构建前以 lint + 单元/API 测试作为门禁。

完整工作流与推荐发布流程见 [CONTRIBUTION.md](CONTRIBUTION.md)。

## 面向 Coding Agents 的 Spec 说明

如果你是在本仓库工作的 coding agent（Claude Code、Cursor、Copilot、Codex 等），按以下顺序阅读：

- **[AGENTS.md](AGENTS.md)** — 权威 spec：数据模型、Filter DSL、API 端点、业务逻辑、前端路由、错误处理、分支规范。这是*系统如何工作*的唯一事实来源。
- **[ARCHITECTURE.md](ARCHITECTURE.md)** — 模块布局与运行时数据流。
- **[overview.md](overview.md)** — 频道与元数据作品库的设计逻辑分析。
- **[DESIGN.md](DESIGN.md)** — 设计 token 与视觉指引（仅前端）。

实现必须遵循 AGENTS.md。当代码与 AGENTS.md 不一致时，以 AGENTS.md 描述的行为为准 — 修复代码，或当设计确实已变更时更新 AGENTS.md。

## 技术栈

| 层 | 技术 |
| --- | --- |
| 后端 | Python 3.11+、FastAPI、SQLAlchemy 2.0 async、Pydantic v2 |
| 数据库 | 默认 SQLite（aiosqlite）；架构兼容 PostgreSQL |
| 队列 / 调度 | MemoryQueue 或 RedisQueue、APScheduler |
| RSS | feedparser |
| 元数据 / AI | OpenAI 兼容 LLM、LangGraph ReAct、Exa / Jina / TMDB / Wikipedia |
| 下载 | Transmission RPC |
| 前端 | React、TypeScript、Vite、Ant Design |
| 包管理 | uv、npm |
