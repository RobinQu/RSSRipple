# RSS Downloader - AI Coding Agent Guide

## Project Overview

RSS Downloader 是一个自动化番剧/影视 RSS 订阅下载服务。核心功能：
1. 定时拉取 RSS 订阅源（如 mikanani.me）
2. 智能解析资源标题（字幕组、分辨率、编码、格式等）
3. 规则过滤 + LLM 辅助决策，确保每集只下载一个最佳版本
4. 推送下载任务至 Transmission
5. Web UI 管理频道、Agent、过滤器和下载进度

## Tech Stack

- **Backend**: Python 3.12, FastAPI, SQLAlchemy 2.0 (async), Pydantic v2
- **Database**: SQLite with aiosqlite
- **RSS**: feedparser
- **Transmission**: transmission-rpc (Python client)
- **Scheduler**: APScheduler (AsyncIOScheduler)
- **Frontend**: React 18, TypeScript, TailwindCSS, Vite
- **Container**: Docker, docker-compose
- **Testing**: pytest, pytest-asyncio, httpx

## Project Structure

```
rss-downloader/
├── app/                        # Backend application
│   ├── main.py                 # FastAPI app entry
│   ├── config.py               # Settings (pydantic-settings)
│   ├── database.py             # SQLAlchemy engine/session
│   ├── models/                 # SQLAlchemy ORM models
│   ├── schemas/                # Pydantic request/response schemas
│   ├── api/v1/                 # API route handlers
│   ├── services/               # Business logic
│   ├── clients/                # External API clients
│   └── utils/                  # Utilities
├── frontend/                   # React frontend
│   └── src/
├── tests/                      # Test suite
│   ├── unit/
│   ├── api/
│   └── integration/
├── docker-compose.yml
├── docker-compose.test.yml
├── Dockerfile
├── requirements.txt
├── DESIGN.md
├── ARCHITECTURE.md
└── AGENTS.md
```

## Key Design Documents

- `DESIGN.md` - Data models, filter logic, UI wireframes, user stories
- `ARCHITECTURE.md` - System architecture, module structure, Docker setup, testing strategy

## API Endpoints (JSON-RPC Style)

All endpoints under `/api/v1/`. Request/response bodies are JSON.

### Dashboard

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/dashboard` | 获取概览数据（活跃Agent、下载任务、待决策项） |

### Channels（订阅频道）

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/channels` | 列出所有频道（支持分页） |
| POST | `/api/v1/channels` | 创建频道 |
| GET | `/api/v1/channels/{id}` | 获取频道详情 |
| PUT | `/api/v1/channels/{id}` | 更新频道 |
| DELETE | `/api/v1/channels/{id}` | 删除频道 |
| POST | `/api/v1/channels/{id}/fetch` | 手动触发 RSS 拉取 |
| POST | `/api/v1/channels/validate-url` | 验证 RSS URL 可达性 |

### Agents（智能代理）

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/agents` | 列出所有 Agent（支持分页） |
| POST | `/api/v1/agents` | 创建 Agent |
| GET | `/api/v1/agents/{id}` | 获取 Agent 详情 |
| PUT | `/api/v1/agents/{id}` | 更新 Agent |
| DELETE | `/api/v1/agents/{id}` | 删除 Agent |
| POST | `/api/v1/agents/{id}/run` | 手动触发 Agent 处理 |

### Resource Filters（资源过滤器）

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/agents/{agent_id}/filters` | 获取 Agent 的过滤器列表 |
| POST | `/api/v1/agents/{agent_id}/filters` | 添加过滤器 |
| PUT | `/api/v1/agents/{agent_id}/filters/{id}` | 更新过滤器 |
| DELETE | `/api/v1/agents/{agent_id}/filters/{id}` | 删除过滤器 |

### Downloader Instances（下载器实例）

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/downloaders` | 列出所有下载器实例 |
| POST | `/api/v1/downloaders` | 创建下载器实例 |
| GET | `/api/v1/downloaders/{id}` | 获取下载器详情 |
| PUT | `/api/v1/downloaders/{id}` | 更新下载器 |
| DELETE | `/api/v1/downloaders/{id}` | 删除下载器 |
| POST | `/api/v1/downloaders/{id}/test` | 测试下载器连接 |

### Download Tasks（下载任务）

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/agents/{agent_id}/tasks` | 获取 Agent 的下载任务（分页） |
| GET | `/api/v1/tasks/{id}` | 获取任务详情 |
| POST | `/api/v1/tasks/{id}/pause` | 暂停任务 |
| POST | `/api/v1/tasks/{id}/resume` | 恢复任务 |
| POST | `/api/v1/tasks/{id}/retry` | 重试任务 |
| DELETE | `/api/v1/tasks/{id}` | 删除任务 |

