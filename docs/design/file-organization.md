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
- **规划只依据快照**：`OrganizePlan.payload` 是创建时冻结的完整通知快照，是执行的唯一依据；规划/执行均不读活库 metadata。新版快照的 `file_associations.status=complete` 是逐文件作品/季集的权威输入，planner 不再解析或覆盖；`partial/unavailable` 明确拒绝并引导在计划详情补全；只有完全没有该字段的历史快照才调用频道扫描共用的确定性解析器。单文件多集使用 Plex 兼容 `{episode_code}`（如 `s01e01-e02`）。
- **多作品短期支持**：按 `work_id` 将权威 assignments 分组，每组使用快照 `works` 中自己的作品元数据完整调用普通单作品 planner；仅当所有分组命中同一个 rule、Library、`file_op` 与 category 时，把 ops 合并进现有单一 OrganizePlan。字幕只在季集号能唯一归属某一作品时随该组整理，否则 keep；未关联文件同样 keep。任一目标不一致必须落明确 failed 计划，禁止选择其中一个目标吞并其余文件。

### TODO：跨目标多作品分组子计划（长期方案）

当前 `OrganizePlan` 的 `rule_id/library_id/category` 是计划级字段，因此短期方案刻意不支持同一下载包跨媒体库或使用不同 file_op。长期改造必须新增 `OrganizePlanGroup`（父计划 1:N，分组持有 `work_type/work_id/rule_id/library_id/category/file_op/status`，现有 ops 改挂 group），并完成：父状态聚合；分组级分类/执行/重试；按卷独立磁盘空间门禁；多个 MediaServer Section 刷新；所有分组终态成功后才清理下载任务/源目录；取消、审计和 regenerate 的分组幂等重建；旧单组计划迁移。该 TODO 未完成前，跨目标多作品计划必须保持 failed，不得自动降级。
- **只扫种子独立目录**：文件定位只扫 `download_dir/torrent_name`（或文件清单逐项存在性精确匹配）；**绝不扫描共享下载根**——那里混放所有任务的文件。清单来源顺序：payload `files` 快照 → torrent 清单回退（`resource.torrent_file` 缓存 → `torrent_url` 拉取 → 下载器 RPC，见 `_resolve_manifest`；回退清单也须整份校验）；拉取成功的缓存路径**不在规划主事务回写资源行**（规划只落计划行），由调用方在计划提交后经独立事务 best-effort 回写；torrent_name 为空时**不做任何目录遍历**，只按清单精确匹配，匹配不到即规划失败。
- **输入路径边界**：所有来源先校验完整 files 与 torrent_name，禁绝对/驱动器路径、空名、非字符串、控制字符、首尾空白及 `.`/`..` 分量。任一非法项拒绝整个计划，不过滤后执行其余文件；原始 torrent 条目先校验再补根名。候选及枚举结果经解析后必须仍在下载根内（含目录/文件符号链接）；种子目录不得指回共享根。校验集中于 organize_source/path_safety；新规划拒绝落 failed/零 ops，清单回退错误也纳入持久化错误边界。
- **冲突绝不覆盖**：规划预检与执行前置门禁两道防线，同计划目标重叠或影响其他源一律拒绝；已有文件目标按内容/inode 验证，文件发布原子不覆盖。
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
    name: str                            # Unique
    type: str                            # "plex" | "emby" | "jellyfin"
    url: str                             # Unique
    token: str                           # 明文存 DB（对齐 DownloaderInstance.password 惯例）
    enabled: bool                        # 停用后不再扫描/刷新，保留行与派生 Library
    created_at / updated_at

class MediaServerBinding(Base):
    __tablename__ = "media_server_bindings"

    id: str                              # UUID
    server_id: str → MediaServerInstance # FK CASCADE
    server_path_prefix: str              # 服务器视角路径前缀（如 "/data/Movies"）
                                         # Unique(server_id, server_path_prefix)：同前缀重复注册会使最长前缀匹配歧义
    volume_id: str → StorageVolume
    subpath: str                         # 卷内相对路径：server_path_prefix ==
                                         # volume.mount_path + subpath
