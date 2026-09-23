# 本地完整集成门禁（隔离数据）

`docker-compose.integration-isolated.yml` 使用独立命名卷、内部网络和固定测试凭证，不读取宿主 `.env`，不挂载宿主 `data/`，不发布端口。适合与正在运行的开发/生产栈并存。所有命令必须使用同一个唯一项目名；每轮使用新项目名，避免遗留数据改变结果。

## 准备镜像

`queue_recovery` 的 `multiwork` 参数使用已录制的 Cowboy Bebop TV+电影种子（27 个主视频）执行实际 inspection、作品 upsert 和关联写入。四项组合覆盖 LLM listing 无结果/有效混合结果与 attempt 保留/替换；正例检查 27 个指派、2 个链接及成功 LLM 电影指派，替换后检查新增作品、合集及关联整体回滚。metadata/LLM 身份为明确的合成替身，不代表公网识别质量；数据库由现有独立 scratch database 流程创建和删除。

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

## B4 通知队列恢复门禁（候选）

`organize_takeover` 参数使用真实 PG/Redis 与共享临时文件锁：旧子进程移动文件后在清理入口暂停，SIGSTOP 后等待默认租约自然过期，新消费者接管并获得计划忙碌错误。SIGCONT 后旧进程完成计划，其队列完成不能覆盖新结果；再次入队以新 job ID 幂等返回 done，目标字节保持一致。队列 handler 为专门测试适配器，忙碌作为显式结果记录；不宣称生产 handler 自动重试策略已由此覆盖。

`organize` 参数使用专用 PostgreSQL、临时媒体字节与真实计划文件锁，验证执行前两处失权不移动文件，以及开始移动后失权仍持锁至清理结束、另一会话收到忙碌错误、最终仅一次清理且目标内容正确。失权信号为替身，不等于真实 Redis 跨进程接管测试；锁目录明确隔离于临时目录。

`responsiveness` 参数运行独立 RedisQueue 子进程和另一真实消费者；录制种子的实际 inspection 中，将分析函数受控暂停 18 秒，保持默认 15 秒租约。父进程连续采样租约 TTL，要求全部有效且至少两次续租，最终执行计数恰好一次、子进程退出 0。该参数验证线程隔离下的真实续租，不代表所有 CPU 密集操作无 GIL 竞争。

`commit` 参数新增四项 PostgreSQL 内部提交断言：资源定向通知重新生成在快照返回后/投递身份作废后失权，旧集数协调在入口/发布 SQL 后失权。逐项重建合成夹具，通过独立会话验证快照、投递 token、集数和发布记录回滚；失权信号为确定性替身，不宣称 Redis 与 SQL 跨系统原子提交。

完整隔离 Compose 增加 queue-recovery-postgres / queue-recovery-redis，只供队列接管回归，均不发布宿主端口。test-runner depends_on 两服务健康，固定 QUEUE_RECOVERY_REQUIRED=1，并显式传入 QUEUE_RECOVERY_POSTGRES_URL / QUEUE_RECOVERY_REDIS_URL；缺失任意地址必须失败。普通无服务的局部 pytest 可跳过，不能据此宣称完整门禁通过。

`tests/integration/queue_recovery/test_notification_recovery.py` 每项创建独立 queue_recovery_UUID PostgreSQL 数据库，两个参数使用专用 Redis 的 DB 0/1；测试前要求为空，测试后清理。切勿指向非测试服务。driver 在独立进程中运行，隔离其他套件加速 sleep 的 monkeypatch，真实默认 15 秒租约/5 秒心跳。SIGKILL 与 SIGSTOP/SIGCONT 均须通过真实 loopback HTTP、作业 ID、token 和最终状态断言；90 秒超时终止整个测试进程组，临时数据库 finally 删除。整栈仍须 down --volumes 清理。

此为通知投递入口的合成快照回归，不代替完整 scheduler tick 或真实媒体快照生成验证。子进程没有额外配置 coverage 自动启动，不能把其执行计入原四份覆盖率；完整覆盖率门槛保持不变。

该子套件另含 metadata 参数，在独立新建的 PostgreSQL 库中验证九种作品并发交错：人工标题保护/显式覆盖、特典日期回填期间的人工修改/删除/改季/换合集、普通候选应用期间改季/换合集/删除。子进程复用单元层的相同断言，逐项清理本轮夹具表；不会调用外部海报或元数据源。缺专用服务仍由 REQUIRED=1 使门禁失败，不以跳过代替成功。

metadata 参数另覆盖八项资源事务断言，总计 17 项：电影/剧集×发布 flush/海报返回时失权四项，以及 MetadataAgent/旧链接×失权返回/抛异常四项。前者验证分阶段提交保留与回滚，后者验证部分字段 flush 后不提交发布。全部使用真实 PostgreSQL 会话与确定性网络/失权替身。

agent 参数在独立 PostgreSQL 库中运行六项事务断言：建议返回/待决策 flush 后失权分别覆盖请求保存点与后台独立事务；资源处理后/游标 UPDATE 后失权保留消费进度，恢复正常执行后消费相同发布。此参数使用合成资源及确定性失权替身，验证真实数据库事务，不作为真实 Redis 接管证据。

magnet 参数在独立 PostgreSQL 库中验证八项：缺少 attempt 列的旧库重复迁移保留历史状态、旧人工重试不清空新领取、旧 inspection 不覆盖新任务数据、旧协程等待时回收重新领取、sweep 在入口/回收 SQL 后/入队时失权的传播与回滚，以及实际子进程领取提交后 SIGKILL 恢复。最后一项独立会话确认 pending 和旧 attempt 后终止子进程，将测试行时间调旧，再执行实际 sweep 和新领取；使用 metadata_corpus_v1 录制种子字节，执行实际单文件 inspection，断言新 attempt、done、缓存字节与可解析性及持久化文件指派路径，记录 SHA256。仅网络解析使用替身，不证明真实 libtorrent 网络恢复或多作品 LLM 路径。数据库由父测试 finally 删除，专用服务缺失时 REQUIRED=1 阻止跳过。

`descriptor` 参数通过两个真实 Redis 连接验证恢复清理后的消费者重新领取：移除最后一个旧元素时键自动消失，新写入描述符不得被整表 DELETE 删除。交错由命令包装器控制，不代替 SIGSTOP 或租约过期测试。
