# V10：退役季数字段不再写入（P1-D3）

## 必要性复核（2026-09-20）

实际端点是 POST /series 与 PUT /series/{id}（原 TODO 写 PATCH 不准确）。两者 schema 仍接受 number_of_seasons；PUT 还把它加入 manually_edited_fields。metadata_dedup._merge_series_group 也会从被合并行继承该字段，现有单测甚至明确要求继承 2。它不只是无效展示字段：is_unsplit_legacy_series 用该数值决定是否绕过单季限制。

独立副本 `/tmp/rssripple-v10-retired-fields` 以 V8 av 冻结实现复现，未修改实现。真实 API 创建/更新 number_of_seasons=2 分别返回 201/200，作品 season_number=1，但 is_unsplit_legacy_series=True；调用生产 upsert_episodes 后实际 Turso Episode 同时包含 season=1 和 season=2，更新入口还持久保护了 number_of_seasons。两项红测 **2 failed，2.73 秒**，报告 `/tmp/rssripple-v10-retired-api-red.xml`。数据为明确合成，不访问生产库或外部来源。结果和可恢复测试见 probes/retired-season-fields-result.json 与 retired-season-fields-reproduction.patch。

保持 P1：已确认关联污染路径；未据此推断生产库已经出现同样污染。前端 WorkEditPage 已将该字段退役，只读返回兼容与第三方请求兼容须分别评估。

## 修复边界与存量方案待论证

1. 创建/更新请求不再写入 number_of_seasons；对显式退役字段给出可诊断拒绝，不能继续成功写入。seasons 同属退役字段，也不得成为旁路；不要简单禁用所有未知键而意外改变其他字段契约。确认前端提交内容与原有只读响应兼容。
2. 从 MANUAL_EDITABLE_FIELDS 移除 number_of_seasons；dedup 不再继承退役季数。保留其他有效字段、作品 ID、人工保护与关联，仍需验证真实去重事务。
3. **不能把全表 SET number_of_seasons=NULL 当作无损清理**：该值仍可能是真正旧版未拆季作品的证据。is_unsplit_legacy_series、work_verified_season、P8 season_split_migration 均依赖这些证据；一律清空可能使旧合集被当作 S1，违背季号不猜测。须先只读导出原值、seasons、身份袋、Episode/resource 季分布与人工字段，对确认已单季化的对象清理；真正多季/证据矛盾的对象保留证据并走显式单季迁移/确认流程。历史快照“1 行非空”不能代替本次数据核查，也不授权操作生产库。
4. 对已经因该入口污染的跨季 Episode 不能仅清空计数字段后宣称修复：需设计可审阅的关联修复/迁移报告，保留人工数据，避免猜测合并。清理须有预检、原值竞争检查、事务回滚与重复执行保证。

## 严格验收

- 实际 POST/PUT 退役字段拒绝、无副作用；合法创建/更新与手工字段保护保持。
- upsert 后每个 Episode.season 与作品一致；真正未拆季旧数据不因修复被误当 S1。
- 去重不写退役字段，其他有效元数据、身份袋和关联不回退。
- 存量清理覆盖已确认单季、真正多季、缺季证据、矛盾证据及已有人工修订，双库实际持久化、只读预检、失败回滚/幂等验证。
- 权威 API/模型/业务/迁移文档同步，完整单元/API ≥95% 和隔离集成 ≥85%，应用退出/覆盖率导出/项目清理齐全。

目前仅完成必要性复现与边界论证，未修复，不关闭 D3。先完成 V8/V9 门禁；不能改动其冻结源码。


## 写入防护原型与存量证据（2026-09-20）

独立副本新增请求级退役字段拒绝（包括 null），移除创建/更新 schema 的 number_of_seasons，但保留只读响应兼容；seasons 也明确拒绝，其他未知键沿用原契约。MANUAL_EDITABLE_FIELDS 移除退役计数；去重不再从重复行继承它。已有真正旧作品的计数/seasons 不自动清空，其他字段与人工保护保留。

