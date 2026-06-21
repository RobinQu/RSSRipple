# RSS Downloader - Architecture Document

## 1. System Architecture

```
┌────────────────────────────────────────────────────────────┐
│                        Docker Container                     │
│                                                            │
│  ┌─────────────────────┐   ┌──────────────────────────┐   │
│  │   React SPA (Vite)  │   │    FastAPI Backend        │   │
│  │   Port: bundled     │──▶│    Port: 8000             │   │
│  │   (served by uvicorn│   │                           │   │
│  │    as static files)  │   │  ┌───────────────────┐   │   │
│  └─────────────────────┘   │  │  API Layer         │   │   │
│                             │  │  (JSON-RPC style)  │   │   │
│                             │  └────────┬──────────┘   │   │
│                             │           │              │   │
│                             │  ┌────────▼──────────┐   │   │
│                             │  │  Service Layer     │   │   │
│                             │  │  ┌─────────────┐   │   │   │
│                             │  │  │Channel Svc  │   │   │   │
│                             │  │  │Agent Svc    │   │   │   │
│                             │  │  │Filter Svc   │   │   │   │
│                             │  │  │Download Svc │   │   │   │
│                             │  │  │LLM Svc      │   │   │   │
│                             │  │  └─────────────┘   │   │   │
│                             │  └────────┬──────────┘   │   │
│                             │           │              │   │
│                             │  ┌────────▼──────────┐   │   │
│                             │  │  Data Layer        │   │   │
│                             │  │  ┌─────────────┐   │   │   │
│                             │  │  │SQLAlchemy   │───┼──▶│ SQLite│
│                             │  │  │Models       │   │   │  .db  │
│                             │  │  └─────────────┘   │   │   │
│                             │  └───────────────────┘   │   │
│                             │           │              │   │
│                             │  ┌────────▼──────────┐   │   │
│                             │  │  External Clients  │   │   │
│                             │  │  ┌─────────────┐   │   │   │
│                             │  │  │Transmission │◀──┼──▶│ TR API│
│                             │  │  │RPC Client   │   │   │   │
│                             │  │  ├─────────────┤   │   │   │
│                             │  │  │RSS Parser   │◀──┼──▶│ RSS  │
│                             │  │  │(feedparser) │   │   │ Feed │
│                             │  │  ├─────────────┤   │   │   │
│                             │  │  │LLM Client   │◀──┼──▶│ LLM  │
│                             │  │  │(OpenAI API) │   │   │  API │
│                             │  │  └─────────────┘   │   │   │
│                             │  └───────────────────┘   │   │
│                             └──────────────────────────┘   │
│                                                            │
│  ┌──────────────────┐  ┌──────────────────┐                │
│  │  APScheduler     │  │  Background      │                │
│  │  (RSS polling)   │  │  Task Queue      │                │
│  └──────────────────┘  └──────────────────┘                │
└────────────────────────────────────────────────────────────┘
           │
           ▼
┌──────────────────────┐
│  Transmission Daemon │  (separate container or external)
│  Port: 9091          │
└──────────────────────┘
```

## 2. Backend Module Structure

