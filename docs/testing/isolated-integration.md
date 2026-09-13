# 本地完整集成门禁（隔离数据）

`docker-compose.integration-isolated.yml` 使用独立命名卷、内部网络和固定测试凭证，不读取宿主 `.env`，不挂载宿主 `data/`，不发布端口。适合与正在运行的开发/生产栈并存。所有命令必须使用同一个唯一项目名；每轮使用新项目名，避免遗留数据改变结果。

## 准备镜像

先准备项目运行时镜像 `rssripple-app:latest` 和测试服务器镜像 `rssripple-itest-test-server:latest`（后者使用 `tests/integration/Dockerfile`）。应用源码、测试和脚本在运行时只读挂载，因此修复 Python 代码后不必重建镜像；锁文件变更必须重新构建依赖镜像。此路径不验证前端构建；宿主 `app/static/` 应有构建产物，静态路由回归会检查挂载存在。

从仓库根目录构建依赖镜像。最小构建上下文避免发送本地数据或凭证；`--builder default` 使构建可引用 Docker daemon 中已有的运行时镜像。

```bash
gate_context=$(mktemp -d /tmp/rssripple-integration-image.XXXXXX)
cp pyproject.toml uv.lock "$gate_context/"
cp docker/Dockerfile.integration "$gate_context/Dockerfile"
docker build --builder default -t rssripple-v0-tests:local "$gate_context"
```

安装依赖需要网络，测试运行网络仅允许栈内通信。服务端外部提供者使用本地 mock；生产语料使用已捕获且附来源的离线夹具。真实媒体文件内容仍为明确声明的合成字节，不能把本套件通过解释为实时提供者质量或真实视频内容验证。

## 运行、汇总、清理

以下步骤顺序执行，记录每一步退出码。测试失败仍应导出报告并清理；不得把清理成功当作测试成功。不要在运行期间修改被测 Python 源码。

```bash
gate_project="rssripple-itest-$(date -u +%Y%m%d%H%M%S)-$$"
gate_artifacts=$(mktemp -d /tmp/rssripple-integration-results.XXXXXX)
docker compose -p "$gate_project" -f docker-compose.integration-isolated.yml up -d --no-build app app-llm test-server transmission
docker compose -p "$gate_project" -f docker-compose.integration-isolated.yml run --rm test-runner

# 停止服务，刷新 coverage 数据并释放 Turso 独占文件锁。
docker compose -p "$gate_project" -f docker-compose.integration-isolated.yml kill -s SIGINT app app-llm
docker compose -p "$gate_project" -f docker-compose.integration-isolated.yml ps -a
# 确认两个应用已退出后再运行；四份 coverage 数据缺失即失败。
docker compose -p "$gate_project" -f docker-compose.integration-isolated.yml run --rm --no-deps coverage-report
docker compose -p "$gate_project" -f docker-compose.integration-isolated.yml cp app:/app/data/. "$gate_artifacts/"
docker compose -p "$gate_project" -f docker-compose.integration-isolated.yml down --volumes --remove-orphans
```

默认收集 `tests/integration/`，排除 `eval/`、`external/` 和实时 feed 用例，范围与原完整离线集成套件一致。汇总 app、app-llm、test-runner 和 dedup 脚本四份覆盖率，严格要求 ≥85%；单元/API ≥95% 是另一道门禁。报告包含 `metadata-corpus/integration.xml`、`coverage.xml` 及 coverage 原始数据。服务退出非零、测试失败、报告缺失或覆盖率不达标均不得记为通过。

分布式 Redis 和多 worker 故障恢复需另跑 [频道调度专项](scheduler-integration.md)，本套件使用 memory 队列不能替代该证据。P0 与后续批次的必要性、来源和验收记录见 [VALIDATION.md](../plans/p0-and-backlog/VALIDATION.md)。
