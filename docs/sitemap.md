# RSSRipple 站点信息结构（Sitemap）

本文档整理前端全部路由页面及其信息层级，并记录每个页面的浏览器标签标题（`document.title`）。
路由定义见 `frontend/src/App.tsx`；标题由 `frontend/src/hooks/useDocumentTitle.ts` 统一设置，
格式为 `<页面标题> - RSSRipple`，随界面语言（zh-CN / en-US）切换。

## 信息结构总览

```
/ 仪表盘 (Dashboard)
/works 作品仓库 (Works)
├── /series/:id 剧集详情
├── /movies/:id 电影详情
└── /audio-works/:id 音频作品详情
/channels 频道
├── /channels/new 新建频道
├── /channels/:id 频道详情
└── /channels/:id/edit 编辑频道
/agents 下载代理
├── /agents/new 新建 Agent
├── /agents/:id Agent 详情
└── /agents/:id/edit 编辑 Agent
/downloaders 下载器
├── /downloaders/new 新建下载器
├── /downloaders/:id 下载器详情
└── /downloaders/:id/edit 编辑下载器
/settings 系统设置
```

旧的 `/series`、`/movies` 列表页已移除，访问会 301 重定向（`<Navigate replace>`）到 `/works`；
作品详情页路由保留。

## 页面清单与浏览器标题

| 路由 | 页面组件 | 页面内容 | 浏览器标题（zh-CN） | 标题来源（i18n key / 数据） |
|------|----------|----------|---------------------|------------------------------|
| `/` | `Dashboard` | 仪表盘：下载统计、待决策、活跃 Agent 与任务 | 仪表盘 - RSSRipple | `nav.dashboard` |
| `/works` | `WorksPage` | 作品仓库：剧集/电影/音频统一列表，支持搜索与批量刷新 | 作品仓库 - RSSRipple | `works.title` |
| `/series/:id` | `SeriesDetail` | 剧集详情：元数据、集列表、资源与任务 | `<剧集名> - RSSRipple` | `title_cn \|\| title_en \|\| original_title`，未加载时为 `series.title` |
| `/movies/:id` | `MovieDetail` | 电影详情：元数据、资源与任务 | `<电影名> - RSSRipple` | 同上，未加载时为 `movies.title` |
| `/audio-works/:id` | `AudioWorkDetail` | 音频作品详情：元数据与资源 | `<作品名> - RSSRipple` | 同上，未加载时为 `works.title` |
| `/channels` | `Channels` | 频道列表：抓取状态、手动抓取、删除 | 频道 - RSSRipple | `channels.title` |
| `/channels/new` | `ChannelForm`（create） | 新建频道：URL 校验、元数据源、字段映射 | 新建频道 - RSSRipple | `channels.newChannel` |
| `/channels/:id/edit` | `ChannelForm`（edit） | 编辑频道配置 | 编辑频道 - RSSRipple | `channels.editChannel` |
| `/channels/:id` | `ChannelDetail` | 频道详情：已解析/未解析资源、作品分组、元数据修正 | `<频道名> - RSSRipple` | `channel.name`，未加载时为 `channels.title` |
| `/agents` | `Agents` | 下载代理列表：状态、手动运行、删除 | 下载代理 - RSSRipple | `agents.title` |
| `/agents/new` | `AgentForm`（create） | 新建 Agent：过滤规则、回填预览 | 新建 Agent - RSSRipple | `agents.newAgent` |
| `/agents/:id/edit` | `AgentForm`（edit） | 编辑 Agent（保存前走 rules-preview 回填流程） | 编辑 Agent - RSSRipple | `agents.editAgent` |
| `/agents/:id` | `AgentDetail` | Agent 详情：订阅作品、任务、待决策、运行记录 | `<Agent 名> - RSSRipple` | `agent.name`，未加载时为 `agents.title` |
| `/downloaders` | `Downloaders` | 下载器列表：连通状态、测试连接 | 下载器 - RSSRipple | `downloaders.title` |
| `/downloaders/new` | `DownloaderForm`（create） | 新建下载器（Transmission / Mock） | 添加下载器 - RSSRipple | `downloaders.addDownloader` |
| `/downloaders/:id/edit` | `DownloaderForm`（edit） | 编辑下载器配置 | 编辑下载器 - RSSRipple | `downloaders.editDownloader` |
| `/downloaders/:id` | `DownloaderDetail` | 下载器详情：Transmission 种子、本地任务记录 | `<下载器名> - RSSRipple` | `dl.name`，未加载时为 `downloaders.title` |
| `/settings` | `SettingsPage` | 系统设置：LLM API、外部搜索数据源等 | 系统设置 - RSSRipple | `settings.title` |

## 实现说明

- `useDocumentTitle(title)` 在 `useEffect` 中写入 `document.title`，依赖 `title` 与
  `i18n.language`，因此数据加载完成（详情页名称）或切换语言时标题会自动更新。
- 详情页在数据未加载完成时先显示所属栏目的通用标题，加载完成后替换为实体名称。
- 新增页面时应：在 `App.tsx` 注册路由 → 在页面组件顶部调用 `useDocumentTitle` → 更新本文档。
