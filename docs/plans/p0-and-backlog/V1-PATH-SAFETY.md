# V1：整理源路径与媒体库路径边界

状态：2026-09-13 O3/O4 已实现并同步主工作树，专项回归通过，完整门禁正在运行。本方案处理 TODO P1-O3/O4，完成标准为修复及测试通过后删除对应 TODO，不提前关闭。下方临时副本记录保留为实施历史，当前结果见末节。

## 必要性与优先级

`_collect_files` 将冻结通知的 `files[].name` 直接拼入根目录。`../outside.mkv` 可选中兄弟文件，单文件 `torrent_name` 的提前返回甚至绕过清单检查。`resolve_server_path` 将服务器 Location 的相对后缀写为 `Library.root_subpath`，`/media/../outside` 会最终解析到卷外。临时哨兵复现未执行移动，结果记录 `/tmp/rssripple-v1-repro.json`。

两项维持 P1：需不安全输入或配置触发，但可能影响文件所有权，优先于界面、索引和可维护性修复。现有 P0 文件内容比较及 no-replace 只保护目标碰撞，不能判断源文件是否属于该下载任务。

## 方案选择

采用两层检查：相对路径语法校验＋本进程解析后的根目录包含校验。复用 `validate_download_subdir` 的禁止绝对路径、控制字符、`.`/`..` 约束；外部文件名不能悄悄 trim 后指向另一文件，空名字或类型错误须确定性拒绝。明确处理 POSIX 与反斜杠分隔符，不把 Windows 驱动器相对路径 `C:foo` 当作合法本地名字。根目录必须非空且是已解析的本进程绝对路径，不能让 `Path("")` 落到工作目录。

仅过滤 `..` 无法阻止符号链接逃逸；跳过非法条目会造成合法与非法混合的半份清单被执行；只在 Pydantic API 层校验无法覆盖冻结快照、扫描及历史 DB 行。因此在服务使用边界统一拒绝整份输入。

`Path.resolve` 包含检查保护规划时已有链接，不能证明规划后被另一进程替换目录仍安全。执行时需重查冻结源/目标边界；目录描述符和 no-follow 的抗恶意并发替换属于执行器后续硬化，不能用路径字符串校验宣称已解决 TOCTOU。此批必须保留这项限制并评估本地可写攻击面的部署条件。

## 具体改动边界

| 位置 | 预期改动 | 失败语义 |
|---|---|---|
| `organize_service._collect_files` | 先校验完整 `files` 与 `torrent_name`，再检查存在性；候选及目录枚举结果均须在下载根内；保留合法嵌套布局与 `root/name`、`base/name` 两种布局 | 抛 `PlanError`；规划落 failed，零 ops，无任务清理 |
| `_resolve_manifest` | torrent 回退与冻结清单使用同一规则，不能过滤危险项后返回半份清单；检查 fallback 在 `_plan_one/_rebuild_plan` 的 try 边界 | 同上；不得变为无清单后的宽泛扫描 |
| `_cleanup_paths/_scoped_source_dir` | 使用相同校验结果；种子目录不能等于下载根或逃出下载根 | 拒绝规划，不生成 movedir/cleanup 越界范围 |
| `media_server_service.resolve_server_path` | 最长前缀命中后校验 binding 子路径及 Location 后缀；保持合法嵌套映射；不用 `.strip('/')` 掩盖非法绝对输入 | 不安全的服务器结果作为 `MediaServerError`，API 沿用现有 502 契约 |
| `scan_server` | 先解析并校验所有 sections/locations，完成后再修改任何 Library；卷内包含检查应覆盖已有卷映射 | 无新增或更新、无部分成功；保留手工绑定的既定重扫语义 |
| `volume_service.resolve_library_root/recycle` | 使用时重验历史相对字段及解析后的卷内包含关系 | 明确 `VolumeResolutionError`；缺卷仍按原待绑定契约返回 None |
| organize 库快照及执行调用方 | 检查新增异常的传播边界；单个坏库不能在循环外使所有通知规划中断；手工分类应返回既有业务错误 | 每个受影响计划有可诊断错误；无关通知仍可处理 |

不能在 `_library_ns` 直接增加会抛出的路径检查而不改调用方：`plan_for_notifications` 目前在逐通知 try 外构造整个 `lib_ns`。同样，`classify_plan` 的 `_library_ns` 当前也在 PlanError 捕获边界外。优先让库路径解析延迟到所选库或显式携带解析失败状态，避免把一个异常配置扩大为全局停摆；最终选型以最小明确类型和集成结果为准。

## 严格集成标准

沿用已 confirmed 的猫与龙 S1E10 原始标题、torrent 哈希和文件名，恶意路径为明确标注的故障注入，不能写回金标。媒体服务器路径是协议故障注入，不冒称生产采集。

| 场景 | 入口与断言 |
|---|---|
| 合法单文件及嵌套清单 | completed Task→通知→计划→执行，原 reviewed 季集不变，实际字节和 Task 清理结果正确 |
| 合法＋`../` 混合清单 | 生产通知规划落 failed，零 ops；合法源与卷外哨兵均完整，禁止部分执行 |
| POSIX/Windows 绝对、驱动器相对、控制字符、空或非字符串 | 均在文件读取/选择前拒绝，不能靠“文件不存在”使测试假通过 |
| 危险 torrent_name＋合法清单 | 单文件快捷路径和 scoped directory 均不得绕过校验 |
| 文件链接/目录链接指向根外 | 显式清单与 rglob 回退各覆盖；哨兵不变，无清理；合法根内链接行为须明确并测试 |
| torrent 清单回退含非法项 | 与冻结 files 同样失败，不能只保留合法部分 |
| 媒体服务器先合法后越界 Location | 真实 API→service→DB；502；独立会话验证新增/更新均未发生，重试结果一致 |
| 合法扫描及最长前缀 | 库映射正确，二次扫描幂等，手工绑定保留规则不变 |
| 历史 DB root/recycle 子路径越界或链接逃逸 | 使用时拒绝；另一个安全库/通知仍可规划；手工分类和执行错误可诊断 |

