# 贡献指南

欢迎为 RSSRipple 贡献代码！请遵循以下规范。

## 分支命名规范

本项目遵循 [Conventional Branch](https://conventionalbranch.org/) v1.1.0。

### 格式

```
<type>/<description>
```

- 全部使用**小写字母**（`a-z`）、**数字**（`0-9`）和**连字符**（`-`）
- **禁止**连续连字符（`--`）、首尾连字符、空格、下划线
- `release/` 分支的描述中允许多个 `.` 表示版本号（如 `release/v1.2.0`）

### 常用前缀

| 前缀 | 别名 | 用途 |
|---|---|---|
| `feature/` | `feat/` | 新功能开发 |
| `bugfix/` | `fix/` | Bug 修复 |
| `hotfix/` | — | 紧急修复（通常从 main 分出） |
| `release/` | — | 发布准备 |
| `chore/` | — | 依赖更新、文档、配置等非代码任务 |
| `ai/` | — | 通用 AI Agent 生成的分支 |
| `copilot/` | — | GitHub Copilot 生成的分支 |
| `cursor/` | — | Cursor 生成的分支 |
| `claude/` | — | Claude Code 生成的分支 |
| `codex/` | — | OpenAI Codex 生成的分支 |

主干分支（`main`、`master`、`develop`）不使用前缀。

### 合法示例

```
feature/add-login-page
feat/agent-filter-dsl
bugfix/fix-sqlite-lock
fix/header-bug
hotfix/security-patch
release/v1.2.0
chore/update-dependencies
ai/refactor-auth-flow
feature/issue-123-new-login
```

### 非法示例

```
Feature/Add-Login       ← 大写字母
feature/new--login      ← 连续连字符
feature/-new-login      ← 描述开头连字符
fix/header bug          ← 空格
fix/header_bug          ← 下划线
release/v1.-2.0         ← 连字符与点相邻
unknown/some-task       ← 未知前缀
```

### 关联 Issue

若分支对应 Issue/任务，将编号放在描述开头：

```
feature/issue-123-new-login
```

## 开发流程

1. Fork 仓库或创建新分支
2. 按照上述分支规范创建分支
3. 进行开发并确保所有测试通过
4. 提交 Pull Request

## CI/CD 与发布流程

本项目通过 GitHub Actions 实现持续集成与持续交付：

- **CI Fast Gate**（`ci-fast.yml`）：`feature/`、`fix/`、`ai/` 等开发分支及其 PR 的快速门禁（lint + 单元/API 测试）。
- **CI Strict Gate**（`ci-strict.yml`）：`develop`、`release/**` 分支及其 PR 的严格门禁（lint + 单元/API + 集成测试）。
- **Docker Publish**（`docker-publish.yml`）：推送到 `main` 或打 `v*` 标签时，构建 **amd64 + arm64** 双架构镜像并发布到 `ghcr.io/robinqu/rssripple`。
  - 推送 `main` → 生成 `:latest`、`:main`、`:sha-<短哈希>` 标签
  - 打标签 `v1.2.3` → 生成 `:1.2.3`、`:1.2`、`:1` 标签
  - 构建前先跑 lint + 单元/API 测试作为门禁，避免发布破损镜像。

发布新版本的标准流程：在 `release/v1.2.0` 分支上准备发布 → 合并到 `main` → 在合并提交上打 `v1.2.0` 标签触发版本镜像发布。

### 本地 pre-commit hook（推荐）

为避免 lint 错误导致 CI 构建失败，仓库提供了 `githooks/pre-commit`，它会在每次 `git commit` 前执行与 `docker-publish.yml` 的 `test` job 相同的 `uv run ruff check .`；失败时提交被中止。

一次性启用（每个 clone）：

```bash
git config core.hooksPath githooks
```

自动修复：`uv run ruff check --fix .`。如需临时跳过（不推荐）：`git commit --no-verify`。

## 本地开发

```bash
uv sync
cd frontend && corepack pnpm install && corepack pnpm run build && cd ..
uv run uvicorn app.main:app --reload --port 9001
```

compose 文件会监听 `./app` 并热重载 Python。前端改动**不会**热重载 — 在 `frontend/` 下运行 `corepack pnpm run build`，或 `docker compose build app` 把新 bundle 重新打包进镜像。前端构建需要 Node.js 20.19+ 或 22.12+（Vite 8），包管理器为 **pnpm**（经 corepack，版本固定在 `frontend/package.json` 的 `packageManager` 字段）；Docker 镜像内的前端构建阶段使用 `node:22-slim` + BuildKit pnpm store 缓存挂载。

在三种数据库后端（旧 SQLite / Turso / PostgreSQL）之间搬数据，用 `scripts/migrate_to_turso.py`、`scripts/migrate_to_postgres.py`，迁移后跑 `scripts/verify_search_parity.py` 校验；完整矩阵与 Docker 部署迁移步骤见 [docs/design/db-migration.md](docs/design/db-migration.md)。

## 测试

**生产 Metadata 离线验收**已纳入两套集成 Compose 的默认收集，也进入 Fast Gate 和发布前门禁。单独运行：

```bash
CORPUS_REPORT_DIR=/tmp/metadata-corpus uv run pytest tests/integration/metadata_corpus -q --junitxml=/tmp/metadata-corpus/integration.xml
```

不需要外部 API key：torrent、源响应和模型响应均冻结。报告将待审核样本与已核验语义场景分开，不能把 torrent 清单通过率当作作品识别正确率。数据集更新、独立审核与真实 LLM 三轮评测见 [验收说明](docs/testing/metadata-corpus.md)。

> **强制要求：测试用 Docker Compose 必须使用独立项目名。**
> dev/生产栈的默认 Compose 项目名是 `rssripple`。任何为单元/API 或集成测试启动的
> `docker compose` 都必须显式隔离项目名，否则会**重建/停止正在运行的 `rssripple`
> 容器**（`app`、`worker`、`postgres`、`redis` 等）。统一使用 `-p <唯一项目名>`
> （如 `-p rssripple-itest`）；若命令由脚本内部调用 compose（如 organize E2E），
> 改用环境变量 `COMPOSE_PROJECT_NAME=<唯一项目名>`。跑完清理：
> `docker compose -p <唯一项目名> -f <compose-file> down -v --remove-orphans`。

**单元 & API 测试**（快速，本地 Turso；纯 `uv run pytest`，不需要 docker）：

```bash
uv run pytest tests/unit tests/api -v
```

**集成测试**（docker-compose）— 两个 profile。首次运行或改动 `app/` 后加 `--build`
重建镜像，避免使用过期镜像导致误报：

单节点（Turso + MemoryQueue）— 快速，无外部依赖：

```bash
rm -rf data/ && mkdir -p data   # 残留的数据库文件在 `down -v` 后仍会保留
docker compose -p rssripple-itest -f docker-compose.test.yml run --build --rm test-runner
# 单个模块：
docker compose -p rssripple-itest -f docker-compose.test.yml run --rm test-runner \
  uv run pytest tests/integration/http/test_channel_workflow.py -v --tb=short
```

分布式（PostgreSQL + Redis，两个 app 副本）— 验证多实例队列去重：

```bash
docker compose -p rssripple-itest-dist -f docker-compose.test-distributed.yml run --build --rm test-runner
```

需要持久网络客户端的测试（E2E、种子生命周期）在两个 profile 中都被排除；Redis 专用的队列测试在单节点模式下自动跳过。浏览器端 E2E（Midscene.js）的运行方式见 [docs/testing/midscene-e2e.md](docs/testing/midscene-e2e.md)。

**变异测试**（mutmut，Phase 1 试点）— 只变异确定性叶子模块（`pyproject.toml` 的 `[tool.mutmut] only_mutate`），用它们的快速单测建立基线：

```bash
uv run mutmut run       # 运行变异测试（可中断续跑，缓存于 mutants/）
uv run mutmut results   # 列出幸存/超时变异体
uv run mutmut show <mutant>   # 查看某个变异体的具体改动
uv run mutmut html      # 生成 HTML 报告（html/ 目录）
uv run mutmut browse    # 交互式 TUI
```

基线（9 个叶子模块）：~2300 变异体，变异分数约 77%。幸存变异体集中在 `genre_registry`（LLM prompt 模板）与 `metadata_wiki_classify`（分类正则）——多为「语义等价或非关键字符串」，对真盲区补测试、对等价变异体加 `# pragma: no mutate` 即可，不必强杀全部。

## 面向 Coding Agents 的 Spec 说明

如果你是在本仓库工作的 coding agent（Claude Code、Cursor、Copilot、Codex 等），按以下顺序阅读：

- **[AGENTS.md](AGENTS.md)** — 权威 spec 索引与核心约束速查；详细设计（数据模型、Filter DSL、API 端点、业务逻辑、前端路由、错误处理、分支规范）在 [docs/design/](docs/design/) 子文档中。这是*系统如何工作*的唯一事实来源。
- **[docs/README.md](docs/README.md)** — 文档总索引（按读者分组：用户 / 贡献者 / Coding Agent / 测试）。
- **[docs/design/constraints.md](docs/design/constraints.md)** — 核心约束完整速查（从 AGENTS.md 拆出）。
- **[ARCHITECTURE.md](ARCHITECTURE.md)** — 模块布局与运行时数据流。
- **[DESIGN.md](DESIGN.md)** — 设计 token 与视觉指引（仅前端）。

实现必须遵循 AGENTS.md。当代码与 AGENTS.md 不一致时，以 AGENTS.md 描述的行为为准 — 修复代码，或当设计确实已变更时更新 AGENTS.md。

## 工具推荐

- [commit-check](https://github.com/commit-check/commit-check)：本地校验分支名和提交信息
- [commit-check-action](https://github.com/commit-check/commit-check-action)：GitHub Actions 自动校验
- [Conventional Branch VS Code 插件](https://marketplace.visualstudio.com/items?itemName=pshaddel.conventional-branch)

## 参考

- [Conventional Branch 规范](https://conventionalbranch.org/)
- [docs/design/branching.md](docs/design/branching.md) — AI Agent 可读的完整分支规范
- [README.md](README.md) — 项目概览、快速开始与使用指引
