# 集成测试清单（重组后）

## 认证器绑定安全回归

`security/test_auth_enrollment.py` 正式包含 Turso 与 PostgreSQL 两个入口，每个以五项断言组覆盖实际进程首次启动/重启不记密钥、真实伪终端运行绑定 CLI、拒绝重定向、要求显式 `--show`、导入 URI 后经生产 ASGI 认证路由登录且旧 Cookie/持久凭证不变。应用使用 web 角色与内存队列，执行实际 lifespan/DDL，未访问外部来源；认证 HTTP 为 ASGI transport，不宣称网络服务器覆盖。密钥均在专用空库自动生成，不使用生产配置。

Turso 的应用子进程完全退出后才由 CLI 打开文件；仅 dispose SQLAlchemy 不代表已释放底层独占句柄。PostgreSQL 沿用 `QUEUE_RECOVERY_POSTGRES_URL` 的专用管理库，每次建 `auth_enrollment_*` 子库并 finally 删除；完整隔离门禁设置 `QUEUE_RECOVERY_REQUIRED=1`，缺库必失败。父测试超时杀进程组，终端文件描述符 finally 关闭。

## 出站请求安全回归

`organize/test_admin_outbound.py` 使用真实本地 HTTP 和 Turso 投递，证明管理员配置的 Plex/Emby/Jellyfin/Wigolo/webhook 内网直连可用，307 不转发请求或凭证。webhook body 使用录制 torrent 的解析文件表，通知外壳为合成；拒绝后检查持久状态与退避。Wigolo 当前空重定向响应在 JSON 解析处报错，本组不宣称其错误分类已统一。

`security/test_sdk_outbound.py` 通过真实 Transmission/OpenAI/OpenRouter/LangChain SDK 与本地协议服务覆盖直连、同源/跨源重定向、409 与 SSE；batch 输入来自既有录制 torrent 的真实解析文件表，模型响应明确合成。`security/test_wiki_outbound.py` 覆盖真实 pageimages→REST summary 回退和私网重定向拒绝，以本地测试 CA 保持 TLS 证书校验。`unit/test_outbound_config.py` 与 `unit/test_outbound_dns.py` 验证 origin 规范化、非法配置、超时和解析容量边界。TLS 矩阵包含同步/异步 `SSL_CERT_FILE`。magnet E2E 经本地 HTTP 回放原 XML，不依赖生产接受文件路径；隔离 Compose 仅显式授权 `http://test-server:8080` 用于派生资源测试。

出站安全：`security/test_untrusted_outbound.py` 使用原录制 torrent 与本地 HTTP/TLS，覆盖生产 torrent/poster 缓存、镜像 infohash、私网重定向、混合 DNS、非标准数字地址、Host/SNI/证书及代理边界；`security/test_feed_outbound.py` 使用含真实磁力条目的既有 XML，覆盖管理员内网 RSS、本地路径/file URI 拒绝和 Basic auth 跨源隔离。公网 DNS/拨号映射、图片及证书为明确合成；不会访问生产或真实公网服务。

`security/test_sdk_outbound.py` 通过生产 Transmission wrapper / RSS 分析 helper 调用真实 SDK，验证私网初始地址、307 跨源拒绝、同源 RPC 跳转、409 session-id 协商及普通/SSE LLM；端点协议响应和凭证为合成。同步/异步连接的 TLS 用真实握手验证 SNI/Host、信任链和 hostname 拒绝。

> 重组完成于 2026-07-24 ｜ 分支 `refactor/integration-tests`
> 范围：`tests/integration/` ｜ 最新验收（2026-09-09）：**82 个测试文件 / 单节点 2351 passed + 17 skipped**（历史计数：重组前 16 文件 / 166 用例，重组去重 17 个）

> **运行约定（强制）：测试 Compose 必须使用独立项目名。**
> dev/生产栈的默认项目名是 `rssripple`；本地跑单节点/分布式测试栈时必须传
> `-p <唯一项目名>`，例如
> `docker compose -p rssripple-itest -f docker-compose.test.yml run --build --rm test-runner`，
> 禁止使用默认项目名（会重建/停止正在运行的 `rssripple` 容器）。分布式同理，用
> `-p rssripple-itest-dist -f docker-compose.test-distributed.yml`。首次运行或改动
> `app/` 后加 `--build`；跑完用 `... down -v --remove-orphans` 清理。CI 在专用
> runner 上运行，不需要该隔离。

## P0 补验与后续分批门禁（2026-09-12）

2026-09-13 新增[本地完整隔离门禁](isolated-integration.md)：独立项目名之外，进一步隔离命名卷与运行时网络，避免原配置 `.env` / `./data` 宿主挂载；本地并存运行优先使用该配置。当前运行结果见 VALIDATION.md，不沿用下方历史计数。

必要性论证、真实数据来源标准、集成通过条件和未完成证据见 [VALIDATION.md](../plans/p0-and-backlog/VALIDATION.md)。本轮新增 18 项：真实 torrent 清单整理 12 项、认证开启静态路径 5 项、真实内存队列到 AgentRun 持久化 1 项。与语料/整理既有套件合并 **1826 passed / 0 skipped / 1 既有 warning**，不等同于完整集成覆盖率门禁或所有 P0 集成补验完成。

## 频道调度专项回归

2026-09-12 新增 PostgreSQL＋Redis＋三 worker 的独立调度验收栈，操作与覆盖边界见 [scheduler-integration.md](scheduler-integration.md)。此专项不计入下列历史全量统计。

## 0. 2026-09 覆盖率门禁提升至 单元 ≥95% / 集成 ≥85%（验收完成）

2026-09-09 将单元/API 覆盖率门禁从 80% 提升到 **95%**（ci-fast / ci-strict 的 `--cov-fail-under=95`），集成覆盖率门禁从 80% 提升到 **85%**（`docker-compose.test.yml` 的 coverage-report `--fail-under=85`，docs/design/branching.md 与 AGENTS.md 同步）。单元侧新增/扩展约 500+ 用例，覆盖 batch_content_analysis、metadata_service、metadata_search_agent、job_handlers、worker、fts、task_queue、organize_*、torrent_inspect、magnet_resolve、metadata_agent、resource_association、database.py 迁移等此前低覆盖模块，单元+API 合计 **97%**。集成侧修复 `test_episode_history_coverage.py::test_only_earlier_absolutes_within_distance_are_used`：该用例早于「manual 锚定的远推外推」特性（aa78259）撰写，其 abs-29 行被标记为 manual 后在新语义下成为合法远推锚（S3E8），改为 `reconciled` 后恢复原意图（仅近邻窗口内的绝对集号参与），集成合计 **90%**，`--fail-under=85` 通过。

## 1. 目录结构

### 2026-09 集成覆盖率 ≥80% 批次（验收完成）

2026-09-06 完成集成测试验收并将覆盖率门禁从 75% 提升到 **80%**（`docker-compose.test.yml` 的 coverage-report `--fail-under=80`，ci-strict 步骤名与 docs/design/branching.md 同步）。为此新增 28 个测试文件 / 约 890 用例（全部进程内或打测试栈 HTTP，离线可跑）：

