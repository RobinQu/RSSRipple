# V33 搜索文本长度一致性

> 最新：已在冻结 V32 ac 之上完成兼容性候选，V33 相对范围 19 文件（与 V32 合计 85 文件）。p/s 联合证据覆盖 289 个不同通过用例，独立项目已清理。V32 ad/ae 尚在运行；V33 仍待其验收后正式重基与完整双门禁，不属于已合入代码。

## 必要性与优先级（2026-10-07）

基线 `5846295`。四类作品的 `search_text` 模型列为 `String(4096)`，而 `build_search_text` 拼接全部规范化标题和别名；别名 API 字段未限制总长度。轻迁移新增列使用 `TEXT`，所以 PostgreSQL 新库与部分旧库的实际类型也可能不同。本轮独立复核，不修改 V27 在跑的冻结副本。

真实录制快照 `prod_works_v1.json` 的 SHA 为 `d11651d2162ced23e8d919af0bff2d9f316e203234cc854909ba5f444a35ec32`：65 条剧集、22 条电影、37 条合集，规范化文本最大长度分别 501、311、54；没有音频记录。音频场景借用录制电影标题作为标签，不能称为真实音频数据。所有长别名、候选类型和身份均为明确合成的边界条件；未检查当前生产数据，保持 P2，不据此声称生产写入已受损。

[探针](probes/search_text_length_probe.py) 对四类模型分别执行新增/更新 × 4095/4096/4097/8192 字符，真实数据库提交后从独立会话重读完整 aliases、search_text 与尾部标记。

- PostgreSQL a：16 通过、16 失败，进程退出 1；超 4096 的新增和更新均报 SQLSTATE 22001 长度错误，两个界内长度全部成功。
- Turso b：32 通过，进程退出 0；每例保存完整文本，临时数据库自动清理。

## 修复方案与原型

四个 ORM 列统一用 `Text`；派生搜索内容应完整保存，不能截断后半段别名。保留现有标准化、搜索、GIN 与 FTS 内容语义。PG 旧列在已有启动 DDL 事务中变为 TEXT，先检查全部四列，再执行 ALTER；已是 TEXT 则不操作。缺列、非 varchar/text 或 domain 类型明确失败，避免推测未知 schema。Turso 不强制 VARCHAR 长度，不对旧表重建。

[六文件原型清单](probes/search-text-f-source.json) 对完整 app 副本与 main 比较推导，包含四个模型、`search_text_schema.py` 和轻迁移调用。副本路径 `/tmp/rssripple-v33-search-902xn062`，指针 `/tmp/rssripple-v33-path`；补丁与六文件归档均在 probes。它只是运行原型，不是完整冻结候选，尚无正式集成测试及权威文档变更，禁止合入。

## 升级证据与失败边界

c 探针因 `all` 内含 await 导致 async generator 的测试脚本错误退出 1，不作产品失败或通过证据。修正后的 d 在 ALTER 提交后复用连接读取时触发 asyncpg `InvalidCachedStatementError`，退出 1；不能吞掉该异常或把该轮计为通过。

e 明确模拟停写升级后重建连接，退出 0，验证：注入异常后四列类型和所有原行回滚；升级提交后全部原行的 id/search_text/aliases 不变；再次调用不执行 ALTER；三张作品表既有 GIN 索引定义不变。四表分别检查 6/6/6/14 行（音频/电影/剧集/合集），均来自 a 的成功写入和更新前置短文本，合集还包含剧集所需父记录。原型 f 对升级后的同一 PG 库运行原 32 项矩阵，全部成功、退出 0。

**升级限制**：DDL 需要表锁，已有 prepared statement 可因类型变化失效。正式方案须按停旧写入进程、迁移并重启连接执行，并以实际启动路径证明这一流程；不能将 e 的重连结果泛化为在线无中断升级。大规模 GIN 重建耗时尚未测量。

所有原始日志、JSON、c/d/e 脚本及原型见 [a–f 摘要](probes/search-text-a-f-result.json)。唯一 PostgreSQL 项目 `rssripple-v33-search-a`（端口 32886）已 down，容器/网络/卷标签检查均为空。未访问生产或远端。

## 后续严格验收