```
app/
├── main.py                    # FastAPI application entry
├── config.py                  # Settings & environment config
├── database.py                # SQLAlchemy engine & session
│
├── models/                    # SQLAlchemy ORM models
│   ├── __init__.py
│   ├── channel.py
│   ├── file_resource.py
│   ├── episode.py
│   ├── series.py
│   ├── movie.py
│   ├── filter.py
│   ├── agent.py
│   ├── downloader.py
│   ├── download_task.py
│   └── pending_decision.py
│
├── schemas/                   # Pydantic schemas (request/response)
│   ├── __init__.py
│   ├── channel.py
│   ├── file_resource.py
│   ├── episode.py
│   ├── series.py              # TVSeries create/update/response
│   ├── movie.py               # Movie create/update/response
│   ├── filter.py
│   ├── agent.py
│   ├── downloader.py
│   ├── download_task.py
│   ├── pending_decision.py
│   └── common.py
│
├── api/                       # API route handlers
│   ├── __init__.py
│   ├── v1/
│   │   ├── __init__.py
│   │   ├── channels.py        # CRUD + analyze + apply-mapping
│   │   ├── agents.py
│   │   ├── filters.py
│   │   ├── downloaders.py
│   │   ├── tasks.py
│   │   ├── decisions.py
│   │   ├── dashboard.py
│   │   ├── resources.py
│   │   ├── series.py          # TVSeries CRUD
│   │   └── movies.py          # Movie CRUD
│   └── deps.py                # Dependency injection
│
├── services/                  # Business logic layer
│   ├── __init__.py
│   ├── channel_service.py     # RSS fetching, channel CRUD
│   ├── agent_service.py       # Agent orchestration
│   ├── filter_service.py      # Filter matching logic
│   ├── download_service.py    # Transmission integration
│   ├── title_parser.py        # RSS title parsing (mikanani fallback)
│   ├── resource_parser.py     # Dynamic field mapping parser
│   ├── feed_analyzer.py       # LLM-based RSS field mapping generation
│   ├── metadata_service.py    # TVSeries/Movie metadata matching
│   ├── llm_service.py         # LLM integration
│   └── scheduler.py           # APScheduler setup
│
├── clients/                   # External API clients
│   ├── __init__.py
│   ├── transmission.py        # Transmission RPC wrapper
│   ├── rss_parser.py          # RSS feed parser (feedparser)
│   ├── tmdb_client.py         # TMDB API client
│   ├── tvdb_client.py         # TVDB API v4 client
│   └── llm_client.py          # LLM API client
│
└── utils/                     # Utility functions
    ├── __init__.py
    └── fuzzy_match.py         # Fuzzy string matching
```

## 3. Frontend Structure

```
frontend/
├── index.html
├── package.json
├── vite.config.ts
├── tailwind.config.js
├── tsconfig.json
├── public/
└── src/
    ├── main.tsx
    ├── App.tsx
    ├── api/                    # API client
    │   ├── client.ts
    │   ├── channels.ts
    │   ├── agents.ts
    │   ├── downloaders.ts
    │   └── tasks.ts
    ├── components/             # Reusable components
    │   ├── Layout.tsx
    │   ├── Sidebar.tsx
    │   ├── ProgressBar.tsx
    │   ├── StatusBadge.tsx
    │   ├── Pagination.tsx
    │   └── Modal.tsx
    ├── pages/                  # Page components
    │   ├── Dashboard.tsx
    │   ├── Channels.tsx
    │   ├── ChannelForm.tsx
    │   ├── Downloaders.tsx
    │   ├── DownloaderForm.tsx
    │   ├── Agents.tsx
    │   ├── AgentForm.tsx
    │   └── AgentDetail.tsx
    ├── hooks/                  # Custom hooks
    │   ├── useApi.ts
    │   └── usePolling.ts
    ├── types/                  # TypeScript types
    │   └── index.ts
    └── utils/
        └── format.ts
```

## 4. API Architecture

### 4.1 REST-style API with JSON bodies

采用 RESTful 风格，所有接口返回 JSON，路径前缀 `/api/v1/`。

### 4.2 Response Envelope

```json
{
  "success": true,
  "data": { ... },
  "error": null,
  "meta": {
    "page": 1,
    "page_size": 20,
    "total": 100
  }
}
```

### 4.3 Error Response

```json
{
  "success": false,
  "data": null,
  "error": {
    "code": "CHANNEL_NOT_FOUND",
    "message": "Channel with id 'xxx' not found"
  }
}
```

## 5. Scheduler Architecture

```python
# APScheduler 负责定时拉取 RSS
scheduler = AsyncIOScheduler()

# 每个活跃 Channel 注册一个 job
scheduler.add_job(
    fetch_channel_rss,
    trigger='interval',
    seconds=channel.fetch_interval,
    args=[channel.id],
    id=f"channel-{channel.id}"
)

# 拉取完成后触发 Agent 处理
async def fetch_channel_rss(channel_id):
    resources = await channel_service.fetch_and_parse(channel_id)
    agents = await agent_service.get_by_channel(channel_id)
    for agent in agents:
        await agent_service.process_resources(agent, resources)
```

