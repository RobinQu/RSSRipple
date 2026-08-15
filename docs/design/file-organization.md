# 内置文件整理（File Organization / Organize）

> **修订记录**：2026-08-13 —— 逻辑存储卷（StorageVolume）+ 媒体服务器实例（MediaServerInstance）取代 P1 的「手工 Library 注册 + DownloaderInstance.path_map」（P1 未发布，无迁移负担）；Library 改为媒体服务器扫描派生，一切外部路径引用统一为「逻辑卷 + 子路径」结构化模型。

文件整理（重命名/移动入库/媒体服务器刷新）以**内置子系统 "organize"** 进入 RSSRipple：它是 notification 流水线的**内置消费者**，与外部 webhook 消费者（如独立部署的 vault-organizer）**并列**。notifications.md 的全部契约保持不变——webhook fan-out、payload 快照、退避重试、下游清理任务 API 一字不改，外部消费者仍是受支持的一等用法；内置 organize 只是多消费一份同一份快照，两者互不感知。

vault-organizer 的独立部署形态在功能对等后归档（见"分期路线"），但其安全不变量**逐条保留**（见"执行器不变量"）。本文档是内置整理子系统的权威设计：定位与边界、路径解析模型、模型、规则与命名模板、触发与执行链路、执行器、API、配置、前端、部署。

## 定位与边界

```
任务 completed → 停种 + DownloadNotification（快照）
                      │
        ┌─────────────┴─────────────┐
        ↓                           ↓
  内置 organize 消费者          webhook fan-out（外部消费者，
  （本 tick 规划落库 →           vault-organizer 等，契约不变）
   人工/自动执行 → 移动重命名
   → 媒体服务器刷新 → 任务清理）
```

- **两阶段不合并**：规划（只读磁盘 + 计划落库，不动文件）与执行（前置门禁 → 幂等执行 → 后置校验）是两个持久化阶段。`auto_execute=true` 只是省掉人工点击——计划照常落库，随后经同一代码路径执行，规划与执行的持久化边界不消失。
- **规划只依据快照**：`OrganizePlan.payload` 是创建时冻结的完整通知快照，是执行的唯一依据；规划/执行均不读活库 metadata。
- **只扫种子独立目录**：文件定位只扫 `download_dir/torrent_name`（或 payload `files` 清单）；**绝不扫描共享下载根**——那里混放所有任务的文件。
- **冲突绝不覆盖**：规划预检与执行前置门禁两道防线，目标已存在且 size 不符一律拒绝。
- **清空目录只走 `os.rmdir` 自底向上**（只删空目录），**绝不 `rm -rf`**。
- **合集缺集拒绝整理**：合集覆盖度校验不过即规划失败，绝不硬猜、绝不静默丢失（语义见下文"规划"）。

## 统一路径解析：逻辑存储卷

整理子系统要触达三类外部路径：daemon 视角的下载目录、媒体服务器视角的库根、以及整理目标根。它们统一表达为**「逻辑卷 + 子路径」**的结构化引用：

- **StorageVolume** 是用户声明的逻辑卷，指向 RSSRipple 容器内一个挂载点（compose 启动时把宿主/远程存储挂进来，如 `/storage/flash-aio`）。
- 一切配置面路径引用（下载器卷绑定、媒体服务器绑定、Library 库根）一律存 `(volume_id, subpath)`，**不落库绝对路径**；使用处动态解析 `volume.mount_path + subpath`——挂载点改了一处修改全局生效。
- P1 的自由文本前缀字典 `path_map` 废弃（前缀替换无法表达结构、改挂载点要逐条改），其"最长前缀匹配"语义由媒体服务器绑定表（`media_server_bindings`）以结构化形式继承。
- **计划 ops 例外**：`OrganizePlanOp.src/dst` 作为执行时点快照，仍在规划时解析为本进程视角绝对路径落库（与 payload 冻结语义一致）；若规划与执行之间挂载点漂移，前置门禁按「src 不在 = 数据丢失」拦下计划置 failed，重规划即可收敛——不存在静默写错位置的可能。

## 概念与数据模型

vault-organizer 的硬编码分流（`content_type == "anime"` → 动漫库、movie genre → 类别目录、六个固定 `library_roots`）全部通用化：**Library** 由媒体服务器扫描派生（而非手工注册），**OrganizeRule** 以 DSL 表达全局有序分流，**OrganizePlan / OrganizePlanOp** 承载两阶段计划（移植自 vault-organizer 的 `plans` / `plan_ops`）。

