# V32 UTC 存储与时间输出（P2，必要性已验证）

## 重新论证

naive DateTime 本身不是充分的缺陷证据：本项目的 `utcnow()` 明确返回 naive UTC，现存 PostgreSQL 列为 timestamp without time zone，应用内部已有该约定。实际问题是数据库 `now()` 转入该列时服从会话时区，与 Python UTC 值混用；此外部分 HTTP 时间没有 UTC 标识。不能仅把模型改成 timezone=True 而不迁移旧列或修正调用方。

当前数据模型清单实际有 **96 个 DateTime 列，全部 timezone=False；72 个 server_default=now()，30 个 onupdate=now()**。另有 14 处引擎创建调用和 18 处 API/队列 isoformat 调用；这些数字是静态范围，不表示每处都是缺陷。清单见 [inventory](probes/utc-contract-inventory.json)。

本轮基线 `1e0744a`。使用固定 SHA256 的 prod_works_v1 录制频道名称、URL 和字段映射，创建明确合成的新行；不拉取该 URL，不读写生产库。专用 PostgreSQL 16 项目 `rssripple-v32-time-a`，三种会话时区；实际 Channel 模型插入和更新、真实频道 HTTP 路由与 API 客户端，重新读取落库值。另有实际临时文件 Turso 对照。

## a–d 证据

- a：9 项失败（退出 1），探针参考查询漏写 SELECT，属于夹具错误，不支持产品结论；保留源码及日志。
- b：修正探针后 **7 failed、2 passed、9.94 秒、退出 1**。UTC 下默认值/更新时间正确；Asia/Shanghai 的数据库时间比 UTC 快约 28800 秒，America/New_York 慢约 14400 秒；同一行 Python `last_fetched_at` 仍接近参考 UTC。三种时区的频道 HTTP 时间均不带 Z 或 +00:00。
- c：仅在探针强制连接使用 UTC，**3 failed、6 passed、10.05 秒、退出 1**。六项存储检查全部恢复，三项 HTTP 格式仍失败。说明连接 UTC 对照不能当作完整修复。
- d：实际 Turso 文件库，**1 failed、2 passed、9 deselected、14.40 秒、退出 1**。创建/更新时间为 UTC，HTTP 时间仍无时区标识。

完整结果与逐例属性见 [a–d](probes/utc-contract-a-d-result.json)。PostgreSQL 已 down --volumes，容器/网络/卷标签均为空；Turso tmp_path 由测试临时目录隔离。无运行代码修改、无本批后台任务。

前端 `formatDate/timeAgo` 已通过 parseApiDate 给无时区输入补 Z，不应声称所有页面均按用户本地时区误解析。但该兼容逻辑无法修正已经写偏的数据库值，也不替代程序端 API 的 UTC 契约。部分端点自行加 Z、部分直接 isoformat，必须按边界盘点，不能通过对所有字符串正则补 Z 来修复。

优先级仍为 **P2**：已证明非 UTC 会话场景缺陷，尚未证明当前生产数据库采用非 UTC 设置或已有错误历史；不升格为已确认生产数据污染。

## 修复方向与取舍

1. **保留既有 naive UTC 存储约定，统一数据库产生时间。** 采用显式 UTC 时间 SQL 表达式：PostgreSQL 用 CURRENT_TIMESTAMP AT TIME ZONE 'UTC'，Turso 保持 CURRENT_TIMESTAMP。模型默认/自动更新时间调用同一专用表达式，避免全局覆盖 SQLAlchemy func.now 的编译行为。同步审阅原生 SQL writer 和轻迁移使用的 CURRENT_TIMESTAMP；必要时固定应用连接 UTC 作为兼容保障，不能仅凭默认测试服务器是 UTC 就验收。
2. **存量模式必须覆盖。** create_all 不修改旧表默认值；按实际列目录升级 PostgreSQL 既有默认值，并验证幂等、失败回滚及默认定义漂移。不要把 SQLite/Turso 的正常 UTC 默认改成 PostgreSQL 表达式，也不要仅修改 ORM 声明冒充旧库升级。连接创建路径要覆盖 worker/web/脚本，不遗漏独立引擎。
3. **明确响应序列化。** 给真实 datetime 做 UTC 归一化和带 Z 的 ISO 输出；date 保持纯日期、null 保持空值。适当在统一响应边界处理仍为 datetime 的对象；已手工 isoformat 的资源、dashboard、队列等路径需显式使用同一 helper。禁止猜测任意业务字符串、作品标题或 JSON 载荷中的日期。单独检查 SSE、队列历史字符串及嵌套响应。外部含偏移输入先转换 UTC 再去掉 tzinfo，不能直接 replace 丢失偏移。
4. **不猜测历史时区。** 旧 naive 时间没有足够信息区分正确 UTC 与历史偏移值。升级前备份并审阅实际部署/会话配置；自动升级只修正未来写入和输出契约，不按固定 8 小时或当前会话时区批量平移历史。若发现明确错误历史，应依据可核验来源另做只读盘点及审核修复，而非隐藏在 schema 迁移中。

