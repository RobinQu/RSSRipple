# V28 热 FK 与后台查询索引（P1-D5，现 P2，必要性验证中）

## 重新论证

不能按 `index=True` 是否存在直接判定缺索引：联合唯一索引的左前缀已经可用，部分索引只在查询满足其谓词时可用。当前仍按 P2 规模问题处理；没有生产耗时或容量事故证据。V25 完整门禁仍在运行，本批只做隔离验证，不修改其冻结源码。

2026-10-07 在实际 ORM 建表后的 Turso 与 PostgreSQL 目录中核对七张表，结果保存于 `probes/hot-fk-index-c-*.json`：

| 查询族 | 已有覆盖与待验证内容 |
|---|---|
| file_resources 工作/合集引用 | channel/guid 唯一索引不覆盖 series/movie/audio/collection；movie 单列选择性查找已验证，其他路径仍待测量 |
| agent_works | 无二级/唯一索引；订阅归属和作品迁移查询分别验证，不用一个组合索引冒充全部前缀 |
| pending_decisions | agent/key 的部分唯一索引仅覆盖 pending；status/created/id 已有普通索引；全状态历史、作品 FK 查询仍待测量 |
| download_tasks | status/agent 与 downloader/torrent 已有索引；resource 查询、资源清理反关联及单独 agent 查询仍待测量 |
| agent_runs | 无二级索引；实际 API 按 agent 过滤、started_at 倒序分页并计数，应比较组合索引而非只补 agent 单列 |
| webhook_deliveries | notification 已被普通索引和联合唯一前缀覆盖；pending 到期筛选带 NULL-or-due 且按 created_at 排序，不能直接断言 status/next_attempt_at 为最佳方案 |
| episodes | series/season/episode 联合唯一索引已经覆盖 series；不重复添加单列索引 |

## 首个规模对照

输入取自固定 SHA `d11651d2162ced23e8d919af0bff2d9f316e203234cc854909ba5f444a35ec32` 的 `prod_works_v1.json`。仅资源标题和电影标题取自录制集；16,000 行规模、资源/作品 UUID、GUID、关联及 16/16000 选择性均明确合成，URL 为不可访问的 example.invalid。不能称为原样生产回放或生产性能测试。

在真实完整 ORM schema 上，通过相同 `movie_id = :id` 谓词查询整行资源，比较临时单列索引前后计划及五次读取。两阶段均返回相同 16 个资源 ID：

- Turso：`SCAN file_resources` → `SEARCH ... USING INDEX ... (movie_id=?)`。
- PostgreSQL：Seq Scan，过滤 15,984 行、命中 844 个 shared blocks → Index Scan，仅返回 16 行；具体 EXPLAIN ANALYZE/BUFFERS JSON 保留。
- 读取中位耗时约为 Turso 7.42 → 0.56 ms、PostgreSQL 2.57 → 0.60 ms。机器同时执行其他门禁，时间仅作描述，不设置毫秒验收阈值，不推导生产加速倍数。

该结果证明代表性 FK 查找存在规模扫描缺口；尚未覆盖真实服务全部 SQL、UPDATE 成本、低选择性输入、写入代价或其余查询族。正式索引名称与最终组合尚未确定。初版探针未启用 MVCC、次版遗漏必填 torrent_url 的失败日志及源代码均保留；修正后两库退出 0，失败不算产品缺陷。

独立 PostgreSQL 项目 `rssripple-v28-index-a` 已清理，容器/网络/卷标签均为空；Turso 临时目录随连接释放后删除。完整目录、SQL、计划、结果 ID、计时和源码见 [c 审计](probes/hot-fk-index-c-result.json)。未修改运行代码或数据库迁移，未关闭 TODO。

## 实施前标准