```

- 一个服务器可注册多条绑定；服务器视角路径 → (volume, subpath) 的解析走**最长前缀匹配**（语义同原 path_map，但目标是结构化卷引用）；无命中 = 该路径**待绑定**。

**扫描派生 Library**（`POST /media-servers/{id}/scan`，幂等）：

- Plex：`GET /library/sections`（取 type=movie/show 的 section：key、title、Location 列表）。
- Emby/Jellyfin：`GET /Library/VirtualFolders`（Name、Locations[]、CollectionType=movies/tvshows；需管理员 API key）。
- 每 `(section, location)` 幂等 upsert 一个 Library；**多 Location 的 section 拆成每 location 一条 Library**。server 视角根路径经该服务器的 bindings 最长前缀匹配解析为 `(volume_id, root_subpath)`；未命中绑定 → 新 Library 落 `volume_id=NULL` 的**待绑定**状态，UI 引导补绑定后重扫（或就地解析已有待绑定行）。幂等键中 `server_path` **规范化**（去首尾空白、去尾部斜杠），避免同一 Location 因尾部斜杠/空白差异被误判为新增而重复建库；重扫时**未命中绑定不覆盖既有 `volume_id`/`root_subpath`**（手工补绑定在重扫后保留，只有命中 binding 才更新解析结果）。
- 扫描在任何新增/更新前校验全部 Location 的绑定子路径与后缀，并验证解析后仍在所属卷内（包括符号链接）；危险路径返回既有 502 `MEDIA_SERVER_ERROR`，整批无写入，不留下前半批更新。卷信息一次读取，路径 I/O 在线程中完成。
- 历史库行在使用时同样校验 root/recycle 路径。无效库的 API 响应保留绑定字段，`root_path=null`、`path_error` 说明原因；`bound` 仍只表示存在卷绑定。规划只拒绝实际命中的坏库，无关库/通知仍可处理；媒体库页面显示路径错误并可从现有设置入口修复。

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
    server_path: str | None              # 服务器视角原始根路径（bindings 解析的输入，留档）；
                                         # 写入经 ORM @validates 做 UTF-8 字节预算校验
                                         #（app/models/guards.py；uq_libraries_server_section_path
                                         # 的 PG btree 单行 ~2704B 上限护栏，为
                                         # media_server_id/section_key 预留 100B）
    volume_id: str | None → StorageVolume
    root_subpath: str | None             # 卷内相对路径；规划时 root_path 由 service 解析 =
                                         # volume.mount_path + root_subpath（不再是静态列）
    recycle_subpath: str | None          # 回收站目录（卷内相对路径，与库根同卷）：合集 + move
                                         # 计划剩余文件整体移入（movedir）；NULL = 原地保留
    kind: str                            # "tv" | "movie" | "mixed"；由 CollectionType 派生
                                         #（movies→movie、tvshows/show→tv）；提示性不做硬分流
    subtitle_lang_map: dict | None       # BCP-47 → Plex 后缀（Library 级覆盖；
                                         # null 用内置默认表，默认表不变）
    created_at / updated_at
```

- `volume_id=NULL` = **待绑定**：可被一个待绑定 Library 占位引用，但以其为目标的计划落「待绑定」pending（见"触发链路"），补绑定后可执行。
- **移除手工注册**：无 POST；PUT 仅限 `subtitle_lang_map`、`volume_id`/`root_subpath`（待绑定就地修复）与 `recycle_subpath`（回收站目录）；DELETE 允许删除未关联计划的行（存在关联计划 409 不变）。
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

filter 求值以通知快照对应的 FileResource + 关联作品为输入，与 Agent 过滤同一求值设施（同样必须 `selectinload` series/movie 及其 collection 关系）。规划/预览路径的快照适配器（`organize_planner.build_filter_context`）必须提供与 FileResource 同形的字段面——尤其是作品互斥 FK（`series_id`/`movie_id`/`audio_work_id`，取自快照 work 段），`content_type` 由引擎从这些 FK 派生，缺失会使一切 content_type 条件静默不命中。vault-organizer 的硬编码分流改由 DSL 表达，示例：

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
    payload: dict                        # 通知快照，与本次规划配置一起确定执行内容
    file_op: str | None                  # 本版本 move/hardlink/copy；历史 NULL 不猜 move
    needs_category: bool                 # 本版本缺类别标志，默认 false
    manual_destination: bool             # 人工分类标志，默认 false；不以 rule_id=NULL 推断
    revision: int (BIGINT)                # 计划版本，默认 0；状态转换/重建 CAS 递增
    config_revision: int | None          # 生成当前 ops 的配置版本；历史 NULL 需重建
    owner_token: str | None              # running 执行者 UUID；结果回写需匹配
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
    size: int (BIGINT)                   # 规划时记录的源文件大小（字节；支持 >2 GiB 媒体，幂等状态表依据）
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

