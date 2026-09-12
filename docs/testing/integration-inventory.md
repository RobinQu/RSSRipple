# 集成测试清单（重组后）

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
