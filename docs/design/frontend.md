# 前端路由与页面设计

| Route | Page | 内容说明 |
|-------|------|----------|
| `/` | Dashboard | 顶部统计卡（活跃 Agent/活跃频道/下载中/待决策），统计卡可点击跳转至对应列表页；**无待决策时**在统计卡正下方显示一行"暂无待决策事项"提示（不渲染待决策卡片）；**下载代理区块**（统计卡下方）：按进行中任务数排序的 Top4 活跃 Agent，每个 item 占半宽，展示名称（点击跳 `/agents/:id`）、频道（可点击）、下载器、订阅作品数/全频道标记、作品海报缩略图、全部订阅条件摘要（全局条件 + 各作品 `filter_overrides`，作品级条件为蓝色 Tag 并带作品名前缀，超过 6 个折叠为 `+N`）、进行中任务数角标；区块下方列出进行中 Top10 下载任务（作品名 · 资源名 + 进度条 + Agent 链接），无任务时显示默认文案；**活跃下载**区块位于下载代理区块下方，卡片内带 Tab 筛选（所有 / 下载代理管理下载任务 / 未跟踪下载任务），按作品分组卡片（卡片含 poster、作品名、该作品下任务列表；任务行显示资源标题、进度、速度、Agent 与 Channel 链接可点击跳转；下载器中活跃但无 DownloadTask 的种子归入"未跟踪"分组，任务行显示下载器链接而非 Agent 链接；分隔线使用主题 token 以适配 dark 模式）；**待决策**仅在有内容时渲染，标题与边框使用警告橙以区别于其他区块，支持快速 confirm/skip |
| `/works` | WorksPage | 作品仓库海报墙：All/TV/Movie 筛选标签、搜索栏、响应式海报网格，点击跳转至详情页 |
| `/channels` | ChannelList | 频道列表表格（名称/状态/抓取间隔/上次抓取/资源数/Agent 数）；支持新建、编辑、删除、手动抓取 |
| `/channels/new` | ChannelForm | 创建频道表单（URL 验证、自动 LLM 分析）；右侧 RSS 预览 |
| `/channels/:id` | ChannelDetail | 顶部频道信息+抓取控制按钮；主体资源按作品分组展示（每组可折叠，含 poster、作品名、剧集数、最新更新时间）；"未识别"组单独展示，点资源可唤起 metadata 修正抽屉；表格多选 → "生成过滤规则"弹窗（后端调用 summarize-filters，返回建议 FilterConfig，可编辑）→ 可选"新建 Agent"或"应用到已有 Agent" |
| `/channels/:id/edit` | ChannelForm | 编辑频道表单，包含 field_mapping 可视化编辑器、metadata_agent_enabled 开关 |
| `/agents` | AgentList | Agent 列表（名称/频道/下载器/状态/作品数/任务数） |
| `/agents/new` | AgentForm | 创建 Agent：选择 Channel + Downloader；可选填写下载子目录；scope_channel_wide 开关；conflict_resolution（默认 `auto`）；`llm_enabled` 开启时显示 `llm_prompt` 自定义指令输入框；可视化 Filter DSL 编辑器；订阅作品选择器（点击"添加作品"打开对话框，宽 920px，含**剧集/电影/音频**三个 tab：剧集/电影可直接添加，音频 tab 仅供浏览并提示"音频作品暂不支持按作品订阅"（`AgentWork` 仅支持 series/movie），每个 tab 独立分页（每页 10 条、显示总数），空查询展示最新一页，键入 300ms 防抖搜索；从频道的已识别作品中多选，最多 10 个）。保存前先调 `/agents/rules-preview` 预览匹配差异，若有新增/不再匹配资源则弹出 **BackfillPreviewModal** 供用户勾选回填资源（选中 id 作为 `dispatch_resource_ids` 提交）；无匹配影响时直接保存（空回填列表，仍推进水位线） |
| `/agents/:id` | AgentDetail | Tab 布局：订阅作品管理 Tab（列表 / 新增 / 移除 / 编辑 per-work 覆盖；**inline 持久化** —— 新增/移除立即调用 `/agents/{id}/works` 接口，per-work 编辑（filter_overrides / enable_episode_dedup / display_name_override）通过每条作品右下角的"保存"按钮显式提交，脏状态用 `Unsaved changes` 提示；列表级"保存"批量替换 works 时同样走 rules-preview 回填弹窗）；下载任务 Tab（按状态过滤、操作按钮 pause/resume/retry/delete；标题列固定左侧+单行省略+悬浮全名，状态列为纯图标+悬浮文字，进度条与速度/ETA 上下堆叠合并为一列，错误信息以图标按钮+Popover 展示并支持一键复制，操作列固定右侧）；待决策 Tab（confirm/skip/**AI 自动处理** 操作，支持多选批量 skip/ai）；过滤器编辑器 Tab（可视化树形 bool-query 构建器 + 测试面板）；运行控制 Tab（手动 run：**"立即运行"**=增量运行，右侧**"指定起始时间运行"**弹窗选择扫描起始时间（从指定时间开始，默认 14 天前 / 不限制=全量历史），提交 `POST /agents/{id}/run` body `{"scan_since": ISO | null}`；状态轮询、**运行历史**：分页列出每次 run 的计数/状态，指定起始时间的运行带扫描窗口标记，点击单条弹出抽屉展示该次匹配的资源明细） |
| `/downloaders` | DownloaderList | 下载器列表 |
| `/downloaders/new` | DownloaderForm | 创建 Transmission 实例；默认下载目录预填 `/downloads/complete`（mock 类型为 `/tmp/mock-downloads`） |
| `/downloaders/:id` | DownloaderDetail | 连接状态；实时速度与总量统计；Transmission 种子列表（直连 RPC 实时刷新；名称列弹性占宽，状态为图标+Tooltip，速度/ETA/大小合并为"下载信息"列）；本地 DownloadTask 分页（同样式：图标状态，进度条下方内嵌速度与 ETA） |
| `/downloaders/:id/edit` | DownloaderForm | 编辑下载器与默认下载目录（已存值为空时回填 `/downloads/complete`）；"测试连接"按**表单中未保存的当前值**探测（`POST /downloaders/{id}/test` 请求体携带 url/username/password/download_dir 覆盖值，空密码 = 沿用已存密码），带覆盖值的探测不更新下载器 status |
| `/series` | SeriesList | 剧集列表，支持模糊搜索 |
| `/series/:id` | SeriesDetail | 剧集详情，资源列表、任务列表、相关 Agent 列表 |
| `/movies` | MovieList | 电影列表 |
| `/movies/:id` | MovieDetail | 电影详情 |

### 关键交互说明

- **Filter DSL 编辑器**：前端使用树形 UI，支持 AND/OR 节点嵌套、添加/删除/拖拽条件节点；每个字段条件提供字段名下拉（分组展示 String / Number / Bool / List / Enum 五类）、operator 下拉（根据字段类型动态展示可用 operator）、value 输入。所有字段类型的 operator 列表都包含 `is_empty`/`is_not_empty`（"为空/不为空"），选中后不渲染 value 输入；取值操作符的 value 留空时输入框标红，且保存前（AgentForm 提交、Agent 详情全局过滤/作品订阅保存）统一用 `findInvalidConditions` 拦截并提示——空值条件禁止保存（后端创建/更新接口同样 422 校验全局 `filter_config` 与每个作品的 `filter_overrides`）。删光条件后的空树（`{combinator, conditions: []}`）在所有保存/预览 payload 中一律经 `nullIfEmptyFilter` 归一化为 `null`（即"无过滤"），后端不接受空 conditions 列表。字符串字段 + `eq/ne/contains/fuzzy` 会通过 `GET /channels/{id}/field-values?field=&q=` 提供服务端 top-10 频率排序 + 前缀搜索的候选值下拉（保留自由输入），列表字段（`subtitle_langs`）预填 BCP-47 语言代码，布尔字段用 `是/否` Select，枚举字段（`episode_confidence`）从固定 `raw/reconciled/ambiguous/manual` 中选择。提供"测试"按钮调用 `/agents/{id}/test-filters` 实时预览当前频道资源匹配情况。
- **Agent 详情过滤条件合并展示**：第一个 Tab（订阅作品管理）顶部以只读 Tag 形式展示全局 `filter_config`（经 `describeCondition` 渲染为人类可读文本）；每个作品的设置折叠面板内（`WorkSelector`，接收 `globalFilter` prop）在作品自身 `filter_overrides` 编辑器上方展示"与全局过滤合并后"的全局条件，让用户看到生效的完整过滤 = 全局 AND 作品覆盖。
- **Channel 详情多选生成规则**：用户在资源表格勾选若干符合预期的资源，点击"生成过滤规则"，前端将选中 resource_ids 发送到后端 `summarize-filters`，后端按 Agent 规则结构返回建议：选中资源链接的作品列表（作品订阅）、全部资源 ≥80% 同值字段的全局条件、以及每个作品内部 ≥80% 同值的差异化条件（详见 api-endpoints.md）。**弹窗加载时即把生成的全局共性条件按 AND 折叠进每个作品的差异化条件**——生成的规则只存在于作品维度（单个作品时全部条件都落在该作品上），全局规则区块置空、仅供人工编辑。弹窗内：顶部按作品展示订阅卡片（poster/标题/资源数 + 可编辑的作品级 FilterBuilder），下方为仅人工编辑的全局 FilterBuilder；"新建Agent"与"应用到已有 Agent"两种模式行为一致：作品级规则按键合并/追加订阅（已订阅同作品则 AND 合并其 filter_overrides），只有用户人工添加的全局规则才会写入（新建）或按 AND 合并进（应用）目标 Agent 的 `filter_config`；"新建Agent"模式名称预填 `agent-{channel_name}-{YYYY-MM-DD}`，确认后带 filter_config + works 预填跳转新建表单。所选资源全部未关联作品时，生成的共性条件无作品可依附会被丢弃（仍可人工添加全局规则）。
- **资源详情抽屉**：Channel 详情点击资源行打开右侧抽屉，展示 poster（若有）、metadata（作品名、集数）、解析字段明细、磁力链接复制按钮、"修正 metadata"按钮；点击修正进入手动 metadata 流程：输入搜索词+选择类型→查看 LLM 候选→确认→自动刷新该资源及其相关分组。TV 单集资源在集号行右侧提供铅笔按钮，弹出 Popover 手动修正 `episode` / `absolute_episode`，保存后 `episode_confidence="manual"` 并重新触发 Agent 过滤。
- **待决策卡片**：Dashboard 和 Agent 详情的待决策项展示候选资源的核心字段对比（字幕组/分辨率/编码/体积/发布时间），llm_enabled 时展示 LLM 推荐理由并高亮 `llm_picked_resource_id` 对应行；点击候选选中，点击"确认"提交。每条决策可点"AI 自动处理"（`POST /decisions/{id}/ai-pick`）一键采纳 LLM 选择；列表支持多选后批量 skip / AI 处理（`POST /agents/{id}/decisions/batch`）。集号不确定类决策（reason 以"集号不确定"开头）不展示候选对比，引导用户去资源详情修正集号。

---

