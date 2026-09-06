# Metadata corpus 回归测试

目标是用作品库与实际 torrent 重建作品、季、合集、文件指派及下载门禁，且不把生产库当前结果直接当作正确答案。入口为 `scripts/metadata_corpus.py`。

## 已有覆盖与边界

v1 收录 1,372 条真实资源的脱敏快照，846 条具有 torrent 证据（843 个去重文件），冻结清单共 8,895 个文件。所有 torrent 都有清单解析回归；这仅验证文件名/大小提取，不代表每个文件的作品与季集语义已核验。

当前完整语义场景为 Bangumi 单集：冷库重建、同标题再次入库、Episode 清单、单季合集归属、文件指派、Channel 必填门禁、mock 下载派发与重复派发抑制。只有 1 个独立核验的资源，其他 1,371 条仍为 pending，526 条缺 torrent。**CI 通过不等于全库作品/合集解析通过**。跨季、电影包、混合包、人工修订回流与批量 backfill 尚须补充独立答案和录制场景。

## 日常离线验证

```bash
.venv/bin/pytest tests/metadata_corpus -q
.venv/bin/python scripts/metadata_corpus.py run --report /tmp/metadata-corpus.json
```

默认使用新建的临时 Turso 数据库。PostgreSQL 与 CI 一致：

```bash
docker compose -p rssripple-corpus -f docker-compose.corpus.yml up -d --wait
CORPUS_TEST_DATABASE_URL=postgresql+asyncpg://corpus:corpus@127.0.0.1:55439/metadata_corpus_test .venv/bin/pytest tests/metadata_corpus -q
docker compose -p rssripple-corpus -f docker-compose.corpus.yml down
```

PostgreSQL 必须名为 `metadata_corpus_test`，每轮创建/删除随机 schema；拒绝连接生产数据库名。runner 使用真实 `fetch_channel_resources`、metadata 搜索/解析/upsert/确认门禁和下载服务，队列不实际投递，下载器为 mock。原始 RSS 未保存，输入明确标记 `title_and_torrent`，不宣称验证历史 RSS 字段映射；每轮也暂停自动 backfill，防止混入未列入场景的工作。

HTTP 在 httpx 同步/异步传输层回放，未录制请求、录制次数变化、未消费证据及旁路 socket 连接都使测试失败，即使业务代码吞掉异常也不能通过。指纹包含完整模型请求（消息、工具 schema、模型参数），因此 prompt 修改必须经显式重新录制和审核，不能用旧结果掩盖变化。

## 扩充与审核

```bash
# 只读导出生产库；已审核数据集不可覆盖，使用新的版本目录。
.venv/bin/python scripts/metadata_corpus.py export --corpus tests/fixtures/metadata_corpus_v2
.venv/bin/python scripts/metadata_corpus.py hydrate --corpus tests/fixtures/metadata_corpus_v2
.venv/bin/python scripts/metadata_corpus.py index-files --corpus tests/fixtures/metadata_corpus_v2
.venv/bin/python scripts/metadata_corpus.py audit --corpus tests/fixtures/metadata_corpus_v2
```

`candidates.json.gz` 把原始标题输入与生产库候选答案分开；重建时不向冷库注入候选作品。导出使用字段白名单，不保留频道 URL/凭证。torrent 去掉 tracker/webseed 等外层字段，校验 info 字节不变，以 SHA-256 命名。清单基线由 bencode 直接读取，与被测应用解析函数独立。快照与清单有 manifest 校验和。

用 `prepare --scenario NAME --case UUID` 声明场景（可重复 `--case` 指定序列），再用 `record --scenario NAME` 显式联网录制。已有录制不可覆盖；换新场景名称保留旧证据。录制只允许 manifest 中声明的源与 LLM host，提交前仍需检查响应正文不存在敏感信息。需要容器/宿主机地址适配时，prepare 支持 `--llm-base-url`。

根据源详情、原始文件清单及身份/季粒度规则独立审核，再用 `review --case UUID --expected-file /tmp/expected.json --note '证据与判断依据'` 提交答案。expected 格式参见已核验的 `reviews.json`：作品身份、作品详情、合集、完整文件指派、资源季集/范围和 confirmation 均必填；可附 `dispatch` 断言。禁止直接复制本次 runner actual 作为期望值。录制成功不自动转为 confirmed。

`audit.json` 分开报告所有资源、证据缺失、审核状态与已录制场景；pending 不计为语义成功。当前样本源证据显示 12 集，而生产候选为 9 集，审核答案依据冻结源修正为 12，演示了候选数据不等于真值。

## 固定源、真实 LLM 质量评测

```bash
.venv/bin/python scripts/metadata_corpus.py llm --scenario bangumi_single --report /tmp/metadata-corpus-llm.json
```

该命令不属于离线 CI：源响应仍冻结，只允许配置的 LLM host 联网，每个场景在新数据库执行三轮。每轮必须实际调用模型；HTTP 错误/传输失败不能算模型质量通过。报告包含模型、语义差异、请求次数及耗时；它不是全模型能力或全数据集正确率。

`record-llm` 用于在已有源证据上另录模型响应，需要场景 manifest 的 `seed` 指向原始 cassette、`cassette` 指向不存在的新文件。v1 的 `bangumi_single.json.gz` 是源证据种子，`bangumi_complete.json.gz` 是完整回放；无需重新请求外部源。