1. 补正式双库测试和完整候选：新库/旧 varchar/旧 TEXT、四模型新增更新、尾部别名可搜索、单独会话读回；覆盖实际 HTTP 更新和元数据写入路径。保留 4095/4096 的对照与 4097/8192 的失效边界，加入 Unicode 规范化扩张。
2. 真实 `init_db` 重复启动、旧表缺列补齐、DDL 回滚、域/类型漂移拒绝、GIN 可用性和并发启动；验证停写重连流程，并明确旧连接缓存失效的处理边界。不能仅靠函数级 ALTER 通过声称升级完成。
3. 同步 data-models、db-migration、业务搜索契约与测试清单；审查新 helper 在其他待合入迁移候选中的顺序和事务边界。
4. 在前批验收后正式重基，完整冻结实际范围；全单元/API ≥95%、完整隔离集成 ≥85%、零失败；逐项审计跳过、真实进程退出、冻结哈希、覆盖率导出与资源清理，并完成五轴终审后才合入本地 main。

本轮只确立必要性和可行原型。已有 P0/B9 合入结论不变，V33 待办保持未完成。

## 正式专项与启动补修（g–m，2026-10-07）

本轮重新论证：a/f 函数级证据不能代替 API 或启动升级。独立候选增加双库存储/搜索、实际 HTTP 和 create_tables 测试，复用固定 SHA 录制标题，并明确保留合成长别名/身份边界。g **80 passed，88.42 秒，退出 0**：四模型 × 两种写入 × 五个长度/Unicode 边界 × 两后端，末尾搜索通过。

h HTTP 首轮 **10 passed / 4 failed，15.65 秒，退出 1**，四个剧集断言误把 aliases 当覆盖；实际 API 和权威契约要求保留旧别名并追加。修正为同时断言旧 short 与新增长别名、完整搜索文本后，j **14 passed，16.07 秒，退出 0**。合集 API 不提供 aliases，测试用已有合法 4095 字符搜索文本，在保持标题字段 ≤512 时增加标题，使拼接后跨过旧限制；没有虚构 API 字段。

i 实际启动 **6 passed / 1 failed，4.82 秒，退出 1**：缺列旧库补列后 WorkCollection 的搜索列仍为 NULL，现有 `backfill_search_text` 只含三种作品。这是实际回填缺口，候选将合集纳入同一 NULL 回填，不改变非 NULL 文本或 aliases。k 启动加既有 FTS/outbox 回归 **55 passed / 1 warning，86.97 秒，退出 0**；warning 为既有 event_loop_policy 弃用提示。

l 最终专项 **105 passed，零跳过，125.84 秒，退出 0**：80 存储/搜索、14 HTTP、11 启动/迁移。后者覆盖新库、旧 varchar、已有 TEXT、缺列、未知域/类型/缺列拒绝、Turso NULL 回填、真实事务回滚与 GIN 保留、两个实际 create_tables 调用并发。并发测试先由第三连接持启动 advisory 锁，真实观察两个调用均处于 advisory 等待；释放后四列只执行四次 ALTER，不靠调用先后猜测发生了竞争。

m 将**同一 HTTP 测试文件**用于当前 main 原运行代码的独立副本：**7 failed / 7 passed，16.67 秒，退出 1**。全部 PostgreSQL HTTP 场景返回 500，Turso 对照均成功；源文件哈希和逐文件 main 代码一致性保存在 [g–m 摘要](probes/search-text-g-m-result.json)。这证明绿色结果不是由弱化 HTTP 断言取得。h 原测试、所有失败轮和原始日志/JUnit 均归档。

当前 [16 文件候选](probes/search-text-m-source.json) 包含七个运行文件、五个测试文件、四份权威设计/迁移/测试文档，归档 `search-text-m-candidate.tar.gz`。独立项目 `rssripple-v33-search-g`（32887）已清理，容器/网络/卷标签均为空。main 运行代码未修改，未关闭 TODO。正式全仓冻结/重基、元数据写入调用方扩大、完整双门禁与五轴终审仍待完成；上述 105 项不能替代完整门禁。


## 元数据调用方补验（n/o，2026-10-07）

重新论证：HTTP 人工编辑通过不足以代表自动来源刷新。新增 `test_metadata.py` 直接调用真实三类 metadata upsert，以已录制标题配合明确合成来源身份/别名，电影和剧集覆盖新增/更新的来源别名批量输入，音频按实际契约通过 14 次刷新逐步累积每个不超过 512 字符的标题。断言原身份不变、全部别名及其规范化文本保留，剧集新增还检查所属合集。海报为返回 None 的边界替身，所有入参都断言为 None；不访问外部元数据或声称来源响应为真实录制。