- `http/test_api_coverage2.py`（70）+ `http/test_api_coverage2_llm.py`（19，打 app-llm）：resources/organize/dashboard/queue/agents API 面的修订、associations、analyze-batch、files、magnet-resolve、计划生命周期等分支；两文件带完整 module 级 teardown（登记-删除/还原），不残留实体污染后续套件。
- `metadata/`：`test_batch_content_analysis_coverage.py`（37）、`test_torrent_inspect_coverage.py`（47）、`test_fts_coverage.py`（28）、`test_metadata_dedup_coverage.py`（17）、`test_filter_engine_coverage.py`（45）、`test_metadata_repository_coverage.py`（19）、`test_metadata_source_io_coverage.py`（13）、`test_episode_history_coverage.py`（26）、`test_metadata_wiki_judge_coverage.py`（21）、`test_metadata_agent_coverage.py`（49）、`test_metadata_search_agent_coverage.py`（17）、`test_feed_analyzer_providers_coverage.py`（21）、`test_metadata_bangumi_coverage.py`（34）、`test_metadata_audio_resolver_coverage.py`（11）、`test_fetch_service_coverage.py`（46）、`test_agent_service_coverage.py`（44）、`test_job_handlers_coverage.py`（17）、`test_misc_services_coverage.py`（59，required_fields/resource_confirmation/settings/volume/text_normalizer）、`test_wikidata_collection_coverage.py`（30）、`test_wikipedia_episode_parser_coverage.py`（33）、`test_download_paths_coverage.py`（24）。
- `organize/`：`test_organize_service_coverage2.py`（45）、`test_organize_template_coverage.py`（27）、`test_organize_parser_coverage.py`（17）、`test_task_cleanup_coverage.py`（12）。
- `magnet/test_magnet_resolve_coverage.py`（62，fake libtorrent/httpx）。

`test_fetch_service_coverage.py` 带 autouse 隔离 fixture（清 `runtime_config._overrides`、`fetch_service._WORK_METADATA_LOCKS`、`metadata_search_agent` 进程级缓存），对跨文件全局残留免疫。

验收结果（2026-09-06，本机 Docker）：单节点干净库全量 **2352 passed / 16 skipped / 0 failed**（28 分钟），coverage-report 合并 app + app-llm + test-runner + dedup 脚本四份数据后 **TOTAL 91%**，`--fail-under=80` 通过；分布式套件结果见第 4 节。期间修复一个真实测试缺陷：`test_metadata_local.py::test_relink_same_external_id_updates` 的 `alt_titles: ["Honzuki S4"]` 在作品单季化语义下被解析为第 4 季标记而新建季作品，改为无季标记别名后恢复「同 external_id 幂等更新」语义。

### 2026-09 生产 Metadata 验收接入

2026-09-06 补齐 `metadata_corpus/test_deepsearch_corpus.py`：51 项默认离线回归消费独立夹具 `tests/fixtures/deepsearch_corpus_v1.json.gz`，覆盖 8 个真实困难场景的 48 个模型观察值及生产候选边界；已知错误为负向契约回归，非新增语义金标。目录合计 939 项；来源、审核边界与报告说明见下方验收文档。

后续 DeepSearch 隔离实验新增 `metadata_corpus/test_deepsearch_eval.py`：17 项纯离线护栏测试，覆盖引用存在性与蕴含判断的区别、字段类型、未知值、答案防泄漏、公共读页 URL 限制及续跑来源一致性；不执行真实搜索/模型。真实困难样本、实验结果与未获准重构的原因见 [DeepSearch 验证方案](../plans/metadata-deepsearch-validation/README.md)。

新增 `metadata_corpus/`，将既有生产作品库/torrent 验收纳入两套 Compose 默认收集；共享工具仍在 `tests/metadata_corpus/`，数据位于 `tests/fixtures/metadata_corpus_v1/`。当前 871 项用例包含 843 个 torrent 清单回归、完整性/回放/报告护栏及一个完整语义场景（一个已审核资源重复入库）。不是 871 个作品样本通过；1,371 条资源仍待审核、526 条缺 torrent。

单节点使用临时 Turso；分布式使用独立 `corpus-postgres`（`metadata_corpus_test` + 每轮随机 schema），不复用 HTTP app 数据库。两者均严格离线回放，不启动真实 LLM/搜索质量评测，且不会继承 `http/` 的 test-server 播种 fixture。`data/metadata-corpus/` 保存 JUnit、审核报告与逐场景差异，现有 CI 上传为 artifact。用法与覆盖限制见 [验收说明](metadata-corpus.md)。下方原目录/计数保留为历史清单，不作为当前总数。