### StorageVolume（逻辑存储卷）

```python
class StorageVolume(Base):
    __tablename__ = "storage_volumes"

    id: str                              # UUID
    name: str                            # Unique 显示名（如「flash-aio」「local」）
    mount_path: str                      # RSSRipple 容器内绝对路径（docker/compose 启动时
                                         # 挂载宿主远程存储到此）；保存时探测存在性，
                                         # 不存在 422；写权限探测结果仅作展示提示（不拦截保存：
                                         # 挂载可能暂时只读，规划/执行阶段自会失败）
    remark: str | None
    created_at / updated_at
```

### DownloaderInstance 卷绑定（path_map 废止）

- P1 的 `path_map` JSON 列**废弃**（P1 未发布，无存量数据；轻迁移直接 DROP 或留孤儿列均可，实现时选型，对齐既有惰性孤儿列惯例）。
- 新列：`volume_id`（可空 FK → StorageVolume）、`volume_subpath`（可空相对路径，校验规则同 `Agent.download_subdir`：禁绝对路径 / `..` 段 / 控制字符）。语义：daemon 视角的 `download_dir` 根 == `volume.mount_path + volume_subpath`；**两者皆 null = 两视角一致（恒等，现状默认）**。
- 翻译：payload 的 `task.download_dir` / `files` 相对部分接在绑定路径后，得到本进程视角源路径。这是 downloader 级通用能力（任何需要在本进程内触达下载文件的消费方共用），非 organize 私有。

### MediaServerInstance / MediaServerBinding（媒体服务器）

取代手工 Library 注册与全局 `PLEX_*` 配置；多服务器、多类型天然支持：

```python
class MediaServerInstance(Base):
    __tablename__ = "media_server_instances"

    id: str                              # UUID
    name: str
    type: str                            # "plex" | "emby" | "jellyfin"
    url: str
    token: str                           # 明文存 DB（对齐 DownloaderInstance.password 惯例）
    enabled: bool                        # 停用后不再扫描/刷新，保留行与派生 Library
    created_at / updated_at

class MediaServerBinding(Base):
    __tablename__ = "media_server_bindings"

    id: str                              # UUID
    server_id: str → MediaServerInstance # FK CASCADE
    server_path_prefix: str              # 服务器视角路径前缀（如 "/data/Movies"）
    volume_id: str → StorageVolume
    subpath: str                         # 卷内相对路径：server_path_prefix ==
                                         # volume.mount_path + subpath
```

- 一个服务器可注册多条绑定；服务器视角路径 → (volume, subpath) 的解析走**最长前缀匹配**（语义同原 path_map，但目标是结构化卷引用）；无命中 = 该路径**待绑定**。

**扫描派生 Library**（`POST /media-servers/{id}/scan`，幂等）：

- Plex：`GET /library/sections`（取 type=movie/show 的 section：key、title、Location 列表）。
- Emby/Jellyfin：`GET /Library/VirtualFolders`（Name、Locations[]、CollectionType=movies/tvshows；需管理员 API key）。
- 每 `(section, location)` 幂等 upsert 一个 Library；**多 Location 的 section 拆成每 location 一条 Library**。server 视角根路径经该服务器的 bindings 最长前缀匹配解析为 `(volume_id, root_subpath)`；未命中绑定 → 新 Library 落 `volume_id=NULL` 的**待绑定**状态，UI 引导补绑定后重扫（或就地解析已有待绑定行）。幂等键中 `server_path` **规范化**（去首尾空白、去尾部斜杠），避免同一 Location 因尾部斜杠/空白差异被误判为新增而重复建库；重扫时**未命中绑定不覆盖既有 `volume_id`/`root_subpath`**（手工补绑定在重扫后保留，只有命中 binding 才更新解析结果）。

### Library（媒体库，扫描派生）

整理目标，对应媒体服务器某 section 的一个 Location：

