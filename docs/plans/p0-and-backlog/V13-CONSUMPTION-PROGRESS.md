# V13 候选：B7 提交可见性与消费进度

状态：必要性已复现，发布基础原型验证中，尚未接入生产；D6 已完成门禁并合入 main（7c99db9）。

## 必要性与优先级

维持 P1。两个真实 PostgreSQL 写事务颠倒创建与提交顺序：较早资源先 flush 未提交，较新资源提交后实际 _handle_run_agent 选中并派发 1 条；较早资源随后提交，连续两轮增量 total_resources=0。水位线已大于其 created_at，若无外部修订/回填，不再被增量选中。对同一遗漏资源实际定向运行 matched=1、dispatched=1、无错误，排除资源不合格。使用合成元数据、内置 mock downloader，未下载媒体；原始红测退出 1，对照退出 0。

## 方案约束

不能用更大时间宽限窗或自增 ID 代替提交可见性：前者无法覆盖任意长事务，后者分配顺序仍不等于提交顺序。需比较事务内持久派发请求/消费账本与可证明不会跳过未提交记录的进度模型；复用 B9 请求机制前须审查所有资源创建路径、Agent 创建时历史回填边界、暂停/删除、频道变更与请求去重。不得把未选中的历史资源偷偷自动派发。

## 严格验收

以上实际 handler 红测转绿；相同时间戳、倒序提交、事务回滚、失败重试与进程中断恢复必须有 PG/Turso 合适的真实事务证明。确认 null 水位线、rules-preview 空选择/部分选择、暂停期间新增与恢复、定向运行及 scan_since 的既有语义；真实录制数据用作候选/过滤对照，事务时序明确为合成。完整单元/API ≥95%、唯一隔离完整集成 ≥85%，正常退出、导出、源码复验和清理全部完成后才关闭 B7。

证据：probes/watermark-late-commit-pg-result.json；可重放探针：probes/watermark_late_commit_pg_probe.py。

## 当前入口复核与方案比较

主运行代码中 FileResource 的构造入口集中在 fetch_service；它先提交新资源、并行 metadata，再在最终提交后以无 resource_ids 的普通 run_agent 消息唤醒 active Agent。B9 的 request_channel_resources 目前只选择 active Agent，且用于资源修订，不是所有新资源的事务性消费记录；直接把抓取末尾 enqueue 换成该函数仍不能证明暂停/恢复与创建边界正确。Agent 保存回填在 agents.py 中将 last_consumed_at 设为所见最大 created_at 或当前时间，必须保留用户选空/选部分的历史排除语义。

候选一：新资源发布事务内记录每个相关 Agent 的持久请求，队列仅唤醒。需确定 paused Agent 是否保留请求、Agent 创建/频道切换与抓取交错时的边界，并验证资源 metadata 尚未完成时请求不会被提前永久确认。现有 B9 按整个 run 成败确认快照，不能直接把“未识别”当作成功消费。

候选二：固定订阅下界＋逐资源消费账本，避免不断移动时间上界跳过晚提交；代价是账本增长、清理/迁移和规则回填的版本管理。必须明确迁移前已被跳过资源的人工审阅补偿，不能自动回放全部历史。

暂不选定实现：先在实际 fetch/metadata 生命周期补两个正反例，证明事务请求的投递时点，再判断是否能复用 B9，而非先为通过当前原始 SQL 插入探针设计旁路。当前红测证明读取水位线存在遗漏；最终生产修复还必须覆盖实际写入入口。

## 元数据完成时点的实际流程红测（ia）

普通事务 Turso 中，两条资源均已提交，较早一条尚未链接作品。实际 Agent 作业先记录 unrecognized=1 并派发另一条，推进水位线。随后调用生产 _process_resource_metadata，由明确的合成外部识别适配填入已存在作品，实际函数提交；后续两次增量仍 total_resources=0，而同一资源定向运行 dispatched=1。断言失败退出 1，证明“仅插入时建立请求并在未识别时确认”不足以修复。

首次 hz 的默认 CONCURRENT 内存夹具没有启用 MVCC，初始化失败，不计缺陷证据；ia 使用显式 DEFERRED。模型输出与 torrent 网络边界被隔离，数据库、metadata 提交、Agent 选择/过滤和 mock 派发均实际执行。最终方案必须让元数据完成/重试成功形成可恢复消费事件，并避免未识别结果提前永久清除唯一补偿。

## 排除无条件 B9 复用方案（ib）

实际 _apply_backfill(agent, []) 后，普通增量正确选中 0 条；模拟候选修复在后台完成时无条件调用现有 request_channel_resources，同一历史资源随后被实际 handler 派发 1 条，违反用户选空的排除意图，断言失败退出 1。这里验证的是候选方案不成立，不是将 B9 的合法定向绕过水位线行为重新判为缺陷。

因此自动补偿必须与显式修订请求区分资格，并持久化回填排除边界。仅加完成时请求、仅比较当前 last_consumed_at 或固定时间宽限窗均不能同时满足已复现的三种场景。下一步评估按 Channel 串行发布序号（分配与资源提交同事务，保证可见前缀）及独立完成修订事件；普通自增序号不具备此保证。任何序号方案必须把后台自动补偿的基线与用户回填选择绑定，且禁止在网络 metadata 调用期间持有 Channel 写锁。

## 发布事务基础原型（ic）

独立副本 /tmp/rssripple-v13-publication-ic（基于 main 5193fb2）新增 ChannelPublicationCounter 与 ResourcePublication：UUID 主键，频道内 sequence 唯一；counter UPDATE 分配编号并持锁到同事务提交，created 事件以自身编号作为 origin_sequence，metadata 事件保留该初始编号。无网络调用、无内部提交。不是 PostgreSQL sequence/autoincrement 的分配即前进。

真实 Turso 基础测试 **4 passed、1 warning，1.96 秒，退出 0**，覆盖 metadata 保留准入编号、事务回滚恢复编号及拒绝无创建事件的修订。尚未证明 PG 两写者可见前缀，未注册到生产模型/迁移、未接入 fetch 或 Agent，B7 两个红测仍未修复。下一步先证明发布序号的提交顺序，再实现独立回填基线和消费游标。基础补丁和文件哈希已保存，main 运行代码未改动。

## PostgreSQL 发布前缀并发验证（id）

专用 PostgreSQL 16 的四场景矩阵全部通过，退出 0：计数器初次创建/预先存在 × 第一写者提交/回滚。通过 pg_stat_activity 确认第二个真实会话处于 Lock 等待，而不是仅以 sleep 推测阻塞；第三会话看不到未提交事件，另一频道仍可发布并提交。第一写者提交后事件序列为 [1, 2]，回滚后仅第二条事件且序号为 [1]，origin_sequence 正确。项目 rssripple-v13-publication-id 已清理，退出 0。