- 「待分类」对应 vault-organizer 的 `__UNCATEGORIZED__` 流程：无规则匹配 → `library_id=null` 的 pending 计划；规则命中且模板含 `{category}` 时优先采用通知冻结作品 `genre` 的第一个非空 canonical 标签，作品没有 genre 才置 `category=null` 待人工选择。**禁止落库根**：待分类计划必须人工在界面指定 library（和/或 category）后重渲染 op 目标才可执行；详情 Drawer 初始化人工分类表单时回填已有 library 与 genre 建议，避免视觉有值但提交按钮因内部 state 为空而禁用。
- 「待绑定」与待分类并列：规则命中的 Library `volume_id=NULL` → 计划照常落 pending（含已解析的 ops 草稿或仅快照），执行门禁拒绝，补绑定（建 binding + 重扫/就地解析）后解除、可正常执行。

## 规则与命名模板

`path_template` 是相对 Library 根（解析后）的可配置格式串（`/` 分隔，各分量过 sanitize：剔除 `/` 与控制字符、去首尾空白与尾部点空格、截断 150 字符，清洗后为空 → 422/规划失败）。占位符穷举：

| 占位符 | 取值来源（快照 payload） |
|---|---|
| `{title}` | 显示标题：`title_cn or title_en or original_title` |
| `{title_en}` / `{title_cn}` / `{original_title}` | `work.title_en` / `title_cn` / `original_title` |
| `{year}` | `work.year`（电影）/ `resource.title_year` 回退 |
| `{season}` / `{episode}` | `resource.season` / `episode`（作品单季化后季号回退到 `work.season_number`——季作品即季）；支持格式说明符 `{season:02d}` |
| `{episode_code}` | Plex 季集标识：单集 `s01e01`，单文件多集 `s01e01-e02` |
| `{episode_title}` | `work.episodes` 按集号查得分集标题（v2 快照无 season 分量——所有行都属于 `work.season_number`；v1 旧快照仍按 (season, episode) 匹配），缺失渲染为空段 |
| `{category}` | `plan.category`（电影类别目录；为空时计划落待分类，见上） |
| `{collection}` | `work.collection`（合集显示名）；作品无合集时渲染为空串并**折叠该目录层级**（不产生空分量），供 `{collection}/Season {season:02d}/...` 式上层目录使用 |
| `{resolution}` / `{container}` | `resource.resolution` / `container` |
| `{ext}` | 源文件扩展名（含前导点，如 `.mkv`） |

- **保存时校验**：非法占位符 / 非法格式说明符 → 422；模板渲染结果含绝对路径、`..` 段 → 422。
- **运行时缺数据**（如 TV 模板缺 `season`）→ 规划失败（落 failed 计划行，见"触发链路"）；仅 `episode_title` 与 `collection` 例外——缺失渲染为空段/空串并折叠层级。
- **可选段**：模板文本中的 `[...]` 是可选段标记（如预设的 `[ - {episode_title}]`），段内渲染只剩空白/连字符时整段剔除；边界只在**模板文本**上识别，占位符取值中的方括号（如 `[SubsPlease]` 发布组前缀）按字面量保留，不被二次解释。
- **内置 Plex 兼容预设**（创建规则时可一键填入）：
  - TV：`{title}/Season {season:02d}/{title} - {episode_code}[ - {episode_title}]{ext}`
  - 电影：`{category}/{title} ({year})/{title} ({year}){ext}`
  - 字幕：正片同主名 + `.{lang}[.forced|.sdh|.cc]{ext}`，`lang` 经 `library.subtitle_lang_map`（BCP-47 → Plex 后缀；未命中查主标签，仍不中取主标签本身）；支持 Plex 官方列明的 SRT/SMI/SSA/ASS 以及尽力兼容的 VTT/VobSub (`.idx`+`.sub`)/PGS (`.sup`)，同语言同标记多份字幕第 2 份起追加序号，VobSub 配对文件共享序号。
