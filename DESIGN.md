# RSS Downloader - Design Document

## 1. Overview

RSS Downloader 是一个自动化的 RSS 订阅下载服务，专注于番剧/影视内容的智能筛选与自动下载。通过 RSS 订阅源获取资源信息，利用规则过滤和 LLM 辅助决策进行智能匹配，最终将下载任务推送至 Transmission 下载器。

## 2. Core Problem

在番剧 RSS 订阅中，同一节目会有多个字幕组同时发布不同版本（不同分辨率、编码、封装格式）。用户希望：
- 同一节目仅下载一个版本
- 不同分集尽量保持同一字幕组（一致性）
- 当无法自动决策时，由用户手动选择

## 3. User Stories

| # | 角色 | 故事 | 验收标准 |
|---|------|------|----------|
| US-1 | 用户 | 创建 RSS 订阅频道 | 输入 RSS URL，系统验证可达性并展示预览 |
| US-2 | 用户 | 配置 Agent 的过滤规则 | 设置字幕组、分辨率、格式等偏好，Agent 按规则筛选 |
| US-3 | 用户 | 查看下载进度 | 首页展示活跃任务的下载速度、进度、状态 |
| US-4 | 用户 | 手动选择下载版本 | 当出现歧义时，系统展示候选列表供用户选择 |
| US-5 | 用户 | 管理 Transmission 实例 | 添加/测试/修改 Transmission API 连接 |
| US-6 | 系统 | 自动匹配并保持字幕组一致性 | 新一集自动匹配上一集的字幕组和参数 |
| US-7 | 系统 | LLM 辅助决策 | 规则无法唯一匹配时调用 LLM 进行智能判断 |

## 4. RSS Feed Analysis

### 4.1 Mikanani RSS 结构

```xml
<rss version="2.0">
  <channel>
    <title>Mikan Project - 我的番组</title>
    <item>
      <guid isPermaLink="false">[字幕组] 标题 - EP [质量信息]</guid>
      <link>https://mikanani.me/Home/Episode/{hash}</link>
      <title>[字幕组] 中文名 / English Name - EP## [WebRip 1080p HEVC-10bit AAC][字幕]</title>
      <description>同标题 + [文件大小]</description>
      <torrent xmlns="https://mikanani.me/0.1/">
        <link>https://mikanani.me/Home/Episode/{hash}</link>
        <contentLength>718442304</contentLength>
        <pubDate>2026-06-21T09:40:23.901</pubDate>
      </torrent>
      <enclosure type="application/x-bittorrent" length="718442304"
        url="https://mikanani.me/Download/20260621/{hash}.torrent" />
    </item>
  </channel>
</rss>
```

### 4.2 标题解析规则

标题格式（正则分组）：
```
[字幕组名] 中文名 / 英文名 - 集数 [分辨率][编码][字幕类型][封装格式]
```

解析字段：
| 字段 | 示例 | 说明 |
|------|------|------|
| `subtitle_group` | `LoliHouse`, `Skymoon-Raws`, `ANi` | 首对方括号内容 |
| `title_cn` | `黄泉使者` | 中文名 |
| `title_en` | `Yomi no Tsugai` / `Daemons of the Shadow Realm` | 英文名 |
| `episode` | `12` | 集数 |
| `resolution` | `1080p`, `1080P`, `720p` | 分辨率 |
| `source` | `WebRip`, `WEB-DL`, `Baha`, `ViuTV` | 来源 |
| `video_codec` | `HEVC-10bit`, `AVC`, `H264` | 视频编码 |
| `audio_codec` | `AAC`, `FLAC` | 音频编码 |
| `subtitle_type` | `简繁内封字幕`, `CHT`, `CHS` | 字幕类型 |
| `container` | `MP4`, `MKV` | 封装格式 |
| `file_size` | `685.16 MB` | 文件大小（从 description 提取）|

### 4.3 资源链接

- `enclosure.url`：指向 `.torrent` 文件下载链接
- `torrent.contentLength`：文件大小（字节）
- 磁力链接需要从 `.torrent` 文件或详情页面获取（Transmission 支持直接添加 `.torrent` URL）

## 5. Data Model Design

### 5.1 Entity Relationship

```
Channel 1──N FileResource
Channel 1──N Agent
Agent   1──N ResourceFilter
Agent   1──1 DownloaderInstance
Agent   1──N DownloadTask
FileResource N──1 Episode
Episode N──1 TVSeries
FileResource N──1 Movie
DownloadTask N──1 FileResource
DownloadTask N──1 Agent
```

