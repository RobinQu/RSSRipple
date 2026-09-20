# V6：元数据身份接地（P1，独立原型）

## 必要性与优先级复核

2026-09-13：六项测试在当前实现全部失败（1.65 秒，`/tmp/rssripple-v6-grounding-red.xml`）：Wikipedia judge 放行不存在的 pageid、错误语言版本及非数字身份；跨语言相同 pageid 被去重并可能取错页面；TMDB finalize 可引用未返回的 ID，或将已返回 TV ID 当作电影身份。旧 Wikipedia 单测还明确要求保留未知身份，不能把既有绿测当作安全证据。上述是可复现的身份污染风险，保持 P1；并未据此声称生产身份袋已实际污染。

## 方案与不采用的替代方案

- Wikipedia 搜索去重与最终匹配统一使用 `(lang,pageid)`；有语言的身份必须精确匹配。旧裸 pageid 仅在候选证据中唯一可确定语言时允许，并归一化为带语言身份。
- judge found 但身份不能定位时，返回 found=False、matched_entity=None，以可重试 not_found 语义阻止身份写入。保留 clean_title 和来源诊断。
- TMDB ReAct 只收集成功 ToolMessage 的 `search_tmdb.data[]` 与 `get_tmdb_details.data`；身份必须带 tv/movie 类型、数字 ID。最终 `(content_type,id)` 必须在集合中。失败工具、错误状态、无媒体类型、其他工具或畸形 JSON 都不能提供身份依据。
- 提示词约束不能替代代码验证；仅校验 ID 数字格式不能证明来源；仅比较 TMDB 数字 ID 会混淆 TV/movie 命名空间；按 Wikipedia pageid 忽略语言会关联到不同条目。

原型在 `/tmp/rssripple-v6-grounding-work`，未修改主工作树实现。主工作树仍为 B9 ab 完整门禁冻结版本。原型与双方哈希保存在 `/tmp/rssripple-v6-grounding-state.json`，[补丁](probes/metadata-identity-grounding-prototype.patch) 可复查。

## 当前证据和严格验收

六项红测修复后 **6 passed、1 warning，0.67 秒**，`/tmp/rssripple-v6-grounding-green.xml`。另补合法搜索/详情正例、错误/失败工具、无类型和畸形数据的九个测试。扩大 Wikipedia/MetadataAgent 回归正在运行；已观察两个旧测试未提供 matched_entity 却断言 found=True，须在原轮结束后结合其测试目的修正夹具，不能简单削弱身份约束来换绿。

合入前仍需：

1. 扩大回归全部完成；裸 ID 跨语言歧义、空/非对象 entity 与畸形响应边界。
2. 审核真实录制 Wikipedia/TMDB 响应正例，并明确网络/LLM故障注入与合成元数据部分；禁止把纯 mock 测试宣称真实数据验收。
3. Turso/PostgreSQL 的真实 process→upsert/身份袋链路：非法身份不写入，合法身份保持稳定且重复运行幂等。
4. 审核所有身份入口：Wikipedia ReAct fallback、历史缓存、别名身份 `alt_external_ids` 及 URL。若可以绕过主身份约束写入同类无证据身份，必须修复或明确拆分并保留待办，不能宣称完整接地。
5. 确认失败分类、缓存/重试及可选 web fallback 不制造永久非作品判定；源整体失败分类 P1-M3 仍独立保留。
6. 同步权威业务文档与测试清单，再跑完整单元/API 95% 与隔离集成 85% 门禁。

本轮不关闭两个身份接地 TODO。暂不引入外部新依赖、不访问业务数据库。

V6 扩大原轮终态：**235 passed、2 failed、1 warning，71.11 秒**（`/tmp/rssripple-v6-grounding-expanded.xml`）。两项失败的夹具在 found=True 时未提供身份；按原测试目的补上搜索证据中的 `wikipedia:en:1`，保留页面异常诊断和候选上限断言后，身份＋Wikipedia 专项 **33 passed、1 warning，3.35 秒**（`/tmp/rssripple-v6-grounding-corrected.xml`）。未将失败原轮改称通过；仍需数据库身份袋、真实语料、其他身份入口及完整门禁。原型补丁与哈希已更新。


