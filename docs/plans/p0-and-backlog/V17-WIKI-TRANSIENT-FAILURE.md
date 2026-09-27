# V17 M3 整体来源故障分类与缓存

## 当前门禁状态：y 失败后 z 修订与 aa 重验

x 单元/API 已退出 0：**4042 passed、15 skipped、6 warnings，3047.53 秒，22856/23509（97.22%）**。3054 原冻结输入未变，跳过项及理由与 M2 相同；完整报告和原始 coverage 归档于 probes/wiki-failure-x.*，机器摘要 wiki-failure-x-terminal.json。aa 完整集成仍在运行，不能关闭 M3。

前序 M2 已验收并合入 `756bce6`，M3 基线哈希与 main 一致。y 完整集成已退出 1：**3183 passed、3 failed、17 skipped，1958.29 秒**；两应用退出 0，覆盖率汇总退出 0，**20735/23509（88.20%）**。3054 冻结文件未变，跳过项及理由与 M2 完全相同，导出和清理完成。三个失败均在 test_metadata_search_agent_coverage.py：旧测试要求 HTTP/响应解析/任务异常返回空列表，与本修复明确区分失败和未命中的契约冲突。覆盖率达标不能抵消失败。

新独立副本 `/tmp/rssripple-v17-error-review` 只修改该集成文件：异常必须抛出，工具包装必须返回 success=false，失败不写负缓存，来源恢复为完整空结果后才允许缓存。保留原成功合并、字段归一化和完整空缓存断言。z 联合 Wiki/TMDB 单元与此集成文件 **55 passed，8.85 秒**，无运行实现改动。

新有效清单九文件见 probes/wiki-failure-aa-source.json；完整 3054 输入冻结于 wiki-failure-aa-frozen.json，完整候选 tar 可恢复。与 x 输入唯一差异是上述集成测试文件，运行代码及全部单元/API 文件字节相同；x 原单元/API session 9731 继续运行，不中断或重复启动。aa 唯一项目 rssripple-v17-final-aa 已启动，完整集成 session 72098，日志 /tmp/rssripple-v17-integration-aa.log。本批仍未验收，须收取 x/aa 完整终态、审计并清理后才可合入。

## 必要性

main 9b9a5fa 上的独立探针已失败：真实录制标题 011c6d44-68cf-43a8-bad3-f0398ce20a95，所有维基搜索返回合成 timeout，LLM 返回合成 not_found，网络回退禁用。judge 返回 source_errors 中保留 zh/en 超时，但 error=None，实际 _classify_failure 返回 not_found。原始记录见 probes/wiki-failure-necessity-a.log。该证据仅证明分类缺陷，尚未验证实际数据库缓存写入；不宣称公网故障重现。

## 方案待验证

追踪搜索/页面请求的成功、明确未命中和瞬态失败，不能只依据 source_errors 是否非空决定整轮失败。有可接地成功证据时保留成功；全部可信路径瞬态失败时返回显式错误并禁止负缓存。回退的成功、明确未命中、故障和未启用应分别覆盖。同步审查 TMDB 的空列表缓存，未确认前不合并问题结论。

## 严格门禁

先增加真实 Turso 与 PostgreSQL 的 Agent→缓存数据库断言：首次整体超时不写负缓存，恢复后第二次确实重新调用来源并成功写入；明确未命中可以缓存；部分失败不抹除可接地成功；所有路径失败不能因 LLM 返回 not_found 变为确定失败。采用录制输入、明确标注故障注入及合成身份。正式测试不得联网。最终仍要求完整单元/API ≥95%、完整隔离集成 ≥85%，零失败、跳过审计及完整清理。本轮未修改运行代码。

## b 真实数据库必要性与 c 候选

独立原型 `/tmp/rssripple-v17-wiki-failure` 基于冻结 M1 vu，未修改 M1。b 真实 Turso 复现 1 failed：全源超时后独立会话读到 MetadataCache 行。测试进一步要求故障恢复后实际重查来源并允许明确空结果缓存。初步修复保留 all_searches_failed，在 judge 最终未命中时给出瞬态 error；不改成功回退结果。c 相关测试句柄 33585，仍须部分失败/页面失败/回退矩阵、PG、TMDB 检查及完整门禁，不能据此关闭 M3。

c 已退出 0：19 passed，5.67 秒。包含真实 Turso 首次失败不缓存、恢复后重查且明确未命中缓存，以及现有 judge 分支。完整矩阵和门禁未完成。

## e 页面失败必要性

d 为替身缺少 clean_title 的测试错误，不作为产品证据。修正后 e：1 passed、1 failed，页面查询返回真实接口形状的 success=false/data={}/timeout，而 ReAct 返回未命中后被分类为 not_found。现有循环只记录抛出异常的页面失败，不记录失败字典；ReAct 返回也未携带先前来源错误。修复仍需保留完整失败链，并证明成功接地结果不被部分失败抹除。

## f 页面失败传播补修

页面失败字典与异常均记录，Page not found 明确未命中单独处理。ReAct 重试返回汇合早先 source_errors；未成功时保留不完整查询的瞬态状态，成功时不降级。f：21 passed，5.78 秒，包含 ReAct 成功/未命中两种故障后结果。TMDB 代码复核发现两语言异常都被转换为空列表，合并为空时无条件缓存；新增全失败/单语言失败探针 g。仍需双库与完整验收。

## TMDB 故障信号补修