数据全部合成，验证数据库事务时序；不充当真实内容识别或完整 Agent 集成证据。结果见 probes/resource-publication-pg-result.json，可重放探针见 probes/resource_publication_pg_probe.py，需在应用基础原型补丁的副本中执行。基础原型仍未注册生产模型、迁移或消费流程，因此 B7 保留。

下步：定义 Agent 的历史排除基线和已处理事件游标，明确首次启用、回填空/部分选择、频道变更、暂停恢复的边界；在实际 fetch 和 metadata 提交事务中发布事件，将原倒序提交和识别完成红测转绿，同时保持历史排除对照不派发。接入前还需审查长事务持锁、事件清理以及旧库迁移补偿策略。

## 独立消费进度原型（if）

在同一隔离副本新增 AgentPublicationProgress 与消费服务：baseline 固定为用户回填成功时可见的发布前缀，cursor 只推进成功处理的事件，generation 为每次重置生成 UUID。事件查询同时限制 sequence > cursor、sequence <= 本轮前缀以及 origin_sequence > baseline；因此新资源后续 metadata 事件可再次消费，旧历史的 metadata 事件仍排除。确认使用 agent/channel/generation 条件更新且只向前推进，迟到确认不能覆盖回填重置。读取和重置不持频道写锁，不跨网络调用。

真实 Turso 专项 **7 passed、1 warning，2.96 秒，退出 0**（含原发布基础 4 项）；新增验证历史排除与元数据迟到、未确认重试、回填重置后的旧确认失效、快照后完成事件不被旧确认吃掉。ie 初次执行未产生测试结果，确认进程存活后主动停止（143），修正下载器夹具无效字段后执行 if；没有将前轮无输出计为通过。

这里仍为服务层单事务时序验证，不代替独立会话并发或实际 handler 派发验证。尚未接入模型注册、迁移、API、fetch 或 handler，未关闭 B7。下一轮接入前需要明确：已有 last_consumed_at 的迁移准入、频道切换初始化、scan_since 与显式定向的进度关系，以及回填/旧运行并发期间实际派发的规则一致性；generation 仅保护确认，不自动取消已启动的派发。补丁与六个文件哈希已刷新，可从 main 5193fb2 恢复原型。

## 实际提交与消费链路部分接入（ig / ih）

隔离副本已注册三个模型；fetch 新资源提交前写 created 事件，_process_resource_metadata 主结果提交前写 metadata 事件；普通增量 handler 使用发布快照并在无处理错误时确认，_apply_backfill 成功结束后重置准入基线。同步了副本中的模型/业务/API 设计说明。尚未迁入 main。

ig 实际 metadata 提交＋handler 回归退出 0：首次 total=2、unrecognized=1、dispatched=1；识别完成后第二轮 total=1、dispatched=1；第三轮 total=0；显式定向对照 duplicates_skipped=1，没有重复下载。ih 实际 _apply_backfill([], db)＋handler 回归退出 0：历史资源创建事件后选空，自动 metadata 事件到达前后均 total=0、dispatched=0。数据库、业务提交、过滤与 mock 下载器真实执行，外部识别用合成适配；资源创建夹具显式发布，尚不代表实际 feed 抓取入口已经验收。

仍不可部署：未实现旧库初始化和新 Agent/null 保存/频道切换初始化；scan_since 尚沿用旧时间字段，普通增量暂未维护 last_consumed_at 的展示兼容。旧 Agent 无进度时会明确失败，不能用这两个绿测宣称 B7 已完成。下一步完成生命周期与迁移语义，补真实 fetch 入口、PG 倒序提交 handler、部分历史选择和进程中断恢复验证，再完整门禁。证据和可重放探针已保存在 probes/publication-*，补丁含 13 个文件。

## 首次消费初始化与已有进度保留（ii / ij）

隔离原型新增 initialize_first_run：已有同频道进度直接保留；缺进度且 last_consumed_at 为 NULL 时，以当前已提交发布前缀初始化，首次 handler 同事务设置兼容时间字段；缺进度但存在旧时间水位线时明确要求迁移，频道不符明确要求重置策略。INSERT ON CONFLICT DO NOTHING 防止初始化覆盖并发创建的进度。

Turso 基础与状态测试 **9 passed、1 warning，3.77 秒，退出 0**，新增证明暂停状态期间发布的资源在既有基线上仍可消费、初始化不重置已存在进度、旧水位线缺进度拒绝静默丢弃。暂停/恢复仅状态与消费服务验证，完整调度恢复另验。ij 实际 handler 从 NULL 首次运行开始（total=0），随后两条新资源中一条先派发、另一条 metadata 完成后派发，第三轮无重复；退出 0。发布夹具时间故意早于首次运行时间，证明新链路不再依赖 created_at 大于时间水位线。

迁移仍待实现：必须保留旧水位线以上尚未消费的资源，不能把所有旧 Agent 重置为当前前缀；水位线以下已遗漏项需可审阅补偿，不能自动回放用户排除历史。还需完成新建/频道切换/null 保存、scan_since、实际 feed 发布、并发及完整门禁。B7 不关闭，原型不合入 main。

## 旧水位线转换核心与实际消费衔接（ik / il）

新增隔离迁移核心 bootstrap_publications：要求停写，PG 获取参与表的 SHARE ROW EXCLUSIVE 锁，Turso 调用者先 BEGIN IMMEDIATE；按频道 created_at/id 排序建立旧资源创建事件，以 bisect_right 精确保留 created_at > last_consumed_at 的消费范围。NULL 水位线不建进度，保留首次运行初始化行为。完成标记和所有变更同事务；已完成重跑只返回 already_applied；无标记但已有发布数据时拒绝覆盖。现有时间字段保留。

基础＋迁移共 **13 passed、1 warning，5.61 秒，退出 0**，覆盖相同时间戳边界、重复执行不重置、失败回滚后可重试、NULL 和未标记混合状态。il 在实际 Turso BEGIN IMMEDIATE 中迁移两条资源和一个旧 Agent，再执行实际 handler：首次派发一条，另一条未识别；metadata 实际提交后下轮派发另一条；下一轮为空，定向对照去重，退出 0。数据明确合成，未访问生产库。

这只是迁移核心，不代表部署迁移完成：尚缺 CLI/预览与潜在遗漏审阅导出、启动接入、PG 迁移写屏障验证，以及大频道内存/批量性能评估。旧水位线以下资源不自动重放，避免推翻历史选择；需要给用户可审阅补偿入口。其余频道切换、scan_since、真实 feed 与完整门禁继续保留。补丁已包含迁移核心、测试及副本 db-migration 说明。

## PostgreSQL 迁移写屏障与发布接续（im）

专用 PostgreSQL 16 验证退出 0：先执行完整迁移并回滚，独立会话确认事件与完成标记均不存在；再次迁移三条资源和一个 Agent，未提交期间并发发布者在真实 pg_stat_activity 中显示 Lock 等待，观察会话仍看不到迁移事件/标记。迁移提交后发布者完成，新资源序号为 4，迁移 Agent 的快照同时保留旧水位线以上资源和这条新资源；重复迁移返回 already_applied 且快照完全相同。