```python
class Library(Base):
    __tablename__ = "libraries"

    id: str                              # UUID
    name: str                            # section/虚拟目录显示名
    media_server_id: str | None → MediaServerInstance
                                         # 来源服务器（SET NULL 保留行）
    section_key: str | None              # Plex section key / Emby·Jellyfin 虚拟目录标识
                                         # （刷新寻址用；取代 P1 的 plex_section 列）
    server_path: str | None              # 服务器视角原始根路径（bindings 解析的输入，留档）
    volume_id: str | None → StorageVolume
    root_subpath: str | None             # 卷内相对路径；规划时 root_path 由 service 解析 =
                                         # volume.mount_path + root_subpath（不再是静态列）
    kind: str                            # "tv" | "movie" | "mixed"；由 CollectionType 派生
                                         #（movies→movie、tvshows/show→tv）；提示性不做硬分流
    subtitle_lang_map: dict | None       # BCP-47 → Plex 后缀（Library 级覆盖；
                                         # null 用内置默认表，默认表不变）
    created_at / updated_at
```

- `volume_id=NULL` = **待绑定**：可被一个待绑定 Library 占位引用，但以其为目标的计划落「待绑定」pending（见"触发链路"），补绑定后可执行。
- **移除手工注册**：无 POST；PUT 仅限 `subtitle_lang_map` 与 `volume_id`/`root_subpath`（待绑定就地修复）；DELETE 允许删除未关联计划的行（存在关联计划 409 不变）。
- 全局 `PLEX_URL`/`PLEX_TOKEN` 配置移除：轻迁移把已配置的全局 Plex 转为一条 `MediaServerInstance`（对齐 `agents.notify_webhook_*` → `agent_webhooks` 的迁移先例）。

### OrganizeRule（整理规则）

全局有序列表，**first-match-wins**（`priority` 升序取第一条 `enabled` 且 filter 通过的规则）：

```python
class OrganizeRule(Base):
    __tablename__ = "organize_rules"

    id: str                              # UUID
    name: str
    priority: int                        # 小在前；同优先级按 created_at 稳定排序
    enabled: bool                        # 默认 true
    filter: dict | None                  # BoolCondition DSL 根（复用 filter-dsl.md 全部语义：
                                         # 空值规则、字符串忽略大小写、取值操作符 value 非空
                                         # 校验——保存时经 validate_filter_config，空 value 422）；
                                         # null = 匹配全部
    library_id: str → Library            # 命中后的目标库 FK（扫描派生产物）
    path_template: str                   # 命名模板（见"规则与命名模板"）
    file_op: str                         # "move"（默认）| "hardlink" | "copy"（R3 起放开，
                                         # schema 对其他值 422）；hardlink/copy 为保种模式，
                                         # 执行后清理按 file_op 分流（见"触发与执行链路"）
    auto_execute: bool                   # 默认 false；true = 计划落库后立即经同一代码路径
                                         # 后台执行（两阶段持久化不变）
    created_at / updated_at
```

filter 求值以通知快照对应的 FileResource + 关联作品为输入，与 Agent 过滤同一求值设施（同样必须 `selectinload` series/movie 及其 collection 关系）。vault-organizer 的硬编码分流改由 DSL 表达，示例：

- 「动漫剧集入动漫库」：`{"field": "series.is_anime", "operator": "eq", "value": true}` → 动漫剧集库 + TV 模板（替代 `content_type == "anime"` 硬编码；is_anime 三态语义见 filter-dsl.md，「未判定」可用 `is_empty` 单列规则兜底）。
- 「SF/恐怖电影入对应类别目录」：`{"field": "movie.genre", "operator": "contains", "value": "Horror"}` → movies 库 + 模板 `Horror/{title} ({year})/{title} ({year}){ext}`（按 priority 排布多条，替代 `movie_category_map` 的表序优先匹配）。
- 「合集与单集分库」：`{"field": "is_batch", "operator": "eq", "value": true}` 置前，单集规则置后。

### OrganizePlan / OrganizePlanOp（两阶段计划）

移植 vault-organizer `plans` / `plan_ops`；`notification_id` 唯一即幂等键：