## 2026-09-19：身份袋与缓存必要性续验（独立原型）

主干仍为 `02865b2`，B9 已完成；本轮实现仅在独立 V6 副本。新增真实 Turso 的生产 `process → upsert → WorkExternalId` 集成，外部来源与 LLM 明确使用合成数据。Wikipedia 首轮 **2 failed、2 passed，2.71 秒**：合法主身份与非法主身份行为正确，但伪造 alt_external_ids 和 generation=6 的旧成功缓存均实际写入非法身份。补修后连同已有专项 **37 passed、1 warning，5.79 秒**。扩展 TMDB 后又复现其伪造别名落库（**1 failed、7 passed，4.04 秒**），修正后两源四场景及既有专项 **41 passed、1 warning，7.64 秒**。

方案因此补充：Wikipedia 仅保留选中证据的语言别名（证据为空也必须清掉模型别名），来源/页面 URL 来自证据；TMDB 当前工具不提供跨源身份，清掉模型 alt_external_ids/Wikipedia URL，规范 primary source/id；缓存代际从 6 升至 7，旧成功缓存重新走来源校验。合法身份重复处理保持同一电影与唯一身份袋行。该缓存变更仅使旧结果失效，不自动修复历史已落库的污染身份；不得把它说成已清理存量污染。

报告分别为 `/tmp/rssripple-v6-persistence-red.xml`、`/tmp/rssripple-v6-persistence-green.xml`、`/tmp/rssripple-v6-tmdb-persistence-red.xml`、`/tmp/rssripple-v6-both-sources-green.xml`。公开 Wikipedia API 的匿名只读录制尝试返回 HTTP 403，未生成成功录制，不能当作真实来源验收。现有数据库证据足以继续修复，不构成整项阻塞。

扩大 MetadataAgent 单元/集成回归已开始：`/tmp/rssripple-v6-expanded-20260919.log`，尚未有最终结果。仍需 Wikipedia ReAct 回退接地、合法跨语言别名正例、畸形 entity 边界、PostgreSQL 实际落库、真实来源语料及完整门禁。原型补丁与哈希已更新；两个身份接地 TODO 不关闭。

扩大回归已结束：**294 passed、1 warning，71.55 秒，退出 0**（`/tmp/rssripple-v6-expanded-20260919.xml`）。另行 Wikipedia ReAct 必要性测试 **2 failed、1 passed、1 warning，0.77 秒**（`/tmp/rssripple-v6-wiki-react-red.xml`）：已观察身份正例通过，但未知 pageid 与错误语言版本仍被回退图接受。该红测已保存进原型补丁，尚未修复；不得将 294 项通过解释为完整接地完成。下一轮先统一 judge/ReAct 的 Wikipedia 身份选择与别名清理，再补失败工具、页面 URL/语言和合法跨语言别名的正反例。主工作树运行代码仍为已验证 B9，未合入本原型。


## 2026-09-19：Wikipedia ReAct 与三入口双库续验

必要性来自上轮两个实际回退图红测。原型现已让 judge 和 ReAct 共用按 `(lang,pageid)` 选择身份与可信别名的函数；ReAct 仅接受成功 Wikipedia 工具响应，并从返回的 Wikipedia 页面 URL 确定语言版本。旧裸 ID 仅能匹配唯一语言身份；同页搜索/详情多次出现不构成歧义，保留详情类别和可信 langlink 身份，清掉模型别名、来源及 URL。初步 **44 passed、1 warning，7.27 秒**，`/tmp/rssripple-v6-react-green.xml`。