此验证使用独立会话、合成数据和真实约束，未调用 Agent 派发（迁移＋handler 的 Turso 证据见 il）。项目 rssripple-v13-migration-im 清理退出 0。结果见 probes/publication-migration-im-result.json，持久探针 publication_migration_pg_probe.py。已验证迁移屏障不等于允许不停写滚动升级：旧版发布者仍可能在迁移后写无事件资源，因此部署仍须停写并同时升级所有写入者。

后续首要实现为可重放迁移入口与审阅导出、启动接入，随后补齐频道切换及 scan_since；保留 B7 为未完成，不把基础迁移并发绿测当作完整生命周期验收。

## 审阅导出与迁移 CLI（in / io）

隔离原型新增 scripts/review_publication_migration.py：导出旧资源/Agent 水位线与预计待消费、历史排除或歧义 id；应用要求 approved_fingerprint，锁内复核同一快照，成功标记保存指纹。同一审阅重复应用直接返回 already_applied；不产生资源重放请求。PG 导出使用 REPEATABLE READ READ ONLY，应用复用迁移表锁；Turso 应用先 BEGIN IMMEDIATE。已存在导出文件不覆盖。

基础、迁移与审阅测试 **16 passed、1 warning，6.77 秒，退出 0**，新增覆盖待消费与排除列表、导出只读、审阅篡改/水位线变化拒绝及重复应用进度保持。io 真实 CLI 多进程使用临时 Turso 文件，种子进程退出释放文件锁后分别导出、应用、重跑，均符合预期；缺必需维护参数退出 2，覆盖导出拒绝退出 1。驱动退出 0，临时库已自动清理；结果 publication-review-io-result.json，驱动 publication_review_cli_probe.py。

审阅导出列的是无法自动判别的历史范围，不声称已确定哪些是实际遗漏。后续可用现有 rules-preview 显式选择补偿，不能凭 created_at 自动恢复。尚需旧 schema 准备与启动门禁、PG CLI 端到端、性能评估、频道切换/scan_since、真实 feed 和完整验收。B7 未完成，main 运行代码未变。

## 旧 schema 准备与启动门禁接入（ip / iq）

CLI 增加 --prepare-schema（必须携带停写/备份参数），仅创建三个新表，可重复执行；解决旧库尚无新表时无法导出的问题。web/worker 在运行配置加载和调度启动前检查发布状态，包含跳过 DDL 的分布式启动路径。新空库标记 fresh；存在旧资源或旧非空水位线但无迁移标记时拒绝启动。有标记仍检测缺初始事件资源和缺进度/频道不符的旧 Agent，避免标记掩盖混合版本写入。

专项累计 **19 passed、1 warning，8.07 秒，退出 0**。iq 在临时 Turso 文件中明确删除三个新表模拟旧 schema，用独立 CLI 进程连续准备两次，再导出三条资源/一个 Agent、审阅应用和幂等重跑，全部通过；缺维护参数与导出覆盖均按预期拒绝，驱动退出 0，临时文件清理完成。只测试启动检查服务，尚未将真正 web/worker 启停算为通过。

剩余：实际启动、PG 旧库 CLI、频道变更/scan_since、真实 feed、性能与故障恢复以及完整门禁。代码仍只在隔离副本；补丁和文件哈希已刷新，B7 不关闭。

## PostgreSQL 旧库 CLI 与实际 web/worker 启停（ir）

从明确删除三个新增表的旧 schema 开始，真实 CLI 进程重复 prepare-schema 两次，导出三条资源/一个旧 Agent，验证禁止覆盖审阅与缺参数拒绝，再审批指纹、应用和重跑，全部符合预期。实际 uvicorn web 和 app.worker 进程在新表已准备但数据未迁移时均因 Publication migration required 退出；迁移后分别观察 Application startup complete / Worker started，再 SIGINT 正常退出 0。驱动退出 0。

专用项目 rssripple-v13-cli-ir 已 down 清理，退出 0。来源为合成旧库；DB_MIGRATE_ON_STARTUP=false 验证分布式跳过 DDL 的门禁仍有效；scheduler=false、memory queue，心跳和海报路径均隔离。这证明升级/启动衔接，不证明 Redis 作业恢复或实际抓取。证据 publication-cli-ir-result.json，持久探针 publication_review_pg_startup_probe.py。

下轮继续完善频道切换、scan_since 与时间水位线展示兼容；之后补真实 feed 和倒序提交 handler、队列/进程恢复以及完整门禁。B7 保留，未合入 main。

## 频道切换的必要兼容边界（is / it）

复核发现普通 PUT 切换频道（无回填数组）原逻辑保留时间水位线，因此不能将新频道当前所有资源直接排除。隔离实现新增 historical_created_after：保留原时间下界的历史准入，cursor 从 0 扫描，新 origin 高于切换 baseline 的资源始终可用，不受 created_at 早晚限制。显式回填重置时清空历史下界，NULL 水位线切换留待首次运行初始化；同频道普通保存不改进度。

基础专项 **20 passed、1 warning，8.80 秒，退出 0**；新增覆盖旧频道快照确认失效、保留时间范围内新频道候选、早时间晚发布的新资源、显式重置后历史完成事件排除。实际 API 专项 **1 passed，0.61 秒，退出 0**，覆盖 PUT 频道变化不改时间字段、普通改名不重置 generation/游标，以及空回填排除历史。历史消费者不能仅靠 timestamp max 映射发布序号，因为发布序号和资源时间不必单调。

尚未声称在途派发竞争已解决：generation 保护最终确认，不阻止已经开始的远程下载操作。下一步补 scan_since、时间展示兼容与实际运行期间频道/回填变更交错，再进入抓取/恢复和完整门禁。B7 未关闭。

## scan_since 的完成补偿和时间展示（iu / iv / iw）

隔离 handler 的指定时间扫描先保存发布前缀/generation，再按原时间范围读取资源；成功且存在扫描资源时以条件 UPDATE 推进至该前缀，并将 historical_created_after 扩为本次扫描范围，保留元数据后续完成的准入。全历史使用 datetime.min，后续较窄扫描不撤回较宽准入；显式回填重置清除历史范围，旧 generation 确认无法恢复它。快照后产生的事件不被确认。普通增量恢复更新兼容 last_consumed_at，但仅成功确认后单调向前更新，增量选取完全不依赖该时间。

累计专项 **22 passed、1 warning，10.04 秒，退出 0**。iv/iw 实际流程先 _apply_backfill([]) 排除旧历史，然后分别指定起始时间/scan_since=null 主动扫描；首次各 total=2、unrecognized=1、dispatched=1，实际 metadata 提交后增量 total=1、dispatched=1，再下一轮 total=0，定向对照去重；两次驱动均成功完成并产生通过断言的结果。范围外历史完成事件、快照后事件和重置后的迟到确认另有服务级用例。来源均合成，不代替真实 feed 验收。