### Pending Decisions（待决策项）

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/agents/{agent_id}/decisions` | 获取待决策列表（分页） |
| POST | `/api/v1/decisions/{id}/confirm` | 确认选择某个候选资源 |
| POST | `/api/v1/decisions/{id}/skip` | 跳过该决策 |

### File Resources（资源条目）

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/channels/{channel_id}/resources` | 获取频道的资源列表（分页） |
| GET | `/api/v1/resources/{id}` | 获取资源详情 |

## Request/Response Examples

### Create Channel

```json
// POST /api/v1/channels
// Request:
{
  "name": "我的番组订阅",
  "type": "rss_feed",
  "url": "https://mikanani.me/RSS/MyBangumi?token=xxx",
  "fetch_interval": 1800
}

// Response:
{
  "success": true,
  "data": {
    "id": "550e8400-e29b-41d4-a716-446655440000",
    "name": "我的番组订阅",
    "type": "rss_feed",
    "url": "https://mikanani.me/RSS/MyBangumi?token=xxx",
    "fetch_interval": 1800,
    "status": "active",
    "last_fetched_at": null,
    "created_at": "2026-06-21T10:00:00Z"
  }
}
```

### Create Agent

```json
// POST /api/v1/agents
// Request:
{
  "name": "番剧自动下载",
  "channel_id": "550e8400-e29b-41d4-a716-446655440000",
  "downloader_id": "660e8400-e29b-41d4-a716-446655440001",
  "download_dir": "/downloads/anime",
  "task_expire_days": 30,
  "llm_enabled": true,
  "filters": [
    {"field": "resolution", "operator": "eq", "value": "1080p", "priority": 10, "is_required": true},
    {"field": "subtitle_group", "operator": "eq", "value": "LoliHouse", "priority": 20, "is_required": false},
    {"field": "container", "operator": "eq", "value": "MKV", "priority": 5, "is_required": false}
  ]
}
```

### Confirm Pending Decision

```json
// POST /api/v1/decisions/{id}/confirm
// Request:
{
  "resource_id": "770e8400-e29b-41d4-a716-446655440002"
}

// Response:
{
  "success": true,
  "data": {
    "id": "...",
    "status": "decided",
    "decided_resource_id": "770e8400-e29b-41d4-a716-446655440002",
    "decided_at": "2026-06-21T10:30:00Z"
  }
}
```

## RSS Title Parsing Rules

Title format: `[SubtitleGroup] ChineseName / EnglishName - Episode [Quality][Codec][Subtitle][Container]`

Key fields to extract:
- `subtitle_group`: first bracketed content
- `title_cn`: Chinese name before ` / `
- `title_en`: English name after ` / ` and before ` - `
- `episode`: number after ` - `
- `resolution`: e.g., `1080p`, `720p`
- `source`: e.g., `WebRip`, `WEB-DL`
- `video_codec`: e.g., `HEVC-10bit`, `AVC`, `H264`
- `audio_codec`: e.g., `AAC`, `FLAC`
- `subtitle_type`: e.g., `简繁内封字幕`, `CHT`, `CHS`
- `container`: e.g., `MP4`, `MKV`

## Coding Conventions

- All async where possible (FastAPI, SQLAlchemy, httpx)
- Pydantic v2 for all data validation
- UUID primary keys for all entities
- ISO 8601 datetime strings in API responses
- Structured error responses with error codes
- Type hints everywhere
- Docstrings on service layer functions
- pytest fixtures for test data

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| DATABASE_URL | sqlite+aiosqlite:///data/rss_downloader.db | Database connection |
| LLM_API_KEY | (empty) | OpenAI API key for LLM decisions |
| LLM_MODEL | gpt-4o-mini | LLM model name |
| LLM_BASE_URL | https://api.openai.com/v1 | LLM API base URL |
| DEFAULT_FETCH_INTERVAL | 1800 | Default RSS fetch interval (seconds) |
| MAX_RETRY_COUNT | 3 | Max download retry count |
| TASK_EXPIRE_DAYS | 30 | Completed task expiry days |
| LOG_LEVEL | INFO | Logging level |
| DEBUG | false | Debug mode |

## Running the Project

```bash
# Development
pip install -r requirements.txt
cd frontend && npm install && npm run build && cd ..
uvicorn app.main:app --reload --port 8000

# Docker
docker-compose up --build

# Tests
pytest tests/unit -v
pytest tests/api -v
docker-compose -f docker-compose.test.yml up --build --abort-on-container-exit
```