## 6. Download Task Lifecycle

```
                    ┌──────────┐
                    │ pending  │ ← 创建但尚未提交
                    └────┬─────┘
                         │ submit to Transmission
                         ▼
                    ┌──────────┐
                    │ queued   │ ← 已提交，等待下载
                    └────┬─────┘
                         │ Transmission starts
                         ▼
                    ┌──────────┐
              ┌────▶│downloading│◀───┐
              │     └────┬─────┘    │
              │          │          │
         pause       complete    resume
              │          │          │
              ▼          ▼          │
        ┌──────────┐ ┌────────┐    │
        │ paused   │ │completed│   │
        └────┬─────┘ └────┬───┘    │
             │            │        │
          resume      expire      │
             │            │        │
             └────────────┴────────┘
                         
        Error at any point → ┌──────────┐
                             │ error    │ → retry → queued
                             └──────────┘ → max retries → cancelled
```

## 7. Concurrency Model

- FastAPI async endpoints + background tasks
- RSS fetch 使用 `asyncio` + `httpx` 异步 HTTP
- Transmission 操作通过 `transmission-rpc`（同步库，使用 `run_in_executor`）
- 数据库操作使用 `sqlalchemy.ext.asyncio`
- APScheduler `AsyncIOScheduler` 驱动定时任务

## 8. Docker Architecture

### docker-compose.yml

```yaml
services:
  app:
    build: .
    ports:
      - "8000:8000"
    volumes:
      - ./data:/app/data
    environment:
      - DATABASE_URL=sqlite+aiosqlite:///data/rss_downloader.db
      - LLM_API_KEY=${LLM_API_KEY}
    depends_on:
      - transmission

  transmission:
    image: linuxserver/transmission:latest
    ports:
      - "9091:9091"
      - "51413:51413"
    volumes:
      - ./transmission-config:/config
      - ./downloads:/downloads
    environment:
      - PUID=1000
      - PGID=1000
```

### Dockerfile

```dockerfile
FROM python:3.12-slim

# Build frontend
FROM node:20-slim AS frontend-builder
WORKDIR /frontend
COPY frontend/ .
RUN npm ci && npm run build

# Python app
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ ./app/
COPY --from=frontend-builder /frontend/dist ./app/static

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

## 9. Testing Strategy

| 层级 | 工具 | 覆盖范围 |
|------|------|----------|
| 单元测试 | pytest + pytest-asyncio | 标题解析、过滤器匹配、模型验证 |
| API 测试 | httpx + TestClient | API 路由、请求/响应验证 |
| 集成测试 | docker-compose + pytest | 端到端：RSS → 解析 → Transmission |
| 前端测试 | Vitest + Testing Library | 组件渲染、交互测试 |

### Integration Test Flow

```
docker-compose (test profile)
├── app (test mode)
├── transmission (官方镜像)
└── test runner
    1. 启动 Transmission
    2. 创建测试 RSS feed (本地 mock server)
    3. 通过 API 创建 Channel + Agent + Filter
    4. 触发 RSS 拉取
    5. 验证 Transmission 任务创建
    6. 验证下载进度上报
```

## 10. Configuration

```python
class Settings(BaseSettings):
    # Database
    database_url: str = "sqlite+aiosqlite:///data/rss_downloader.db"
    
    # Scheduler
    default_fetch_interval: int = 1800  # 30 minutes
    
    # LLM (OpenAI-compatible API)
    llm_api_key: str = ""
    llm_model: str = "openrouter/free"
    llm_base_url: str = "https://openrouter.ai/api/v1"
    
    # Metadata providers
    tmdb_api_key: str = ""
    tvdb_api_key: str = ""
    
    # App
    app_name: str = "RSS Downloader"
    debug: bool = False
    log_level: str = "INFO"
    
    # Download
    max_retry_count: int = 3
    task_expire_days: int = 30
```