还需失败扫描和实际处理期间换频道/回填竞争、进程恢复、实际生产入口与完整门禁。基础绿测不覆盖远程调用在途副作用，B7 保留。

## 失败历史扫描的必要性复现与补修（ix / iy / iz / ja）

ix 最初在 mock 下载器 add_torrent 注入 RPC 错误，实际实现保存 error DownloadTask，RunResult 仍计 dispatched=1，故“应有 run errors”断言不成立；这属于下载任务重试路径，不能作为消费丢失证据。随后 iy 在真实 download_tasks INSERT 边界注入 RuntimeError：实际处理回滚，扫描 errors 非空、dispatched=0；下一轮普通增量 total=0，缺乏恢复入口，探针退出 1。

补修 prepare_window_retry：在 handler 第一阶段提交扫描意图，按最早选中创建事件回退 cursor，持久化所选历史准入并换 generation。成功后再确认起始前缀；失败不确认，普通增量重试依赖持久状态。旧运行确认失效；后续回填可清空范围并再次换 generation。iz 同一探针转绿：失败后普通增量 total=2、dispatched=1、errors=[]，退出 0，数据库/handler/事务实际执行，故障明确为合成 INSERT 异常。

累计专项 **24 passed、1 warning，10.70 秒，退出 0**，新增验证未确认扫描可重试、后续回填撤销范围、迟到旧准备不能重开新回填。红绿结果 publication-failed-window-iy/iz-result.json；红测保存补修前源码哈希。尚未将真正杀进程/重启计为已验证，真实在途规则变化和崩溃恢复继续跟进。

## 实际进程中断后的扫描恢复（jb）

新建临时文件型 Turso 数据库，独立 seed 进程创建合成资源并空回填排除历史；scanner 真实 handler 完成阶段一提交后，在 process_resources 入口输出屏障并暂停。父进程确认进程仍存活后 SIGKILL，wait 返回 -9。全新 recover 进程先读取数据库消费快照确认候选仍在，再实际普通 handler 派发 1 个任务；下一轮 total=0，数据库任务恰好一条。驱动退出 0，临时库随目录清理。

证据 publication-crash-jb-result.json，重放 publication_scan_crash_probe.py。这里没有借用前进程内存状态；只有故障时点屏障被替换，恢复的过滤、事务与 mock 下载器真实执行。范围只到处理前崩溃，不声称远程 RPC 已成功但提交前崩溃或 Redis 重投递已验收。

后续真实来源入口已定位：tests/metadata_corpus 的 confirmed case f79ef2eb-02d5-42d3-80dc-70dd3c1d733b，现有 test_p0_real_manifest.py 已验证 torrent/listing 哈希和 S1E10 独立审核。下一步用其录制输入及可追踪边界替换进入实际 fetch_channel_resources，验证 created/metadata 事件与资源同事务，不能只手工插入资源后调用 publish_resource。

## 录制来源的实际 feed 生产入口（jc / jd / je / jf）

confirmed case f79ef2eb-02d5-42d3-80dc-70dd3c1d733b：真实标题与 torrent SHA 7f2b6d96aeeb800355ca64985173449a91b4b76df85edeba6a105368e64dd813，独立审核 S1E10；验证文件列表整体哈希及实际 torrent 解析等于录制清单。该案例明确 raw_rss_available=false，所以只使用合成 RSS 外壳承载录制标题，不能声称原始 RSS 重放。feedparser 和生产 fetch_channel_resources 真实执行，网络 metadata 用审核适配，torrent 缓存/检查与队列唤醒边界替换，媒体未下载。

jc 正常路径在独立会话证明 metadata 前资源和 created 事件已提交，最终 created+metadata，实际 handler 派发 1 次，下一轮空。jd 注入 created 事件 INSERT 失败，资源、事件、计数器均回滚，handler 无派发。je 注入 metadata 事件 INSERT 失败，作品关联回滚且仅留下 created；首次 handler 未识别，实际 metadata 重试后增量派发 1 次、下一轮空。三个探针退出均为 0。

已纳入正式 `tests/integration/organize/test_publication_feed.py`，驱动 `publication_feed_driver.py`，三参数测试 **3 passed，4.15 秒，退出 0**，JUnit /tmp/rssripple-v13-feed-jf.xml 附 case/hash/替换边界属性。此为子进程 Turso 集成，不代表完整容器门禁；PG handler 倒序提交、Redis 唤醒恢复与在途规则变化仍待完成。补丁和清单已同步，B7 保留。

## 丢失唤醒的数据库补偿调度（jg / jh）

必要性：仅持久化事件而无再次唤醒，抓取最后 enqueue 失败或进程退出后仍可能等不到消费。新增 publication_dispatch：每 5 秒从数据库读取 active Agent 的 counter/cursor 差，事务结束后入普通 run_agent；NULL 水位线且无进度的新 Agent 可被初始化。队列忙/异常不修改进度；暂停 Agent 不入队但保留事件，恢复可发现。单独接入现有 scheduler，B9 修订请求路径维持独立。

服务级 **2 passed、1 warning，0.88 秒，退出 0**，覆盖 paused 不唤醒、恢复后队列异常/下轮重试、成功确认后停止唤醒、新 Agent 初始化。正式录制来源 feed 集成扩为 **4 passed，5.79 秒，退出 0**：第四场景让抓取末尾 enqueue 实际抛错，再调用周期扫描进入真实 MemoryQueue/handler，成功派发并确认，再次 tick 不创建新 job。保留真实标题/torrent 来源和合成 RSS/metadata 边界说明。

本次证明持久数据库→活队列的补偿链，未证明 Redis 重启/多个 worker 竞争与调度器实际定时触发；这些仍保留。事件保留策略、在途规则修改、PG handler 倒序提交和完整门禁也尚未完成，B7 不关闭。

## 原始 PostgreSQL 倒序提交红测转绿与 Redis 双进程恢复（ji / jj / jk）

ji 沿用最初两个真实事务的时序：较早 created_at 资源先 flush 未提交，较晚资源先发布并提交，实际 handler 派发 1 条；较早资源随后在自身事务发布并提交，第二轮派发 1 条；第三轮为空，定向对照仅去重。退出 0。创建时序与发布提交序号明确分离，不以固定时间窗回避原问题。

同一专用项目中再提交一条 created_at 更早的新资源并通过真实 Redis 入队。jj 初次探针把队列状态误写 pending，实际值 queued，因此断言失败；候选已持久化，修正断言后继续同一资源，没有重复种子。jk 擦除专用 Redis 数据（模拟队列状态丢失），启动两个全新消费者进程，各自扫描 PostgreSQL 并使用真实 RedisQueue；两者观察同一个新的 job_id（不同于丢失作业），实际 handler dispatched=1，数据库仅一个 downloading 任务，再次 tick 不重建作业。驱动退出 0。