```
tests/integration/
  __init__.py
  conftest.py                      # 仅包注释；HTTP fixture 已下移到 http/conftest.py
  Dockerfile                       # test-server 镜像构建（不变）
  requirements-test.txt            # 不变
  INVENTORY.md                     # 本文件
  http/                            # HTTP 级集成测试（需 docker 栈：app + test-server [+ transmission]）
    __init__.py
    conftest.py                    # test_server / rssripple_url / http_client + setup_test_environment(session autouse)
    _http.py                       # 合并后的共享 helper：_api / _poll_fetch / _poll_run / _ensure_downloader / DEFAULT_FIELD_MAPPING / URL 常量
    test_channel_real_feeds.py     # Channel CRUD + fetch ground-truth + 字段映射 + 多格式
    test_channel_workflow.py       # test-server feed 冒烟 + LLM analyze-stream + edit-with-mapping
    test_channel_delete_and_token.py  # DELETE 级联回归 + X-Form-Token 防重放
    test_rss_subscription.py       # feed 校验（validate×3 + invalid_feed）
    test_agent_pipeline.py         # Agent CRUD + works + run + filter DSL
    test_downloader_pipeline.py    # Downloader CRUD + Transmission 连通性
    test_e2e_pipeline.py           # 完整 Channel->Agent->Task 链路
    test_metadata_api.py           # metadata HTTP API（search / detail / link）
    test_task_queue.py             # 后台 job 生命周期（Memory/Redis 后端）
    test_torrent_lifecycle.py      # test-server BT 协议链路
    test_fetch_with_real_feed.py   # 真实 nyaa.si + 实时 LLM（compose 默认 ignore）
    # ── 2026-08 覆盖率批次（integration ≥70% 目标）──
    test_agent_dispatch.py         # Mock downloader + rules-preview 回填派发 + 任务操作 + 派发错误路径
    test_decisions_flow.py         # ask 冲突 PendingDecision 全流程 + 集号修正 + suggestions
    test_metadata_local.py         # 本地 metadata 匹配（Layer2/3）+ FTS local search + manual link + CRUD + settings
    test_filter_dsl.py             # Filter DSL 操作符矩阵 + 保存期校验 422
    test_batch_dispatch.py         # 合集（is_batch）资源派发与去重
    test_misc_api.py               # validate-url / preview-feed / fetch 失败 / downloader 409 / works PUT
    test_dedup_seed.py             # 为 coverage-report 的去重脚本播种重复 series/movie 行
    test_llm_mock.py               # 打 app-llm（mock LLM）：analyze(-stream) + LLM 候选选择 + metadata ReAct + 解析变体
    test_metadata_sources_mock.py  # 打 app-llm：tmdb 单源 ReAct（假 key 快速失败）+ 废弃源 422（jina/exa/local/combined）
    test_transmission_actions.py   # 真实 Transmission RPC：磁力派发 + pause/resume/retry/delete
    test_notifications.py          # 下载通知全链路（打 app-llm，scheduler 开）：mock webhook 注册
                                   # → completed 自动建通知 → fan-out 投递 → 重试 → regenerate；
                                   # 派发资源先 link 带 genre 作品，断言 payload.work.genre 快照归一化
    test_genre_unification.py      # genre 统一：works CRUD 枚举 422 + OpenAPI 枚举渲染、手动 link
                                   # 写回归一化、DSL series/movie.genre 语义与 422、mock-LLM 钳制（app-llm）
    test_coverage_supplement.py    # 覆盖率补充：API Keys 全生命周期 + AuthMiddleware 401、合集 CRUD/
                                    # attach/detach/siblings、tmdb_collection 链接失败分支、
                                    # refresh-metadata 各源错误分支与 canned 填充（app-llm）
    test_tasks_api.py              # DownloadTask API 面：全局 GET /tasks 过滤 + 非法 status 422、
                                    # 手动创建（resource/downloader 404 + 成功 201）、动作 404、
                                    # batch-retry（暂停后处理 + 范围 + 错误路径）、delete_data 两种语义
    test_notifications_api_coverage.py  # 通知 API 次路径（主 app）：webhook CRUD/更新/删除 + 404、
                                    # 列表 status 过滤 + 校验、bulk retry、regenerate 无 completed 任务、
                                    # 通知/重试 404
    test_misc_coverage.py          # 杂项覆盖：/auth/status|otp(401)|logout、/volumes CRUD+重复 409+
                                    # dirs 错误路径、decisions 列表与动作 404、/dashboard、作品 404/校验
  external/                        # 直连 Python + 真实外部 API（无需 docker 栈，只需 API key）
    __init__.py
    test_metadata_agent_accuracy.py     # MetadataAgent.process_title_only 对 ground_truth_v1 准确率（LLM）
  organize/                        # organize 子系统进程内集成测试（无需 docker 栈；CI 的 test-runner 一并收集）
    __init__.py
    conftest.py                    # 复用 tests/unit/conftest.py 的 DB fixtures + 共享卷/RPC/Plex mock fixtures
    test_organize_pipeline.py      # 通知→规划→执行→落位→清理全链路（卷绑定解析、单集/合集/电影/待分类/恒等）
    test_notify_service_coverage.py  # notify 服务进程内覆盖：快照构建、create 幂等、fan-out、投递
                                      # （mock/HTTP 成功/失败退避→failed）、regenerate 重建/保旧、retry 重置
    test_scheduler_coverage.py     # 调度周期 tick 进程内覆盖：periodic enqueue、下载进度同步（状态迁移/
                                      # RPC 失败）、daily cleanup（决策过期/任务保留护栏/通知保留）、连通性检查
    test_task_queue_redis.py       # RedisQueue（fakeredis）进程内覆盖：worker/去重/状态/节流/consume=false
    README.md                      # 本目录说明 + 容器级半 E2E（docker-compose.organize-e2e.yml + scripts/organize_e2e.py）用法
  metadata/                        # metadata 纯函数/DB 服务进程内集成测试（覆盖率批次，复用 tests/unit/conftest.py）
    __init__.py
    conftest.py                    # 复用 tests/unit/conftest.py 的 db_engine/db_session fixtures
    test_metadata_db_integration.py  # metadata_dedup 合并系列/电影/跨类型 + collection_service 确定性 TMDB 链接
    test_feed_analyzer_coverage.py   # feed_analyzer 进程内覆盖：JSON 解析变体（含 \\[ 合法转义对修复）、
                                      # validate/confidence、analyze_feed 成功/无 key/重试/限流、stream 路径
    test_auth_service_coverage.py    # auth_service 进程内覆盖：cookie 签名/校验/过期、TOTP 校验、密钥 get-or-create
  metadata_corpus/                 # 真实生产样本离线验收（独立数据库与回放，不依赖 HTTP fixtures）
    conftest.py                    # audit 输出到可写 CORPUS_REPORT_DIR，不改只读 fixture
    test_dataset.py                # 输入/答案分离、校验和、数据库护栏、失败报告
    test_replay.py                 # 精确请求回放、凭证保护、网络隔离、次数与 LLM 失败断言
    test_torrent_files.py          # 全部冻结 torrent 的原始文件清单
    test_scenarios.py              # 冷库重建与重复入库、作品/集合/文件指派/门禁/派发
  magnet/                          # magnet 元数据解析进程内集成测试（复用 tests/unit/conftest.py；fixture 为生产
    __init__.py                    #   PriateBay-4K-Movies 频道真实磁力数据 tests/fixtures/magnet_feed.xml + magnet_links.json）
    conftest.py                    # 复用 tests/unit/conftest.py 的 db_engine/db_session fixtures
    test_magnet_resolve_e2e.py     # 抓取→解析 E2E：fixture feed 抓取 → magnet 资源落库 + 入队 → handler 认领 →
                                   #   重建 .torrent 缓存 → Channel A → files 链（fake libtorrent，CI 安全）；
                                   #   live 层 RSSRIPPLE_LIVE_MAGNET=1 才跑（真实 libtorrent/DHT，需 UDP 出网）
  test_metadata_core_integration.py  # 顶层纯函数覆盖率批次：wikipedia 剧集解析、集号 reconciliation、
                                     # anime 信号、wiki classify/query、url/parser/text 归一化、Filter DSL、
                                     # metadata_dedup 纯 helper、genre 注册表、failure 分类、wikidata 纯 helper
  eval/                            # 独立 Metadata Eval 应用（应用代码 + test_api.py，不变）
  server/                          # 假 test-server 应用代码（RSS / tracker / torrent / mock LLM）
    mock_llm.py                    # OpenAI 兼容 /v1/chat/completions：canned 字段映射、LLM pick、ReAct tool_calls 状态机
```

**两类测试已物理隔离**：
- `http/`：分布式 HTTP 测试，session autouse `setup_test_environment` 种子化 test-server。
- `external/`：直连 Python 打真实 LLM/TMDB/Exa，**不继承** `setup_test_environment`，可独立运行（只需 `LLM_API_KEY`/`TMDB_API_KEY`）。
- `eval/`：eval 应用测试，同样不继承种子化。

**mock-LLM 第二实例（app-llm）**：docker-compose.test.yml 中的 `app-llm` 服务与主 app 同镜像，
但 `LLM_BASE_URL=http://test-server:8080/v1`（确定性 mock）、独立 DB 文件、`SCHEDULER_ENABLED=true`。
`test_llm_mock.py` / `test_metadata_sources_mock.py` 通过 `RSSRIPPLE_LLM_URL` 寻址该实例（未设置时自动 skip，
如 distributed 栈）。覆盖率数据由 coverage-report 服务合并三份：主 app、app-llm、以及
`app.scripts.dedup_metadata` 脚本（去重无 HTTP 触发，由 coverage-report 在测试结束后对测试库运行，
`test_dedup_seed.py` 负责播种可合并的重复行）。

## 2. 用例计数