- 配套预览 API `POST /organize-rules/preview`（见"API"），与 `/agents/rules-preview` 同构：保存前 dry-run 渲染逐文件 src→dst。

### 规划（planner 语义）

规划是纯函数：`build_plan(快照, 磁盘文件列表, 解析后的库根)`——**接口不变**，收的是 service 层已解析好的 `root_path`（volume.mount_path + root_subpath），planner 自身不感知卷模型。沿用 vault-organizer 的文件归类语义：主视频 = 最大视频文件，按模板渲染 move；字幕判定语言与 forced/SDH/CC 标记后同名随正片 move；电影 Blu-ray 转录常见的外置音轨（MKA/AC3/EAC3/DTS/DTSHD/TrueHD/FLAC/AAC/OPUS/WAV 等）因 Plex 无外置电影音轨挂载约定，按原相对路径完整保存至电影目录 `Audio Tracks/` 供后续 remux，避免误识别与数据丢失；其余文件 keep。安全不变量：

- **绝不扫描共享下载根**：优先按 payload `files` 清单定位；清单缺失（RPC 降级）先回退 torrent 文件清单（`_resolve_manifest`：torrent 缓存 → torrent_url 拉取 → 下载器 RPC，过滤绝对路径与 `.`/`..` 分量；.torrent 解析的清单相对于种子根，多文件种子补上 `info/name` 根目录分量以匹配 `download_dir/<根目录>/<文件>` 落盘布局）逐项精确匹配，再退回扫描 `download_dir/torrent_name`（经下载器卷绑定解析后）；皆无 → 规划失败。
- **合集缺集拒绝整理**：合集逐文件解析 (season, episode)（文件名 SxxExx / E09 / EP09 / 第09話 / 裸方括号 `[01]`（含 vN 修订号）→ 目录分量 → `resource.season` → `work.season_number` 回退链（v2 快照：季作品即季，后者兜底）），覆盖度校验「期望集 ⊆ 已解析集」，缺集 / 重复集号 / 无校验依据 → 规划失败，绝不硬猜。期望集来自**这一季作品自身**（作品单季化后不再有逐季 `seasons` 数据）：`episode_start/end` 优先 → 季作品的 Episode 行 / `number_of_episodes` → **本地文件清单推导**（已解析集同季时取 min..max 连续区间，中间缺集仍拒绝——torrent 文件清单本地可缓存，合集范围解析以实际内容为准）→ 皆无才视为无校验依据。解析不出集号的视频按特典 keep。**按 `batch_scope` 分流**：`NULL`/`"season"` 维持上述单季语义；`"multi_season"` 终态经权威文件关联（`file_associations`）**按季作品拆分**（`_plan_same_target_multi_work`：每个关联季作品独立成组，逐组复用单季校验与模板渲染）；无权威关联的 legacy 快照按文件解析季号分组、以本地文件清单推导的 min..max 区间逐组校验（该季已解析集 <2 无法构成区间时只记 warning 跳过该季——多季包边界信息不全，不整个拒绝；不回退 `resource.season`——该 scope 下恒为 NULL）；`"franchise"` 资源四作品 FK 全空（payload.work 为 None），规划直接落 `library_id=null` 的 pending（pending_reason=unclassified，待人工指定库），不进 `_plan_batch`、不抛 PlanError——等成员作品链接成熟后再支持自动整理。
- **冲突预检**：move op 的 dst 已存在且 size 与源不符 → 规划失败（绝不覆盖）；size 相符仍须按文件操作语义验证内容或 inode，不能单凭大小收敛。

## 触发与执行链路

规划挂在 scheduler 每分钟 notify tick 内，**补建通知之后、fan-out 之前**插入 organize 规划步：