项目 rssripple-v13-distributed-ji 的 PG/Redis 已 down -v，退出 0。来源为合成候选＋mock 下载器，不触碰生产 Redis。持久 Redis 探针额外要求 PROBE_ALLOW_ERASE_ISOLATED_REDIS=1；必须在唯一隔离测试项目使用，且先由倒序提交探针种好唯一 Agent。结果 publication-late-commit-ji-result.json、publication-redis-jk-result.json。

剩余关键项为在途频道/回填变更、外部副作用与崩溃边界、事件保留/实现审查以及完整门禁。B7 未关闭，代码尚未进入 main。

## 回填提交与旧运行派发竞争（jl / jm / jn / jo / jp）

jl 实际扫描选取后、process_resources 处理前，在另一会话调用 _apply_backfill([]) 并提交。旧实现仍 dispatched=1、任务一条，探针退出 1，证明仅 generation 保护确认不足。隔离补修在候选组开始与最终 RPC 前用新 AsyncSession 检查 Agent 当前频道和 generation；新的提交可见，不复用 Turso 长读快照。jm 同一交错退出 0、无任务；jn 在实际两候选组挑选期间提交回填，最终 RPC 前同样拦截，dispatched=0、tasks=0，退出 0。

范围校验只阻止尚未开始的派发，不承诺撤销已发出的远程请求；不为此在网络调用期间持有写锁。显式定向无发布快照，以及普通 null 规则保存期间变化仍需单独审查，不能将两项绿测推广为全部并发安全。

Agent 服务与发布链回归 jo：125 passed / 1 failed / 1 warning，52.68 秒，退出 1。唯一失败为旧 `test_failed_candidate_is_retried_by_next_incremental_job` 直接构造有时间水位线但无发布进度的旧 Agent，被迁移门禁正确拒绝；更新夹具先执行真实 bootstrap，并允许故障适配接收消费快照参数，原失败/部分提交/下轮补偿断言不变。jp 单独复验 1 passed / 1 warning，0.88 秒，退出 0。未虚称 jo 全绿；完整门禁仍待运行。

## 发布记录保留与兼容性预检（jq / jr）

必要性：metadata 每次完成均追加会使事件表随刷新次数无界增长，而消费者只读取资源当前状态，不需要历史 payload。隔离实现以 (resource_id,kind) 唯一约束保证每资源最多 created/metadata 两条；发布事务在持频道计数器锁期间替换旧 metadata，origin 仍指向长期保留创建序号。回滚恢复被替换记录和计数器；新序号大于旧快照上界，旧确认不能吞掉新唤醒。创建记录和计数器不压缩重编号。

专项、迁移、API 与正式 feed 集成合跑 **32 passed、1 warning，18.29 秒，退出 0**，新增十次刷新仍两条事件、失败替换回滚、旧快照确认后新事件仍可消费。PG 发布锁已有基础证据，但新唯一约束下的并发替换仍需最终 PG 集成覆盖。

已启动完整 unit/API 兼容性预检 jr（非最终覆盖率门禁），冻结副本 /tmp/rssripple-v13-preflight-jr 共 2960 文件，哈希 /tmp/rssripple-v13-preflight-jr-source.json。命令 --no-cov --maxfail=10，session 32852，log /tmp/rssripple-v13-preflight-jr.log，JUnit 同名 xml；当前未完成，不能宣称通过。后续继续观察同一进程，不因超时或无输出重启。原型实现副本可继续独立修复，冻结副本不改动。

## 普通规则保存的旧运行失效（js / jt / ju / jv / jw）

js 在实际 handler 选取后，通过 update_agent 的 null-backfill 保存将 scope_channel_wide 从 True 改 False（无订阅），旧运行仍派发，退出 1。补修 invalidate_running_scope：实际规则/派发参数变化、订阅 CRUD 仅更换 generation，不移动 baseline/cursor/历史准入/last_consumed_at；旧运行已有 fresh-scope 检查因此被拦截。jt 同一交错任务数 0，退出 0。

ju 相关专项 17 passed / 1 failed：请求校验器会补入未变化字段，仅按请求 key 判断导致普通改名误失效。已改成比较 Agent 实际旧值；jv 改名通过，但新测试夹具原本已是 scope_channel_wide=False，不构成规则变化，故其代次变更断言失败。修正夹具明确从 True 开始，jw 两个实际 API 测试均通过（3.27 秒、退出 0），覆盖改名、频道切换、规则保存及订阅增删改，确认只有代次变化而消费边界保持。未把前两轮失败隐藏为全绿。

冻结的 jr 全量兼容性预检仍运行，同一 session 32852；日志已出现失败，尚未输出终态详情。未修改冻结副本，也未重启。下一步取得终态失败清单，并继续核对自动暂停/恢复和显式定向的边界，再完成质量审查与完整门禁。

## 暂停与自动消费（jx / jy / jz）

jx 实际 API 暂停后自动 handler 仍派发一条任务，待消费列表被清空。隔离补修为 fetch、发布补偿和持久请求唤醒添加 automatic 标记；handler 在建立运行和读取进度前检查 Agent active 状态。jy 同一探针确认暂停时 skipped、任务数 0、待消费列表不变，恢复后派发 1。jz 发布唤醒、API 与正式 feed 集成 8 passed、1 warning，11.82 秒。探针与红绿结果已保存 probes；此为专项证据，不替代完整门禁。

另发现 reconcile_stale_raw_episodes 在内部提交修订时没有发布事件，而 metadata_backfill 对修订资源直接入队定向运行，会绕过历史排除边界。下一步必须补原子发布并使用普通增量唤醒，独立验证历史排除和应消费资源；B7 继续保留。jr 同一 session 32852 已再次确认仍运行，冻结副本不修改。

## 历史修订发布缺口（ka / kb / kc / kd）

必要性红测 kb：真实 Turso 上调用实际历史集数修订，历史排除和新资源均从 E18 修订为 E19，但应消费新资源无事件，pending 为空（1 failed，1.64 秒）。ka 沙箱进程无结果后明确终止、退出 143，不算测试结果。数据为明确合成的同组历史锚点，非捕获语料。

隔离修复为修订提交前按 channel/id 排序发布 metadata；handler 提交后调用普通发布补偿，不再直接定向派发修订 IDs。kc 进度专项 11 passed，14.59 秒；kd 补入发布后故障、rollback、旧集数/空待消费保持及正常重试，11 passed，14.88 秒。两轮均 1 warning、退出 0；最后仅 Ruff 导入排序修正。日志和 JUnit 已保存在 probes/publication-reconcile-*，补丁与清单已更新。

后续必须更新旧修订测试夹具为真实创建发布或迁移准备，补 handler 集成和 PG 原子性验证；不能将本轮服务专项当作完整集成验收。jr session 32852 再次确认仍运行（约 86%），已有失败但尚无终态报告；保持冻结副本不变。B7 未关闭、未合入 main。

## 修订 handler 与旧夹具兼容（ke / kf），迁移审核成本（kg）

