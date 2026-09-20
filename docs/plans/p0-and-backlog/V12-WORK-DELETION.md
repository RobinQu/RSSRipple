# V12 候选批次：作品删除的身份清理与人工关联保护

状态：必要性已复现，删除策略与事务回滚已有局部原型；并发协议尚未实施，未验收。P1-D6 保留 TODO。主干基线 3570707；独立副本 /tmp/rssripple-v12-deletion。V11 完整门禁仍运行，未将此批次代码混入其冻结副本或 main。

## 必要性与优先级

实际 DELETE API＋Turso 四例红测（gg，4 failed，5.50 秒，退出 1）：剧集和电影删除均返回 200，但 WorkExternalId 残留；创建替代作品后调用生产 add_external_id 返回 False，不能重新登记相同身份。另两例同时持久化 manual work link 和 manual file assignment，DELETE 200 后两者均消失。不是仅根据 CASCADE 声明推断。

维持 P1：用户显式删除作品不应静默抹去独立资源的人工文件映射，也不应留下阻断后续识别的身份。真实录制 prod_works_v1.json 含 213 条 link（4 manual）、3765 条 assignment（95 manual）、249 条身份袋；这些仅证明历史图有相应数据形态，不是线上影响数量。SHA256 d11651d2162ced23e8d919af0bff2d9f316e203234cc854909ba5f444a35ec32，未改写。

## 方案论证与边界

1. 允许删除的作品必须在同一事务中清理身份袋、解除自动关联并删除作品；异常整体回滚，不能先提交身份清理。禁止仅在查找 miss 时抢占残留身份来掩盖删除缺陷。
2. 没有替代目标时不猜测人工映射的归属。建议有 manual link/assignment 或明确人工标题映射时返回 409 DELETE_BLOCKED，并提供引用计数/编辑入口；先通过现有作品合并或资源关联编辑完成转移/解除，再执行删除。不可默默 CASCADE 丢人工证据。此项会修改删除契约，需同步 API/前端提示和业务文档。
3. 保留 AgentWork 已有删除阻断。自动 links/assignments 的移除与资源状态更新需评估是否进入待确认；历史下载/通知快照不得改写。V11 决策身份和归档需要一起检查，不能仅清直接 pending FK 而遗留可派发旧 scope。
4. 并发检查必须与删除串行化：作品行锁、人工关联插入/更新、AgentWork 新订阅及身份登记竞争均需实际 PG 交错证明。不能单靠 count→DELETE；锁顺序必须兼容 V11 确认、合并及资源编辑。
5. 存量孤儿身份清理单独提供只读审阅与显式应用；不在启动或 API 请求中无条件删除历史记录。

## 严格验收

- 当前四个红测转绿：普通删除清理身份并允许替代作品重新登记；人工链接/文件映射阻断且作品与全部证据保持不变。
- 追加独立 manual link、独立 manual assignment、人工标题映射、纯自动关联、无引用、订阅阻断、404 和重复删除；错误码与 details 可供前端处理。
- 故障注入覆盖身份袋清理后、关联变更后、作品删除后，验证完整事务回滚。
- 真实 PG 两连接覆盖并发关联新增/改为 manual、并发订阅、反向删除与合并/确认；既验证保护，也验证提交后可继续。
- 使用未改写的真实录制图回放可安全删除与受保护记录；合成边界明确标注，不能伪称录制 pending 数据。
- 完整单元/API ≥95%，唯一 Compose 项目完整集成 ≥85%，runner/应用退出为 0、证据导出、冻结源码复验与项目清理全部完成后才能关闭 D6。

红测补丁：probes/work-deletion-necessity-tests.patch；机器结果：probes/work-deletion-necessity-result.json。

## 真实录制回放补证（gh）

未改写的 prod_works_v1.json 经 ORM 完整加载，每例使用独立临时 Turso，再调用实际 HTTP DELETE /series。录制作品 1d721de9-dbf9-46af-977a-fda92eb336c1 删除返回 200，其 1 条 manual work link 全部消失；作品 4bc342c7-4701-4d95-afbe-0e6b976cd9bd 删除返回 200，其 26 条 manual file assignment 全部消失。两例保护断言失败（2 failed，5.63 秒，退出 1）。这是录制图上的实测删除影响，不是当前线上数据损失结论；生产数据库未改动。

红测补丁已包含 API 合成边界和 season_model 真实录制回放两个文件。下一步实施时不能只让合成测试通过：录制回放必须同时转绿，事务/并发保护及身份重新登记仍须验证。

## 删除策略原型与回滚验证（gm）