```python
class OrganizePlan(Base):
    __tablename__ = "organize_plans"

    id: str                              # UUID
    notification_id: str → DownloadNotification
                                         # Unique：一条通知至多一条计划（幂等基础）。
                                         # 通知 regenerate 时：pending / failed 计划重建
                                         # （沿用已人工指定的 library/category，op 目标重渲染），
                                         # done / running 短路不重建（running 短路防幽灵执行）
    rule_id: str | None → OrganizeRule   # 命中的规则（SET NULL 保留历史）；null = 待分类
    library_id: str | None → Library     # null = 未匹配规则的「待分类」计划；指向
                                         # volume_id=NULL 的库 = 「待绑定」计划
    category: str | None                 # 电影类别目录名（模板含 {category} 时使用）；
                                         # 可人工指定/修正（classify 端点）
    status: str                          # "pending" | "running" | "done" | "failed" | "cancelled"
    payload: dict                        # 创建时冻结的完整通知快照，执行唯一依据
    error_message: str | None            # 最近失败原因（前置门禁/冲突/校验，带前 3 条明细）
    executed_at: datetime | None
    created_at / updated_at

class OrganizePlanOp(Base):
    __tablename__ = "organize_plan_ops"

    id: str                              # UUID
    plan_id: str → OrganizePlan          # FK CASCADE
    seq: int                             # 计划内顺序
    op_type: str                         # "move" | "keep" | "movedir"
    src: str                             # 源路径（本进程视角绝对路径，经下载器卷绑定解析）
    dst: str | None                      # 目标路径（keep 为 null）
    size: int                            # 规划时记录的源文件大小（幂等状态表依据）
    status: str                          # "pending" | "done" | "kept" | "failed"
    error_message: str | None

class OrganizeAuditEntry(Base):
    __tablename__ = "organize_audit_entries"

    id: str                              # UUID
    plan_id: str → OrganizePlan          # FK CASCADE
    action: str                          # 如 "plan_created" / "move" / "movedir" / "cleanup" / ...
    detail: dict                         # 操作明细 JSON
    created_at: datetime                 # 只读展示与排障，不参与任何整理决策/幂等
```

- 「待分类」对应 vault-organizer 的 `__UNCATEGORIZED__` 流程：无规则匹配 → `library_id=null` 的 pending 计划；规则命中但模板含 `{category}` 而无法确定类别 → `category=null`。**禁止落库根**：待分类计划必须人工在界面指定 library（和/或 category）后重渲染 op 目标才可执行。
- 「待绑定」与待分类并列：规则命中的 Library `volume_id=NULL` → 计划照常落 pending（含已解析的 ops 草稿或仅快照），执行门禁拒绝，补绑定（建 binding + 重扫/就地解析）后解除、可正常执行。

## 规则与命名模板

`path_template` 是相对 Library 根（解析后）的可配置格式串（`/` 分隔，各分量过 sanitize：剔除 `/` 与控制字符、去首尾空白与尾部点空格、截断 150 字符，清洗后为空 → 422/规划失败）。占位符穷举：

| 占位符 | 取值来源（快照 payload） |
|---|---|
| `{title}` | 显示标题：`title_cn or title_en or original_title` |
| `{title_en}` / `{title_cn}` / `{original_title}` | `work.title_en` / `title_cn` / `original_title` |
| `{year}` | `work.year`（电影）/ `resource.title_year` 回退 |
| `{season}` / `{episode}` | `resource.season` / `episode`；支持格式说明符 `{season:02d}` |
| `{episode_title}` | `work.episodes` 按 (season, episode) 查得分集标题，缺失渲染为空段 |
| `{category}` | `plan.category`（电影类别目录；为空时计划落待分类，见上） |
| `{collection}` | `work.collection`（合集显示名） |
| `{resolution}` / `{container}` | `resource.resolution` / `container` |
| `{ext}` | 源文件扩展名（含前导点，如 `.mkv`） |

- **保存时校验**：非法占位符 / 非法格式说明符 → 422；模板渲染结果含绝对路径、`..` 段 → 422。
- **运行时缺数据**（如 TV 模板缺 `season`）→ 规划失败（不落计划，下 tick 重试，见"触发链路"）。
- **内置 Plex 兼容预设**（创建规则时可一键填入）：
  - TV：`{title}/Season {season:02d}/{title} - s{season:02d}e{episode:02d}[ - {episode_title}]{ext}`
  - 电影：`{category}/{title} ({year})/{title} ({year}){ext}`
  - 字幕：正片同主名 + `.{lang}{ext}`，`lang` 经 `library.subtitle_lang_map`（BCP-47 → 语言后缀；未命中查主标签，仍不中取主标签本身）；同集同语言多份字幕第 2 份起追加序号。
