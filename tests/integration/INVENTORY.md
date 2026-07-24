# 集成测试清单（重组后）

> 重组完成于 2026-07-24 ｜ 分支 `refactor/integration-tests`
> 范围：`tests/integration/` ｜ **14 个测试文件 / 149 个测试用例**（重组前 16 文件 / 166 用例，去重 17 个）

## 1. 目录结构

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
  external/                        # 直连 Python + 真实外部 API（无需 docker 栈，只需 API key）
    __init__.py
    test_metadata_agent_accuracy.py     # MetadataAgent.process_title_only 对 ground_truth_v1 准确率（LLM）
    test_metadata_search_agent.py       # search_metadata 多源（22 标题）+ _search_tmdb TMDB-only（17 CBC 标题）合并
  eval/                            # 独立 Metadata Eval 应用（应用代码 + test_api.py，不变）
  server/                          # 假 test-server 应用代码（RSS / tracker / torrent，不变）
```

**两类测试已物理隔离**：
- `http/`：分布式 HTTP 测试，session autouse `setup_test_environment` 种子化 test-server。
- `external/`：直连 Python 打真实 LLM/TMDB/Exa，**不继承** `setup_test_environment`，可独立运行（只需 `LLM_API_KEY`/`TMDB_API_KEY`）。
- `eval/`：eval 应用测试，同样不继承种子化。

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
| external/test_metadata_agent_accuracy.py | 5 |
| external/test_metadata_search_agent.py | 6 |
| eval/test_api.py | 31 |
| **合计** | **149** |

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
- `test_metadata_search_agent_integration.py` + `test_tmdb_dataset.py` -> `external/test_metadata_search_agent.py`（多源 + TMDB-only 合并）。
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

| 检查 | 结果 |
|---|---|
| `ruff check tests/integration/` | ✅ All checks passed |
| `pytest --collect-only tests/integration/` | ✅ 165 tests collected (含参数化展开), 0 error |
| `pytest --collect-only tests/integration/external/` | ✅ 27 tests, 独立收集（无 test-server 依赖） |
| `docker compose -f docker-compose.test.yml config` | ✅ 语法有效 |
| 运行时冒烟（`test_torrent_lifecycle` + `test_rss_subscription`，9 用例，对运行中的 dev 栈） | ✅ 9 passed in 23s -- 验证 http/conftest 的 `setup_test_environment`、迁移后 import、env-URL 修复、去重后的 rss_subscription 均运行时正常 |
| **完整 compose 套件**（`docker compose -f docker-compose.test.yml run --rm test-runner`，`SCHEDULER_ENABLED=false`） | ⏳ 建议合并前执行；冒烟未覆盖涉及 fetch/scheduler/queue 的 channel/agent/e2e/task_queue/downloader/metadata_api（对开了 scheduler 的 dev 栈跑可能假失败） |

## 5. 已知遗留 / 延后

- **Channel 4->6 文件拆分、Agent 1->3 文件拆分**：原计划方案 2 的纯组织性拆分。考虑到无法在本地运行 docker 套件做运行时验证、且拆分涉及 class-scoped fixture 迁移（运行时风险高于 collect-only 能覆盖的范围），本轮保守保留为 4 个 channel 文件 + 1 个 agent 文件（已做 helper 去重与用例去重）。如需进一步拆分，建议在能运行 compose 套件的环境下逐域推进。
- `test_task_queue.py` 仍保留本地 `DEFAULT_FIELD_MAPPING`（与 `_http.py` 的完全相同，但该文件自包含、改动风险收益低，未强制合并）。
- `test_channel_workflow.py` 保留本地 `_poll_fetch`（done/failed 语义）与 `_list_resources`/`_stream_analyze*`（其 LLM fixture 链较脆，未强行换用 `_http._poll_fetch`）。
- `server/test_data.py` 的 `TestFile` dataclass 触发 `PytestCollectionWarning`（预存问题，非本轮引入）。

## 6. 历史

原 16 文件 / 166 用例的逐用例清单（含每条验证点、依赖、问题标注）见本文件 git 历史的重组前版本（commit 之前的 INVENTORY.md）。
