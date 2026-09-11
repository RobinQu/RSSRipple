# Metadata corpus 回归测试

目标是用作品库与实际 torrent 重建作品、季、合集、文件指派及下载门禁，且不把生产库当前结果直接当作正确答案。CLI 入口为 `scripts/metadata_corpus.py`；验收用例在 `tests/integration/metadata_corpus/`，本目录仅保留共享导出/回放工具，不重复收集用例。

## 已有覆盖与边界

当前语料为 **v2**（`tests/fixtures/metadata_corpus_v2`，2026-09-09 导出；v1 目录保留为只读存档，`tests/metadata_corpus/dataset.py` 的 `ROOT` 已指向 v2）。v2 收录 1,512 条真实资源的脱敏快照，1,487 个去重 torrent 证据（19 条缺 torrent），冻结清单共 16,134 个文件；图快照含 167 部剧集、470 部电影、265 个合集、2,629 行 Episode。v1 唯一已确认 review（猫与龙 `f79ef2eb…`）及其 bangumi_single 场景 cassette 已迁入 v2。所有 torrent 都有清单解析回归；这仅验证文件名/大小提取，不代表每个文件的作品与季集语义已核验。

当前完整语义场景两个：Bangumi 单集（冷库重建、同标题再次入库、Episode 清单、单季合集归属、文件指派、Channel 必填门禁、mock 下载派发与重复派发抑制）与头文字D 跨季 franchise 包（见下）。只有 2 个独立核验的资源，其他 1,510 条仍为 pending，19 条缺 torrent。**CI 通过不等于全库作品/合集解析通过**。电影包、单季合集缺集、人工修订回流与批量 backfill 尚须补充独立答案和录制场景。2026-09-11 因 F6（LLM 请求新增 `chat_template_kwargs`，指纹变化）对 bangumi_single 场景执行 record-llm 重录：源证据 seed 不变、expected 不变，新完整回放为 `http/bangumi_complete_f6.json.gz`（场景 `llm_base_url` 记录为录制期代理地址 `http://127.0.0.1:18000/v1`；重录后 runner actual 与已确认 expected 零差异）。

### initial_d_franchise 场景（第二个语义金标）

真实资源 `头文字D.全六季.日语.英文字幕.Initial D [BD] [1080p] [AV1]`（case `995c8c1d-…`，93 个媒体文件：5 部 TV 季 + Third Stage 剧场版 + Legend 三部曲 + 若干部 OVA/总集篇）。冻结证据覆盖 bangumi 搜索/详情/剧集/关系端点、wigolo 网络回退与 LLM judge。expected 依据冻结 Bangumi 条目与 torrent 清单独立认定：First/Second/Fourth/Fifth/Final Stage 五个季作品（bangumi 8290/9568/3816/46312/104569，季号 1/2/4/5/6——包内目录 S01–S05 为顺序编号，Third Stage 是剧场版不占 TV 季，Fourth 起按 Stage 序数 remap）、Extra Stage 占 season 0、Third Stage + Legend 三部曲电影、Battle Stage/Extra Stage 2/Battle Stage 2 各落壳合集（mal 身份）、franchise scope、confirmation 空、不派发。

该场景经四轮"录制→暴露缺陷→修复→重录"收敛（2026-09-09 至 09-11）：R1 暴露图谱跨 IP 污染（MF Ghost）、air-date 阈值污染、合集标题未清洗、包内季号冲突；R2 暴露图谱 upsert 顺序缺陷（Fifth Stage 被陈旧槽位 park）、LLM 精判跳过 franchise 链接致资源合集 NULL、OVA form guard 误伤；R3 暴露 OVA 抢占主合集季槽位 + season-marker-tier 跨标题误绑（First Stage 26 文件错挂 Battle Stage）；R4（Fix D）全部收敛。

已知残留（expected 如实断言或豁免并在此注明）：Battle Stage 3 未绑定（冻结 wigolo 证据只有无关 wikipedia 命中，found=False 是对证据的正确裁决）；Movie Battle Digest 绑到 wikipedia 的 Legend 三部曲汇总条目（冻结证据中只有该身份可用）；104569/44758 的 `episode_seasons` 未断言——自愈季号纠正（s1→s6 / s1→s0）遗留旧季标记的 Episode 行，不作为金标祝福；`batch_seasons=[1..5]` 为 torrent 目录序派生缓存（franchise scope 不从作品身份重推导）；Fourth Stage Battle Digest 单文件按冻结 LLM judge 裁决绑 3816:s4。

备选第三场景 `[整理搬运] 头文字D…`（case `20010314-…`，156 文件：95 视频 + 59 zip/文本非视频，含漫画/CD/字幕/字体）已有 torrent 证据、review pending；非视频文件不进入 assignments 的处理与 156 文件的独立审核成本较高，留待后续轮次立项。

## 日常离线验证

```bash
.venv/bin/pytest tests/integration/metadata_corpus -q
.venv/bin/python scripts/metadata_corpus.py run --report /tmp/metadata-corpus.json
```