这些是待实现方案，不代表连接固定、模型替换、迁移或序列化已经完成。下一轮实施前继续核对实际调用点，避免扩大成未论证的全库有时区列迁移。

## 严格验收

- 新库/历史库双后端：录制文本与合成时间、真实独立连接；UTC/东八区/负偏移及 DST 日期、插入默认/ORM 更新/原生 SQL、重连与池复用。期望使用独立 UTC 参考，不能以两个同样错误的值相等作为通过。
- 旧默认升级：重复启动、事务失败、原始业务值与日期逐项不变；明确报告历史数据无法推定的范围。
- 真实 HTTP：频道、资源、作品、队列、dashboard 及嵌套时间，UTC 后缀和时刻都正确；date/null/业务字符串不变。前端既有日期兼容和直接 Date 消费路径回归。
- 对带偏移和无时区的输入逐一证明策略；非 UTC 输入不能仅去掉 tzinfo 后写入。
- 完整单元/API ≥95%、隔离集成 ≥85%，零失败；审计跳过、冻结全输入、由完整基线推导有效范围、退出、报告导出与唯一 Compose 清理。V26 q/r 继续冻结，本轮证据不允许合入运行代码。

## e–i：存储与旧默认值升级候选

独立副本 `/tmp/rssripple-v32-utc-m6c1uygj` 基于 `2e7a6a0`，当前 **51 个有效文件**。全部模型的 102 处默认/自动更新调用使用小型 `UTCNow` 表达式；PostgreSQL 编译为显式 UTC 事务时间，Turso 保持 CURRENT_TIMESTAMP。三个启动原生 SQL 写入点同步使用相同表达式。未全局改写 func.now，没有改成有时区列，也未改动历史数据。

旧 PostgreSQL 默认升级按当前 schema 的真实目录校验全部模型受管列；先验证类型和定义，再按表合并 ALTER。异常默认/类型或缺失列明确拒绝；旧 now()/CURRENT_TIMESTAMP/transaction_timestamp() 和缺失默认可升级。重复升级通过实际 SQL 观察确认无 ALTER；整个步骤使用调用方既有 DDL 事务，失败回滚。模型/迁移权威文档及集成清单已在候选同步。

证据：

- e：原三时区 PG + 实际 Turso 探针的创建/更新时间检查 **8 passed、4 deselected、11.25 秒、退出 0**。本轮没有宣称四项 HTTP 检查已修复。
- f：正式双库存储及所有旧默认升级测试 **8 passed、4.84 秒、退出 0**。
- g：完整既有迁移文件 **49 passed、1 failed、54.83 秒、退出 1**；唯一失败为旧 `_FakeResult` 没有新目录查询使用的 mappings()，真实数据库路径通过。补齐测试替身的目录及结果接口，没有放宽生产检查。
- h：修正夹具后完整迁移文件 **50 passed、2 warnings、53.78 秒、退出 0**，无跳过。现存未 await 的 fake savepoint warning 保留。
- i：补缺失默认/缺失列处理及重复升级无 DDL 断言，正式集成 **10 passed、6.57 秒、退出 0**。历史行逐字段保持，DDL 后故障回滚及重试、非 UTC 会话原生 INSERT 消费升级后默认均通过。

