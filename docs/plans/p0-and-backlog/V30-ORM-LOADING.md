# V30 列表与校验路径的 ORM 加载边界

状态：p/q 完整门禁及最终五轴审查通过，14 文件已同步本地 main 工作树。单元/API 4183 项、96.86%，集成 3861 项、89.41%，零失败；跳过、6454 冻结输入、独立范围、正常退出、报告归档与项目清理均核对通过。P1-D7（当前 P2）从待办移除。[最终审查](V30-REVIEW.md)、[验收与提交后核验](probes/orm-loading-p-q-accepted.json)。以下保留早期记录。

## 必要性与优先级复核

现有 Channel 三个集合和 Agent 六个集合配置默认 selectin（旧清单“七个”不准确，runs 当前不是 selectin）。频道列表只返回标量及计数，却实例化频道全部资源、Agent 及其历史；Agent 列表还经 `AgentResponse.channel: Any` 将已加载资源带入嵌套响应。存在真实默认 PostgreSQL 后端证据，保留 P2 规模问题定位；未测当前生产规模，不将探针耗时换算生产延迟。

同一实际 `/api/v1/channels` 与 `/api/v1/agents` 路由，4 个频道、每频道 2 Agent，资源规模 32/512，Agent 各有同规模的下载和已决策历史；每个 Agent 1 个合法电影订阅，2 个 active task。电影身份/标题及资源原始标题来自既有录制语料（固定 SHA-256），UUID、GUID、关联关系、时间、分布、历史和网络地址明确合成，不宣称为完整生产回放。使用真实临时 Turso/独立 PG schema，ASGI 调用，验证分页 total、行数、订阅和计数。

| 后端 / 单行响应 | 原始 SQL 数 | 原始实例化的额外实体 | 响应字节 |
|---|---:|---|---:|
| Turso 频道，512 资源/频道 | 13 | 512 Resource、2 Agent、1024 Decision、1024 Task、2 AgentWork | 890 |
| PostgreSQL 频道，同规模 | 13 | 同上 | 897 |
| Turso Agent，同规模 | 14 | 512 Resource、512 Decision、512 Task；另有响应所需关系 | 716514 |
| PostgreSQL Agent，同规模 | 14 | 同上 | 723738 |

频道 page_size 从 1→2→4，SQL 数 13→15→19，反映每频道额外两次 COUNT；Agent 同样有逐行 active task COUNT（14→15→17）。历史规模扩大时响应所需数量不变，无关实体数量和 Agent 嵌套响应却随历史增长。

## a–e 方案反例与对照

- a：原始两个后端均退出 0，保存 SQL 与 ORM `loaded_as_persistent` 计数、响应大小。时间只作诊断，不作为稳定性或性能门槛。
- b：仅在探针会话给根 Channel/Agent 查询加 `raiseload('*')`，保留端点显式 selectin。两个后端均退出 0；频道只实例化自身，Agent 只加载自身、订阅、频道、下载器和目标电影。单行 Agent 响应为 2813/2869 字节，原分页/计数/订阅断言不变。但 COUNT 仍随页大小增长，不能视为最终修复。
- c：有意在依赖会话中提前加载并保留 Channel，再使用 b，Turso 退出 1，FastAPI `jsonable_encoder` 因 ORM 图循环 RecursionError。查询选项不会清除会话已有关系。
- d：完全不加 b 的原始基线，在同样预加载会话下也退出 1，证明 c 并非新策略制造的错误；不宣称未经注入的生产请求已出现该异常。
- e：在探针进程将嵌套 channel 明确限定为既有 `ChannelResponse`，结合 b，双库均退出 0；预加载会话仍返回正确、有限的响应。e 的 SQL 计数包含故意预加载，不应与冷会话 b 直接比较。e 仅证明频道响应方向，尚未修复所有 Any 关系或全局 ORM 默认加载。

因此不能只给两个 SELECT 打补丁后关闭原待办，也不能把全局关系改成隐式懒加载以绕开问题。

## 正式实现范围

