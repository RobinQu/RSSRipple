<p>
  <img src="docs/assets/rssripple-banner.svg" alt="RSSRipple - RSS 订阅下载器" width="596">
</p>

[English](README.md) | **中文**

[![CI Fast Gate](https://github.com/RobinQu/RSSRipple/actions/workflows/ci-fast.yml/badge.svg)](https://github.com/RobinQu/RSSRipple/actions/workflows/ci-fast.yml)
[![CI Strict Gate](https://github.com/RobinQu/RSSRipple/actions/workflows/ci-strict.yml/badge.svg)](https://github.com/RobinQu/RSSRipple/actions/workflows/ci-strict.yml)
[![Docker Publish](https://github.com/RobinQu/RSSRipple/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/RobinQu/RSSRipple/actions/workflows/docker-publish.yml)

RSSRipple 是一个面向 TV / 番剧 / 电影资源的 RSS 订阅下载器。它抓取 RSS 源，按每个频道的字段映射规则解析每条资源，将资源关联到本地元数据作品库，通过 Agent 过滤后，把匹配的种子推送到 Transmission —— 打通从订阅到下载的完整闭环。

## 亮点

- **端到端管线** — RSS 抓取 → 字段映射解析 → 元数据关联 → Agent 过滤 → Transmission 推送。Agent 运行为增量模式（`last_consumed_at` 水位线）；规则变更走 rules-preview / 回填流程，历史资源不会被静默自动派发。
- **LLM 辅助 Feed 分析** — 把 RSS 源指给 RSSRipple，LLM 会自动生成 `field_mapping` 规则，可在 UI 中调整后再保存。
- **统一元数据 Agent** — LangGraph ReAct agent 清洗标题、推断集数/季数，并只使用一个选定的数据源（`exa` / `jina` / `tmdb` / `wikipedia`）搜索。结果以 `TVSeries` / `Movie` 缓存到本地，避免重复查询。
- **Filter DSL** — 布尔查询，支持嵌套 `and` / `or`、字段操作符、按作品覆盖，以及对合集（`is_batch`）和多值字幕语言（`zh-CN`、`zh-TW`、`ja`、`en`、`multi`）的一等支持。
- **Transmission 集成** — 多下载器实例、必填默认目录、可选的按 Agent 子目录、带持久化目标路径的重试、实时进度同步。内置 `mock` 下载器用于测试。
- **React 仪表盘** — 核心指标、Top 活跃 Agent 及其进行中任务、活跃下载、待决策、频道、作品库、下载器，一个界面全搞定。

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

默认使用 Turso（嵌入式，兼容 SQLite 文件格式）+ 内存队列；数据持久化在 `./data/` 下。

### 3. 手动运行

```bash
uv sync
cd frontend && npm install && npm run build && cd ..
uv run uvicorn app.main:app --reload --port 9001
```

前端构建需要 Node.js 20.19+ 或 22.12+（Vite 8）。

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

常用变量（完整列表见 [docs/design/conventions.md](docs/design/conventions.md)）：

| 变量 | 说明 |
| --- | --- |
| `DATABASE_URL` | SQLAlchemy 数据库 URL |
| `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` | OpenAI 兼容 LLM，用于 feed 分析、元数据 agent、建议 |
| `EXA_API_KEY` / `JINA_API_KEY` / `TMDB_API_KEY` | 元数据源凭证 — 按需配置 |
| `QUEUE_BACKEND` | `"memory"`（默认）或 `"redis"`（需 `REDIS_URL`） |
| `POSTER_CACHE_DIR` | 海报缓存目录，挂载到 `/posters` |

## 使用指引

应用运行在 http://localhost:9001 后：

1. **添加频道** — *频道 → 新建频道*：粘贴 RSS 地址，点击**验证**，然后让 LLM 生成字段映射（或自行调整）并创建。
2. **添加下载器** — *下载器 → 添加下载器*：填写 Transmission RPC 地址；默认下载目录已预填 `/downloads/complete`。保存前可点**测试连接** —— 它会按表单里当前填写的值探测，无需先保存。
3. **创建 Agent** — *下载代理 → 新建 Agent*：选择频道和下载器，订阅指定作品（剧集/电影，最多 10 个）或使用全频道模式，再调整过滤条件。保存时会经过 rules-preview，可选择是否回填历史资源。也可以在频道详情页勾选若干资源，用**生成过滤规则**快速引导创建一个 Agent。
4. **关注仪表盘** — `/` 展示核心指标、Top 活跃 Agent 及其进行中下载、活跃下载列表，以及等待你处理的待决策项（启用 LLM 时附带 AI 建议，可一键确认/跳过）。

## 反馈与缺陷报告

发现 bug 或有功能建议？请到 [GitHub Issues](https://github.com/RobinQu/RSSRipple/issues) 提交 issue。

为了帮助我们快速定位和修复，请尽量提供：

- 你运行的版本或镜像标签（如 `ghcr.io/robinqu/rssripple:latest`）和部署方式（Docker Compose / 手动）。
- 复现步骤、期望行为与实际行为 — 截图非常有帮助。
- 相关日志（`docker compose logs app` 或服务端控制台输出）— 提交前请抹掉 API key 等敏感信息。

## 参与贡献

开发者环境搭建、测试、分支规范与 CI/CD 见 [CONTRIBUTION.md](CONTRIBUTION.md)。Coding agent 请从 [AGENTS.md](AGENTS.md) 开始阅读。

## 技术栈

| 层 | 技术 |
| --- | --- |
| 后端 | Python 3.11+、FastAPI、SQLAlchemy 2.0 async、Pydantic v2 |
| 数据库 | 默认 Turso（嵌入式，MVCC 并发写）；架构兼容 PostgreSQL |
| 队列 / 调度 | MemoryQueue 或 RedisQueue、APScheduler |
| RSS | feedparser |
| 元数据 / AI | OpenAI 兼容 LLM、LangGraph ReAct、Exa / Jina / TMDB / Wikipedia |
| 下载 | Transmission RPC |
| 前端 | React、TypeScript、Vite、Ant Design |
| 包管理 | uv、npm |