n **10 passed，11.58 秒，退出 0**；o 既有 metadata_service 单元文件与 batch_content_analysis 集成文件 **229 passed、1 warning，299.80 秒，退出 0**，无跳过。原始日志/JUnit 见 [n/o 摘要](probes/search-text-n-o-result.json)。独立项目 rssripple-v33-metadata-n（32888）已清理，标签容器/网络/卷为空。七个运行文件与 m 候选逐字节相同，仅补测试和清单；最新 [17 文件候选](probes/search-text-o-source.json) 及完整有效文件归档已保存。

元数据调用方缺口现已补验；仍待前批验收后正式重基、全仓输入冻结、完整双门禁和五轴终审，未合入运行代码。


## p–s：与 V32 的迁移兼容性（2026-10-07）

再次论证：长别名在真实 PostgreSQL 写入失败的已有红测仍成立；没有新增生产受损证据，保持 P2。V32 与 V33 同时改动四个模型和启动迁移位置，因此仅各自专项通过不足以证明共同升级。以当前 main `3f589e2` 加冻结 V32 ac 构建独立候选 `/tmp/rssripple-v33-with-v32-uw7c30cl`，不修改运行中的 V32 源码。

三个重叠层次均保留：V27 工作 FK 检查、V32 UTC 默认修正、V33 搜索列扩展；仍在既有 PostgreSQL startup advisory lock 与同一 DDL 事务内。四模型同时保留 UTCNow 和 Text，不恢复旧 func.now。V29 Turso FTS 升级与 V30 加载策略保留。初始 V33 差异仍为 17 文件；全 Git 非计划输入和新 Python 文件独立比较，未仅依赖旧清单。

p 执行完整 search_text、time_contract 与数据库迁移文件，**284 passed、1 failed、2 既有 warnings，316.78 秒、实际退出 1**。唯一失败是旧 `_FakeConn` 未提供新搜索列目录，真实数据库用例全部通过；补齐替身的 catalog 返回形状，不改生产校验。p 原始失败日志和范围清单保留。

新增四项实际 create_tables 组合测试：同一旧库同时有 now() 默认与 VARCHAR(4096)，重建连接使用 Asia/Shanghai 会话；分别注入 UTC 默认漂移、搜索类型漂移、两类 DDL 已执行后的故障。失败须完整恢复默认与类型目录，明确解决漂移后可重试；重复启动历史 Channel 全字段不变，新 raw INSERT 仍产生 UTC，8192 字符完整落库且尾部可查。

q 首次从外置文件调用 pytest 未加载项目 asyncio 配置，**4 setup errors、8.90 秒、退出 1**，不支持产品结论。r 显式 `-c pyproject.toml` 后，同一文件 **4 passed、6.02 秒、退出 0**。用例随后归入正式 search_text 测试树。s 对完整迁移文件与新组合文件复验，**54 passed、零跳过、2 既有 warnings，62.98 秒、退出 0**；之后仅规范导入顺序，全仓 Ruff 无缓存退出 0。p 的成功项与 s 按 class/name 去重，共 **289 个不同通过用例**，不把重复执行数累加为覆盖数。

[最终来源](probes/search-text-s-source.json) 记录 V33 相对 V32 的 **19 文件**，与 V32 共同相对当前 main 的 **85 文件**；多出的两项为组合测试和旧迁移替身修正。运行实现没有在 p 后改变；设计迁移文档与测试清单补充组合契约。完整可恢复归档 `probes/search-text-s-combined-candidate.tar.gz`；结果、原始 JUnit、失败边界与清理见 [p–s 摘要](probes/search-text-p-s-result.json)。

专用项目 `rssripple-v33-compat-p`（32907）已 down，容器/网络/卷标签为空，所有本批测试终止。另核对 V32 6647 冻结输入逐文件未变，ad/ae 原会话仍在运行。该副本用于兼容性准备，不代替 V33 在已验收主干上的正式重基、全输入冻结、完整单元/API ≥95% 与隔离集成 ≥85%、零失败、退出/跳过/导出/清理及五轴最终审查。