### 5.2 Core Entities

#### Channel（订阅频道）
| 字段 | 类型 | 说明 |
|------|------|------|
| id | UUID | 主键 |
| name | str | 频道名称 |
| type | enum | 频道类型（当前仅 `rss_feed`） |
| url | str | RSS 订阅 URL |
| fetch_interval | int | 拉取间隔（秒） |
| last_fetched_at | datetime | 上次拉取时间 |
| status | enum | `active`, `inactive`, `error` |
| created_at | datetime | 创建时间 |
| updated_at | datetime | 更新时间 |

#### FileResource（资源条目）
| 字段 | 类型 | 说明 |
|------|------|------|
| id | UUID | 主键 |
| channel_id | UUID | 所属频道 |
| guid | str | RSS 条目 GUID |
| title_raw | str | 原始标题 |
| title_cn | str? | 解析-中文名 |
| title_en | str? | 解析-英文名 |
| subtitle_group | str? | 解析-字幕组 |
| episode | int? | 解析-集数 |
| resolution | str? | 解析-分辨率 |
| source | str? | 解析-来源 |
| video_codec | str? | 解析-视频编码 |
| audio_codec | str? | 解析-音频编码 |
| subtitle_type | str? | 解析-字幕类型 |
| container | str? | 解析-封装格式 |
| file_size | int? | 文件大小（字节） |
| torrent_url | str | .torrent 下载链接 |
| detail_url | str | 详情页面链接 |
| published_at | datetime | 发布时间 |
| parsed_at | datetime | 解析时间 |
| episode_id | UUID? | 关联剧集 |
| movie_id | UUID? | 关联电影 |
| created_at | datetime | 创建时间 |

#### Episode（剧集）
| 字段 | 类型 | 说明 |
|------|------|------|
| id | UUID | 主键 |
| series_id | UUID | 所属剧集系列 |
| episode_number | int | 集数 |
| title | str? | 集标题 |
| air_date | date? | 播出日期 |
| preferred_profile_id | UUID? | 首选下载配置（来自历史选择） |
| created_at | datetime | 创建时间 |

#### TVSeries（剧集系列）
| 字段 | 类型 | 说明 |
|------|------|------|
| id | UUID | 主键 |
| title_cn | str? | 中文名 |
| title_en | str? | 英文名 |
| aliases | list[str] | 别名列表 |
| external_id | str? | 外部数据库 ID（如 TMDB） |
| external_source | str? | 外部数据源名称 |
| description | str? | 剧情简介 |
| genre | list[str] | 分类标签 |
| start_date | date? | 开播日期 |
| created_at | datetime | 创建时间 |
| updated_at | datetime | 更新时间 |

#### Movie（电影）
| 字段 | 类型 | 说明 |
|------|------|------|
| id | UUID | 主键 |
| title_cn | str? | 中文名 |
| title_en | str? | 英文名 |
| aliases | list[str] | 别名列表 |
| external_id | str? | 外部数据库 ID |
| external_source | str? | 外部数据源名称 |
| description | str? | 简介 |
| release_date | date? | 上映日期 |
| created_at | datetime | 创建时间 |

#### ResourceFilter（资源过滤器）
| 字段 | 类型 | 说明 |
|------|------|------|
| id | UUID | 主键 |
| agent_id | UUID | 所属 Agent |
| field | enum | 匹配字段：`subtitle_group`, `resolution`, `container`, `video_codec`, `audio_codec`, `subtitle_type`, `source`, `title_cn`, `title_en` |
| operator | enum | 操作符：`eq`（精确匹配）, `contains`（包含）, `fuzzy`（模糊匹配）, `in`（在列表中）, `regex`（正则） |
| value | str | 匹配值 |
| priority | int | 优先级（越高越优先） |
| is_required | bool | 是否为必要条件（false 为加分条件） |
| created_at | datetime | 创建时间 |

#### Agent（智能代理）
| 字段 | 类型 | 说明 |
|------|------|------|
| id | UUID | 主键 |
| name | str | Agent 名称 |
| channel_id | UUID | 关联频道 |
| downloader_id | UUID | 关联下载器实例 |
| download_dir | str? | 下载保存目录 |
| task_expire_days | int | 已完成任务过期天数 |
| llm_enabled | bool | 是否启用 LLM 辅助决策 |
| status | enum | `active`, `paused`, `error` |
| last_run_at | datetime | 上次运行时间 |
| created_at | datetime | 创建时间 |
| updated_at | datetime | 更新时间 |