独立副本 `/tmp/rssripple-v12-implementation-gl` 基于当前 V11 原型，未修改 V11 冻结集成副本或 main 运行代码。剧集/电影删除新增人工引用阻断，并在原事务中清理身份袋。实际 HTTP API、Turso、两例未改写录制图回放以及身份清理后故障注入共 **8 passed，5.47 秒**；JUnit 与终端日志完整，进程句柄已失效，未单独恢复 runner 退出码，不能作为完整验收。故障注入后作品、资源 FK 与身份袋均保留。

目前仍是 count→DELETE，不能保护并发新增引用或自动引用改为人工。身份登记竞争、V11 决策归档、自动关联解除后的状态、其他故障阶段和完整门禁均未完成，D6 继续保留。补丁 `probes/work-deletion-policy-prototype.patch` 是相对 V11 原型的增量，不能直接当作 main 上可合入的修复；基线/文件哈希及测试证据见 `probes/work-deletion-policy-result.json`。

新增三种人工引用各自独立阻断的剧集/电影参数化测试（6 例），检查精确引用计数及目标保留；gn 轮共预计 14 例，仍运行，尚无通过结论。此前 gm 的 8 例通过不覆盖这些新增断言。

## 删除后旧身份登记的必要性与补修（go / gp）

实际 DELETE 成功后，再用旧作品 ID 调用生产 add_external_id，剧集与电影均返回 True 并重新产生孤儿身份：go 两例失败，1.15 秒，退出 1。这是确定性提交顺序复现，尚不是 PG 并发证明。原型登记函数增加目标存在检查及 PG FOR KEY SHARE，防止登记事务提交前作品被删除；Turso 仍依靠事务写冲突。

包含新增身份边界、六例独立人工引用、原有删除/回滚及两例录制回放的 gp 轮 **16 passed，9.14 秒，退出 0**。原 gn 沙箱进程仍存活且无输出，未据观察超时重启；gp 是修复新缺陷后的扩大验证。身份服务调用兼容性、PG 锁序与交错仍未验证，不据此接受 D6。

## PostgreSQL 交错红测（gr）

身份登记、单季 upsert、metadata fallback 现有回归 59 passed（14.42 秒，退出 0）。但真实 PG 两连接交错仍复现孤儿：生产 add_external_id 插入后未提交并持有作品 KEY SHARE；另一连接调用实际 delete_movie，完成身份清理后阻塞于作品删除（pg_stat_activity 确认 Lock）；登记连接提交，删除继续成功，作品消失而身份仍残留。gr 断言失败、退出 1，不能把 gp 的 16 例或上述 59 例当作并发验收。

这证明仅登记端加存活锁不够：删除必须在身份清理前取得排他保护，同时兼容 Agent→决策→资源→作品的锁序；还需保护既有自动引用更新为 manual。后续设计应统一处理这些竞争，不能只调换一处 DELETE 掩盖其余竞态。探针与结果保存为 probes/work_deletion_identity_pg_probe.py、probes/work-deletion-identity-pg-result.json。

## 自动文件覆盖度保护（gs / gt）

真实 API/Turso 的合成双电影包用例 gs 复现：删除其中一部电影，关联的自动文件映射被 CASCADE 删除（1 failed，0.64 秒，退出 1）。该文件从覆盖度输入中消失，不能保证包仍保持未知。原型删除事务现先把自动 assignment 目标置空，保留文件路径及其他文件证据；manual 引用仍阻断。断言验证初始覆盖已知、删除后两文件仍存在、删除目标文件未分配、最终覆盖未知。

扩大专项 gt **17 passed，9.58 秒，退出 0**，包含前述身份、人工保护、录制回放及回滚测试。原型 API/业务文档已同步。PG gr 所揭示的删除竞态仍未修复，此结果不构成 D6 完整验收。

## 身份登记与删除串行化补修（gu）

删除 API 在清理身份前 SELECT 作品 FOR UPDATE NOWAIT；55P03 时整体 rollback，返回 409 INVALID_STATE，提示待当前操作完成后重试。登记端 KEY SHARE 与此互斥。真实 PostgreSQL 四个交错均通过（series/movie × 登记先行/删除先行），退出 0，隔离项目清理退出 0：登记先行时删除先返回冲突，登记提交后重试删除不留孤儿；删除先行时通过 pg_stat_activity 确认登记阻塞于 Lock，删除提交后登记返回 False，最终作品和身份均不存在。

这只修复 gr 身份竞态；现有子行改为 manual 的竞争、资源/决策反向锁序与历史决策归档仍未解决。证据见 probes/work-deletion-identity-orders-pg-result.json；保留 gr 红测说明修复必要性。