1. 整理**常开、无环境变量开关**：只要存在 enabled 规则即对本 tick 的通知做规划；无 enabled 规则时整步跳过（对通知流水线零影响）。
2. consume 本 tick 新建/重建的通知，外加**兜底补扫**：每 tick 限量（50）选取没有任何计划行的存量通知一并规划——规划被意外异常中断（非 PlanError）不留任何行，靠补扫在后续 tick 重试；落库走 SAVEPOINT 吸收并发竞争。
3. **规划被确定性拒绝落 failed 计划行**（缺集/缺季号/文件定位不到/模板缺数据/冲突预检不过等 PlanError）：计划行 `status=failed`、无 ops、`error_message` 记原因——拒绝在变更计划界面可见、可人工处置（重试/取消/分类），这是 vault-organizer「webhook 500 → 退避重投」语义在内置语境的等价物。payload 未变的 failed 计划不随 tick 重复规划（快照一致短路仅限 pending）；被显式触及时（通知 regenerate、配置变更 `replan_open_plans`）即便快照未变也重建——failed 多因磁盘状态等外部条件，快照不变也值得重试。
4. 无规则匹配 → 落「待分类」pending 计划（`library_id=null`）；命中规则的 Library 待绑定 → 落「待绑定」pending 计划。
5. `auto_execute=true` 的规则：计划落库后立即经同一代码路径后台执行（两阶段持久化不变）。
6. 执行完成后按命中规则的 `file_op` 分流清理：
   | file_op | 文件语义 | 执行后清理 |
   |---|---|---|
   | `move` | 移动（EXDEV 退化为 copy+校验+删源） | 任务清理：调内部任务删除 service 函数（与 `DELETE /tasks/{id}?delete_data=false` 同一实现，不再走 HTTP 回环）+ 源目录空目录清理 |
   | `hardlink` | `os.link`，源文件保留；EXDEV/EPERM → op failed + 明确 error_message，**不静默退化为 copy**（静默复制会偷偷翻倍存储并违背保种意图） | 保种：不删任务、不清源目录；恢复快照时停过的做种（`resume_torrent` RPC，与 `POST /tasks/{id}/resume` 同一 RPC，幂等） |
   | `copy` | 独占临时文件复制 + 完整内容校验 + 原子不覆盖发布，源文件保留 | 同 hardlink（保种 + 恢复做种） |
   - 清理/恢复均为 best-effort，失败只记日志不改写计划状态。
   - **媒体服务器刷新**（三种 file_op 一致）：经 `Library → MediaServerInstance` 寻址（天然支持多服务器/多类型），按 adapter 分 type——Plex 优先**按触及目录 partial refresh**（`GET /library/sections/{section_key}/refresh?path=...`），失败或不适用退整库刷新；Emby/Jellyfin 走对应 refresh 端点。服务器停用/未配置/刷新失败一律 best-effort：只记日志，不改写计划状态。

**未执行计划的刷新（replan）**：pending/failed 计划的语义是「按当前规则与库绑定待执行」，因此两类时机都会触发重建（共用 `_rebuild_plan`：人工指定的 library/category 沿用，其余按当前规则 first-match 重路由、op 目标重渲染；done/running/cancelled 不动）：

1. **通知 regenerate**（快照变化）：notify tick / 手动重新生成消费到 payload 已变的通知时自动重建（上方链路）。
2. **配置变更**（快照未变）：规则增删改（`POST/PUT/DELETE /organize-rules`）与媒体库更新（`PUT /libraries/{id}`，卷绑定/根子路径修复）提交后，API 调度**后台任务**经 `replan_open_plans` 对**全部**未执行计划重建（请求不阻塞；进程内并发去重——运行中再次变更只置合并标记，结束后以最新配置补跑一轮）——规则改指库则计划重路由、新建规则收编待分类计划、补绑定后待绑定计划渲染出 ops。**当前规则无一命中时退回「待分类」**（rule_id/library_id 置空、ops 清空，绝不让规则指向停留在已不匹配的旧规则上；人工指定过 library 的计划走合成规则分支、不受规则集变化影响）。附带动作：重建失败只记日志，不影响配置变更本身的响应；无 enabled 规则时整步跳过（规则全禁用不清空既有计划）。

重建均落 `plan_rebuilt` 审计；命中 `auto_execute` 规则的重建与新建一样随后台自动执行。

并发模型：规划与重建在提交处使用版本 CAS；执行与取消使用跨进程共享文件锁，进程内执行仍串行。阻塞文件操作在线程中运行，取消需等待真实线程结束再释放所有权；批量执行逐计划处理，单个失败不影响其余。详见下文「计划版本与执行所有权」。

## 执行器不变量

在 vault-organizer 原始语义上增加完整内容校验及不覆盖发布：