#### DownloaderInstance（下载器实例）
| 字段 | 类型 | 说明 |
|------|------|------|
| id | UUID | 主键 |
| name | str | 实例名称 |
| type | enum | 下载器类型（当前仅 `transmission`） |
| url | str | API 地址（如 `http://localhost:9091/transmission/rpc`） |
| username | str? | 认证用户名 |
| password | str? | 认证密码 |
| status | enum | `connected`, `disconnected`, `error` |
| last_checked_at | datetime | 上次连接检测时间 |
| created_at | datetime | 创建时间 |
| updated_at | datetime | 更新时间 |

#### DownloadTask（下载任务）
| 字段 | 类型 | 说明 |
|------|------|------|
| id | UUID | 主键 |
| agent_id | UUID | 所属 Agent |
| file_resource_id | UUID | 关联资源 |
| downloader_id | UUID | 使用的下载器 |
| transmission_torrent_id | int? | Transmission 内部 torrent ID |
| status | enum | `pending`, `queued`, `downloading`, `paused`, `completed`, `error`, `cancelled` |
| progress | float | 下载进度（0-100） |
| download_speed | int | 下载速度（bytes/s） |
| eta | int? | 预计剩余时间（秒） |
| error_message | str? | 错误信息 |
| retry_count | int | 重试次数 |
| max_retries | int | 最大重试次数 |
| confirmed_at | datetime? | 确认下载时间 |
| completed_at | datetime? | 完成时间 |
| created_at | datetime | 创建时间 |
| updated_at | datetime | 更新时间 |

#### PendingDecision（待决策项）
| 字段 | 类型 | 说明 |
|------|------|------|
| id | UUID | 主键 |
| agent_id | UUID | 所属 Agent |
| episode_id | UUID? | 关联剧集 |
| movie_id | UUID? | 关联电影 |
| candidates | list[UUID] | 候选 FileResource ID 列表 |
| reason | str | 需要决策的原因 |
| llm_suggestion | str? | LLM 建议 |
| decided_resource_id | UUID? | 用户选中的资源 |
| status | enum | `pending`, `decided`, `expired`, `skipped` |
| created_at | datetime | 创建时间 |
| decided_at | datetime? | 决策时间 |

## 6. Filter Logic Design

### 6.1 Three-Tier Resolution

```
Tier 1: Rule-based Filter（规则过滤）
  ↓ 唯一匹配 → 直接下载
  ↓ 多个匹配 → 进入 Tier 2
  ↓ 无匹配 → 跳过（或标记为未匹配）

Tier 2: LLM Decision（LLM 辅助决策）
  ↓ LLM 选择 → 下载选定项
  ↓ LLM 无法决定 → 进入 Tier 3
  ↓ LLM 调用失败 → 进入 Tier 3

Tier 3: Human Decision（人工决策）
  ↓ 展示候选列表 → 用户选择
  ↓ 用户选择 → 下载
  ↓ 用户跳过 → 标记为跳过
```

### 6.2 Consistency Matching

为保持字幕组一致性，系统维护 `EpisodeProfile`（每集偏好配置）：

```python
class EpisodeProfile:
    subtitle_group: str      # 上次选择的字幕组
    resolution: str          # 上次选择的分辨率
    container: str           # 上次选择的封装格式
    video_codec: str         # 上次选择的编码
    subtitle_type: str       # 上次选择的字幕类型
```

匹配流程：
1. 从同系列上一集获取 `EpisodeProfile`
2. 用 profile 字段作为加分条件（非硬性过滤）
3. 得分最高的候选被选中
4. 若得分相同，进入 Tier 2/3

### 6.3 Title Fuzzy Matching

节目名称匹配策略：
1. 精确匹配 `title_cn` 或 `title_en`
2. 别名匹配（TVSeries.aliases）
3. 编辑距离/模糊匹配（Levenshtein ratio > 0.7）
4. 外部数据源关联（TMDB/Bangumi API）

## 7. Agent Workflow

