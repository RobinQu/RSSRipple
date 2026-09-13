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
