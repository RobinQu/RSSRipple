# V30 列表与校验路径的 ORM 加载边界

状态：双库必要性及受控方案对照完成，未修改运行代码，未关闭 P1-D7（当前 P2）。V26 全量门禁保持原冻结输入；本批不能借用其他批次通过结果。

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