1. 清点 Channel/Agent 无界集合的所有读取、Pydantic 隐式访问与删除 cascade。33 个语法访问已存档，其中含 schema/result 同名属性及 loader options，不能当成 33 个未修复业务调用。实际直接关系读取包括 fetch 末尾 `channel.agents`、规则构建及创建订阅的 `agent.works`；Dashboard/Agent 响应已有显式部分加载。
2. 对无界集合采用显式加载策略，遗漏访问尽早报错；不能切为普通 `lazy='select'` 造成 MissingGreenlet。逐一改用所需字段/ID 查询或显式 selectin。`Agent.works` 有订阅上限且确为规则输入，是否保留默认批量加载须结合所有调用者论证，不能机械替换。
3. 频道/Agent 列表的 count 采用仅针对本页 ID 的分组聚合，保持零计数及 active status 语义。SQL 数随页面关系批次有界，不能随总历史条数或每行 COUNT 增长；不为了避免一次显式查询而把全部任务载入内存。
4. 显式定义 Agent 嵌套 channel/downloader/works 目标响应，遵循既有公开字段，不能把 ORM `__dict__` 的偶然加载内容当接口契约。复用公开响应类型、排除额外关系与敏感内部字段，验证前端所用标题、频道名、订阅目标、下载器名均保留。
5. 保持频道抓取后 active Agent 入队、规则预览/提交、水位线、10 订阅上限、详情以及删除/保留历史契约。避免为了列表更快而漏跑 Agent 或改变 ORM cascade。

## 严格测试与验收

- 正式双库实际 API 回归：0/1/多项数据、首/中/末/越界页、不同页大小、32/512 历史规模；加入足量真实录制资源子集，并明确区分合成规模。独立断言 ID、所有公开字段、total、计数和订阅目标，不能只断言 HTTP 200 或响应更小。
- 冷会话及显式预加载会话；实体/SQL 计数以作用域事件采样，预加载成本独立记录；限定 ORM materialization 和聚合查询预算，不以墙钟耗时作为唯一门禁。频道列表预期最多 4 个查询（总数、页、两组计数）；Agent 按已返回订阅量及 selectin 分批推导预算。
- 抓取入队、Agent CRUD/详情、规则预览、定向/增量运行、订阅上限、删除历史及后台相关调用回归，覆盖显式加载遗漏和异步访问错误。默认关系变更不能只用两个列表通过作为验证。
- 同步 data-models/api-endpoints/business-logic 及测试清单；正式重基，冻结输入，完成单元/API ≥95%、隔离集成 ≥85%、零失败，跳过/哈希/退出/覆盖率/清理审计和五轴终审，再合入。

专用 `rssripple-v30-loading-a` 已清理，标签容器/网络/卷均为空；真实失败和控制原样保留。机器证据及原始源码：[a–e 结果](probes/orm-loading-a-e-result.json)；访问清单见同结果 artifacts。


## f–n：实现、必要性再核对与正式回归

首候选 `/tmp/rssripple-v30-loading-lq597tje` 将 Channel 三个集合和 Agent 五个历史集合设为 lazy=raise，批量页内计数，Agent/AgentWork 嵌套复用公开响应类型；抓取按活跃 Agent ID 入队。g 原有调用方回归 **191 passed、420.25 秒、退出 0**。f 的十二例因测试在运行中的 event loop 内请求异步 fixture 而失败，未到 API 断言；用同步参数 fixture 正确初始化后，h **12 passed、44.50 秒、退出 0**，保留 f 原始源码和失败。

进一步复核 B6 权威契约推翻“works 一律有界”的初始假设：全范围允许超过 10 条覆盖配置。第二候选 `/tmp/rssripple-v30-explicit-egw46fl0` 将 works 也改为显式加载，规则服务仅在未加载时显式 refresh，保持本事务已加载规则；上限校验在既有锁内 COUNT，不改变全范围例外。i **14 passed、47.30 秒、退出 0**，新增双库 22 配置保留/按需读取回归。j 扩大调用方 **338 passed、669.03 秒、退出 0**。