1. 对表中每个查询族提取实际服务 SQL；保留录制身份和明确合成的规模/分布，至少覆盖高低选择性、无结果、NULL 与多状态。比较计划与结果集合，不只查看是否存在索引。
2. 从证据选择最少索引；避免重复唯一索引前缀，明确部分索引谓词和排序收益。测量写入/更新影响，检查 PostgreSQL 多字节索引风险，不混入无关大字段。
3. 新建模型和旧库升级同步。实际双库验证幂等、同名定义漂移、创建失败的启动行为、历史数据不变及 FK 表重建后的索引保留；不能以 `create_all` 代替旧表升级。
4. 在 V25/V26/V27 接受后重基，补正式集成测试。最终五维审查、冻结输入、完整单元/API ≥95%、完整隔离集成 ≥85%、零失败，审计跳过、哈希、退出和清理后才可合入。

## d–k 查询矩阵与正确性发现（2026-10-07）

d 在两库实际 schema 上构造每张热表 8192 行、1024 个 Agent、每 Agent 八个作品订阅，覆盖 15 类查询。录制输入仍仅为标题；UUID、关联、时间、通知 payload 与规模分布明确合成，只验证数据库查询，不宣称完整业务回放。Core 查询保留相关服务的筛选、排序和分页；ORM 自动 eager load 属于 D7，不计入本测量。d 的订阅查询另加 ID 排序消除同时间歧义，e 起按原关系排序，仅比较结果集合。

两库 d 均退出 0，索引前后结果 ID 集合相同。资源三类 FK、订阅三类 FK、决策作品 FK 和历史、运行历史等从扫描变为索引访问；已有 pending 部分唯一索引继续用于 pending 查询，无需再为该查询重复造索引。PostgreSQL 单独 agent 下载查询已经能使用现有 status/agent 索引，Turso 则扫描；不能仅凭左前缀规则把两库实际行为混为一谈。是否再加 agent 单列仍需写入代价与规模证据权衡。

e 扩展零结果、高占比和大 Agent 历史，并分别验证两种 webhook 分布。PostgreSQL 退出 0：8192 条全为 pending、仅 16 条到期时，status/created 索引没有使计划优于顺序扫描；status/next/created 则通过 BitmapOr 分别处理 NULL 和到期时间。g 在同库比较仅 status/next 两列，仍得到正确的 16 行及相同范围访问，说明第三个 created 列不是这一路径的必要条件。少量 pending 场景中 status/created 能省排序，不能据单一分布断定全局最优；正式选型尚未完成。

**Turso e/f/j 的扩展矩阵均退出 1，不是可接受结果。** 三轮都在“大 Agent 决策历史前 20 条”的索引前后集合比较失败。j 保存了实际记录：未加索引返回序号 6690–6709，而添加 agent/created 索引后返回按已知时间应得到的 16–35。此前只比较两阶段相等的标准不足以辨别哪一方正确，必须补独立的已知时间顺序基准，不能把不正确的基线当作正确答案。

h 的 64 行和 i 的 8192 行窄记录对照均通过；k 在 8192 行、较宽记录（reason 增加 512 字符、两个候选 UUID）时复现：无新增索引与 `NOT INDEXED` 强制扫描均返回 7904–7923，独立基准要求 16–35；升序/降序两种 agent/created 索引均返回正确结果。k 保留失败退出 1。当前锁定 `pyturso 0.8.0rc2`、SQLAlchemy 2.0.51；底层排序/缓冲原因尚未查明，不能推断所有 Turso 版本或全部排序都受影响，也不能仅加这一个索引就宣称修复整个问题。此正确性风险已单独列入 TODO。

完整成功/失败报告、精确各版探针和输入范围见 [d–k 审计](probes/hot-fk-index-d-k-result.json)。PostgreSQL 项目 `rssripple-v28-matrix-d` 已清理，容器/网络/卷标签均为空；各 Turso 临时库均已释放并删除。仍未添加生产索引或修改依赖、查询、迁移；下一步先将已知时间顺序作为正式回归基准，定位宽行排序问题，再确定索引和升级方案。