| 文件 | 用例数 |
|---|---|
| http/test_channel_real_feeds.py | 16 |
| http/test_channel_workflow.py | 15 |
| http/test_channel_delete_and_token.py | 8 |
| http/test_rss_subscription.py | 4 |
| http/test_agent_pipeline.py | 19 |
| http/test_downloader_pipeline.py | 7 |
| http/test_e2e_pipeline.py | 5 |
| http/test_metadata_api.py | 5 |
| http/test_task_queue.py | 16 |
| http/test_torrent_lifecycle.py | 5 |
| http/test_fetch_with_real_feed.py | 7 |
| http/test_agent_dispatch.py | 16 |
| http/test_decisions_flow.py | 14 |
| http/test_metadata_local.py | 28 |
| http/test_filter_dsl.py | 23 |
| http/test_batch_dispatch.py | 1 |
| http/test_misc_api.py | 13 |
| http/test_dedup_seed.py | 1 |
| http/test_llm_mock.py | 11 |
| http/test_metadata_sources_mock.py | 5 |
| http/test_transmission_actions.py | 3 |
| http/test_notifications.py | 1 |
| http/test_genre_unification.py | 10 |
| http/test_coverage_supplement.py | 13 |
| http/test_tasks_api.py | 10 |
| http/test_notifications_api_coverage.py | 8 |
| http/test_misc_coverage.py | 11 |
| external/test_metadata_agent_accuracy.py | 5 |
| organize/test_organize_pipeline.py | 6 |
| organize/test_notify_service_coverage.py | 14 |
| organize/test_scheduler_coverage.py | 7 |
| organize/test_task_queue_redis.py | 13 |
| metadata/test_metadata_db_integration.py | 6 |
| metadata/test_feed_analyzer_coverage.py | 19 |
| metadata/test_auth_service_coverage.py | 5 |
| test_metadata_core_integration.py | 85 |
| eval/test_api.py | 31 |
| **合计** | **262** |

## 3. 重组做了什么

### 删除（17 个重复用例）
- `test_e2e.py` 整文件（4）：`test_e2e_pipeline.py` 的薄弱重复。
- `test_channel_real_feeds.py::test_delete_channel_cascades_resources`（1）：与 `test_channel_delete_and_token` 的 SQLite NOT NULL 回归版重复。
- `test_channel_workflow.py::TestCreateChannelBasic`（6）：被 `test_channel_real_feeds` 的 CRUD/FetchGroundTruth 更彻底覆盖。
- `test_rss_subscription.py` 的 create_mikanani / create_eztv / list（3）：被 CRUD + 多格式覆盖。
- `test_rss_subscription.py::test_analyze_feed_generates_mapping`（1）：名不副实（只断言 200）、无 LLM skip 守卫，被 analyze-stream 覆盖。
- `test_metadata_search_agent_integration.py::test_search_metadata_inception`（1）：在 22 标题数据集内。
- `test_metadata_search_agent_integration.py::test_tmdb_chinese_title_spirited_away`（1）：在 22 标题数据集内。

### 合并 / 拆分 / 重命名
- `test_metadata_pipeline.py` -> `http/test_metadata_api.py`（改名，反映其只测 metadata API）。

### Helper 去重
- `_client` / `_api` / `_poll_fetch` / `_poll_run` / `_ensure_downloader` / `DEFAULT_FIELD_MAPPING` / URL 常量，从 6+ 文件复制粘贴 -> 上提到 `http/_http.py`。各 http 测试改为 `from tests.integration.http._http import ...`。
- `_poll_fetch` 标准化为 `_poll_fetch(channel_id, timeout=120, accept_failed=False)`；e2e/agent/metadata 的调用点（原本接受 done/failed）显式传 `accept_failed=True` 保留原语义。

### 解耦
- `setup_test_environment`（session autouse）从 `tests/integration/conftest.py` 下移到 `http/conftest.py` -> `external/` 与 `eval/` 不再被 test-server 种子化绑架，可独立运行。

### 修复
- `test_torrent_lifecycle.py` / `test_rss_subscription.py`：硬编码 URL 改读 env（`TEST_SERVER_URL` / `RSSRIPPLE_URL`）。
- `test_metadata_agent_accuracy.py`：补 LLM skip 守卫（无 `LLM_API_KEY` 时 skip 而非失败）；修正 `_DATA_DIR` 路径（迁移到 external/ 后用 `parents[2]` 定位 `tests/data`）。
- 改名 4 个名不副实的测试：`test_set_invalid_filter_field_rejected`->`_accepted_deferred`、`test_dataset_20_titles`->`_dataset_22_titles`、`test_status_reflects_all_transitions`->`_reaches_terminal_state`、`test_search_empty_may_fail_or_noop`->`_without_llm_key_degrades_gracefully`。
- `test_channel_workflow.py` 的 `basic_channel_id` / `channel_with_mapping` fixture 补 yield+teardown（不再泄漏 channel）。

### 配置 / 文档同步
- `docker-compose.test.yml` + `docker-compose.test-distributed.yml` 的 `--ignore` 列表改为 `--ignore=tests/integration/eval --ignore=tests/integration/external --ignore=tests/integration/http/test_fetch_with_real_feed.py`。
- `README.md` / `README_CN.md` / `.slim/deepwork/metadata-search-agent.md` / 各 docstring 路径同步更新。

## 4. 验证状态

2026-09-06 全量验收（本机 Docker，隔离 project 名避免与 dev 栈冲突）：

| 检查 | 结果 |
|---|---|
| `ruff check tests/integration/` | ✅ All checks passed |
| 单节点套件（`docker-compose.test.yml`，干净库全量） | ✅ **2352 passed / 16 skipped / 0 failed**（28 min），含 metadata_corpus 离线验收与全部覆盖率补充批次 |
| 单节点覆盖率门禁 | ✅ coverage-report 合并 app + app-llm + test-runner + dedup 脚本四份数据：**TOTAL 91%**，`--fail-under=80` 通过（门禁已由 75% 提升至 80%） |
| 分布式套件（`docker-compose.test-distributed.yml`，PostgreSQL+Redis，干净库全量） | ✅ **2306 passed / 62 skipped / 0 failed**（20 min；skip 为无 app-llm 实例与 Turso 专属用例的预期跳过） |
| 双后端回归 | ✅ `test_fts_coverage.py` 3 个主库 Turso 假设用例加 `requires_turso_main` skip 守卫；`test_api_coverage2.py::TestQueueApi::test_overview` 改为后端感知断言（memory/all/since_restart vs redis/web/last_24h） |

## 5. 已知遗留 / 延后

- **Channel 4->6 文件拆分、Agent 1->3 文件拆分**：原计划方案 2 的纯组织性拆分。考虑到无法在本地运行 docker 套件做运行时验证、且拆分涉及 class-scoped fixture 迁移（运行时风险高于 collect-only 能覆盖的范围），本轮保守保留为 4 个 channel 文件 + 1 个 agent 文件（已做 helper 去重与用例去重）。如需进一步拆分，建议在能运行 compose 套件的环境下逐域推进。
- `test_task_queue.py` 仍保留本地 `DEFAULT_FIELD_MAPPING`（与 `_http.py` 的完全相同，但该文件自包含、改动风险收益低，未强制合并）。
- `test_channel_workflow.py` 保留本地 `_poll_fetch`（done/failed 语义）与 `_list_resources`/`_stream_analyze*`（其 LLM fixture 链较脆，未强行换用 `_http._poll_fetch`）。
- `server/test_data.py` 的 `TestFile` dataclass 触发 `PytestCollectionWarning`（预存问题，非本轮引入）。