全仓无缓存 Ruff 和最终新增测试 Ruff 均退出 0。类命名/导入排序的早期 lint 失败原样保存；普通 func 的无用导入因表达式替换移除。专用项目 `rssripple-v32-time-e` 已清理，容器/网络/卷均为空，无本批后台测试。

源码/双方哈希/可恢复 tar 见 [i-source](probes/utc-contract-i-source.json)，逐轮结果和原始日志/JUnit 见 [e–i](probes/utc-contract-e-i-result.json)。范围从全部跟踪输入独立推导，并检查 app/tests/scripts 新 Python 文件无漏项。

**尚未完成的同一问题范围**：API UTC 输出、已经手工序列化的路径、SSE、偏移输入归一化及 DST/边界验证。当前候选仅完成存储部分，不缩减 V32 目标，不关闭 TODO，不批准合入；后续先补这些边界，再正式重基和完整双门禁。V26 q/r 冻结副本不受影响。


## j–l：外部偏移输入的实际写入与 DST 回归

重新核对必要性：RSS 的 `torrent_pubdate` 与字段映射 `iso_datetime` 原来直接返回 datetime.fromisoformat 结果，含偏移输入会进入 naive 数据列。本轮保持 i 存储实现和同一测试文件不变，先验证原解析逻辑：j **24 failed、7 passed、35.02 秒、退出 1**。PostgreSQL 的实际绑定拒绝 aware 时间；Turso 写入非零偏移时丢失原时刻，Z 输入虽然存储时刻正确，解析值仍违反 naive 约定。日志保留每项错误，不归因于连接时区默认值。尚无生产对应数据损坏证据，仍按 P2。

独立继承候选 `/tmp/rssripple-v32-input-lbomiqmh` 在 `app/utils/time.py` 增加小型 `naive_utc`：aware 先 astimezone(UTC) 再去掉 tzinfo；naive 不变。两个解析入口统一调用，标准 RSS 已由 feedparser 归一化的路径保持不变。不根据机器时区猜测无偏移输入，不平移历史行。

新增正式集成使用固定 SHA256 的 prod_works_v1 录制资源标题/guid/torrent_url，合成发布时间；真实两库写入后用独立连接读取，期望 UTC 时刻为人工列出的常量。覆盖东八区跨年、纽约春季跳时和秋季同一墙钟时间的两种偏移、Z 微秒、naive、非法输入及标准 RSS。录制 URL 不被请求；这是实际解析器与持久化边界测试，不是完整抓取流水线测试。

k 全部 time_contract 集成 **41 passed、0 skipped、39.09 秒、退出 0**；l 两个完整解析器单元文件 **227 passed、1 warning、0.48 秒、退出 0**。全仓无缓存 Ruff 退出 0。专用 PostgreSQL 项目 `rssripple-v32-input-j` 启动/清理均退出 0，容器/卷/网络标签为空；无本批后台测试。

来源由完整 Git 基线独立推导，新增文件扫描纳入，当前 **56 个有效文件**。可恢复源码见 [l-source](probes/utc-contract-l-source.json) 与 l-candidate.tar.gz，红/绿报告、退出和清理见 [j–l](probes/utc-contract-j-l-result.json)。业务逻辑和测试清单已同步到候选。

仍须完成 API 输出、手工字符串、SSE/队列历史与其他类型化 API 偏移输入审计、前端消费边界，再正式重基及完整双门禁。当前不是 V32 完整修复，不关闭 TODO，不批准合入。V27 r/s 冻结门禁不受此候选改动影响。


补充下一轮输入审计入口：静态检查发现 `RetryRequest.since` / `RegenerateRequest.since`（schemas/notification.py）直接由 notifications API 传给 notify_service 的 created_at/completed_at 比较，尚未见偏移归一化；需用真实双库与明确时间窗口复现后再修改，不能仅凭搜索认定生产影响。AgentRunRequest.scan_since 在 agents API 已有 astimezone(UTC) 后去 tzinfo 的正确处理，应补边界验证并避免重复修复。