实际 API 的两个原始红测、12 组退役请求无副作用、合法单季 Episode 写入，以及已有 API/去重扩大回归 **64 passed、1 warning，37.37 秒**（`/tmp/rssripple-v10-write-guards-green.xml`）。同时保留旧幸存行 count=3/seasons 证据且不新登记退役人工字段。八文件原型（含三份权威文档）仅在独立副本，未应用 root，仍缺存量方案和完整门禁。

只读审计仓库已有 `tests/fixtures/prod_works_v1.json`：65 个 TVSeries 中 27 个带非空 number_of_seasons，其中包含明确多季 JSON。这是迁移前录制夹具，不能与 TODO 所述另一份历史主库快照“1 行非空”混为一谈。禁止改写此录制数据来让清理测试通过；应据它建立真正旧多季、单季与缺证据样本。

清理还必须处理证据退化：work_verified_season 对 S1 可能依赖旧 count=1；直接清空可能把可验证的 S1 变成 unknown。下一步应设计显式确认且可审计的清理流程，保留原始证据、并发前置条件和真正多季分流，既不能猜季，也不能只做新写入防护就关闭 D3。

## 存量清理候选方案（待实现与反例验证）

采用“只读证据导出 → 显式确认既有季身份 → 带原值校验的单作品事务清理”，不增加启动自动清空逻辑。导出应包含作品原计数/seasons/人工字段、季号/合集、身份袋、Episode、资源与文件指派的季分布及可核验指纹；录制夹具的真正多季与缺证据行必须出现在报告中，不能过滤掉难处理行。

清理只接受已审阅记录与明确 confirmed_season，且必须等于现有 season_number；有跨季 Episode、JSON 多季声明或矛盾的单季资源/文件指派时拒绝，先走明确的关联修复或 P8 拆季。事务重新读取并核对指纹，发生并发修改则拒绝，不能把报告中的旧值覆盖回数据库。只清 number_of_seasons 并移除其退役人工标记，其他值保持；既有 seasons 证据不新写、不抹去。

对显式确认的季身份可使用已有 manually_edited_fields 中的 season_number 保护标记保存确认，避免清除 count=1 后 S1 证据变 unknown；若采用此方案，work_verified_season 必须在拒绝真正 unsplit legacy 之后才认可该标记，并有“旧多季 + 手工标记仍不绕过保护”的负例。不能仅凭默认 season_number=1 自动添加该标记。此处为待论证设计，不表示已经实施或验收。

## 存量清理原型验证（2026-09-20，bf）

上述候选流程现已在独立副本实现，尚未应用主工作区或验收。`scripts/retired_season_fields.py` 提供只读导出及显式审核后清理；后者要求停写备份、完整指纹匹配、明确既有季号，锁定合集后锁作品。跨季或矛盾证据拒绝，原始报告保留旧值。仅清退役计数并记录人工确认的季号，其他作品字段、人工保护和关联保持。

首轮反例发现清空 count=1 会丢失 S1 可验证性，已增加显式人工确认标记的识别，且旧多季保护先于该标记。真实 PostgreSQL 另发现 ORM flush 会改写旧 search_text，改用限定字段 UPDATE，补充保留旧搜索文本与幂等测试。扩大回归最终 **302 passed、1 warning，104.78 秒**。

真实 PostgreSQL 验证只读 CLI、过期审核拒绝、更新后故障回滚、两个真实连接并发清理（观察到阻塞，结果依次 changed=true/false）、CLI 幂等及关联保持，测试项目已清理。生产录制夹具的 27 条计数均完整导出；V7 合集回填后仅 8 条有明确单季证据的记录通过清理，其余 19 条原值不变，录制文件未改写。这里使用的是历史录制夹具，未操作当前生产库。结果见 probes/retired-season-fields-result.json 与 probes/retired-season-cleanup-pg-result.json。

后续先继承最终 V8/V9 基线、补齐清理权威文档，再完成两道完整门禁；上述原型回归不代表 D3 已关闭。

## 继承 V8/V9 后的门禁（2026-09-20，bh）

独立副本已继承联合 V8/V9 实现，15 个自有文件保留并同步 API/模型/业务/迁移/单季化/集成清单文档。组合回归 **68 passed、1 warning，97.34 秒**，Ruff 通过。完整单元/API bh 运行中，句柄 `/tmp/rssripple-v10-gates-bh.json`，2997 个源码/静态/夹具文件（包含 symlink 指向的录制数据）冻结。主工作区仍为 V8/V9，V10 完整集成未启动。