每个安全测试先建立实际存在的卷外哨兵，并包含合法成功对照。检查 DB、文件内容及清理/RPC 副作用，不止断言异常。必要测试应先在未修复实现上失败，再在修复后通过；回归覆盖完整 organize、媒体服务器 API、卷解析及真实语料。最终重跑单元/API ≥95%、完整隔离集成 ≥85%，源码版本与报告对应；不降低门禁或以专项计数代替全量结果。

权威文档同步：`docs/design/file-organization.md` 的输入路径/卷映射/失败语义，必要时同步 `api-endpoints.md`；验收清单与结果更新 `docs/testing/organize-integration.md`、`VALIDATION.md`。

## O3 临时副本实施记录

为避免修改运行中的 V0 被测源码，将当前 app/tests 复制到 `/tmp/rssripple-v1-path-work`；只有 immutable fixtures 与依赖环境使用原目录只读引用，不携带 `.env`。本节结果针对该临时副本，不能当作主工作树已修复或 V1 完整门禁通过。

- 新增 `organize_source.py`，集中校验完整清单、种子目录与解析后的下载根边界；禁止驱动器相对路径、控制字符、首尾空白及非字符串等不安全名称。torrent 回退先校验原始条目，再拼根名，避免拼接掩盖不安全输入。
- 清单回退的 `PlanError` 纳入创建/重建/分类的既有错误边界；新规划确定性失败落 failed/零 ops。执行前再检查旧计划源和当前目录解析，失败返回既有 `OrganizeError`，不执行文件操作或清理任务。检查与清理范围解析均在线程中完成。
- 新增 16 场景集成：真实已审核 S1E10 标题/文件名/hash＋明确注入的危险路径，包括合法嵌套对照、混合清单、RPC 回退、种子名、目录/文件链接、旧计划源、规划后目录替换；检查 DB、原文件、卷外哨兵、目标及清理副作用。
- 红测：合法对照通过；`../outside.mkv` 和绝对路径均产生 pending 可执行计划，2 failed / 1 passed，证明回归能检测原缺陷。日志 `/tmp/rssripple-v1-red.log`。
- 第一版完整整理服务＋集成回归最终 **333 passed / 6 warnings，80.73 秒**，报告 `/tmp/rssripple-v1-scope3.xml`。调整执行线程边界后，最新 API＋服务＋新集成 **141 passed / 1 warning，48.63 秒**，报告 `/tmp/rssripple-v1-api-source-final2.xml`。警告均为既有 fixture/coroutine/LangGraph 弃用问题。
- 旧测试中“丢弃不安全条目后继续执行”的预期改为整份拒绝，并保留合法 RPC/磁盘大小对照。P0 目标碰撞测试的第二个源改放下载根内，确保它仍独立验证碰撞保护，而不是被新增的路径门禁提前拒绝。
- 该阶段尚待同步、权威文档和 O4 实施；这些步骤随后完成，完整门禁仍待结果。`Path.resolve` 校验不等于抵御同时发生的恶意目录替换，不宣称解决描述符级 TOCTOU。

## O4 实施与主工作树同步

- 真实扫描 API 红测确认：含 `../` 的批次仍返回 200，created=2/updated=1；合法对照通过。随后将所有解析及卷内包含检查前置，全部通过才写入。新增 Location 后缀绝对路径、驱动器相对路径、控制字符与符号链接正反例，检查既有行不改、新行不增、会话无待提交修改及重试一致。
- 共用 `app/utils/path_safety.py`，O3 通过业务错误适配复用；路径解析异常不会静默变成有效路径。历史库根/回收站也严格校验，坏库保存到视图的 `path_error`；planner 仅拒绝命中的坏库，无关库不受影响。执行前补查旧计划目标必须仍在当前库根/回收站内。
- API 库响应新增可空 `path_error`，非法历史配置返回 `root_path=null` 及原因，列表仍可读取，bound 仍表示存在绑定。前端根目录列显示本地化错误提示，悬停可见原因，现有设置入口可修复。
- 最新合并专项（含整理完整集成、服务、卷和 API）**416 passed / 8 warnings，132.20 秒**，`/tmp/rssripple-v1-combined.xml`。另修正 metadata misc 套件旧的“绝对子路径静默去斜杠”假设，保留合法对照，**59 passed，0.95 秒**。
- 验证主工作树基线哈希后同步 13 个 V1 文件，未覆盖独立 P0 修复；随后更新前端类型、界面和中英 locale、权威设计及测试文档。前端 tsc/Vite 构建、相关 ESLint、Ruff、diff whitespace 均通过。
- 完整单元/API 95% 与隔离集成 85% 已启动，源码/依赖哈希 `/tmp/rssripple-v01-source.json`；日志与当前会话状态见 VALIDATION.md。TODO O3/O4 等待完整门禁通过后关闭。


## V0/V1 验收完成（2026-09-13）

最新完整单元/API 3481 passed、14 skipped，覆盖率 98.12%；完整隔离集成 3088 passed、17 skipped，四份覆盖率合并 18907/21076＝89.71%。两道门禁及应用正常退出均通过，报告已导出，隔离项目已清理。V0 补验与 V1 O3/O4 已完成，TODO 已删除 O3/O4；源哈希与报告路径见 [VALIDATION.md](VALIDATION.md) 最后验收记录。V2 仍独立验证中。