ke 将成功路径改为调用实际 metadata_backfill handler、真实提交会话和发布补偿，仅替换无关网络回填与队列传输。断言普通 automatic 唤醒不携带 resource_ids，修订后的两条资源仅未被排除者重新待消费；保留前置发布失败回滚。旧 unit 和 integration 修订夹具先执行真实 bootstrap，不为测试在运行代码中自动补造创建事件。ke 4 passed / 63 deselected / 1 warning，5.46 秒；kf 原有 metadata 修订集成 4 passed / 42 deselected，5.82 秒，均退出 0。kf 是直接 Python 集成、不是完整 Docker 门禁。

性能审查：审核导出原先每 Agent 扫描全部资源；改为先按频道分组，再计算该频道待消费/排除清单，审核格式及顺序不变。kg 现有迁移审核测试 3 passed / 1 warning，3.69 秒、退出 0。审核仍完整输出逐 Agent 资源 ID，数据量与审核内容相关，不声称已完成大规模容量测试。日志/JUnit 和修改文件均纳入 probes 清单。

全量 jr 最后观察约 92%，同一进程仍运行，尚无终态；不得把专项结果或旧冻结代码预检当作最新原型最终门禁。后续先获取失败清单、验证冻结文件哈希，再修复和跑最终覆盖率/隔离集成；B7 保留 TODO。

## 完整兼容性预检终态（jr）与生产入口扩大回归（kh / ki）

jr 同一进程已终态：3801 passed、2 failed、15 skipped、6 warnings，1719.07 秒，退出 1。再次核验冻结副本 2960 个文件哈希全部一致。两项失败均为 test_fetch_service 的旧回填夹具缺少创建发布记录，metadata 原子事务回滚而未保留尝试次数；不是修改水位线断言掩盖漏消费。本轮使用 --no-cov，仅兼容性预检，不属于最终覆盖率门禁。完整日志、JUnit、冻结源清单已复制 probes/publication-preflight-jr-*。不要再次轮询已结束 session 32852。

最新原型 fetch 单元加 metadata 直接集成扩大 kh：96 passed、6 failed、1 warning，112.72 秒、退出 1。包括 jr 的两项、三个 metadata 后处理（标题回退/两种海报）因同样缺创建事件回滚，以及队列 payload 预期缺 automatic。已对明确构造旧资源的夹具执行真实 bootstrap，并为若干原本仅断言不抛异常的正常 metadata 路径补齐准备，避免用早期发布失败覆盖原测试目标；未给产品添加隐式补造事件。保持原业务断言并更新自动唤醒字段。

ki 重跑两个完整测试文件，session 24795、/tmp/rssripple-v13-fetch-ki.log 和同名 xml，当前运行。下一次继续观察同一进程，取得终态后刷新证据。最终完整 unit/API 覆盖率及隔离集成、PG 新约束并发、设计文档整合和合并审查仍待完成，B7 不关闭。

## 更正失败归因：真实 Turso 写冲突（ki / kj / kk）

ki 终态 100 passed、2 failed、1 warning，79.07 秒、退出 1。补齐创建发布后原两项仍失败，日志明确为 UPDATE channel_publication_counters 的 Write-write conflict，第一例仅 17/30 metadata 尝试持久化。因此此前“只是夹具”的解释不完整；旧夹具缺失会先遮蔽这一真实并发缺陷。证据已保存，不将失败标为通过。

方案：复用数据库层 retry_on_lock，在 _process_resource_metadata 外层重试尚未提交的完整操作，每次新会话/快照；内部先 rollback 再传播可重试锁异常。primary metadata+publication 已提交后不重跑，海报错误维持原处理，避免重复计次。没有延长持锁区至网络调用，也没有在旧会话上仅重试计数器 SQL。两项真实并发回填 kj 均通过（3.93 秒、1 warning、退出 0），原准确处理数与尝试次数断言保持。

kk 扩大到两个完整 fetch 测试文件及已捕获种子 feed 集成，正在运行，日志 /tmp/rssripple-v13-fetch-kk.log、JUnit 同名 xml。最新原型尚需最终覆盖率和完整隔离集成，B7 仍未验收。

kk 续接句柄为 session 68347；不要轮询已终态的 24795、32852 或 59623。

kk 已终态：106 passed、1 warning，41.84 秒、退出 0。完整日志/JUnit 已存 probes/publication-fetch-kk.*；session 68347 已结束，无待轮询测试。包含捕获种子 feed 四场景及真实 Turso 并发回填，不替代最终覆盖率/全量 Docker 集成。下一步完成 PG metadata 替换并发和最终设计审查，冻结最新代码后执行完整门禁。

## PostgreSQL metadata 替换并发（kl）与完整 unit/API 门禁启动（km）

kl 独立 PostgreSQL 16 项目 rssripple-v13-retention-kl：同资源已有 created seq1 / metadata seq2，第一事务替换未提交，第二事务替换；实际 pg_stat_activity 观察第二连接 Lock，旁观者仍只见 seq1/2。第一事务回滚后最终 seq1/3，提交后 seq1/4；两种均仅两条事件，origin 保持 1。旧快照 through=2 确认后最新事件仍待消费，新确认后为空。合成数据、真实生产 publish/snapshot/ack 调用；两场景退出 0，探针哈希及结果已保存。项目 down -v 清理退出 0。

km 冻结 /tmp/rssripple-v13-unit-km 共 2960 文件，源哈希已保存 probes/publication-unit-km-source.json。完整 tests/unit tests/api 启用 --cov=app --cov-fail-under=95，session 39121；log /tmp/rssripple-v13-unit-km.log、JUnit 同名 xml、coverage /tmp/rssripple-v13-unit-km-coverage.json。当前运行，不能提前宣称门禁通过；不要编辑冻结副本或因观察超时重启。下一步继续文档规范整合与完整隔离集成配置，最终核验源码哈希和测试终态后才考虑合并。

## 权威文档整合（km 运行期间）

在独立原型中重写 business-logic/data-models/constraints/api-endpoints/db-migration 的 V13 阶段堆叠说明，将四种运行模式、baseline/cursor/generation、普通规则/频道变更、扫描失败恢复、自动暂停及同事务发布整理为当前协议。删除“CLI 尚未实现”等过期描述和“恰好消费一次”不成立的保证；明确重解析不创建定向请求、完成事件按准入消费。同步 AGENTS 索引；这些文件已包含在持久补丁与哈希清单中，未修改 main 的运行代码。

数据库迁移文档统一为停写备份→prepare-schema→导出审核→指纹应用→启动检查流程；实验表不视为受支持旧版本。验收状态仍在本计划，不以设计文档描述代替已完成验收。km session 39121 已确认仍运行（约 11%）；冻结副本未修改。完整隔离集成配置与实际调度补偿测试仍待继续。

## 完整隔离集成启动（kn）

