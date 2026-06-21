# RSSRipple - AI Coding Agent Guide

## Project Overview

RSSRipple 是一个自动化番剧/影视 RSS 订阅下载服务。核心功能：
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
rssripple/
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
├── pyproject.toml
├── uv.lock
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
| POST | `/api/v1/channels/{id}/analyze` | LLM 分析 RSS 源生成字段映射 |
| POST | `/api/v1/channels/{id}/apply-mapping` | 应用字段映射到频道 |
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
| POST | `/api/v1/agents/{id}/test-filters` | 测试过滤器对频道资源的匹配结果 |

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

### TVSeries（剧集系列）

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/series` | 列出所有剧集系列（分页） |
| POST | `/api/v1/series` | 创建剧集系列 |
| GET | `/api/v1/series/{id}` | 获取剧集系列详情 |
| PUT | `/api/v1/series/{id}` | 更新剧集系列 |
| DELETE | `/api/v1/series/{id}` | 删除剧集系列 |

### Movies（电影）

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/movies` | 列出所有电影（分页） |
| POST | `/api/v1/movies` | 创建电影 |
| GET | `/api/v1/movies/{id}` | 获取电影详情 |
| PUT | `/api/v1/movies/{id}` | 更新电影 |
| DELETE | `/api/v1/movies/{id}` | 删除电影 |

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

## RSS Parsing Architecture

### Dynamic Field Mapping (per-channel)

Different RSS sources (mikanani.me, dmhy.org, myrss.org, etc.) have varying title formats and XML structures. The system uses a **dynamic field mapping** approach:

1. When creating a Channel, call `POST /channels/{id}/analyze` to have the LLM analyze sample RSS entries
2. The LLM generates a `field_mapping` JSON that describes how to extract each FileResource field
3. Review the proposed mapping, then apply it via `POST /channels/{id}/apply-mapping`
4. Subsequent RSS fetches use the channel's field_mapping via `resource_parser.py`

**Field mapping rule structure:**
```json
{
  "title_cn": {"source": "title", "regex": "\\]\\s*(.+?)\\s*/", "group": 1},
  "episode": {"source": "title", "regex": "-\\s*(\\d+)\\b", "group": 1, "transform": "int"},
  "torrent_url": {"source": "enclosures[0].url"},
  "file_size": {"source": "enclosures[0].length", "transform": "int"}
}
```

- `source`: dotted path into feedparser entry dict (e.g., `title`, `enclosures[0].url`)
- `regex`: optional regex applied to the source value
- `group`: regex capture group index (0 = full match)
- `transform`: optional type coercion (`int`, `float`, `iso_datetime`, `lowercase`, `uppercase`)

**Fallback parser** (`title_parser.py`): When no field_mapping is set (`parser_type: "mikanani"`), uses hardcoded regex for the mikanani title format: `[字幕组] CN / EN - EP [quality]`

### Target Fields to Extract
- `subtitle_group`: release group name
- `title_cn` / `title_en`: Chinese and English titles
- `episode`: episode number
- `resolution`: e.g., `1080p`, `720p`
- `source`: e.g., `WebRip`, `WEB-DL`
- `video_codec`: e.g., `HEVC-10bit`, `AVC`
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
| LLM_API_KEY | (empty) | LLM API key (required for feed analysis + decisions) |
| LLM_MODEL | openrouter/free | LLM model name (OpenAI-compatible) |
| LLM_BASE_URL | https://openrouter.ai/api/v1 | LLM API base URL |
| TVDB_API_KEY | (empty) | TVDB API key for metadata matching |
| DEFAULT_FETCH_INTERVAL | 1800 | Default RSS fetch interval (seconds) |
| MAX_RETRY_COUNT | 3 | Max download retry count |
| TASK_EXPIRE_DAYS | 30 | Completed task expiry days |
| LOG_LEVEL | INFO | Logging level |
| DEBUG | false | Debug mode |

## Integration Test Infrastructure

The project includes a full integration test server (`tests/integration/server/`) that provides:

### Test Server Components

| Component | Endpoint | Description |
|-----------|----------|-------------|
| RSS Feed (dmhy) | `GET /rss/dmhy` | Anime feed with magnet links (dmhy.org format) |
| RSS Feed (mikanani) | `GET /rss/mikanani` | Anime feed with .torrent files (mikanani.me format) |
| RSS Feed (eztv) | `GET /rss/eztv` | Western TV feed (EZTV scene format) |
| RSS Feed (movies) | `GET /rss/movies` | Movie feed with IMDB metadata |
| BitTorrent Tracker | `GET /announce`, `GET /scrape` | Minimal HTTP tracker (BEP 3) |
| Torrent Files | `GET /torrents/{hash}.torrent` | Serves generated .torrent files |
| Test Files | `GET /files/{path}` | Serves mock test file content |
| Torrent API | `POST /api/torrents/create`, `/seed`, `/download` | Create/seed/download torrents via libtorrent |
| Assertions | `POST /api/torrents/{hash}/assert-complete` | Verify download completion |
| Setup | `POST /api/setup/full` | One-shot: create + seed all test torrents |

### Integration Test Scenarios

1. **Torrent Lifecycle** (`test_torrent_lifecycle.py`): Create torrent → seed → download → verify file integrity
2. **RSS Subscription** (`test_rss_subscription.py`): Validate feed → create Channel → verify resources → create Agent
3. **Filter & Metadata** (`test_filter_metadata.py`): Create agents with filters → test filter matching → IMDB metadata config → Series/Movie CRUD

### Running Integration Tests

```bash
docker-compose -f docker-compose.test.yml up --build --abort-on-container-exit
```

This starts: `app` (RSSRipple) + `test-server` (mock feeds/tracker/torrents) + `test-runner` (pytest) + `transmission`

## Running the Project

```bash
# Development
uv sync
cd frontend && npm install && npm run build && cd ..
uv run uvicorn app.main:app --reload --port 8000

# Docker
docker-compose up --build

# Unit + API tests
uv run pytest tests/unit tests/api -v

# Integration tests (requires Docker — starts test-server + RSSRipple + test-runner)
docker-compose -f docker-compose.test.yml up --build --abort-on-container-exit
```
