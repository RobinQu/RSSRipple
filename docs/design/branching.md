# 分支与协作规范

本项目遵循 [Conventional Branch](https://conventionalbranch.org/) v1.1.0 分支命名规范。

### 分支命名格式

```
<type>/<description>
```

- 全部使用小写字母（`a-z`）、数字（`0-9`）和连字符（`-`）
- 禁止连续/首尾连字符、禁止空格、禁止下划线
- `release/` 分支的描述中允许多个 `.` 表示版本号

### 类型前缀

| 前缀 | 别名 | 用途 |
|---|---|---|
| `feature/` | `feat/` | 新功能 |
| `bugfix/` | `fix/` | Bug 修复 |
| `hotfix/` | — | 紧急修复（通常从 main 分出） |
| `release/` | — | 发布准备（如 `release/v1.2.0`） |
| `chore/` | — | 非代码任务：依赖更新、文档、配置 |
| `ai/` | — | 通用 AI Agent 生成的分支 |
| `copilot/` | — | GitHub Copilot |
| `cursor/` | — | Cursor |
| `claude/` | — | Claude Code (Anthropic) |
| `codex/` | — | OpenAI Codex |

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
claude/metadata-service
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

### 包含 Ticket 编号

若关联 Issue/任务追踪，将编号放在描述开头：

```
feature/issue-123-new-login
```

### 自动化工具

- 本地校验：[commit-check](https://github.com/commit-check/commit-check)
- CI 校验（GitHub Actions）：[commit-check-action](https://github.com/commit-check/commit-check-action)
- VS Code 插件：[Conventional Branch](https://marketplace.visualstudio.com/items?itemName=pshaddel.conventional-branch)

### CI/CD 与发布

生产 Metadata 验收统一位于 `tests/integration/metadata_corpus/`：Fast Gate 与 Docker Publish 的现有测试 job 执行离线子集，Strict Gate 两套 Compose 的默认集成收集执行同一组用例。单节点为临时 Turso，分布式为独立 `corpus-postgres` 测试服务（禁止复用 app 库）；单节点进程内覆盖率合入原 test-runner 统计。JUnit、审核覆盖率与逐场景语义差异在失败时同样上传，Compose 的报告目录为 `data/metadata-corpus/`。DeepSearch 的 8 个真实困难场景另冻结为 `tests/fixtures/deepsearch_corpus_v1.json.gz`，51 项证据契约/候选边界回归同样默认执行，`deepsearch-audit.json` 明确区分观察值和语义金标，不依赖 docs 挂载或真实模型。新增模型评测不得仅留在方案目录而没有明确的离线回归边界。不再维护独立 Metadata corpus workflow；真实 LLM 三轮质量评测仍是显式 CLI，不混入离线门禁。详见 [验收说明](../testing/metadata-corpus.md)。

- **CI Fast Gate**（`.github/workflows/ci-fast.yml`）：开发分支（`feature/`、`fix/`、`ai/` 等）及其 PR 的快速门禁——lint + 单元/API 测试（pytest-cov 覆盖率门禁：`app/` ≥ 95%）。
- **CI Strict Gate**（`.github/workflows/ci-strict.yml`）：`develop`、`release/**` 分支及其 PR 的严格门禁——lint + 单元/API（覆盖率 ≥ 95%）+ 集成测试（单节点 `docker-compose.test.yml` 的 app 服务在 coverage 下运行，测试后 `stop app` 落盘并由 `coverage-report` 服务校验 `app/` ≥ 85%；分布式 `docker-compose.test-distributed.yml` 以 `--scale test-runner=0` 启动，避免与显式 `run --rm test-runner` 双跑互相污染）。`main` 不在 push 触发范围内，但支持 `workflow_dispatch` 手动对任意分支运行。
- **Docker Publish**（`.github/workflows/docker-publish.yml`）：推送到 `main` 或打 `v*` 标签时触发，构建 **linux/amd64 + linux/arm64** 双架构镜像并发布到 GHCR 项目命名空间 `ghcr.io/robinqu/rssripple`。构建前以 lint + 单元/API 测试作为门禁（`build-and-push` 依赖 `test`）。
  - 推送 `main` → 标签 `:latest`、`:main`、`:sha-<短哈希>`
  - 打标签 `v1.2.3` → 标签 `:1.2.3`、`:1.2`、`:1`（基于 `docker/metadata-action` 的 semver 模式）
- 发布流程：在 `release/v1.2.0` 分支准备发布 → 合并到 `main` → 在合并提交上打 `v1.2.0` 标签触发版本镜像发布。
- 本地 pre-commit 钩子：`githooks/pre-commit` 在每次 `git commit` 前执行与 `docker-publish.yml` 的 `test` job 相同的 `uv run ruff check .`，失败则中止提交。一次性启用：`git config core.hooksPath githooks`；跳过：`git commit --no-verify`。
- **本地测试 Compose 隔离（强制）**：dev/生产栈的默认 Compose 项目名为 `rssripple`。本地跑单元/API 或集成测试所用的任何 `docker compose` 都必须用 `-p <唯一项目名>`（如 `rssripple-itest`）隔离项目名，由脚本内部调用 compose 时改用 `COMPOSE_PROJECT_NAME`；**禁止**默认项目名，否则会重建/停止正在运行的 `rssripple` 容器。CI 在专用 runner 上运行，无需该隔离。详见 [CONTRIBUTION.md](../../CONTRIBUTION.md)「测试」章节。