目标锁补修后的 gv 回归为 **41 passed、1 warning，16.89 秒，退出 0**（删除 API 专项、录制回放、身份服务）。仍不替代完整门禁或剩余并发矩阵。

## 既有引用并发修改（gw / gx）

PG gw 红测在实际 delete_movie 的人工检查之后暂停，另一连接成功把 auto link/assignment 改为 manual 并提交；删除继续，人工目标丢失，两类引用均复现，退出 1。作品行锁不会阻止不改 FK 的 source UPDATE。

原型在作品锁之后，对关联资源以及现有 link/assignment/title mapping/AgentWork/直接 PendingDecision 行按 ID 排序加 NOWAIT 锁，再检查人工计数；遇到竞争回滚为 409 INVALID_STATE，避免持有作品锁等待反向资源锁。gx 两类引用的编辑先行交错通过：编辑未提交时删除返回冲突；提交后重试返回 DELETE_BLOCKED，作品与人工目标均保留，退出 0。该 PG 探针目前只覆盖电影、编辑先行，尚需扩展剧集、删除先行、新增引用及决策关联完整矩阵。

## 决策生命周期红测（gz）

引用锁改动后 gy 回归 41 passed、1 warning，17.63 秒，退出 0。新增实际 create_pending_decision→HTTP DELETE→数据库检查 gz 复现：作品删除后原决策仍为 pending，未失效；1 failed，0.69 秒，退出 1。必须补身份变更协调及归档，不能只把 PendingDecision.movie_id 置空。该新测试尚未修复，不计入 gy 通过范围。

## 删除决策归档补修（ha / hb）

原型删除先复用 V11 身份变更协调及受影响 Agent 锁，再取得作品/引用锁。解除资源及自动文件绑定并移除工作链接后，复用 rekey_agent_choices；在直接 PendingDecision FK 置空前保存原始审核，过时 pending 失效而候选和 scope 保留。ha 删除专项与录制回放 **18 passed，10.78 秒，退出 0**。新增 hb 断言审核原记录保留旧 movie_id、pending 状态、候选和 scope；其结果见原始报告。PG 删除/确认/新建决策交错、历史终态与 links-only 用例仍须扩展，此补修尚未验收。

## 决策生命周期扩展与归档回滚（hc / hd）

实际 HTTP 删除扩大到 series/movie × 直接工作 FK/links-only 文件关联，hc **4 passed，2.37 秒，退出 0**。旧 pending 正确失效且保留候选/scope；审计保留删除前工作 FK（links-only 原为空）与 pending 状态；已 decided 历史的状态、reason、候选和最终选择保持不变。

在 rekey 真正完成后注入异常，hd 扩大为成功/失败共 **8 passed，4.58 秒，退出 0**：HTTP 500 后作品及 pending 恢复，新增 DecisionMigration 审计整体回滚，links-only 文件绑定恢复，终态记录保留。数据明确为合成边界；此前真实录制回放仍作为独立证据。PG 确认/创建竞争和完整门禁尚未完成。

## 既有 API 兼容性与前端提示（he / hf）

既有 series/movie/works/decisions API、身份服务、rekey、全部删除专项和录制回放合计 **131 passed、1 warning，60.75 秒，退出 0**。电影详情原先将 INVALID_STATE 隐藏为通用错误，原型现展示服务器重试提示；中英文删除确认补充 Agent 订阅和人工映射需先解除/转移，frontend 权威文档同步。前端 tsc -b 退出 0，仅证明类型检查，尚未验证浏览器交互或生产构建。

前端文件以当前 main 为基线新增到 V12 增量补丁，其余文件仍以已接收的 V11 原型为基线。以上改动尚未写入 main 运行代码，PG 决策并发矩阵与完整门禁仍未完成。

## 决策创建与删除的 PG 交错（hh）

两个真实 PG 事务调用生产 create_pending_decision 与 delete_movie。创建先行：共享身份协调未提交时删除返回 409，创建提交后重试删除，原决策 expired 且仍保留历史。删除先行：排他身份协调期间创建失败并回滚；删除提交后以旧资源身份再创建会因 changed identity 拒绝。最终没有旧身份 pending，hh 退出 0。探针使用合成电影与普通资源，未覆盖确认派发、剧集或 links-only，不能替代剩余矩阵。

首次 hg 在夹具插入时因 aware parsed_at 与 PG timestamp 类型不兼容而失败，尚未进入交错；夹具显式 parsed_at=None 后重跑 hh，未更改应用实现。探针与机器结果已保存。

