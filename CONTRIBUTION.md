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
- **CI Strict Gate**（`ci-strict.yml`）：`main`、`develop`、`release/**` 分支及其 PR 的严格门禁（lint + 单元/API + 集成测试）。
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

## 测试

```bash
# 单元测试和 API 测试
uv run pytest tests/unit tests/api -v

# 集成测试（单节点：SQLite + MemoryQueue）
rm -rf data/ && mkdir -p data
docker compose -f docker-compose.test.yml run --rm test-runner

# 集成测试（分布式：PostgreSQL + Redis，双实例）
docker compose -f docker-compose.test-distributed.yml run --rm test-runner
```

## 工具推荐

- [commit-check](https://github.com/commit-check/commit-check)：本地校验分支名和提交信息
- [commit-check-action](https://github.com/commit-check/commit-check-action)：GitHub Actions 自动校验
- [Conventional Branch VS Code 插件](https://marketplace.visualstudio.com/items?itemName=pshaddel.conventional-branch)

## 参考

- [Conventional Branch 规范](https://conventionalbranch.org/)
- [AGENTS.md](AGENTS.md#分支与协作规范) — AI Agent 可读的完整分支规范
- [README.md](README.md) — 项目概览和本地开发指南