- 配套预览 API `POST /organize-rules/preview`（见"API"），与 `/agents/rules-preview` 同构：保存前 dry-run 渲染逐文件 src→dst。

### 规划（planner 语义）

规划是纯函数：`build_plan(快照, 磁盘文件列表, 解析后的库根)`——**接口不变**，收的是 service 层已解析好的 `root_path`（volume.mount_path + root_subpath），planner 自身不感知卷模型。沿用 vault-organizer 的文件归类语义：主视频 = 最大视频文件，按模板渲染 move；字幕判定语言后同名随正片 move；其余文件 keep。安全不变量：

- **绝不扫描共享下载根**：优先按 payload `files` 清单定位；清单缺失（RPC 降级）退回扫描 `download_dir/torrent_name`（经下载器卷绑定解析后）；两者皆无 → 规划失败。
- **合集缺集拒绝整理**：合集逐文件解析 (season, episode)（文件名 SxxExx → 目录分量 → `resource.season` 回退链），覆盖度校验「期望集 ⊆ 已解析集」（期望集由 `episode_start/end` 或 `work.seasons` 展开），缺集 / 重复集号 / 无校验依据 → 规划失败，绝不硬猜。解析不出集号的视频按特典 keep。
- **冲突预检**：move op 的 dst 已存在且 size 与源不符 → 规划失败（绝不覆盖）；size 相符视为已移动的重放，交执行器收敛。

## 触发与执行链路

规划挂在 scheduler 每分钟 notify tick 内，**补建通知之后、fan-out 之前**插入 organize 规划步：

1. 整理**常开、无环境变量开关**：只要存在 enabled 规则即对本 tick 的通知做规划；无 enabled 规则时整步跳过（对通知流水线零影响）。
2. consume 本 tick 新建/重建的通知：以 `notification_id` 唯一约束为幂等键，对尚无 OrganizePlan 的通知做规划（落库走 SAVEPOINT 吸收并发竞争）。
3. **规划失败不落计划**（如文件定位不到、模板缺数据、冲突预检不过）：记 error 日志（含原因与 notification_id），因无计划行，下一 tick 自然重试——这是 vault-organizer「webhook 500 → 退避重投」语义在内置语境的等价物。
4. 无规则匹配 → 落「待分类」pending 计划（`library_id=null`）；命中规则的 Library 待绑定 → 落「待绑定」pending 计划。
5. `auto_execute=true` 的规则：计划落库后立即经同一代码路径后台执行（两阶段持久化不变）。
6. 执行完成后按命中规则的 `file_op` 分流清理：
   | file_op | 文件语义 | 执行后清理 |
   |---|---|---|
   | `move` | 移动（EXDEV 退化为 copy+校验+删源） | 任务清理：调内部任务删除 service 函数（与 `DELETE /tasks/{id}?delete_data=false` 同一实现，不再走 HTTP 回环）+ 源目录空目录清理 |
   | `hardlink` | `os.link`，源文件保留；EXDEV/EPERM → op failed + 明确 error_message，**不静默退化为 copy**（静默复制会偷偷翻倍存储并违背保种意图） | 保种：不删任务、不清源目录；恢复快照时停过的做种（`resume_torrent` RPC，与 `POST /tasks/{id}/resume` 同一 RPC，幂等） |
   | `copy` | 复制 + size 校验（失败删不完整 dst），源文件保留 | 同 hardlink（保种 + 恢复做种） |
   - 清理/恢复均为 best-effort，失败只记日志不改写计划状态。
   - **媒体服务器刷新**（三种 file_op 一致）：经 `Library → MediaServerInstance` 寻址（天然支持多服务器/多类型），按 adapter 分 type——Plex 优先**按触及目录 partial refresh**（`GET /library/sections/{section_key}/refresh?path=...`），失败或不适用退整库刷新；Emby/Jellyfin 走对应 refresh 端点。服务器停用/未配置/刷新失败一律 best-effort：只记日志，不改写计划状态。

并发模型沿用 vault-organizer：单 `asyncio.Lock` 串行化规划与执行，阻塞文件操作经 `asyncio.to_thread` 跑线程，不卡事件循环；批量执行锁内逐计划顺序执行，单个失败不影响其余。

## 执行器不变量

逐条保留 vault-organizer executor 语义：