## 确认派发与删除的 PG 交错（hi / hj）

hi 在实际 _ai_pick_and_dispatch 已锁 Agent/决策/资源、进入派发边界时调用另一连接 delete_movie；测试连接设 300ms lock_timeout，删除返回 409 INVALID_STATE 并回滚。随后调用真实 dispatch_download＋内置 mock downloader，确认提交为 decided、实际生成一个 DownloadTask；提交后删除成功，任务与 decided 历史保留。生产未注入该 timeout，可等待 Agent 锁释放。

hj 在模型选择窗口调用真实删除并提交；旧推荐返回后确认拒绝（expired），派发调用次数为 0。两轮退出 0。模型答案和元数据明确为合成输入，未下载真实媒体。探针覆盖电影普通资源，尚需其他关联/新增引用矩阵以及完整门禁。

## 完整单元/API 门禁启动（hl）

当前原型 app/tests/scripts 已冻结到 /tmp/rssripple-v12-gate-hk，2957 个文件哈希记录于 /tmp/rssripple-v12-gate-hk-source.json。hl 完整 tests/unit＋tests/api 启动，要求 app 覆盖率 ≥95%，进程句柄 4914，日志 /tmp/rssripple-v12-unit-hl.log；尚无最终结果。不得修改冻结副本。首次 hk 准备因原型未含 uv.lock 中止，启动命令退出 127，pytest 未运行；补齐主干锁文件、覆盖率配置及虚拟环境链接后才启动 hl。

## 前端构建与完整集成启动（hm / hn）

前端 tsc -b＋vite build 退出 0，构建结果随 app/static 进入新的冻结集成副本 /tmp/rssripple-v12-integration-hn。3028 文件哈希已记录；应用 Python 与单元/API hl 冻结副本完全一致。唯一项目 rssripple-v12-complete-20260920-hn 四服务 healthy，启动退出 0，完整 test-runner 已启动，日志 /tmp/rssripple-v12-integration-hn.log。尚需 runner 结果、应用正常退出、四份 coverage 汇总 ≥85%、证据导出、源码复验及项目清理，不能提前接受。

准备阶段旧副本缺少 uv.lock，首次复制中止且 Compose 退出 14、未创建服务；补齐主干锁文件和 Compose 后成功启动。hl 单元/API 仍运行。

## 新增引用完整 PG 矩阵（hp）

series/movie × manual work link/file assignment/title mapping/AgentWork × 新增先行/删除先行，共 **16 个真实 PG 两连接交错通过，退出 0**。新增未提交时删除返回 INVALID_STATE；新增提交后删除返回 DELETE_BLOCKED。删除持锁先行时通过 pg_stat_activity 确认新增等待 Lock，删除提交后新增因 FK 23503 失败并回滚，未产生悬空引用。此矩阵直接 ORM 插入以验证 FK 最终保护，删除走实际端点函数，不宣称覆盖前端交互。

首轮 ho 因 AgentWork 合成夹具缺少 content_type 中止，不作为验收；补齐必填字段后 hp 全量重跑，应用实现未变，完整门禁冻结文件未改动。

## 存量身份清理的必要性与约束复核

未改写的 prod_works_v1.json 中，249 条 WorkExternalId 全部可解析到所属 series/movie/collection，未知 work_type 与孤儿均为 0；来源 SHA256 仍为 d11651d2162ced23e8d919af0bff2d9f316e203234cc854909ba5f444a35ec32。结果见 probes/work-deletion-captured-identity-audit-result.json。这只证明该历史快照没有现存孤儿，不否认 DELETE 红测产生孤儿，也不能推断当前生产。

历史修复工具仍作为独立、显式离线操作提供：只读导出完整孤儿行及拥有者存在性判断，未知类型列为阻断项；审核选择准确行 ID 和快照指纹。应用阶段要求停写/备份，在事务内重新核验完整行值与目标仍不存在，拒绝审核后的身份变更或新出现拥有者，不能只按 work_id 批量删除。PG 应锁身份袋及三类拥有者表防止目标重建竞争；Turso 利用独占离线连接与事务。保留原始审核文件作为恢复证据，重复应用应有明确无变化结果。

验收需合成孤儿正例、合法拥有者保护、未知类型阻断、审核后行值/拥有者变化、故障回滚和重复应用；录制图只作无变化负例，不能伪造录制孤儿。该工具尚未实施，当前 hl/hn 冻结门禁不包含此项，不能据其通过宣称全部 D6 已完成。

## 孤儿身份审阅工具原型（hq）