## bh 单元终态与 bp 集成失败（2026-09-20）

完整单元/API bh **3648 passed、14 skipped、6 warnings，2305.07 秒，97.75%，退出 0**；2997 个冻结文件哈希一致。完整集成 bp **3016 passed、3 failed、115 errors、20 skipped，1059.39 秒，退出 1**。115 个 setup error 及 2 个直接失败来自共享 HTTP ensure_series 助手仍发送 number_of_seasons，实际 API 按新契约返回 422；另一个失败仍要求 dedup 继承旧计数。不能放宽生产校验来让旧夹具通过，也不能改写生产录制数据。下一步为正例夹具使用明确声明的单季主体证据，保留 API 退役字段拒绝负例，并重新完成集成门禁。

失败轮证据已导出并清理，覆盖率 86.19% 不能抵消测试失败。夹具修复需同时覆盖 `_http.ensure_series` 与三个重复本地 helper；notifications 的同名字段是合法的提供者元数据输入，无需删除；正例可采用明确标注的合成 Bangumi 单季主体身份（现有单季证据契约），不能假装这些 ID 来自真实提供者。原始 feed/torrent/生产录制文件保持不变。新增容器 HTTP 退役字段拒绝负例，并修正 dedup 旧继承断言后，先跑受影响场景再启动新一轮完整集成。该夹具修正已实施，结果见下节。

## V10 bx 专项通过，bz 完整集成启动（2026-09-20）

重新确认必要性：写入退役季数字段会重新制造旧多季作品；不能为了恢复旧夹具而放开 API。共享及重复 HTTP helper 改为明确标注的合成 Bangumi 单季身份，保留已有合法身份；原始录制数据未修改。新增容器 HTTP 创建/更新拒绝测试，覆盖两个退役字段及显式 null，并检查拒绝后没有状态改变。dedup 断言改为禁止继承退役季数。

bx 受影响容器专项 **12 passed，216.54 秒，退出 0**；2998 个冻结文件未变，两应用 SIGINT 后退出 0，JUnit 已导出，测试项目已清理。原型现为 27 文件。运行代码未变，保留 bh 完整单元/API 3648 passed（97.75%）；完整集成使用新项目 `rssripple-v10-complete-20260920-bz`，句柄 26134，日志 `/tmp/rssripple-v10-integration-bz.log`，冻结副本 `/tmp/rssripple-v10-integration-bx`。完整测试、应用退出、覆盖率 ≥85%、导出与清理全部完成前不验收。

## V10 D3 最终验收（2026-09-20，bz）

完整单元/API bh：**3648 passed、14 skipped、6 warnings，97.75%（21349/21841）**，退出 0。完整集成 bz：**3141 passed、17 skipped、8 warnings，1669.61 秒，89.70%（19591/21841）**，测试与覆盖率汇总均退出 0。2998 个冻结文件未变；相比 bh 只有集成夹具改动，运行代码及单元/API 保持一致，因此继承 bh 完整门禁。两应用 SIGINT 后均退出 0，报告导出 `/tmp/rssripple-v10-artifacts-bz`，项目清理退出 0。

按 code-review-and-quality 复核 API 拒绝边界、清理事务/锁顺序、审阅指纹和幂等、未知季号不猜测、真实录制保留及合成身份标注；无新增依赖。27 个实现/测试/权威文档文件核对基线与最终哈希后同步本地 main 工作树，全仓 Ruff 和差异检查通过。D3 已从 pending-only TODO 删除。存量清理工具已验证，不等于已执行生产清理；生产数据库未改动。原失败 bp 证据保留，不能解释为成功轮。

机器摘要见 probes/retired-season-complete-bz-result.json。后续 V11 D4/M4/M5 仍为未验收原型，下一步先继承 V10 已验收基线并复验，再处理资源/指派并发、旧库迁移与 rekey。

V10 有效代码与权威文档已提交本地 main：`6852cf0`；尚未推送远端。V11 仅保存原型和证据。