默认使用新建的临时 Turso 数据库。PostgreSQL 与 CI 一致：

```bash
docker compose -p rssripple-corpus -f docker-compose.corpus.yml up -d --wait
CORPUS_TEST_DATABASE_URL=postgresql+asyncpg://corpus:corpus@127.0.0.1:55439/metadata_corpus_test .venv/bin/pytest tests/integration/metadata_corpus -q
docker compose -p rssripple-corpus -f docker-compose.corpus.yml down
```

PostgreSQL 必须名为 `metadata_corpus_test`，每轮创建/删除随机 schema；拒绝连接生产数据库名。Docker 服务名或 localhost 在安装网络守卫之前解析为精确 IP/端口白名单。runner 使用真实 `fetch_channel_resources`、metadata 搜索/解析/upsert/确认门禁和下载服务，队列不实际投递，下载器为 mock。原始 RSS 未保存，输入明确标记 `title_and_torrent`，不宣称验证历史 RSS 字段映射；每轮也暂停自动 backfill，防止混入未列入场景的工作。

## 集成测试与 CI

现有两套 Compose 的 `pytest tests/integration/` 默认收集本验收集，无需另加命令或开启开关：

- 单节点：临时 Turso，与 HTTP app 数据库隔离，覆盖率并入现有 test-runner 统计。
- 分布式：独立 tmpfs `corpus-postgres` 服务，数据库名 `metadata_corpus_test`，不读写 app 的 `rssripple_test` 库。这里只验证 PostgreSQL 上的进程内重建，不冒充 Redis/worker 全链路验收。
- Fast Gate 与 Docker Publish 的已有测试 job 也执行这组离线验收；不再维护独立的 `metadata-corpus.yml` 工作流。
- Compose 输出到宿主机 `data/metadata-corpus/`：整个集成套件的 `integration.xml`、实时生成的 `audit.json`、逐场景 `scenario-<hash>.json`（含语义差异或执行异常）。Strict Gate 无论测试成败都上传该目录。Fast/Publish 使用 `/tmp/metadata-corpus/` 并上传。

本地可通过 `CORPUS_REPORT_DIR=/tmp/my-corpus-reports` 指定报告目录；未指定时使用 pytest 临时目录。测试不会回写只读 fixture 中的 audit。收集期异常可能仅有 JUnit 报告；审核状态必须与测试结果分开阅读，不能以用例数量代替已核验资源数量。

HTTP 在 httpx 同步/异步传输层回放，未录制请求、录制次数变化、未消费证据及旁路 socket 连接都使测试失败，即使业务代码吞掉异常也不能通过。指纹包含完整模型请求（消息、工具 schema、模型参数），因此 prompt 修改必须经显式重新录制和审核，不能用旧结果掩盖变化。

### DeepSearch 真实困难场景（默认收集）

`tests/fixtures/deepsearch_corpus_v1.json.gz` 将先前方案目录中的 8 个真实资源场景、48 个已完成 B/C 回答及其实际输入证据独立冻结，含源文件 SHA-256；容器只需既有 `tests/` 与 `scripts/`，不依赖未打包的 `docs/`。导出入口 `scripts/freeze_deepsearch_corpus.py` 拒绝覆盖已有版本。

`tests/integration/metadata_corpus/test_deepsearch_corpus.py` 新增 51 项默认离线用例：完整性/真实资源 UUID 关联、48 次真实回答的字段/引用契约，以及实际 AniList 命中与越界候选的生产 fallback 边界。6 个已知引文契约失败保留为**负向回归**，不是把错误回答标成语义金标。测试封锁外连；不调用真实 LLM，不将旧模型回答当作新 prompt 的质量验证。`deepsearch-audit.json` 随原有报告上传，仅报告库存和已知失败数，执行成败看 JUnit。

当前整个 corpus 子目录为 **1,584 项**（v2：1,488 项 torrent 清单回归＋51 项冻结 DeepSearch 数据回归＋17 项通用 DeepSearch 护栏＋15 项回放机制＋10 项数据集完整性＋3 项场景门禁）。完整作品/集合语义金标为 2 个独立资源（猫与龙单集、头文字D 跨季 franchise 包）；其余 1,510 条 pending 的审核状态不变。

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

该命令不属于离线 CI：源响应仍冻结，只允许配置的 LLM host 联网，每个场景在新数据库执行三轮。每轮必须实际调用模型；HTTP 错误/传输失败不能算模型质量通过。报告包含模型、语义差异、请求次数及耗时；它不是全模型能力或全数据集正确率。当前 Bangumi 场景的身份命中为确定性 auto-link，真实 LLM 只参与 genre 分类，而现有期望答案未断言 genre；因此三轮通过仅说明已断言的结果不受该调用影响，不代表 LLM 身份判别或分类准确率已验证。

`record-llm` 用于在已有源证据上另录模型响应，需要场景 manifest 的 `seed` 指向原始 cassette、`cassette` 指向不存在的新文件。v1 的 `bangumi_single.json.gz` 是源证据种子，`bangumi_complete.json.gz` 是完整回放；无需重新请求外部源。