补正常跨语言别名、同页多观察、跨语言同号歧义、错误工具状态/失败响应/无语言 URL/假 Wikipedia 域名及畸形 entity 测试后，扩大回归 **308 passed、1 warning，65.45 秒**（`/tmp/rssripple-v6-react-expanded.xml`）。旧 `_run_react` 成功测试原本只有 `external_id=x` 且无工具证据，已补真实形状的 search 调用/响应，并保持 finalize 提取和诊断断言；没有放松接地约束。

数据库矩阵扩大到 Wikipedia judge、真实 judge→ReAct 回退和 TMDB ReAct 三入口，分别检查合法身份、非法主身份、伪造别名、可信别名和旧缓存；Turso **15 passed，6.91 秒**（`/tmp/rssripple-v6-all-paths.xml`），独立 PostgreSQL **15 场景通过、退出 0**。同一生产 process/upsert/cache/身份袋路径验证无非法身份、合法关联稳定、重复执行不重复建档；TMDB 工具无跨源身份，trusted_alias 场景仍只保留其主 ID。

[PostgreSQL 驱动](probes/identity_pg_probe.py) 与 [结果](probes/identity-pg-result.json) 已保存；日志 `/tmp/rssripple-v6-identity-pg-ac.log`。驱动会清空指定测试库，必须在已应用原型且无业务 .env 的独立副本运行，显式 `IDENTITY_PROBE_DATABASE_URL=postgresql+asyncpg://organize_test:organize_test@127.0.0.1:<临时端口>/organize_test`、`PYTHONPATH=.`；仅接受回环地址及专用库身份。使用 [已有测试 Compose](probes/agent-request-replay-compose.yml) 的 postgres 服务并指定唯一 `-p`。本轮 `rssripple-v6-grounding-20260919-ac` 容器及网络已全部清理。

这批数据为合成来源/LLM＋真实数据库实现，不称为真实来源录制。匿名 Wikipedia REST 页面身份录制接口也返回 HTTP 403；尚未取得成功的来源录制。原型共 9 个实现/测试文件，补丁与 `/tmp/rssripple-v6-grounding-state.json` 已同步，主工作树运行代码未改。下一轮继续审查畸形工具数据边界和可用真实来源回放，完成权威文档同步及完整 95%/85% 门禁后才能关闭两项 P1；上轮“ReAct 尚未修复”是历史状态。


## 2026-09-19：来源 HTTP 实测暴露并修正字段契约

畸形来源数据验证先复现 10 项失败（0.15 秒）：非标量类型、超长数字身份、非对象页面及畸形可选别名/类别。补修后扩大回归 **326 passed、1 warning，83.01 秒，退出 0**，`/tmp/rssripple-v6-final-expanded.xml`。PostgreSQL ad 轮三入口五场景 **15 项通过、退出 0**；项目 `rssripple-v6-grounding-20260919-ad` 已清理。这两项证据早于下述 HTTP 字段修正，不当作修正后的全量结果。

必要性复核：旧 HTTP 正例使用无效 TMDB key 令来源搜索失败，却仍要求模型的 `mock-exa-*` 身份落库，与接地要求冲突。独立原型新增测试专用 TMDB 协议端点和 mock-LLM 应用入口，只重定向来源 HTTP 目的地，仍运行生产适配器、ReAct 和身份校验。TV/movie 模型结果改选来源返回的合成 ID；原 TMDB 广播剧正例改为拒绝无来源依据的音频身份，不能借 TMDB 视频证据创建音频作品。未新增生产环境配置或依赖。

真实 loopback HTTP→生产 `_execute_search_tmdb`→身份校验测试发现：生产搜索返回 `content_type`，详情返回 `media_type`，此前原型仅接受后者。修正测试设置和断言字段后，**2 failed、4 passed，0.82 秒**（`/tmp/rssripple-v6-source-http-red.xml`），两个合法搜索身份被错误拒绝。修复为兼容两个接口的类型字段、拒绝互相冲突类型；来源 HTTP＋身份边界＋Turso 持久化联合回归 **62 passed、1 warning，7.48 秒，退出 0**（`/tmp/rssripple-v6-source-http-green.xml`）。这说明工具消息合成用例不能替代生产适配器验证。

