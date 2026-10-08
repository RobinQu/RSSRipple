# V35 元数据缓存标题与键长度

## 必要性与优先级（2026-10-08）

基线 main `2f9cf00`。`FileResource.title_raw` 可存 1024 字符，但 `MetadataCache.title` 只有 VARCHAR(512)，且有 title 单列索引和 (title, source) 复合唯一键。真实 `_set_cache` 仅 strip 后写入原始标题，未做长度限制；`_get_cache` 按完整标题和 source 命名空间读取。

固定 SHA 的 prod_works_v1 共有 559 个资源标题，最长 176 字符，超过 512 的为零。因此这是合法边界的可复现缺口，继续按 **P2** 处理，不声称录制数据或生产库已有损坏。探针使用第一条录制标题的前 80 字符及录制 GUID/URL，追加明确标记的确定性合成 ASCII/CJK 后缀；不访问录制 URL 或外部来源。

## a/b 真实双库证据

先在真实数据库提交完整 FileResource，再调用实际 metadata_repository `_set_cache`，成功时独立会话 `_get_cache` 和原始列重读，检查 source 隔离。来源 verdict 明确合成，不伪称完整 MetadataAgent/LLM 流程。

- a：512/513/1024 × ASCII/CJK × PostgreSQL/Turso，**4 failed、8 passed，24.29 秒、实际退出 1**。PostgreSQL 的四个 513/1024 缓存写入失败，但资源行此前已成功保存；两个 512 对照与全部六个 Turso 场景通过。
- b：仅在独立测试库把缓存列扩为 VARCHAR(1024)，执行相同六个 PG 用例，**1 failed、5 passed、6 deselected，11.99 秒、实际退出 1**。1024 字符 CJK 仍失败：`uq_metadata_cache_key` 索引行 2968 字节超过该 PostgreSQL 实例 btree 限制 2704。不是把列改成 TEXT 或 1024 就能完成修复。

原始源码、日志/JUnit、实际退出与数据来源见 [a/b 摘要](probes/metadata-cache-title-a-b-result.json)。专用项目 `rssripple-v35-cache-a`（32911）已清理，容器/网络/卷标签为空。主干 app/tests/scripts 无改动，无本批后台任务。

## 待验证修复方向

1. 保留完整原始标题，采用 TEXT 加定长 SHA-256 摘要键，唯一性按摘要与 source 命名空间建立，移除对完整长标题的 btree 索引依赖。缓存读取和冲突更新还须比较完整标题；摘要碰撞不得命中或覆盖另一标题结果。不得通过截断、仅取前 512 字符、跳过普通长标题缓存或吞异常来冒充修复。
2. 所有缓存 writer 统一计算同一规范化键，保持现有 strip、来源隔离、generation 与完整 metadata_json 语义。验证同键并发更新与陈旧 generation 删除竞争，避免删除/插入竞态或过期 reader 删除新结果；不新增外部依赖或要求数据库扩展权限。
3. 实际旧库升级必须覆盖两后端：有界回填摘要、完整保存原 id/title/source/payload/generation/时间，目录/索引定义校验，失败整体回滚与可重试。显式处理异常旧数据及冲突，不自动清空整张缓存；需要表重建时先证明无遗漏引用，并遵守停旧写入进程与重连要求。新旧库约束必须一致。
4. 下一阶段用真实 MetadataAgent 调用方补验长标题缓存 miss→写入→hit、source 隔离、force refresh 与失败行为；当前 a/b 仅验证资源和 repository 边界，不能声称完整抓取流程已修复。

## 严格验收

- 上述 ASCII/CJK 矩阵在实际双库全部通过，加入共用长前缀而尾部不同的标题、空白规范化、来源隔离、旧 generation、完整结果保留与人工构造摘要碰撞。
- 旧库新键迁移、重复启动、异常数据拒绝、DDL/回填故障回滚、并发启动与停止旧连接流程；大数据回填须有界，不能用只含空表的迁移测试替代。
- 来源文本继续复用录制快照，长文本/来源结果/故障明确合成；不编造生产超长样本。
- 在前批验收后正式重基与全输入冻结，完整单元/API ≥95%、隔离集成 ≥85%、零失败，逐项审计跳过、退出、覆盖率导出、清理和五轴审查。

## c/e 新库原型（未合入）

在 main `efc3698` 的独立归档上修改四个运行文件：完整 title 用 TEXT，SHA-256 与 source 构成唯一键；查询与原子 upsert 同时核对原文。Metadata repository 和批量分析 SSE 共用读写规则；ORM 插入/标题修改更新摘要，旧 generation 删除增加观测版本条件，避免同 id 新版本被无条件删除。未新增依赖。

- c：原 a 的同一组双库长标题用例 **12 passed、无跳过，14.98 秒，实际退出 0**。
- d：新增探针有 6 个异步 fixture 装配错误；既有 repository/资源 API 回归仍运行（session 54261），不能记为通过。原探针保留。
- e：修正装配后的双库对抗探针 **6 passed、无跳过，8.05 秒，实际退出 0**，覆盖人工强制摘要碰撞不命中/不覆盖、source 隔离、长公共前缀尾部区分、strip 语义、同会话 upsert 身份/创建时间保留、ORM 改名重建键及旧 generation 清除。碰撞和长文本均明确为合成边界。

[证据与四文件 SHA](probes/metadata-cache-title-c-e-result.json)附原型归档、日志/JUnit 和原/修正探针。专用项目 `rssripple-v35-cache-c`（32912）仍供 d 使用，尚未清理。候选路径 `/tmp/rssripple-v35-cache-uyusqqzh`。

**旧库迁移尚未实现，当前原型不能用于升级或合入**。并发写、陈旧读竞争、完整 MetadataAgent 调用方、双库迁移与全部门禁仍待完成，不关闭 TODO。V33 u/v 门禁及 V34 候选不受本探针影响。