```
┌─────────────┐
│  RSS Fetch   │ ← 定时触发 / 手动触发
│  (Channel)   │
└──────┬──────┘
       │ new FileResources
       ▼
┌─────────────┐
│ Parse &     │ ← 解析标题字段
│ Classify    │ ← 关联 Episode/Series
└──────┬──────┘
       │ classified resources
       ▼
┌─────────────┐
│ Rule Filter │ ← 应用 ResourceFilter 规则
│ (Tier 1)    │
└──────┬──────┘
       │
   ┌───┴───┐
   │unique?│
   ├──yes──┤──no──┐
   ▼       │      ▼
┌──────┐   │  ┌──────────┐
│Enqueue│   │  │LLM Judge │ ← Tier 2
│(TR)  │   │  │          │
└──────┘   │  └────┬─────┘
           │       │
           │   ┌───┴───┐
           │   │decided?│
           │   ├──yes──┤──no──┐
           │   ▼       │      ▼
           │┌──────┐   │  ┌──────────┐
           ││Enqueue│   │  │Human     │ ← Tier 3
           ││(TR)  │   │  │Decision  │
           │└──────┘   │  └────┬─────┘
           │           │       │
           │           │   ┌───┴───┐
           │           │   │chosen?│
           │           │   ├──yes──┤──no/skip──┐
           │           │   ▼       │            ▼
           │           │┌──────┐   │       ┌───────┐
           │           ││Enqueue│   │       │Mark   │
           │           ││(TR)  │   │       │Skipped│
           │           │└──────┘   │       └───────┘
           │           │           │
           └───────────┴───────────┘
```

## 8. UI Information Architecture

### 8.1 Pages

| 页面 | 路由 | 描述 |
|------|------|------|
| Dashboard | `/` | 活跃 Agent + 活跃下载任务概览 |
| Channels | `/channels` | 频道列表 |
| Channel Create | `/channels/new` | 创建频道表单 |
| Channel Detail | `/channels/:id` | 频道详情（模态对话框） |
| Downloaders | `/downloaders` | 下载器实例列表 |
| Downloader Create | `/downloaders/new` | 创建下载器表单 |
| Agents | `/agents` | Agent 列表 |
| Agent Create | `/agents/new` | 创建 Agent 表单 |
| Agent Detail | `/agents/:id` | Agent 详情：下载任务 + 待确认项 |

### 8.2 Dashboard Layout

```
┌─────────────────────────────────────────────────────┐
│  RSS Downloader                          [Settings] │
├─────────────────────────────────────────────────────┤
│                                                      │
│  Active Agents (3)                                   │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐             │
│  │ Agent-1  │ │ Agent-2  │ │ Agent-3  │             │
│  │ 3 tasks  │ │ 1 task   │ │ 5 tasks  │             │
│  │ ▼ 12MB/s │ │ ▼ 3MB/s  │ │ ▼ 25MB/s │             │
│  └──────────┘ └──────────┘ └──────────┘             │
│                                                      │
│  Active Downloads                                    │
│  ┌───────────────────────────────────────────────┐   │
│  │ [LoliHouse] 黄泉使者 EP12          78% ▼5MB/s│   │
│  │ ████████████████████░░░░░░  ETA: 2min         │   │
│  ├───────────────────────────────────────────────┤   │
│  │ [ANi] 葬送的芙莉莲 EP24            45% ▼8MB/s│   │
│  │ ███████████░░░░░░░░░░░░░░░  ETA: 5min         │   │
│  └───────────────────────────────────────────────┘   │
│                                                      │
│  Pending Decisions (2)                   [View All]  │
│  ┌───────────────────────────────────────────────┐   │
│  │ ⚠ 黄泉使者 EP12 - 3 candidates found         │   │
│  │ ⚠ 药屋少女 EP18 - 2 candidates found         │   │
│  └───────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────┘
```

## 9. Technology Stack

| 层级 | 技术 | 说明 |
|------|------|------|
| Backend Framework | FastAPI | 异步 Python Web 框架 |
| Database | SQLite + SQLAlchemy | 轻量级持久化，支持异步 |
| ORM | SQLAlchemy 2.0 (async) | 异步 ORM |
| Data Validation | Pydantic v2 | 数据模型验证 |
| RSS Parsing | feedparser | RSS/Atom 解析 |
| Transmission | transmission-rpc | Transmission API 客户端 |
| Task Scheduling | APScheduler | 定时任务调度 |
| Frontend | React + TailwindCSS | 单页面应用 |
| Build Tool | Vite | 前端构建工具 |
| Container | Docker + docker-compose | 容器化部署 |
| Testing | pytest + httpx | 单元/集成测试 |

## 10. Non-Functional Requirements

| 要求 | 标准 |
|------|------|
| RSS 拉取频率 | 可配置，默认 30 分钟 |
| 下载重试 | 最多 3 次，指数退避 |
| 数据持久化 | SQLite 文件挂载到 volume |
| 容器化 | 单容器或前后端分离均可 |
| 日志 | 结构化 JSON 日志 |
| API 版本 | 所有 API 路径前缀 `/api/v1/` |