新增独立 scripts/review_orphan_identities.py：默认只读导出；显式 approved_fingerprint＋selected_ids 才可清理，应用时重新检查完整 orphan/blocked 快照。合法拥有者保留、拥有者恢复/身份行变更/未批准/未知类型拒绝、重复应用与外层事务回滚等真实 Turso 合成测试 **6 passed、1 warning，2.71 秒，退出 0**。迁移文档已补命令和限制。

CLI 子进程、PG 表锁及真实录制无变化回放尚待验证。此独立脚本/测试新增于 hl/hn 冻结之后，两套完整门禁不覆盖它；应用运行代码未变，禁止改动冻结副本或宣称完整门禁包含新增工具。

## 孤儿工具命令行集成（hs）

真实子进程 CLI 对独立磁盘 Turso 验证导出、禁止覆盖原审核、缺少停写/备份声明拒绝应用、显式审核清理、重复应用返回 already_absent_ids、清理后再次导出无孤儿，hs **1 passed，3.92 秒，退出 0**。原始审核文件字节保持不变。首次 hr 因同进程初始化后 Turso 原生文件锁未释放失败；将夹具初始化放到独立进程、待退出后运行 CLI 解决，未更改应用或工具实现。

另增真实录制图的数据库负例回放：审阅 249 条有效身份应无孤儿/未知类型，并在提交及新连接检查全部行值未变。新增测试同样不在 hl/hn 冻结范围内，PG 工具锁验证仍待完成。

## 孤儿工具 PG 保护与录制负例完成（ht / hu）

录制图数据库回放 ht **1 passed，2.41 秒，退出 0**：249 条有效身份无孤儿误报，提交后新连接全字段比对保持不变。hu 真实 PG 两连接退出 0：apply_review 删除后未提交期间，重建拥有者因表锁阻塞（测试连接 300ms lock_timeout）；清理事务 rollback 后身份恢复；拥有者随后正常重建，旧审核因指纹变化拒绝应用，作品和身份仍在。

工具已有真实 CLI/Turso、录制图负例与 PG 锁专项；仍需最终质量复核，且 hl/hn 冻结范围不包含新增工具，必须与这些补充证据一起评估。应用运行代码与冻结门禁版本未变化。

## 合入前初步质量复核

完成事务、锁序、审阅工具及前端错误路径复核；尚不批准合入，具体维度与余项见 probes/work-deletion-quality-review-result.json。修正权威业务文档中过期的 V11“原型未验收”描述，并更新 V12 已有并发证据。V12 增量补丁现以当前本地 main 加该文档修正为基线重新生成，冻结测试源码未改动。

## 既有引用更新双向 PG 矩阵（hv）

series/movie × work link/file assignment × 人工修改先行/删除先行，共 **8 个 PG 两连接交错通过，退出 0**。修改先行时删除返回 INVALID_STATE，提交后重试返回 DELETE_BLOCKED，人工目标保留；删除先行时 source-only UPDATE 被当前引用锁阻止（测试连接 300ms lock_timeout），修改回滚后删除正常完成，link 删除、assignment 保留为 auto 且目标空。补齐了 gx 仅电影编辑先行的范围；应用代码未改动，冻结门禁不受影响。

## 合并与删除反向竞争（hw）

生产 _merge_movie_group / delete_movie 在两个 PG 事务中交错，两个顺序均通过，退出 0。合并先行未提交时删除返回 409，合并提交后旧目标删除返回 404；删除先行时合并因身份协调冲突整体回滚，删除完成。两种结果旧目标不存在、保留作品仍在。使用无资源的合成电影，证明身份操作互斥与终态，不把此探针当作合并资源转移的新增覆盖。

## 补充工具联合验证与续接检查点（hx）

冻结门禁后新增的离线工具四个文件单独汇总运行：函数保护＋真实 CLI＋录制图负例 **8 passed、1 warning，14.12 秒，退出 0**，文件哈希与报告见 probes/work-deletion-orphan-supplement-result.json。此结果补充 hl/hn，不声称这些文件已在两套冻结门禁中运行。

当前续接：hl 会话 4914，日志 /tmp/rssripple-v12-unit-hl.log；hn 会话 1964，项目 rssripple-v12-complete-20260920-hn，日志 /tmp/rssripple-v12-integration-hn.log。两者仍运行；2957/3028 个冻结文件均复查无改动。下一步收集终态、覆盖率、应用退出与导出/清理，再做最终代码复核。D6 继续保留 TODO，main 运行代码仍为已验收 V11，V12 仅补丁。