- **执行前状态门禁**：`done` → 幂等短路；`running` 且本进程正在执行 → 拒绝（状态检查与 running 过渡在锁内原子完成，内存态区分「真正执行中」与「崩溃遗留的 running」，后者可重放）；待分类 / 待绑定计划（library 未定、category 未定或目标库未绑定卷）→ 拒绝执行。
- **整计划路径门禁**：planner 在单作品/多作品全部 ops（含最终 movedir）组装后、executor 在执行旧计划前，共用 `organize_file_safety.plan_path_conflicts`。真实父目录路径归一化后，不同操作写同目标、文件/目录目标重叠、目标占其他源、路径循环均拒绝；保留正片先搬出、keep 文件随源目录后移的合法流程。错误带路径与操作序号，不自动合并/改名。
- **前置门禁（precheck）**：逐 op 复核文件与计划快照；任何违例整个计划 failed，不触碰文件。move/copy 源目标都在时必须完整分块比较（无缓存、无浅比较）；比较前后核对 dev/inode/size/mtime/ctime，读取错误或变化失败并保源。hardlink 已有目标必须同 inode。同步 IO 均在线程中执行。
- **幂等状态表**：同路径也须存在且大小正确；同 inode 可证明一致。move 源目标都在且内容一致才删源收敛（删除前再次校验）；不同大小/不同内容一律 failed，绝不以目标为权威删源。copy 保源，hardlink 要求同 inode。只有目标存在时，仅 move 保留大小检查作为旧计划恢复判据（不能事后证明内容，不执行删源）；copy/hardlink 缺保种源则 failed。两者皆无 failed，keep 不触碰。
- **文件移动与发布**：优先 `renameat2(RENAME_NOREPLACE)`，预检后新出现的目标不被覆盖。原生调用因 `ENOSYS/EINVAL/EOPNOTSUPP` 不可用时（本地 Docker ZFS 根已复现），普通文件改用 `link` 原子发布，再校验身份/大小/mtime 与双方 inode，最后删除源名称；目标竞争仍失败，绝不退回可覆盖的 rename。发布后崩溃可留下同 inode 双名称，由既有内容验证恢复；源名称删除与发布并非同一个原子操作。安全链接也不支持时明确 failed，目录不使用该文件回退。跨盘 move（EXDEV）与 copy 先写目标目录中的独占 `.rssripple-*.tmp` 描述符，完整验证且源未变化后再发布；move 发布成功后才删源。失败只清理本次仍属同 inode 的临时文件，保留其他文件；崩溃遗留临时文件不自动当成功——进程借规划 tick 节流（每小时至多一次）回收库根/回收站范围内超宽限年龄（24h）的 `.rssripple-*.tmp` 孤儿文件（只删常规文件，绝不触碰正常文件）。发布（rename/link）成功后对文件本体与父目录 fsync 作持久化屏障；fsync 失败不阻断发布语义（幂等状态表仍可收敛）但记录 warning。
- **执行前路径复查**：持久化旧计划也检查全部源仍在当前下载根内、文件目标在当前库根内、movedir 目标在当前回收站内，禁止操作这些根本身。检查在线程中完成；失败返回 OrganizeError，不执行文件/任务清理。检查可捕获规划后已发生的目录链接替换，不等于描述符级防护，不能承诺抵御与检查/操作同时发生的恶意目录替换。
- **硬链接**：`os.link`，源保留；EXDEV/EPERM 失败且不静默改成 copy。发布后确认同 inode。普通文件符号链接在文件状态门禁被拒绝（不跟随最终文件链接）。
- **后置校验**：全部文件 op 后复核每个 dst 存在且 size 一致；src 已消失仅对 move 校验（hardlink/copy 源文件本应保留）；任一不符 → failed（可修复后重执行，幂等）。
- **movedir**：目录级移动，目标已存在 = 冲突违例，绝不覆盖；平铺在下载根的散文件不产生 movedir；仅 move 语义，hardlink/copy 计划不产 movedir。当前唯一产生场景：**合集（batch）+ move 计划且目标库配置了回收站目录**（`Library.recycle_subpath`，卷内相对路径，媒体库设置「其他设置」表单经文件夹选择器设置；NULL = 默认原地保留）——正片/字幕移走后，种子目录内的剩余文件（特典、附件等 keep 部分）随整个种子目录移入 ``<卷挂载点>/<recycle_subpath>/<种子目录名>``；无 keep 剩余时不产 op（空目录照常自底向上清理）。规划期冲突预检拒绝已存在的回收目标；执行期 movedir 在全部文件 op + 后置校验之后执行，源目录已空视为无需移动。跨盘（EXDEV）不复用 `shutil.move` 的 copytree+删源一把梭，而是复用 copy 的 tmp+校验+原子发布模式逐文件发布，**全部文件就位后才删源**；崩溃半成品经 dst 根标记文件（`.rssripple-movedir.tmp`）识别续传（缺什么补什么、已有文件逐一再校验），无标记的已存在目录按外来目录严格裁决（每个文件须有内容一致的 src 对应，否则冲突拒绝），门禁对「src 仍在且 dst 为目录」不预先拒绝、交给执行期逐文件比对。
- **空目录清理**：`os.walk(topdown=False)` 自底向上 `os.rmdir`（只删空目录，非空自然失败跳过），preserve 边界 = 经下载器卷绑定解析的下载根；**绝不 `rm -rf`**。hardlink/copy 计划恒跳过（源文件保留保种，目录本就不会空）；torrent_name 为空（清单定位的平铺/单文件种子落在共享下载根）同样恒跳过——绝不以共享下载根为清理范围。
- **崩溃恢复**：running 计划只有取得共享文件锁且 file_op 快照完整才可重放（幂等收敛：已移动的视为完成、跨盘 move 发布后遗留 src 仅在完整内容一致时删除、冲突仍 failed）；failed 可反复重试收敛；任一 op failed 计划即 failed，已完成 op 不回滚。

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
| DELETE | `/libraries/{id}` | 删除；仅 **pending/running** 计划阻断（409）；done/failed/cancelled 计划不阻断——删除时解除其 library 引用（显式 UPDATE 置空，方言安全等价于 FK 的 ON DELETE SET NULL），历史计划行保留 |

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
| POST | `/organize/plans/{id}/execute` | 执行单个计划（幂等；done 短路、崩溃遗留 running 可重放、持有共享文件锁的活动计划 拒绝 409 `ALREADY_RUNNING`、待分类/待绑定拒绝）；异步后台执行，返回 202 + 当前状态 |
| POST | `/organize/plans/execute-batch` | 批量执行 `{plan_ids: [...]}` → `{results: [{plan_id, status}]}`；锁内逐个，单个失败不影响其余 |
| POST | `/organize/plans/{id}/classify` | 待分类计划人工指定 `{library_id, category?}`：改写计划并重渲染全部 op 的 dst；pending/failed 可改 |
| POST | `/organize/plans/{id}/cancel` | 取消 pending/failed 及崩溃遗留 running 计划（→ cancelled；done 拒绝 409；持有共享文件锁的活动计划 拒绝 409 `ALREADY_RUNNING`）。可选 body `{delete_task, delete_data}` 附带删除关联下载任务（`delete_data=true` 蕴含删除任务并连同磁盘数据；复用 `task_cleanup` 实现，清理失败不阻断取消，结果随响应 `task_cleaned` 返回） |
| GET | `/organize/audit` | 审计条目分页（`plan_id` 过滤；最新在前） |