冻结 /tmp/rssripple-v13-integration-kn 共 3056 文件，app/tests/scripts 来自 unit km 冻结副本，未混入后续文档改动。B7 无前端改动，复用 V12 已验收 app/static；uv.lock 与 V12 完全一致。哈希已保存 probes/publication-integration-kn-source.json。唯一项目 rssripple-v13-complete-kn 使用内部网络、测试凭据和独立卷，无宿主 .env/data 挂载；四服务 healthy，启动退出 0。

完整 runner session 24311，容器 rssripple-v13-complete-kn-runner，日志 /tmp/rssripple-v13-integration-kn.log，Compose /tmp/rssripple-v13-integration-kn/docker-compose.integration-isolated.yml。当前运行，继续观察同一进程；终态后必须核对测试结果、两个应用 SIGINT 正常退出、四份 coverage 汇总 >=85%、导出 JUnit/coverage/日志、冻结源码哈希与 down -v 清理。不因测试成功提前清理证据或关闭 B7。unit/API km session 39121 仍运行，>=95% 门禁未得终态。

## 实际五秒发布补偿调度（ko / kp）

必要性：先前手动调用补偿服务不能证明调度注册与实际 tick 有效。新增正式 integration/organize/test_publication_scheduler.py：真实 init_scheduler，保留生产五秒发布任务，移除无关任务；暂停 Agent 经一次真实执行事件后无队列作业且进度保留，恢复后由下一次 timer→MemoryQueue→实际 handler 自动产生唯一成功 AgentRun，处理一条合成资源并确认游标。未手动触发回调、未改短周期。

ko 测试模型导入错误导致 collection exit 2，未运行；修正 AgentRun 模块后 kp 1 passed，11.08 秒、退出 0。日志/JUnit 已保存，测试与清单已加入原型补丁。此为冻结后补充集成，不计入正在运行的 kn 3167 项，也未修改 km/kn 冻结副本。两项完整门禁仍在运行，继续使用 km session 39121 / kn session 24311。

## 合并前一致性检查（门禁运行期间）

逐一对比已跟踪文件与原型后发现 constraints.md 漏入持久补丁清单，已补入并刷新哈希。所有清单内 Python 文件 Ruff 通过，`git apply --check` 对当前 main 工作树通过。原型 app/scripts 与 km、kn 两份冻结副本内容一致；km 2960 文件、kn 3056 文件再次核验均无变化。冻结后新增的 scheduler 专项只有测试变化，另有 kp 独立通过证据。

正确性审查核对：快照上界只确认已读前缀，metadata 替换的新序号不会被旧确认吞掉；generation 失效阻止旧范围确认，派发前独立会话检查不承诺撤销已发 RPC；回填历史排除与 B9 显式修订仍分开。外部副作用幂等沿用既有任务/下载器契约，不能借发布序号声称 exactly-once。迁移拒绝未审核或混合状态，导出只描述历史，不自动重放排除资源。

本次仅完成一致性与局部质量检查，不表示已批准合并：km session 39121、kn session 24311 已再次确认仍运行，完整终态/覆盖率/应用退出/证据导出/清理尚未齐备。

km 完整 unit/API 最新观察约 96%，输出已出现失败，尚无终态详情；不能宣称门禁通过。继续原 session 39121，取得失败清单和覆盖率后修复；kn session 24311 完整集成仍在推进，未重启任一冻结测试。

## 完整门禁终态与 native crash（2026-09-23 复核）

km 已结束退出 1：3803 passed、2 failed、15 skipped、6 warnings，1850.85 秒；22838 行中覆盖 22259，97.46% 达 95%，但测试失败不能验收。两项 API 仍断言旧 payload，缺自动任务 automatic=true。kn 已结束退出 1：3140 passed、10 failed、17 skipped、8 warnings，1729.87 秒。6 项 B9 集成旧夹具缺发布迁移；2 项 worker wiring 最小替身未替换新增启动门禁；另有 LLM 空响应返回非空与通知连接失败。

异常不是全部夹具问题：LLM 应用退出 139、非 OOM，末尾 Turso panic `core/storage/page_cache.rs:284:21` / `Attempted to insert different page with same key`，此前有 FTS outbox 写冲突。普通应用 SIGINT 退出 0；LLM coverage 文件未写出，coverage-report 退出 1、无法汇总。不得跳过该应用覆盖率或忽略连接失败。完整数据/日志/容器 inspect 已导出 /tmp/rssripple-v13-artifacts-kn，末尾 panic 日志持久保存 probes/publication-kn-native-panic.log；全部 JUnit 和主日志已保存。km 2960 文件与 kn 3056 文件终态哈希无变化。

隔离原型只修明确兼容项：API payload 增 automatic；B9 并发测试 seed 后真实 bootstrap；worker wiring 明确桩替换 ensure_publication_ready 并断言其早于 handler 注册。新回归 la session 5862，/tmp/rssripple-v13-fixtures-la.log 和同名 xml；尚在运行。下一步必须取得 la 终态，独立复现 native panic（启用回溯、保留库副本，核对 FTS/并发）、调查 LLM 空响应失败，再重跑最终完整门禁。B7 不关闭，旧 km session 39121 / kn session 24311 已终态，不再轮询。V14 仍独立未验收。

la 回归终态：136 passed、8 skipped、1 warning，60.43 秒、退出 0。8 skip 全部为原有退役 metadata API，不涉及本轮失败用例；证据已存 probes/publication-fixtures-la.*。kn 初次 down 因已退出 runner/coverage 容器仍占卷而未删完；明确删除这两个终态容器后再次 down -v，退出 0、专用卷清理完成。当前无待轮询测试；下一步优先处理 native panic 与 LLM 空响应失败，不能以本次夹具绿测关闭 B7。

## 空响应失败的配置竞争候选（lb / lc）

检查真实 kn 日志发现 empty-model 保存后分析返回正常 mapping，期间多个调度后台任务刷新配置。源码 load_runtime_config 在 await SELECT 前 clear 全局 overrides，异步读者可能短暂使用环境模型。确定性测试在真实 DB 已加载 empty-model 后暂停刷新 SELECT，成功/失败两种路径均观察不到旧模型，lb 2 failed、0.92 秒、退出 1。

隔离修复为完整读成功后构造 loaded，再无 await 地 clear/update，保持 dict 身份以兼容 scoped override；读取失败保留旧值。lc 新测试加 settings API 10 passed、1 warning，4.66 秒、退出 0。补丁/哈希/日志/JUnit 与 conventions 已更新。此已证明独立配置竞争，不足以独自认定 kn HTTP 失败根因；下一步需复跑原 TestFeedAnalysisVariants，并继续复现 Turso native panic。没有修改依赖或降低集成门禁。当前无运行中的测试进程。

## 原 HTTP 失败路径重放启动（ld）