审查发现第二候选的活跃 ID 查询位于最终 commit 后，会开启新事务并跨越队列调用；k 双库正式断言均失败（2 failed、3.46 秒、退出 1）。最终候选 `/tmp/rssripple-v30-transactions-jl46u0f_` 把 ID 查询移到最终提交前，提交结束后再入队，并移除 Agent 运行第一扫描阶段多余的 works 加载。l **16 passed、50.17 秒、退出 0**；新增断言在 enqueue 外检查仅活跃 ID、独立观察到已提交 success、无活跃事务，避免生产捕获异常使测试误过。m 受影响完整 fetch_service/job_handlers 单元文件 **92 passed、178.26 秒、退出 0**。

n 将同一正式测试放回未修改 main：双库列表加载边界与全范围配置自动加载共 **4 failed、12 deselected、6.62 秒、退出 1**，失败是实际加载断言，不是夹具错误。该负向对照确认回归能拦住原行为；正式候选不能沿用 n 的失败为自身门禁，也不能把专项通过说成完整验收。

最终 14 文件包含八个运行文件、两个正式测试文件及四份权威设计/测试清单。全仓无缓存 Ruff 通过，源码与 tar 归档可恢复：[l 源](probes/orm-loading-l-source.json)、[f–l 证据](probes/orm-loading-f-l-result.json)、[j–n 终态](probes/orm-loading-j-n-result.json)。专用 PG rssripple-v30-tests-f 已清理，标签资源为空。所有本轮测试句柄已终态，下一轮不重复轮询。

阶段审查：正确性边界已经从冷列表扩展到预加载图、全范围规则和事务后入队；公开 schema 防止历史图/下载器密码被偶然序列化。规则集合在业务需要处读取，默认查询不承担隐含 IO；页内聚合没有逐行 COUNT。没有新增依赖或 DDL，既有 FK/cascade 未变。批量加载开销仍随实际返回作品配置而增长，这是接口自身数据量，不能声称所有响应恒定大小。仍需正式重基及完整双门禁、全部相关 API/后台生命周期覆盖与最终终审；**本批未批准合入**。

## o–q 正式重基与完整门禁

必要性再核对：新主干的八个相关运行文件仍与原型旧基线一致，V25–V29 没有替代此加载修复；保留已有实际 API 红/绿与录制数据证据，仍为 P2。o 从 Git 导出完整 `07633cd`，应用已存档的 l 候选；运行实现与测试原样保留，data-models 三方合并无冲突，测试清单按已证明的旧基线完整前缀追加后保留主干全部 V29 内容。V25 离线脚本补齐、V26/V27 和 V29 驱动/FTS 修复均继承。

重基候选 `/tmp/rssripple-v30-rebased-sznr_fn7`，指针 `/tmp/rssripple-v30-rebased-path`。独立比较 Git 对象推导 14 个有效文件，冻结全仓 6454 输入（3306 个非计划输入），全部 app/tests/scripts 新 Python 文件已包含。无缓存 Ruff 通过。使用 pyturso 0.8.2 的实际双库列表/全范围/入队事务边界 16 项全部通过，预检 PG `rssripple-v30-preflight-o` 已清理、标签为空。证据：[o 预检](probes/orm-loading-o-result.json)、[源码](probes/orm-loading-o-source.json)、[冻结](probes/orm-loading-o-frozen.json)、[独立范围](probes/orm-loading-o-scope.json)。

p 完整单元/API 会话 30903，专用 PG `rssripple-v30-unit-p`（32896）；q 完整集成会话 79513，项目 `rssripple-v30-final-q`，保留 runner 供导出。两次 startup 均退出 0。锁文件与已验收 V29 相同，复用 `rssripple-v29-tests:local`；实际 app/app-llm 镜像摘要均核对为 `sha256:9f79b2a0d7b37b8e52545b7d0582df5474ecbf3ff93ebdbd2633fa6cbacf3985`，健康检查通过。不得修改在跑候选、因观察超时重启，或在实际退出/覆盖率/清理和最终审查前关闭待办。后续跳过基线为 V29 t/u；完整续接信息见 [p/q](probes/orm-loading-p-q-running.json)。