g 两项红测证明全部/部分语言超时仍写空缓存。候选区分 None（失败）与 []（成功空），无有效候选且查询不完整时抛来源错误，由工具层返回 success=false；有候选则保留成功但不缓存不完整结果，取消继续传播。h 48 passed / 1 failed 揭示旧成功空响应夹具的 __aenter__ 不可 await，旧异常吞掉后误通过；修正夹具并加部分成功候选测试，i 结果见归档。仍需双库及完整门禁。

i 48 passed / 2 failed 均为夹具错误：空响应 client.get 仍非 AsyncMock；部分成功测试的海报/genre 替身未接受 api_key 参数。已修正后重跑 j（句柄 40649，日志 /tmp/rssripple-v17-j.log），产品断言未更改。

j：50 passed，6.45 秒。k 正式 PostgreSQL 父测试 1 passed，2.39 秒，驱动 6 项通过（仅缓存恢复项使用数据库，另五项编排断言）；专用容器及卷已清理。l 扩大 Agent/来源工具/严格语料回归句柄 52084，结果未定；当前八文件候选归档、补丁和哈希已同步。M1 vy/vz 冻结 3045 文件复核无变化，继续运行。

l 扩大 Agent/来源/严格语料回归 330 passed，98.89 秒，相关 Ruff 全通过。审查继续补 m 真实数据库组合：一种语言超时、另一种语言明确空结果；先验证未命中是否仍错误缓存，不能以成功候选保留测试代替该边界。m 日志/JUnit 已归档。

m 为 pytest 参数带默认值的收集错误（退出 2），不作为产品缺陷证据。移除默认值并显式更新 PG 驱动参数后执行 n；新测试仍在独立原型，待处理边界后统一更新最终补丁/哈希。

n 混合失败红测 1 failed/1 passed：一种语言超时且另一语言空结果仍污染缓存。候选改为任一请求失败均阻止无成功候选的负缓存，并新增可接地成功候选不被降级。o 52 passed，8.60 秒；p PostgreSQL 父测试 1 passed，2.82 秒，驱动八项断言（两项数据库缓存恢复），专用容器与卷已清理。最新八文件补丁、哈希和候选归档已更新；仍未完成全量验收。

q 最新扩大回归 332 passed，114.44 秒，含混合失败补修后的 Agent/来源与严格语料；不是完整门禁。

## r 与 M2 组合基线验证

八文件 M3 差异移入独立 /tmp/rssripple-v17-rebased，基于冻结 M2（尚未验收）。运行文件逐一检查旧/新基线一致，仅追加的两份设计/测试文档保留双边内容。r 回归含 M2 38 项季证据与严格新版语料，句柄 51621，日志 /tmp/rssripple-v17-r.log；不改动 M2 被测目录。

## M2 候选基线上的组合回归（r）

`/tmp/rssripple-v17-rebased` 的原句柄 51621 已退出 0：370 passed、1 warning，153.88 秒。包含 M2 的 38 项季号证据矩阵和既有 Wiki/TMDB/Agent/语料回归；日志与 JUnit 已归档为 probes/wiki-failure-r.*。M2 完整门禁尚未结束且单元输出已有失败，因此该组合结果不能证明 M2 或 M3 可合入；先定位 M2 门禁失败，再审查对 M3 基线的影响。

## 修订后 M2 基线与 web 空回退遗漏（s–w）

s 将 M3 八份文件移到 M2 an 的冻结基线，权威文档三方合并保留 franchise 不猜季修订；组合回归 **453 passed，240.01 秒**。该通过结果不能证明尚未覆盖的分支正确。

后续源码复审发现：主源请求失败后，web 回退若成功返回空结果，会直接 error=None，绕过 M3 的最终错误标记。t 使用既有录制标题和合成超时/空答复，在实际 judge→失败分类路径复现 not_found（1 failed，7.75 秒）；u 在独立 /tmp/rssripple-v17-web-review-eyo54230 用真实 Turso、实际 Agent.process、commit 与独立观察会话确认负缓存确实落库（全部/部分主源失败两项，2 failed，5.02 秒）。Web 搜索空结果不能证明故障主源上不存在作品，应保留瞬态状态；成功候选仍可以恢复匹配。

补修 web 分支后，v **56 passed，10.58 秒**。四项真实数据库用例覆盖全部/部分主源失败 × web 禁用/成功空结果；故障期不缓存、恢复后重新调用来源并缓存真正空结果。另补 web 成功结果保留的正例。正式 PG 驱动扩展为 12 项（4 项实际数据库故障恢复、8 项编排/来源适配器），w 专项运行中。完整门禁尚未执行，新候选不得合入；审查见 V17-FINAL-REVIEW.md。

w 已退出 0：父测试 1 passed（2.90 秒），内部 12 项均通过；专用子库 finally 删除，使用 tmpfs/自动删除的 rssripple-v17-web-w 容器已停止清理。实际结果清单、日志与 JUnit 归档为 probes/wiki-failure-w.*。

最新八文件候选及 3054 文件完整冻结清单为 wiki-failure-x-source.json / wiki-failure-x-frozen.json，完整有效文件归档为 wiki-failure-x-candidate.tar.gz。候选全仓 Ruff 通过。x 完整单元/API 已启动（原句柄 9731），y 完整集成使用新项目 rssripple-v17-final-y；待完整门禁终态及基线 M2 验收后，才可按逐文件哈希审查合入。

t 的原始日志/JUnit 以 wiki-failure-t-raw.*.gz 完整保留；便于 Git 检查的文本副本仅移除行末空白，不改变失败内容。