## 配置

整理子系统**无环境变量开关**：常开，规划步在存在 enabled 规则时自动激活。

媒体服务器**无全局配置**：`PLEX_URL`/`PLEX_TOKEN` 已移除，服务器地址/凭证全部入库（`media_server_instances`，token 明文对齐 DownloaderInstance.password 惯例）；存量全局 Plex 配置由轻迁移转为一条 MediaServerInstance。逻辑卷、绑定、规则、模板、字幕映射同样全部入库（StorageVolume / MediaServerBinding / OrganizeRule / Library）。

## 前端

- **`/volumes`**：逻辑卷管理（增删改、存在/可写探测结果展示、被引用计数；删除受阻时提示引用来源）。
- **`/media-servers`**：服务器列表（增删改、test 连通性、enabled 开关）+ 扫描按钮 + 库绑定表格（bindings 编辑：服务器路径前缀 → 卷 + 子路径；**待绑定 Library 醒目置前**，引导补绑定后重扫）。
- **Downloader 表单**：原 path_map 输入改为「逻辑卷 Select + 子路径输入」（留空 = 两视角一致）。
- **`/libraries`** 页面取消独立路由（库是扫描产物，在 `/media-servers` 内管理；`subtitle_lang_map` 在库详情编辑）。
- **`/organize`** 不变：计划列表 status 过滤 + 「待分类/待绑定」维度（`pending_reason` 徽标）、详情 Drawer（源/目标逐文件清单 move/keep/movedir 分色 + payload 快照 JSON + audit 时间线）、操作（确认执行 / 勾选批量执行 / 待分类指定 / 取消（可勾选同时删除下载任务、删除磁盘文件）/ failed 重试）。审计 Tab 分页展示 `organize_audit_entries`。
- i18n：zh-CN / en-US 双语，文案键随路由命名空间（`volumes.*` / `mediaServers.*` / `organize.*`）。