- **执行前状态门禁**：`done` → 幂等短路；`running` 且本进程正在执行 → 拒绝（状态检查与 running 过渡在锁内原子完成，内存态区分「真正执行中」与「崩溃遗留的 running」，后者可重放）；待分类 / 待绑定计划（library 未定、category 未定或目标库未绑定卷）→ 拒绝执行。
- **前置门禁（precheck）**：执行前逐 op 复核磁盘与快照一致（规划与执行之间文件系统可能被改动）——dst 存在且 size 匹配 = 已完成通过；src 在且 size 一致、dst 不在 = 就绪；src size 不符 / dst size 不符 / 双不在 / movedir 目标目录冲突 = **违例**。任一违例 → 整个计划 failed，**不触碰任何文件**；修复磁盘后重新触发即可。
- **幂等状态表**（逐 move op，三种 file_op 共用）：dst 存在 size 匹配 = 已完成（move 模式下 src 残留且 size 相同 → 删 src 收敛，size 不同不删、dst 为权威；hardlink/copy 保留 src 保种）；src 在 dst 不在 = 执行文件操作；dst size 不符 = 冲突 failed，**绝不覆盖**；双不在 = 数据丢失 failed。`src == dst` 直接 done；`keep` 不触碰标 kept。
- **移动策略**（`file_op="move"`）：同文件系统 `os.rename`（原子）；跨文件系统（EXDEV）退化为 copy + size 校验 + 删源，校验失败删不完整 dst 报 failed；dst 父目录 `mkdir(parents=True, exist_ok=True)`。
- **硬链接**（`file_op="hardlink"`）：`os.link`，源文件保留；EXDEV/EPERM 等 OSError → 该 op failed + 明确 error_message，**不静默退化为 copy**。
- **复制**（`file_op="copy"`）：copy + size 校验（失败删不完整 dst 报 failed），源文件保留。
- **后置校验**：全部文件 op 后复核每个 dst 存在且 size 一致；src 已消失仅对 move 校验（hardlink/copy 源文件本应保留）；任一不符 → failed（可修复后重执行，幂等）。
- **movedir**：电影种子文件夹在关键文件移走后仍有剩余 → 移入 Extras 库（以一个 kind=mixed 的 Library 表达；未配置则不产生 movedir op，剩余文件原地保留）；目标已存在 = 冲突违例，绝不覆盖。平铺在下载根的散文件不产生 movedir。movedir 仅 move 语义，hardlink/copy 计划不产 movedir。
- **空目录清理**：`os.walk(topdown=False)` 自底向上 `os.rmdir`（只删空目录，非空自然失败跳过），preserve 边界 = 经下载器卷绑定解析的下载根；**绝不 `rm -rf`**。hardlink/copy 计划恒跳过（源文件保留保种，目录本就不会空）。
- **崩溃恢复**：running 计划可重放（幂等收敛：已移动的视为完成、半完成 copy 删残留 src、冲突仍 failed）；failed 可反复重试收敛；任一 op failed 计划即 failed，已完成 op 不回滚。

## API（前缀 /api/v1）

统一响应结构 `{success, data, error, meta}`；全部需认证（AuthMiddleware，与 `/api/v1/*` 其余端点一致）。分页参数 `page`/`page_size`（≤100）。

### Storage Volumes

| Method | Path | 说明 |
|--------|------|------|
| GET | `/volumes` | 逻辑卷列表（含存在/可写最近探测结果） |
| POST | `/volumes` | 创建 `{name, mount_path, remark?}` → 201；`mount_path` 必须绝对路径且**存在**（422） |
| GET | `/volumes/{id}` | 详情 |
| GET | `/volumes/dirs?path=` | 列出服务器本地目录子目录（`{path, parent, dirs, exists}`，隐藏/符号链接目录过滤，供挂载路径与卷内子路径的目录选择器使用） |
| PUT | `/volumes/{id}` | 更新（改 mount_path 全局生效——所有卷引用动态解析） |
| DELETE | `/volumes/{id}` | 删除；被下载器绑定 / 媒体服务器绑定 / Library 引用时 409 |
| POST | `/volumes/{id}/check` | 探测存在性、可读性与写权限 → `{exists, readable, writable}`（均仅展示提示） |

### Media Servers

