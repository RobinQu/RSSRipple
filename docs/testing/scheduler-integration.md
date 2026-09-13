# 频道调度分布式回归

`docker-compose.scheduler-e2e.yml` 使用独立 PostgreSQL、Redis、Web、三个 worker 和确定性本地 RSS 服务。测试驱动 `scripts/scheduler_e2e.py` 通过 Web API 在 worker 启动后创建频道，验证首次与后续周期抓取、修改间隔和删除；暂停/恢复直接提交测试库的 Channel.status（现有更新 API 未暴露该字段）；无需外部 metadata 服务。

必须使用唯一项目名；栈不发布宿主端口、不读取 `.env`、不挂载生产数据，数据库使用 tmpfs，网络仅供测试容器互通。以下命令构建镜像并在退出时清理本次栈：

```bash
scheduler_test_project="rssripple-scheduler-$(date +%s)-$$"
export SCHEDULER_TEST_IMAGE=rssripple-scheduler-test:local
scheduler_compose() {
  docker compose -p "$scheduler_test_project" -f docker-compose.scheduler-e2e.yml "$@"
}
trap 'scheduler_compose down --volumes --remove-orphans' EXIT
scheduler_compose build web
scheduler_compose up -d --no-build --scale worker=3 web worker feed
scheduler_compose run --rm check
```

可复用具备项目依赖的本地镜像：把 `SCHEDULER_TEST_IMAGE` 改为该镜像并跳过 build。Compose 始终只读挂载当前 `app/` 和测试驱动，因此验证当前源码；这不替代发布镜像构建验收。每次完整验收使用空数据库，失败后先清理该测试项目再重跑。

驱动跨越多轮 30 秒对账，通常需要数分钟；成功应输出五项 PASS。可用同一项目的 `logs worker` 核对三个 worker 都有 `channel reconciliation` 记录。测试不承诺每个周期全局 exactly-once；现有 Redis active-key 去重仍可能在不同 worker 的错开时刻产生连续抓取。

更细的确定性覆盖见 `tests/unit/test_channel_schedule.py`：三个独立 APScheduler 的 fetch/refresh 分别更新、刷新开关、无变化时 next_run_time 不漂移、坏频道隔离、DB 故障恢复、旧自动任务跳过停用/删除频道。`tests/api/test_channels.py` 用独立事务验证首次任务入队前频道已提交。自动任务不取消已开始的抓取；暂停用例先延长抓取间隔并等待 worker 对账及当前抓取完成后发起。

## Redis 修订消费与 worker 故障恢复（V0 补验）

复用上方**唯一项目名**的隔离栈，保持三个 worker；镜像需含项目依赖，当前源码、驱动和真实语料只读挂载。以下所有命令均使用同一 `scheduler_compose` 函数，退出时沿用清理 trap：

```bash
scheduler_compose run --rm --no-deps check /app/.venv/bin/python -m scripts.scheduler_revision_e2e
scheduler_compose run --rm --no-deps check /app/.venv/bin/python -m scripts.scheduler_recovery_e2e seed
scheduler_compose kill -s SIGKILL worker
scheduler_compose ps -a worker
scheduler_compose run --rm --no-deps check /app/.venv/bin/python -m scripts.scheduler_recovery_e2e mutate
scheduler_compose start worker
scheduler_compose run --rm --no-deps check /app/.venv/bin/python -m scripts.scheduler_recovery_e2e check
```

必须确认 Web 健康后再执行 `--no-deps` 驱动。每次完整运行用空库；这些有状态阶段不得跳过 seed 或在生产项目执行。

`revision` 校验 v2 candidates 文件的 manifest SHA-256，复用真实 case `f79ef2eb-02d5-42d3-80dc-70dd3c1d733b` 原始标题；测试刻意不挂作品，使实际 metadata 门禁拒绝派发。Web 的三次修订必须分别由 Redis worker 持久化 AgentRun，旧资源仍被定向消费，消费水位线不动且无下载任务。驱动只初始化测试行、调用 HTTP 和读取结果，不直接入队、不调用生产 handler；不把这个场景声称为语义匹配或实际下载验收。

`recovery` 在真实抓取及自动刷新成功后，由外部 Compose 对三个 worker 注入 SIGKILL（预期退出码 137）；停机期间 Web 修改间隔/关闭刷新。重启后必须连续抓取，证明启动对账读到新配置；随后重新开启刷新，必须出现新的已完成 worker job。刷新用空作品集证明调度→队列→handler 的实际调用，不证明外部 metadata 更新质量；下载派发中的恰好一次执行和 B4 所有权仍需独立竞争测试。