来源内容仍为明确标注的合成数据，HTTP 和数据库执行真实；尚无成功录制的 Wikipedia/TMDB 来源 API 响应。21 个原型文件已写入可应用补丁并通过 `git apply --check`。容器 HTTP 专项使用唯一项目名 `rssripple-v6-http-20260919-ae`，日志 `/tmp/rssripple-v6-http-ae.log`；最终结果待记录。仍需该应用级验证、权威设计文档及完整覆盖率门禁，主工作树运行代码未改，两项 P1 不关闭。


## 2026-09-20：容器 HTTP 专项完成

ae 轮 **4 failed、2 passed，119.51 秒**：来源候选响应中的标题干扰旧模拟模型按消息全文选作品；另有 genre 用例在 fetch 前清掉测试 TMDB key。af 轮修正这两项后 **2 failed、4 passed，117.20 秒**：TV 资源已关联，但旧断言使用系列级 `tmdb:900001`，单季作品实际按既有契约保存为 `tmdb:900001#s1`。仅修正持久化作品断言，手工搜索仍断言系列级身份。

ag 轮使用全新数据库后 **6 passed，117.47 秒，退出 0**（`/tmp/rssripple-v6-http-ag.xml`）：TV 关联、未命中、手工在线搜索正负例、电影关联、TMDB 无依据音频拒绝、genre 钳制全部通过。ae/af/ag 三轮独立容器、网络及测试卷均已清理，报告已导出。机器可读证据见 [HTTP 结果](probes/identity-http-results.json)。不把修正夹具后的通过视为真实提供者准确性验证。

权威 business-logic/data-models/integration-inventory 文档已在独立副本准备，原型累计 24 文件；全 app/tests Ruff 和补丁应用检查通过。下一步按 code-review-and-quality 复审最终补丁及测试独立性，复验修正后的 PostgreSQL，检查主工作树基线后同步，执行完整单元/API ≥95% 和隔离集成 ≥85% 门禁。完整门禁前不关闭两项 P1，也不把当前原型宣称已合入 main。公开来源录制仍缺失，保留明确的数据真实性限制；已有作品错误身份清理不在本轮验收声明内。


## 2026-09-20：V6 已同步工作树，ai 完整门禁运行中

按 code-review-and-quality 复核正确性、可读性、模块边界、安全与性能：接地函数不发网络、不访问 DB，复用现有 Wikipedia ID 解析；无新依赖，来源集合受上游候选/工具预算约束。测试专用 HTTP 入口仅用于 mock-LLM 服务。数据库矩阵的 TMDB 消息改为生产搜索候选实际 `external_id/content_type`；复验 Turso/HTTP/边界联合 **62 passed、1 warning，7.53 秒**（`/tmp/rssripple-v6-review-ah.xml`），PostgreSQL **15 场景通过、退出 0**，ah 专用项目已清理。来源 HTTP 测试的 genre 全局缓存也改为逐例恢复，避免影响其他测试。

24 个实现/测试/权威文档文件均核对主工作树原始哈希和原型最终哈希后同步；全仓 Ruff、差异空白检查通过。当前是未提交的本地工作树变更，main 已提交历史仍为 B9，不能称 V6 已验收或已提交。旧原型未合入的记录为历史阶段。

冻结 490 个 app/tests/scripts/config 文件：`/tmp/rssripple-v6-source-ai.json`。完整单元/API（95%）日志 `/tmp/rssripple-v6-unit-ai.log`，JUnit `/tmp/rssripple-v6-unit-ai.xml`；完整隔离集成日志 `/tmp/rssripple-v6-integration-ai.log`，项目 `rssripple-v6-complete-20260920-ai`。两道门禁正在运行，必须等最终退出码、导出 JUnit 并对集成应用优雅退出后汇总覆盖率（85%）；在终态前不修改冻结源码、不重启测试、不关闭 TODO。运行句柄保存 `/tmp/rssripple-v6-gates-ai.json`。本轮未推送远端。