## 部署（共享卷与逻辑卷）

- **compose 启动时**把宿主/远程存储挂载进 RSSRipple 容器（如 `/storage/<name>`），**运行时**建逻辑卷记录指向这些挂载点（`StorageVolume.mount_path`）；下载器与媒体服务器的路径差异全部由卷绑定/绑定表消解。
- 下载目录与媒体库尽量落在**同一文件系统/同一 SMB share** 下，使用文件原子不覆盖重命名；跨文件系统触发 EXDEV 复制回退（完整校验会增加多轮 I/O/网络），应避免。
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


### 计划版本与执行所有权

`OrganizeConfiguration` 单例保存全局配置 `revision` 与共享锁目录 `lock_domain`。规则的路由/模板/模式/自动执行、库的绑定/路径/字幕映射/媒体刷新目标、卷挂载点和下载器路径映射变更，在同一配置事务推进 revision（包括 ORM 批量 UPDATE/DELETE）；备注及在线状态不失效计划。管理脚本用原始 SQL 改这些配置时也必须在同事务推进版本。

pending/failed 计划在执行前发现配置版本过期或旧 file_op 缺失时，先按当前配置重建并提交全部 ops，再读取冻结模式执行。重建、分类以旧 revision、pending/failed 状态和当前配置版本 CAS，命中后才同事务替换 ops、快照和审计。不得用新规则的模式执行旧 ops；done/running/cancelled 不被重建覆盖。`rule_id=NULL` 可能来自规则删除，只有 manual_destination 才代表已人工选择目标。

执行与取消都取得 `ORGANIZE_LOCK_DIR` 中该计划的持久文件锁。执行抢占以 revision/状态/配置版本 CAS 写入新 owner_token；完成/失败回写必须同时匹配该 token、revision 与 running。文件锁覆盖文件线程、结果提交及后续任务清理；协程取消必须等实际文件线程结束，不能提前释放锁。DB 断连本身不证明执行者停止，不能仅凭 DB 锁释放或租约超时接管。活动执行期间取消（包括 delete_data）返回 ALREADY_RUNNING，零清理副作用；非活动取消先 CAS 提交 cancelled，再按请求清理。

锁目录使用原子发布的 `.domain` 身份文件并在数据库注册，缺失/不匹配拒绝执行和取消。默认单宿主 Docker 共享 app-data 命名卷满足锁共享；所有 Web/worker 必须使用同一 Linux 文件锁域。目录与锁文件不可在运行中删除、复制或替换，不能把 UUID 相同当作独立文件系统共享锁的证明。跨宿主和未验证 flock 语义的远程挂载不属于该实现支持的所有权部署；见 conventions.md。

配置变更提交后的附带重规划使用独立数据库会话；刷新或回滚不得使配置 API 响应会话的已加载对象失效。

队列上下文中的执行在取得计划锁及进程执行锁后检查队列所有权，文件路径预检结束、预留执行版本前再次检查；已失权的 worker 不得启动新的文件移动。人工入口没有队列上下文时保持原行为。已启动执行仍由计划文件锁、revision/owner_token 及取消收尾协议保护，不能因 Redis 租约失效提前释放文件锁。检查到文件操作启动之间仍有窗口，不宣称 Redis 与文件系统原子提交。

文件结果提交后，进程内执行锁释放，但外层计划文件锁持续覆盖任务清理、恢复做种及媒体刷新。已开始的执行在队列失权后仍须完成该受保护的收尾；不能仅因 Redis 租约结束允许另一执行重入同计划。定向回归用真实文件 move、清理挂起和第二数据库会话验证该顺序，仍需独立 worker 与真实 Redis 的组合验收。
