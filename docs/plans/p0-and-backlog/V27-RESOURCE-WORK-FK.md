# V27 资源工作 FK 互斥约束（P2，未验收）

## 必要性与范围

V25 最新完整门禁 au/av 仍在运行，V26 已有海报专项证据但待重基。本批在独立副本重新验证原 P0-5：三个平面工作 FK `series_id/movie_id/audio_work_id` 至多一个非空。`collection_id` 不属于互斥集合；合集与单季关联的合法共存不能被破坏。franchise 的业务形态规则仍由原服务维护，不扩展本次数据库约束。

既有 `prod_works_v1.json` 于 2026-09-04 导出，SHA-256 `d11651d2162ced23e8d919af0bff2d9f316e203234cc854909ba5f444a35ec32`。559 条资源的三个字段均完整，违规 0，工作/collection 共存 1。这只是录制子集，不能代表当前生产。真实资源 `bac4892f-dd3b-41b2-b648-ff20a589779b` 的 ID/GUID/标题/torrent URL 与 series/collection 身份用于回放，频道、音频父记录及冲突组合显式合成，不请求外部网络。

原代码双库直接 SQLAlchemy Core INSERT/UPDATE 全组合测试 a：**32 failed、32 passed，62.30 秒，退出 1**。全部非法双/三工作 FK 写入提交成功，期望的 DB 拒绝没有发生；空关联、单工作关联及可选 collection 共存对照均通过。证明数据库约束缺口，未证明生产已有脏数据，因此保持 P2。原始报告及精确红测源见 `probes/resource-work-fk-a-*`。

## 方案论证

- 新库使用命名 CHECK `ck_file_resources_work_fk`；表达式只计算三个工作 FK 的非空数量 ≤1，NULL 全空合法。
- PostgreSQL 旧库通过命名 CHECK 的幂等安装与验证升级；同名约束漂移必须识别，不能仅凭名字当作正确。现存违规必须报告并阻止宣称升级成功，不自动决定删除哪个身份。
- Turso/SQLite 旧库优先使用 INSERT/UPDATE 拒绝触发器，避免为单一约束重建大表；先验证当前 Turso 对新库 CHECK 的实际支持，不能假设与 SQLite 完全一致。
- 必须在旧库列补齐后安装保护；后续父表重建不能丢失约束。保持正常清空 FK、切换工作类型以及 ON DELETE SET NULL 行为。
- 三列互斥是数据库状态约束；应用真实写入若分两次产生中间非法组合，应修正为一次原子更新，不能让数据库约束容忍非法提交。

## 严格验收

1. PostgreSQL/Turso 两后端：8 个 FK 空/非空组合 × collection 有/无 × INSERT/UPDATE，全部预期正确；失败更新原值不变，失败插入无残留。
2. 新库及真实旧表升级、重复运行、缺少历史列、同名约束/触发器漂移、已有非法行、安装中途失败回滚，均需正式集成证据；违规历史不得自动删除或猜测身份。
3. 录制合法共存资源、直接 SQL、真实 associations/metadata/rehome 写入、清空与跨类型切换、父工作删除以及迁移表重建回归。
4. 合并顺序为 V25、V26 后重基；当前候选只基于本地 main `ef3c2d8`，不改动前两批。权威数据模型、迁移和测试清单必须同步。
5. 最终五维审查；冻结后完整单元/API ≥95%、完整唯一 Compose 集成 ≥85%，零失败，审计全部跳过、哈希、真实进程退出和资源清理，才可合入本地 main。

候选 `/tmp/rssripple-v27-work-fk-j9q729vt`。新库命名 CHECK 原型 b 已通过全部 **64 项，51.68 秒，退出 0**，证明当前 Turso 实际执行该 CHECK；尚未实现旧库升级，不可关闭 TODO。六文件候选及双方哈希见 `probes/resource-work-fk-b-result.json`，正式测试与权威文档修改仅在候选内。专项无缓存 Ruff 通过，本轮唯一 PG 项目已完整清理；无 V27 测试进程继续运行。阶段五维审查见 [V27-REVIEW.md](V27-REVIEW.md)。