V6 ai 完整单元/API 已结束，退出 0：**3540 passed、14 skipped、6 warnings，1557.00 秒**，覆盖 **21159/21627（97.84%）**，95% 门禁通过。JUnit `/tmp/rssripple-v6-unit-ai.xml`，coverage XML `/tmp/rssripple-v6-unit-coverage-ai.xml`。完整隔离集成仍运行，尚不验收/提交 V6；保持冻结源码并续接既有进程。


## V6 ai 完整集成结果及 al 复验

ai 完整集成已终态：**3129 passed、7 failed、17 skipped、8 warnings，1728.31 秒，退出 1**。7 项集中于旧夹具：Wikipedia judge 用例要求保留无证据身份；6 个 P0 季号场景模型直接 finalize、没有来源工具证据，先被接地拒绝而到不了季号校验。两个应用 SIGINT 正常退出 0 后，覆盖率汇总退出 0，**19413/21627（89.76%）**；测试失败意味着本轮不能验收。完整 JUnit/原始覆盖率在 `/tmp/rssripple-v6-artifacts-ai`，机器结果为 probes/identity-complete-ai-result.json，ai 项目已清理。

只修改两份集成测试：无证据 Wiki 身份改为明确拒绝；P0 用例让实际 ReAct 图先调用 search_tmdb，返回合成的合法身份，再 finalize 原故障注入季号。仍使用原捕获标题、保留所有 DB/cache/再次运行/非法季不落库断言，并断言工具确被调用且不出网。相关 **27 passed、6 warnings，4.13 秒**（`/tmp/rssripple-v6-fixture-green.xml`）。实现、单元/API 源码及配置与 ai 哈希完全一致，因此不重复已通过的 3540 项单元/API 门禁。

26 文件补丁已更新，两份修正已同步工作树。完整集成改用新项目 `rssripple-v6-complete-20260920-al`、日志 `/tmp/rssripple-v6-integration-al.log`，正在运行。冻结源码 `/tmp/rssripple-v6-source-al.json`；仍需终态结果、应用优雅退出、覆盖率汇总和清理，不关闭 V6 TODO。

## V6 最终验收（2026-09-20，al 轮）

身份接地的完整验收已完成：单元/API ai **3540 passed、14 skipped、6 warnings，1557.00 秒，21159/21627（97.84%）**；完整集成 al **3136 passed、17 skipped、8 warnings，1724.08 秒，19424/21627（89.81%）**。两轮 runner 和覆盖率门禁均退出 0；ai 单元/API 对应的实现及单元/API 源码未变，仅两份集成夹具补足来源证据后重跑完整集成。

490 个冻结文件哈希一致；两应用 SIGINT 后退出 0。JUnit/原始四路覆盖率/合并 XML 全量导出 `/tmp/rssripple-v6-artifacts-al`，JUnit failures/errors 均 0，al 项目已 down -v 清理。全仓 Ruff、差异空白检查通过，按 code-review-and-quality 复查了主/别名身份边界、工具失败拒绝、语言/类型匹配、缓存 generation=7、真实持久化以及测试替身与生产接口形状的一致性，无新增依赖或未解决的合并阻断项。

已从 pending-only TODO 删除原 P0-4 的 Wikipedia/TMDB 两项，权威契约已同步。实际 process/upsert/身份袋、Turso/PG、HTTP 来源适配与 P0 季号回归有证据；来源响应为标注合成数据，已捕获真实标题继续回放，不能称新增了在线成功录制的 Wikipedia/TMDB API 数据。缓存升级不清理历史非法身份。机器摘要见 probes/identity-complete-al-result.json。V7/V8 与其他待办仍未完成。

V6 有效代码、测试与权威契约已提交本地 main：`7fd8121`。尚未推送远端。