## 6. 历史

原 16 文件 / 166 用例的逐用例清单（含每条验证点、依赖、问题标注）见本文件 git 历史的重组前版本（commit 之前的 INVENTORY.md）。


2026-09-13 P0 分布式补验：独立 PostgreSQL/Redis/Web/三 worker 栈验证三次修订实际消费落库，以及全部 worker SIGKILL（退出码 137）→停机修改配置→重启连续抓取→重新启用自动刷新；全部通过。驱动与限制见 [scheduler-integration.md](scheduler-integration.md)，测试容器已清理。该记录不计入上方历史 pytest 用例数或完整集成覆盖率。


通知生成隔离：`tests/integration/organize/test_notify_poison_task.py` 使用已审核真实种子/清单正例及显式坏 RPC、flush 后故障，断言部分写入回滚、正常新任务和已有投递继续、退避与到期恢复；媒体内容为合成字节。`test_notification_build_retry.py` 检查退避上限、取消/删除、实际旧 schema 升级和 FK 级联。独立 PostgreSQL 进程及 SQL 屏障验证脚本位于 `docs/plans/p0-and-backlog/probes/notification_*_pg_probe.py`，严格使用可丢弃专用项目；不能把进程内 Turso 通过替代 PostgreSQL 并发门禁。


Agent 事务恢复：`tests/unit/test_agent_service.py` 的真实约束故障和实际 `_handle_run_agent` 两轮增量运行断言失败组回滚、正常组持久化、消费水位线保留、已成功组不重复派发；`tests/api/test_agents.py` 覆盖新建/编辑失败回填的请求原子性。独立 PostgreSQL 的真正 COMMIT 故障见 `probes/agent_commit_pg_probe.py`；已审核原始种子＋真实 Transmission 的后台和 API 重试见 `probes/agent_rpc_retry_probe.py`、`probes/agent_api_rpc_retry_probe.py`（均在 `docs/plans/p0-and-backlog/` 下），无媒体下载、须使用配套唯一项目及 internal 网络。


B9 定向补偿回归：`test_p0_queue_consumption.py` 覆盖真实队列已选资源后修订及旧版本确认竞争；`test_agent_request_persistence.py` 覆盖真实 Turso 事务、版本与退避；`test_agent_request_failures.py` 覆盖三个 HTTP 端点原子失败、broker 故障、作业错误恢复和生命周期；`test_agent_request_scheduler.py` 覆盖实际 APScheduler 触发与兄弟资源修复。跨进程 PostgreSQL/Redis、升级与级联、审核种子的真实 Transmission 故障重试驱动位于 `docs/plans/p0-and-backlog/probes/agent_request_*_probe.py`。后者仅允许专用隔离环境，数据真实性与运行限制见 VALIDATION.md；专项通过不能代替完整 95%/85% 门禁。


元数据身份接地回归：`tests/integration/metadata/test_identity_persistence.py` 对 Wikipedia judge、Wikipedia ReAct、TMDB ReAct 三入口，验证合法/非法主身份、伪造/可信别名、旧缓存五类场景；执行真实 process/upsert/cache/身份袋路径及重复处理幂等性。`test_identity_source_http.py` 启动随机 loopback 端口来源服务，验证生产 TMDB 搜索适配器字段契约与接地的正负例。PostgreSQL 矩阵驱动为 `docs/plans/p0-and-backlog/probes/identity_pg_probe.py`，只允许专用临时数据库。

容器 mock-LLM 实例使用 `tests.integration.server.llm_app:app` 测试入口，将 TMDB HTTP 请求转发给测试服务的合成协议端点；生产 app 入口不变。模型仍必须选择来源候选，音频等未获 TMDB 证据的身份必须拒绝。来源响应与模型为合成数据，不能称作真实来源录制；HTTP、数据库和生产适配器实际执行。仍要求合法正例、未知/类型冲突负例和完整 95%/85% 覆盖率门禁；Compose 必须使用唯一项目名。


合集归属回归：tests/api/test_collection_invariants.py 覆盖真实 API→commit→重新读库的创建/删除/解绑、身份袋清理、人工关联保留、多作品不猜合集、故障回滚、回填幂等及预加载父子关系。test_collection_resource_boundaries.py 每场景植入 103 条资源，交叉覆盖电影/剧集、删除/解绑、直接 FK/work_links/文件指派，验证跨页处理及不相关资源不变。test_database_migrations.py 对实际 Turso create_tables 连跑两次验证旧孤儿修复，PG 分支单测只验证调用顺序。

PostgreSQL 实际回填并发、预加载关系、重复删除及反向壳吸收的驱动位于 docs/plans/p0-and-backlog/probes/collection*_pg_probe.py。它们会清空明确指定的回环地址 organize_test 专用库，必须配独立 Compose 项目；禁止指向业务库。数据为明确的合成 ORM 行，SQL/事务/锁行为真实。专项不替代完整单元/API 95% 与隔离集成 85% 门禁。

### 合集单季唯一性与升级预检

`test_collection_season_index.py` 通过真实 Turso 新装/升级与人工编辑、Episode、身份袋保留验证；`test_collection_season_conflicts.py` 覆盖 409 和非季号异常不误分类；`test_collection_conflict_report.py` 覆盖 103 个冲突成员流式报告与只读 SQL。PostgreSQL 复现驱动在 docs/plans/p0-and-backlog/probes/collection_season*_pg_probe.py，分别验证双启动、写入竞争、检查后竞争、两个 HTTP ASGI 请求抢槽及实际 CLI。数据为标注合成数据，不冒充在线来源录制。

D1 写入路径回归覆盖去重继承合集的删除/赋值顺序、Wikidata 同季冲突的 apply/dry-run 拒绝及标签保留；Bangumi 旧默认季号修正用独立壳合集作为合法初态，人工季号与播出日期保护断言保持。


D2 升级外键验证：`tests/unit/test_upgrade_foreign_keys.py` 使用真实 Turso 验证七 FK × 新装/缺列/已有列缺约束、非法 INSERT/UPDATE、父项 DELETE 的 SET NULL/NO ACTION；包含带 Episode/资源/身份袋/人工保护/历史额外列和索引的升级及脏数据拒绝、换表后注入故障与恢复。`test_upgrade_fk_related_data.py` 验证多表重建保留下载任务、通知、RESTRICT 规则及 SET NULL 计划，末尾失败整批回滚。`test_upgrade_fk_report.py` 验证预检全程只读并完整导出跨页 103 个悬空引用。`test_turso_fk_rebuild_capability.py` 记录后端事务、索引/触发器与回滚能力边界。

PostgreSQL 真实 schema/动作矩阵、脏数据回滚、双进程启动锁等待与恢复使用 `docs/plans/p0-and-backlog/probes/upgrade_foreign_keys*_pg_probe.py`，必须只运行于探针检查允许的独立本地测试库，报告与 Compose 清理证据见 V9 计划。上述定向测试不能替代完整单元/API ≥95% 和隔离集成 ≥85% 门禁。

### 退役季数字段清理