| Method | Path | 说明 |
|--------|------|------|
| GET | `/media-servers` | 服务器列表（含各服务器派生 Library 计数与待绑定计数） |
| POST | `/media-servers` | 创建 `{name, type, url, token, enabled?, bindings?}` → 201；type 限 `plex/emby/jellyfin`（422） |
| GET | `/media-servers/{id}` | 详情（含 bindings 数组） |
| PUT | `/media-servers/{id}` | 更新；bindings 内嵌整体替换（`[{server_path_prefix, volume_id, subpath}]`） |
| DELETE | `/media-servers/{id}` | 删除（bindings 随 FK CASCADE；派生 Library `media_server_id` SET NULL 保留） |
| POST | `/media-servers/{id}/test` | 连通性 + 凭证校验 → `{ok, server_version?, message?}`；可选请求体 `{type?, url?, token?}`（编辑表单按未保存值探测，空 token = 沿用已存凭证） |
| POST | `/media-servers/test` | 无 id 的连通性探测（创建表单用）：请求体 `{type, url, token?}` → `{ok, server_version?, message?}` |
| POST | `/media-servers/{id}/scan` | 扫描 sections/虚拟目录，幂等 upsert Library（经 bindings 最长前缀匹配解析卷；未命中落待绑定）→ `{created, updated, unbound}` |

### Libraries（收敛为只读 + 局部更新）

| Method | Path | 说明 |
|--------|------|------|
| GET | `/libraries` | 库列表（含 volume 绑定状态、各库 pending 计划计数；`unbound=true` 过滤待绑定） |
| GET | `/libraries/{id}` | 详情（含来源服务器、server_path、解析后的 root_path 展示） |
| PUT | `/libraries/{id}` | 仅可更新 `subtitle_lang_map` 与 `volume_id`/`root_subpath`（待绑定就地修复；其余字段由扫描派生，提交 422） |
| DELETE | `/libraries/{id}` | 删除未关联计划的行；存在关联计划 409 |

### Organize Rules

| Method | Path | 说明 |
|--------|------|------|
| GET | `/organize-rules` | 规则列表（priority 升序） |
| POST | `/organize-rules` | 创建 → 201；filter 经 `validate_filter_config`（空 value / 非法结构 422）；模板校验（非法占位符/绝对路径/`..` 422）；`file_op` 限 `move`/`hardlink`/`copy`（其他 422） |
| GET | `/organize-rules/{id}` | 详情 |
| PUT | `/organize-rules/{id}` | 更新（含 priority 调整；同样校验 filter/模板） |
| DELETE | `/organize-rules/{id}` | 删除（已有计划 `rule_id` SET NULL 保留历史） |
| POST | `/organize-rules/preview` | dry-run 预览：body `{resource_id 或 notification_id, rule?: <规则草稿>}`；按草稿（缺省=当前规则列表 first-match）渲染，返回逐文件 `{op_type, src, dst}` 与命中规则名，不落库不动磁盘；与 `/agents/rules-preview` 同构 |

### Plans / Audit

| Method | Path | 说明 |
|--------|------|------|
| GET | `/organize/plans` | 计划列表（分页；`status` / `library_id` 过滤；created_at 倒序；列表项不含 payload，带 ops 摘要与 `pending_reason: "unclassified" \| "unbound" \| null` 派生字段） |
| GET | `/organize/plans/{id}` | 详情：完整 payload 快照 + ops 数组 + 关联 library/rule 信息 |
| POST | `/organize/plans/{id}/execute` | 执行单个计划（幂等；done 短路、running 拒绝、待分类/待绑定拒绝）；异步后台执行，返回 202 + 当前状态 |
| POST | `/organize/plans/execute-batch` | 批量执行 `{plan_ids: [...]}` → `{results: [{plan_id, status}]}`；锁内逐个，单个失败不影响其余 |
| POST | `/organize/plans/{id}/classify` | 待分类计划人工指定 `{library_id, category?}`：改写计划并重渲染全部 op 的 dst；pending/failed 可改 |
| POST | `/organize/plans/{id}/cancel` | 取消 pending/failed 计划（→ cancelled；done/running 拒绝 409） |
| GET | `/organize/audit` | 审计条目分页（`plan_id` 过滤；最新在前） |

## 配置

整理子系统**无环境变量开关**：常开，规划步在存在 enabled 规则时自动激活。