新冻结 /tmp/rssripple-v13-integration-ld，共 3066 文件，包含 la 夹具修正与 lc runtime_config 原子发布。唯一项目 rssripple-v13-http-ld，四服务 healthy、启动退出 0，应用启用 RUST_BACKTRACE=full / PYTHONFAULTHANDLER=1，其他依赖与数据库模式未更换。runner session 58649 重放 TestFeedAnalysisVariants 与 test_notifications.py；日志 /tmp/rssripple-v13-http-ld.log，容器 rssripple-v13-http-ld-runner 的 /app/data/http-regression.xml。尚未终态；继续原进程，完成后导出应用日志/退出状态与测试结果，再清理唯一项目。

已阅读 FTS outbox→sidecar 路径；kn panic 前 FTS 冲突只提供定位线索，不据此宣称根因。若原用例未重现 panic，仍需保留完整负载复验及原生回溯，不能通过关闭 FTS/调度降低验收范围。

## HTTP 重放终态与独立 FTS 崩溃复现（ld / le / lf / lg）

ld 五项原 HTTP 回归全部通过，184.96 秒、退出 0；两个应用 SIGINT 退出 0，JUnit/日志已导出，唯一项目清理退出 0。本轮未复现 kn panic，不能据此排除原崩溃。

独立 FTS 探针调用生产 ensure_fts_tables/_upsert/_search_fts，临时合成 sidecar、8 个并发调用者、各 100 次。le 缺数据库方言注册初始化即失败，不算负载结果；补齐 app.database 导入后 lf 0.76 秒内 native abort 134，完整回溯为 core/storage/btree.rs:8243 `page should be loaded`。位置与 kn page_cache 不同，不认定两者同根因。原日志及探针哈希已保存。

方案局部化为 FTS 派生缓存引擎单连接池（pool_size=1/max_overflow=0），串行短事务、不关闭 FTS/调度、不改主数据库并发、不升级依赖。全新 green 目录同探针 lg 退出 0，400 写 + 400 读全部成功、无捕获异常。持久 patch/权威业务文档已同步；还需正式子进程集成与最终索引内容断言、原完整负载复验和完整门禁，B7 未验收。临时原始复现库留在 /tmp/rssripple-v13-fts-le 供审查，未碰归档库；当前无运行测试或 Compose 项目。

## FTS 正式集成终态（lh）

已核对原运行日志及 JUnit：1 passed，7.77 秒。子进程正常退出，400 写/400 读均成功，无异常，最终 10 个唯一 ID 与预期一致。正式驱动、测试和清单纳入可恢复补丁，日志/JUnit 保存为 probes/publication-fts-lh.*。必要性仍来自 lf 原生崩溃复现；本测试采用明确标注的合成并发数据，真实标题/torrent 覆盖继续由 publication_feed 提供。下一步为最新组合的相关回归及冻结后的全量单元/API、完整集成覆盖率门禁；km/kn 的失败结果仍然有效，B7 不可据此标记完成或合入。

## 最新组合回归与完整门禁启动（li / lj）

li 相关回归 46 passed、1 个既有 pytest_asyncio 弃用警告，32.39 秒、退出 0；覆盖 test_fts、runtime_config_atomic_reload、publication_feed、publication_scheduler 和正式 FTS 子进程集成。日志/JUnit 保存为 probes/publication-related-li.*。补丁 git apply --check 通过。

lj 从旧冻结源码复制并覆盖当前原型清单，排除数据库、coverage 和缓存，冻结目录 /tmp/rssripple-v13-unit-lj；app/tests/scripts 2947 个文件哈希保存为 probes/publication-unit-lj-source.json。完整 unit/API 命令 pytest tests/unit tests/api --cov=app --cov-fail-under=95，session 84551，日志 /tmp/rssripple-v13-unit-lj.log、JUnit /tmp/rssripple-v13-unit-lj.xml、覆盖率 /tmp/rssripple-v13-unit-lj-coverage.json。已启动，尚未终态；下次继续观察同一进程，不能凭观察超时重启。完整集成新冻结门禁尚未启动，仍需验证双应用退出、四份 coverage 汇总 >=85%、导出与隔离项目清理。

## 完整集成门禁启动（lk）

冻结 /tmp/rssripple-v13-integration-lk，从 ld 测试基础复制并覆盖 lj 的 app/tests/scripts，源文件清单共 3068 项保存于 probes/publication-integration-lk-source.json。唯一项目 rssripple-v13-complete-lk 四个服务健康，启动退出 0；启用完整原生回溯，未关闭 LLM 应用调度或 FTS。runner 名 rssripple-v13-complete-lk-runner，session 66950，日志 /tmp/rssripple-v13-integration-lk.log。已开始完整 tests/integration 既定范围；尚未终态，后续需要导出 JUnit、双应用正常退出、四份覆盖率汇总 >=85%、冻结哈希复核及 down -v。lj session 84551 同时仍在运行，最新日志约 16%；不要重启现有进程。

## 单元/API 完整门禁终态（lj）

原 session 84551 正常结束、退出 0：3807 passed、15 skipped、6 warnings，1933.24 秒；22258/22837 行，97.4646%，达到 95% 门槛。冻结源码 2947 个哈希复核全部未变，日志/JUnit/结果保存为 probes/publication-unit-lj.* 与 publication-unit-lj-result.json。跳过项沿用原测试条件，不因本次改动增加豁免。完整集成 lk/session 66950 仍在运行，最新约 96%；仍需测试终态、双应用 SIGINT 正常退出、四份 coverage 合并 >=85%、证据导出与项目清理，尚不能合入 B7。

## 完整集成终态与合入评审（lk）

lk 原 session 66950 已终态退出 0：3152 passed、17 skipped、8 warnings，1819.10 秒。两个应用接收 SIGINT 后均退出 0、非 OOM；四份覆盖率（普通应用、LLM 应用、runner、脚本）合并退出 0，20335/22837 行，89.04%，达到 85% 门槛。3068 个冻结文件哈希复核无变化；导出目录 /tmp/rssripple-v13-artifacts-lk 含 JUnit、原始覆盖率、数据库、日志与应用 inspect。专用 runner/coverage 容器移除后 down -v 退出 0，项目已清理。日志/JUnit/结果保存为 probes/publication-integration-lk.*、publication-integration-lk-result.json、publication-coverage-lk.log。

按 code-review-and-quality 复核模型约束、事务发布顺序、最新 metadata 保留、generation 条件确认、历史排除、暂停与规则变更、迁移审核和启动门禁；同时核对 PG 锁/回滚、实际 CLI/web/worker 启动、Redis 清空后双消费者恢复及真实 torrent 流水线证据。全部 manifest 当前哈希一致，运行代码与通过 lj/lk 的冻结源码一致，全部变更 Python Ruff 通过。未发现阻止本批 B7 合入的问题。FTS 本轮完整负载无原生崩溃，但不将独立 btree 复现与此前 page_cache 崩溃描述为已证明同源。

合入范围仅 B7 及其验证中证实的配置刷新/FTS 并发修复；B4 执行所有权和下载持久幂等实验均不纳入。现有库升级必须遵循 docs/design/db-migration.md 的停写、备份、prepare-schema、审核指纹与 apply 流程，本次未执行生产迁移。