`tests/api/test_retired_season_fields.py` 验证实际 POST/PUT 拒绝退役字段且无副作用、合法单季 Episode 写入；`tests/unit/test_retired_season_cleanup.py` 覆盖真实 Turso 指纹、关联/人工字段保留、过期与矛盾证据拒绝、回滚、幂等、旧派生字段不被重写。`tests/integration/season_model/test_retired_field_review.py` 使用原始 prod_works_v1 录制夹具，只读导出 27 个带旧计数作品；实际合集回填后 8 个明确单季样本经审核清理，其他 19 个保留原值。录制夹具不代表当前生产库。实际 PostgreSQL CLI、并发阻塞和故障回滚证据见 p0-and-backlog/probes/retired-season-cleanup-pg-result.json。

## V11 决策覆盖验证标准（原型，尚未完整验收）

- 覆盖证据：单季半包/整季、区间顺序与缺口、多季 links-only、未知指派；实际 process_resources 验证派发、冲突与跨运行去重，不能只测哈希函数。
- 持久化：真实 PostgreSQL 双连接同槽创建；Turso 锁重试；审核旧表、P8 拆季衔接、启动拒绝未审核数据、事务回滚与历史/候选保留。
- 确认并发：规则/候选在模型等待期改变时必须拒绝；最终确认持锁期间资源、指派、链接及资格元数据修改受保护，提交后释放；实际资源修订与持久请求写入须无反向锁死。模型、下载或唤醒替身必须在结果中标明，函数级探针不能冒充 HTTP/真实下载器端到端验收。
- 原始数据回放：保留 `tests/fixtures/prod_works_v1.json` 原始快照及 SHA-256；已有审计含 559 个资源、101 个批量资源和 3 个已处理单候选历史决策，不能把它宣称为生产多候选 pending 证据。未知文件指派应保持未知；严禁补造捕获数据的季号或范围以求通过。缺失的并发/混合决策边界用明确合成数据补充。
- 最终门禁：单元/API 覆盖率 ≥95%，隔离完整集成覆盖率 ≥85%，测试及应用进程退出码、覆盖率导出、唯一 Compose 项目清理、源文件快照均需确认。限制失败数的诊断不计验收。

当前专项与缺口见 [V11 记录](../plans/p0-and-backlog/V11-DECISION-COVERAGE.md)；相关临时探针保存在该目录 `probes/`，未完成的完整门禁不可标记通过。

### D6 作品删除验收

- `season_model/test_work_deletion_captured_review.py`：未改写录制图中人工工作链接和文件指派的删除阻断。
- `season_model/test_orphan_identity_cli.py`：真实磁盘 Turso 与 CLI 子进程的导出、禁止覆盖、明确应用、重复执行。
- `season_model/test_orphan_identity_captured.py`：录制图 249 条有效身份无误报且全字段保持不变。
- PostgreSQL 插入/更新、身份登记、决策创建/确认、合并竞争及离线清理锁专项见 `docs/plans/p0-and-backlog/probes/work-deletion-*-pg-result.json`；合成边界与录制来源分别记录。


### V13 隔离原型：发布事件 feed 集成（未合入）

`tests/integration/organize/test_publication_feed.py` 三参数场景在新 Turso 子进程库运行，驱动实际 fetch_channel_resources / _process_resource_metadata / Agent handler。confirmed corpus case f79ef2eb-02d5-42d3-80dc-70dd3c1d733b 提供真实标题、torrent 和独立 S1E10 审核，验证 torrent/listing SHA 与解析结果；raw_rss_available=false，RSS 外壳明确合成，网络识别由审核适配替代，torrent 缓存/检查边界和队列唤醒被替换。断言正常 created+metadata/派发去重、created 事件失败时资源同事务回滚、metadata 事件失败时关联回滚并在重试后恢复消费。媒体没有下载。JUnit 附来源属性，3 passed；不代替 PostgreSQL/Redis 完整分布式验收。

V13 jh 扩为四参数场景：模拟 fetch 最终 enqueue 抛错，随后 dispatch_pending_publications → 实际运行的 MemoryQueue → handler 恢复，完成后再次 tick 不重复建作业。4 passed；不是 Redis 分布式证据。

V13 ji/jk 独立 PG/Redis 探针：较早创建晚提交资源由下轮 handler 派发；擦除专用 Redis 队列状态后两个新进程恢复同一 job_id，数据库任务唯一。原始 red/green 与结果位于 p0-and-backlog/probes，完整容器门禁仍待跑。

V13 发布补偿定时集成：`organize/test_publication_scheduler.py` 使用真实生产五秒 IntervalTrigger、MemoryQueue、handler 和临时 Turso；暂停经历一次 tick 后仍待消费，恢复后自动产生一条成功 AgentRun 并确认游标。仅移除无关定时任务，不手动调用发布回调或缩短周期。数据为合成资源；此用例不证明外部下载副作用。

V13 FTS 并发回归：`organize/test_fts_concurrency.py` 在独立子进程和临时 Turso sidecar 上运行生产 FTS 初始化、写入和检索；8 个并发调用者共 400 写、400 读，要求进程正常退出、无操作异常、最终索引恰好包含 10 个唯一预期 ID。数据明确为合成压力夹具；进程隔离保留原生崩溃输出。此测试不替代完整 HTTP/调度负载门禁，也不证明此前 page_cache 崩溃与已复现 btree 崩溃同源。

V14 派发持久化原型测试：test_download_dispatch 验证预留复用、参数漂移、不同逻辑操作、结果复用、删除后不重建与直接 SQL 默认值；test_queue_download_ownership 使用临时真实数据库覆盖八种身份/连接状态。真实 PG/Redis/Transmission 的已审核 torrent 接管探针见 V14 文档：两次 RPC 接受仍仅一条持久任务，无媒体下载。这些证据不替代全量门禁或其他 handler 验收。

V14 md/me/mf 补验：实际 NOT NULL 故障回滚后预留保持 unsettled，重试复用 UUID；同作业目录/payload/下载器漂移在第二次 RPC 前拒绝。真实 PG 的延迟外键约束使 COMMIT（不是 flush）失败，已审核 torrent RPC 接受后重试复用同一任务 UUID，最终一任务一 torrent。该探针显式在同一逻辑 handler 内换事务重试，不宣称验证了生产自动重试或进程崩溃恢复。

V14 mg 崩溃探针使用两个独立进程和真实 PG/Redis/Transmission：A 在真实 RPC 接受后 SIGKILL（-9），B 等待默认 lease 自然过期后通过生产队列自动接管（退出 0）；保留 job_id/task UUID，最终一任务一 torrent。使用 confirmed torrent、无媒体下载；handler 为专用薄包装，不表示全部生产 job handler 已验收。mh/mi 连接绑定测试先复现外层回滚抹掉预留，再验证独立引擎连接修复。

V14 派发表升级正式集成：organize/test_download_dispatch_schema.py 在新子进程构造仅缺 download_dispatches 的旧 schema，通过生产 create_tables 连续两次执行，保留旧 AppSetting 与首次升级后预留；直接 SQL 验证 operation_key/task_id 唯一、settled 非空和 false 默认值。默认 Turso 入常规集成收集；同驱动在专用 PostgreSQL 项目亦验证通过。数据为合成迁移夹具，不涉及 RSS 或 RPC。