媒体服务器**无全局配置**：`PLEX_URL`/`PLEX_TOKEN` 已移除，服务器地址/凭证全部入库（`media_server_instances`，token 明文对齐 DownloaderInstance.password 惯例）；存量全局 Plex 配置由轻迁移转为一条 MediaServerInstance。逻辑卷、绑定、规则、模板、字幕映射同样全部入库（StorageVolume / MediaServerBinding / OrganizeRule / Library）。

## 前端

- **`/volumes`**：逻辑卷管理（增删改、存在/可写探测结果展示、被引用计数；删除受阻时提示引用来源）。
- **`/media-servers`**：服务器列表（增删改、test 连通性、enabled 开关）+ 扫描按钮 + 库绑定表格（bindings 编辑：服务器路径前缀 → 卷 + 子路径；**待绑定 Library 醒目置前**，引导补绑定后重扫）。
- **Downloader 表单**：原 path_map 输入改为「逻辑卷 Select + 子路径输入」（留空 = 两视角一致）。
- **`/libraries`** 页面取消独立路由（库是扫描产物，在 `/media-servers` 内管理；`subtitle_lang_map` 在库详情编辑）。
- **`/organize`** 不变：计划列表 status 过滤 + 「待分类/待绑定」维度（`pending_reason` 徽标）、详情 Drawer（源/目标逐文件清单 move/keep/movedir 分色 + payload 快照 JSON + audit 时间线）、操作（确认执行 / 勾选批量执行 / 待分类指定 / 取消 / failed 重试）。审计 Tab 分页展示 `organize_audit_entries`。
- i18n：zh-CN / en-US 双语，文案键随路由命名空间（`volumes.*` / `mediaServers.*` / `organize.*`）。

## 部署（共享卷与逻辑卷）

- **compose 启动时**把宿主/远程存储挂载进 RSSRipple 容器（如 `/storage/<name>`），**运行时**建逻辑卷记录指向这些挂载点（`StorageVolume.mount_path`）；下载器与媒体服务器的路径差异全部由卷绑定/绑定表消解。
- 下载目录与媒体库尽量落在**同一文件系统/同一 SMB share** 下，保证 `os.rename` 原子；跨文件系统触发 EXDEV 复制回退（大文件走两遍 I/O/网络），应避免。
- 数据库文件约束不变（conventions.md）：媒体/下载文件可在网络共享上，Turso/SQLite 库文件必须本地盘。
- `docker-compose.yml` 与部署文档相应更新：内置 Transmission 服务与 app 服务挂同一命名卷（可挂**不同路径**——如 Transmission 挂 `/downloads`、app 挂 `/storage/main/downloads`——e2e 经下载器卷绑定表达，顺便验证解析链路）。
- **集成测试方案**：docker-compose 将同一共享卷挂载到内置 Transmission 与 RSSRipple 容器（不同挂载点 + 配置卷绑定），跑「mock 频道 → 下载完成 → 通知 → 规划落库 → 执行 → 断言文件落位与任务清理」全链路；媒体服务器侧用 mock adapter 验证扫描派生与刷新寻址。

## 分期路线

原 P1「手工 Library 注册 + path_map」**已被本修订取代**（未发布，无迁移负担）。新路线：

| 期 | 内容 |
|---|---|
| R1 | 逻辑卷 + 下载器卷绑定改造：`storage_volumes` 表、`DownloaderInstance.volume_id/volume_subpath`、统一路径解析 service、path_map 列废止；规划/预览/执行链路改走卷解析（行为对等） |
| R2 | 媒体服务器接入：`media_server_instances` / `media_server_bindings`、Plex adapter 先行（扫描派生 Library + partial refresh），Emby/Jellyfin adapter 按同一契约实现 + mock 测试；Library 只读化、`PLEX_*` 全局配置移除 + 轻迁移；刷新改址（Library→MediaServerInstance）；「待绑定」计划流程与前端两新页面 |
| R3 | `file_op` 开放 hardlink/copy（已实现）：schema 三值、执行器 `os.link`/copy+校验、EXDEV/EPERM failed 不静默退化、执行后清理按 file_op 分流（move=删任务 / hardlink·copy=保种+恢复做种）、后置校验与空目录清理相应调整 |

vault-organizer 仓库在 R2 功能对等后归档（README 指向本文档）；外部 webhook 消费者仍受支持，其设计文档（architecture/planner/executor/configuration/deployment）中的不变量已由本文档吸收。