V14 预留清理测试 test_download_dispatch_cleanup 覆盖 Redis 终态/替换/缺失、queued/running/active 孤儿/未知状态、年龄、缺身份、Redis 故障、分页越过活动记录、数据库 settled 竞争以及已有任务不删除。使用真实临时 DB、FakeRedis 的生产 RedisQueue 判断；不替代真实 Redis 清理集成。每日 scheduler 回归确认数据库绑定和事务外接线。

V14 mq 在真实 PG/Redis 中确认退休孤立记录删除、queued/running 保留；任务事务先持有预留行锁，清理 DELETE 在 pg_stat_activity 显示 Lock 等待，任务提交后 DELETE 影响零行，任务及 settled 预留保留。mr 在实际队列上下文/临时 DB 下验证不同 Agent 或新逻辑作业（同 queue key）各保留独立任务，下载器替身返回同一 torrent ID 也不会错误合并。

V14 通知所有权回归 test_queue_notification_ownership：生产队列上下文、临时数据库和 HTTP 替身，覆盖发送前失权不发送、发送后失权不确认，以及同批异常传播前等待在途 HTTP 协程完成。负向 ms/mu 与正向 mv 证据见 V14；不证明外部消费者的 exactly-once，也未替代真实 HTTP 接管竞争。

B4 通知候选增加旧结果覆盖和 retry/两类 regenerate 在途失效测试。download_dispatch_schema_driver 同时删除旧表的 attempt_token，再通过生产 create_tables 验证恢复 nullable 字段。HTTP 测试目前使用替身；独立进程真实 HTTP 接管及 PostgreSQL 新列升级尚待验证。

B4 双会话同 token 领取测试通过 barrier 保证双方均先读取；Turso 失败方应通过独立短事务重试得到 CAS 未命中，不重复 HTTP。nc 保留 write-write conflict 红测，后续回归不可用单会话测试替代。

B4 nf/ng 独立验证：真实 PG/Redis + loopback HTTP，默认租约下 SIGKILL 恢复及 SIGSTOP/SIGCONT 旧执行失权均通过；使用合成 payload 和专用 handler，尚未纳入常规集成执行入口，不能代替完整通知 tick 验收。驱动与证据见 V14。

B4 正式 queue_recovery 子套件包含 SIGKILL、SIGSTOP/SIGCONT、metadata（17 项）、agent（六项）、magnet（八项）、commit（四项）、responsiveness、organize（三项）、organize_takeover 和 multiwork（四项）十个参数。commit 覆盖内部提交失权回滚；responsiveness 验证真实续租；organize 验证 PG/文件锁边界；organize_takeover 将真实 Redis 接管、SIGSTOP/SIGCONT 和计划文件锁组合，验证旧完成不覆盖新结果及重入幂等。其余矩阵覆盖作品/资源事务、Agent 消费、magnet 迁移与恢复。完整 isolated Compose 开启 QUEUE_RECOVERY_REQUIRED=1，专用 PG/Redis 缺失即失败。数据库矩阵的失权信号为确定性替身；magnet 使用录制种子，未连接 libtorrent 网络。详细隔离/清理契约见 isolated-integration.md。

B4 nk/nl 覆盖快照停种前、停种后、文件返回后失权，要求停止 RPC 链、无快照和无失败退避记录；nl 联合既有构建重试/毒任务隔离回归通过。下载器与 guard 使用替身，真实 Redis/RPC 组合仍待补充。


M1 正式 `metadata/test_manual_mapping_concurrency.py` 包含 PostgreSQL 九项和 Turso 六项。使用录制发布标题 `00e48de8-b5fd-4e99-8467-d384bd4a3183`、真实 associations/works-merge ASGI 路由和独立数据库会话；作品、候选响应与缓存/本地命中结果为合成，队列唤醒替换，不覆盖认证中间件或实时提供者。覆盖外部查询期间编辑（含改为 franchise 的形态编辑）、已有人工映射、缓存/本地快捷返回、查询期间合并及后续恢复；PostgreSQL 另验证资源锁等待、候选写入前合并及唯一约束错误不被吞。沿用隔离门禁 `QUEUE_RECOVERY_POSTGRES_URL` 和 `QUEUE_RECOVERY_REQUIRED=1`：缺 PostgreSQL 时必失败；每项独立建库，finally DROP DATABASE。Turso 使用 pytest 临时文件并启用 MVCC，简化建库未配置 FTS sidecar，FTS 正常路径仍由现有套件承担。

### OTP 持久额度

同文件新增 `test_otp_rate_limit_turso_bursts`：独立子进程、真实 Turso MVCC 与生产退避时序，不加载 pytest 的快进 sleep 夹具；12/40 并发各重复五轮，要求每轮恰好 5 次放行、其余拒绝且无数据库异常，落库来源/全局额度分别为 5/min(并发数,30)。来源地址与窗口时刻是合成数据，事务/重试真实执行，进程退出后临时文件由 pytest 清理。

单元/API 使用真实 Turso 验证独立会话并发、失败计数持久、全局来源轮换、时钟边界、清理、伪造转发头、schema 错误及真实 TOTP/Cookie。tests/integration/auth/test_rate_limit.py 使用 QUEUE_RECOVERY_POSTGRES_URL 专用 admin 库，自建 auth_limit_* 数据库，四个独立 Python 进程竞争来源/全局额度，验证进程退出后计数、旧库幂等新增表与恢复清理。QUEUE_RECOVERY_REQUIRED=1 时缺环境必失败；父进程 finally 删除专用库。地址/时间为合成数据，TOTP 密钥仅在测试库生成。该专项验证共享预算服务，HTTP 边界由 API 测试验证，不声称启动四个 HTTP web worker。

同一正式入口另有 HTTP 参数：两个独立 Uvicorn 进程使用生产 app 路由/认证中间件/异常处理器及同一专用 PostgreSQL。真实 TOTP 并发验证来源上限，跨进程 Cookie、替换服务进程后预算保留、八个实际回环源地址的全局额度，以及 401 后计数和过期恢复。服务器禁用代理头信任，不连接代理或公网；lifespan 关闭，建表与测试密钥由驱动准备，不能据此声称完整部署启动流程已验收。父驱动超时会杀死其进程组，HTTP 子进程均受 finally 清理，随后删除专用库。
### Metadata 季证据与失败缓存

M2 正式 `metadata/test_collection_season_evidence.py` 在专用 PostgreSQL 子库执行 38 项真实断言：标题/合集身份袋的 16 种季证据组合、2 种显式人工目标、16 种录制标题经 repository/MetadataAgent/成功缓存/实际抓取元数据事务的资源写入，以及系列身份/季身份各两种无季号外层兜底反例。复用 Turso 单元矩阵的断言并独立 commit/观察，不把 mock upsert 当作持久化证据。原始标题取录制 case `011c6d44-68cf-43a8-bad3-f0398ce20a95`；合集成员、TMDB ID 和来源响应明确合成，语料中历史候选 season 不作为真值。未知季必须保持 resource 工作 FK 空、collection 关联与 ambiguous，且不新增季作品；显式季号正例须实际创建/关联。沿用 QUEUE_RECOVERY_REQUIRED=1 缺 PG 必失败，独立建库与 finally 删除，子进程超时 120 秒。

M3 候选 `metadata/test_wiki_failure_cache.py`：专用 PostgreSQL scratch database、独立进程运行 12 项断言，其中 Agent→真实缓存数据库故障/恢复 4 项（全部/部分搜索失败 × web 禁用/成功空结果）；另有 web 成功/未命中两项编排，以及页面重试与 TMDB 源适配器编排。录制原始标题，来源响应和模型故障显式合成；缺专用数据库且 REQUIRED=1 时失败，最后删除测试库，不代表公网识别质量。


## 过期清理与权威关联

`organize/test_cleanup_associations.py` 使用已录制 Cowboy Bebop TV+电影种子（SHA 912c2bd…，27 个主视频），执行真实编辑向导关联写入/挂合集 helper 后调用自动和手动清理。断言资源、两条关联和 27 条指派保留，原始 torrent 字节不变；未处理资源正常删除。工作身份、年龄、频道开关与用户选择季 1 为明确合成输入。补充指派边界区分未绑定 auto/llm 与人工/已绑定数据。

`organize/test_cleanup_postgres.py` 在唯一 scratch PostgreSQL 数据库重放相同六场景，并用真实 FK 锁、生产编辑向导及 pg_blocking_pids 观察清理/提交竞争。完整隔离门禁复用指定的 queue-recovery-postgres；缺失其配置且 QUEUE_RECOVERY_REQUIRED=1 时失败，不跳过。普通局部运行可通过 CLEANUP_TEST_POSTGRES_URL 提供明确的 cleanup_probe 管理库。所有 scratch DB finally 删除。


清理并发扩展：PostgreSQL 验证 501 条跨批处理、忙碌行跳过、完整回滚和 NOWAIT 锁释放；Turso 对三类保留资源的子表 INSERT/UPDATE，分别建立真实 SAVEPOINT 与先前写入的旧快照，验证整笔冲突重试后父子行保留。新建/重复升级检查六个触发器；父表 DROP 后、RENAME 前注入故障，检查旧表、父子数据和触发器一起恢复，再验证成功升级。API 入口用真实 get_db 提交，scheduler 调用实际 _cleanup_expired；HTTP 使用 ASGI，明确不宣称浏览器或网络服务器级端到端验证。
### CORS 边界专项

`security/test_cors_policy.py` 使用生产 ASGI 中间件栈与明确合成来源/凭证，验证预检、未授权来源、401/422/404/500、无 Origin、默认空白名单及 SSE；异常/流端点为临时测试路由，不访问真实业务数据。`test_cors_config.py` 验证配置默认、JSON 环境输入和拒绝非法 origin。手工 Cookie 只检查头处理，不能当浏览器 SameSite/CSRF 证据；浏览器专项使用下述可复用入口。

CORS/来源防护扩展覆盖不可信 Origin/Referer/Fetch Metadata、真实 logout Set-Cookie 副作用、同源与白名单退出、合法请求模型自动 422。Chromium 的独立回环探针使用生产 TOTP 登录及临时 Turso：拒绝来源 logout 后仍 authenticated=true，白名单 logout 后 authenticated=false。该探针记录在 V22 计划证据中，已整理为下述可移植入口；不把浏览器 API 无法返回的请求头作为 Cookie 未发送证据。

可复用浏览器入口为 `tests/browser/cors_fixture_server.py` 与 `tests/browser/cors_policy.cjs`，运行及依赖说明见同目录 README。真实临时数据库在退出时删除，浏览器与页面服务 finally 关闭。专项新增重复 Origin/Referer、伪造转发头、受信代理还原的 HTTPS scope、GZip/Vary 与 poster 挂载认证。
Agent 作品上限专项通过真实 API/Turso 检查更新后的有效 scope/works；并发添加用例协调真实 SQL 前的时序，观察实际数据库冲突并由生产 HTTP 中间件重试，最终 201/400 且总数 10。请求重放单元矩阵的 DatabaseError 为明确合成，覆盖分块/大请求/部分读取、响应已发送、取消和上限；不能将其作为真实数据库冲突证据。PostgreSQL 与混合编辑并发证据见下述双库扩大验证。

Agent 上限双库扩大验证：`agents/test_work_limit_postgres.py` 对 add/add、replace/add、scope/add 调用真实生产 API 与 get_db，观察 pg_blocking_pids 确实等待后再释放父锁，第二请求重新读取并拒绝超限。专用 scratch DB finally 删除，完整门禁 REQUIRED=1 缺 PG 必须失败。对应 Turso API 场景观察真实 DatabaseError 和生产 HTTP 重放，保留 10 条；两种数据库都不以伪造 SQL 结果或锁异常替代证据。

`tests/api/test_request_replay_production.py` 通过完整 app.main ASGI 栈、认证/GZip/真实 get_db 和临时 Turso 验证小体及大于 1 MiB 的请求：首次 flush 后注入明确合成冲突，重放后新会话只查到一条 Movie。用于生产栈与事务回滚组合验证，不将注入冲突混作真实 MVCC 证据。


### 重解析请求生命周期（候选，待完整集成验收）

`tests/api/test_reparse_durability.py` 使用真实临时 Turso/API/dashboard 查询验证：成功/普通失败各三种人工忽略时序、真实 TCP 拒绝连接后可见性及补发、重复请求身份稳定、commit 后省略 enqueue 的恢复及旧 UUID 确认隔离。元数据处理明确为合成注入，省略 enqueue 不是进程崩溃实验。正式 Redis worker、双库并发、生产迁移、有界补发及录制输入覆盖见下文；部署回退及完整门禁仍须验收。

正式生命周期集成位于 `tests/integration/resources/`，默认纳入 integration 收集；需要显式 REPARSE_TEST_POSTGRES_URL/REPARSE_TEST_REDIS_URL 或复用独立门禁专用 QUEUE_RECOVERY_* 服务，QUEUE_RECOVERY_REQUIRED=1 时缺少服务直接失败。每测创建并删除 PG scratch DB；Redis 使用专用测试服务 DB14，只删除本例已知键，不 FLUSHDB；不导入 API conftest 的全局 sleep 加速。

覆盖真实 PostgreSQL 锁等待下重复 HTTP 提交；实际 Redis producer/worker 所有权与成败收尾；子进程 DB commit 后 os._exit(97)、处理中 SIGKILL(-9) 的补发/同一 Redis job 恢复；生产 create_tables 双库两次升级、历史标记保留、唯一约束、请求事务回滚和资源 FK 级联。元数据提供者为明确合成替身，这些测试不等于真实元数据管线回放；没有将请求事务回滚宣称为应用版本回滚验证。

录制输入重解析覆盖：`test_reparse_captured.py` 在 PG/Turso 使用语料 `affd5114-f61d-40b6-95f1-0d0b937495ab` 原始已录制标题与 SHA-256 为 `6936d731675641e065b7af4c3780b3c36c27b2782e65fc3989a6557b0cff75c3` 的原始 torrent。完整生产 `_process_resource_metadata` 执行缓存读取、合集识别、12 条文件指派、metadata publication 和提交，任务确认后因身份仍未知恢复待确认。身份提供者明确替换为离线未匹配结果，队列投递使用替身；未下载媒体，不宣称 RSS 原文存在或实际外部元数据搜索通过。
