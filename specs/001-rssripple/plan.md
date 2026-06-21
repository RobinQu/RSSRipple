# Implementation Plan: RSSRipple

**Branch**: `001-rssripple` | **Date**: 2026-06-21 | **Spec**: [spec.md](./spec.md)

## Summary
RSSRipple is an automated RSS subscription download service focused on intelligent filtering and auto-downloading of anime/TV content. The system fetches RSS feeds, parses resource metadata from titles, applies a three-tier resolution system (Rule Filters → LLM Decision → Human Decision) to select the best version per episode, and pushes downloads to Transmission. It includes a React SPA frontend for management and a FastAPI backend with SQLite persistence, all deployable as a single Docker container.

## Technical Context
**Language/Version**: Python 3.12
**Primary Dependencies**: FastAPI, SQLAlchemy 2.0 (async), Pydantic v2, feedparser, transmission-rpc, APScheduler (AsyncIOScheduler)
**Storage**: SQLite with aiosqlite
**Testing**: pytest, pytest-asyncio, httpx (unit + API tests); docker-compose + pytest (integration tests); Vitest + Testing Library (frontend)
**Target Platform**: Docker container, Linux
**Project Type**: Web service (FastAPI) + React SPA frontend
**Performance Goals**:
- RSS feed fetch and parse within 10 seconds for typical feeds (50-200 items)
- Filter matching for a single Agent's resources within 1 second
- Dashboard API response within 500ms
- Frontend initial load within 2 seconds (bundled SPA)
**Constraints**:
- SQLite limits concurrent writes (single-writer); acceptable for personal/small-team use
- transmission-rpc is synchronous; wrapped in `run_in_executor` for async compatibility
- LLM API calls are external and may have latency; must not block the processing pipeline
- No external database required; SQLite file mounted via Docker volume
- Frontend built and served as static files from the same uvicorn process

## Constitution Check
- **Async-first**: All I/O operations (HTTP, DB, scheduler) use async patterns. Synchronous libraries (transmission-rpc) are wrapped in executors.
- **Separation of concerns**: Clear layering — API routes → Service layer → Data layer → External clients. Business logic lives in services, not in route handlers or models.
- **Type safety**: Pydantic v2 for all request/response schemas; type hints on all functions; SQLAlchemy 2.0 mapped_column declarations.
- **Testability**: Service layer functions accept injected dependencies; API tests use httpx TestClient; integration tests run in Docker with mock RSS feeds and a real Transmission instance.
- **Minimal infrastructure**: SQLite + single Docker container; no Redis, no Celery, no external DB.

## Project Structure

```
rssripple/
├── app/                        # Backend application
│   ├── main.py                 # FastAPI app entry, static file serving
│   ├── config.py               # Settings (pydantic-settings)
│   ├── database.py             # SQLAlchemy async engine & session
│   │
│   ├── models/                 # SQLAlchemy ORM models
│   │   ├── __init__.py
│   │   ├── channel.py          # Channel entity
│   │   ├── file_resource.py    # FileResource entity
│   │   ├── episode.py          # Episode entity
│   │   ├── series.py           # TVSeries entity
│   │   ├── movie.py            # Movie entity
│   │   ├── filter.py           # ResourceFilter entity
│   │   ├── agent.py            # Agent entity
│   │   ├── downloader.py       # DownloaderInstance entity
│   │   ├── download_task.py    # DownloadTask entity
│   │   └── pending_decision.py # PendingDecision entity
│   │
│   ├── schemas/                # Pydantic request/response schemas
│   │   ├── __init__.py
│   │   ├── channel.py
│   │   ├── file_resource.py
│   │   ├── episode.py
│   │   ├── series.py
│   │   ├── movie.py
│   │   ├── filter.py
│   │   ├── agent.py
│   │   ├── downloader.py
│   │   ├── download_task.py
│   │   ├── pending_decision.py
│   │   └── common.py           # Response envelope, pagination
│   │
│   ├── api/                    # API route handlers
│   │   ├── __init__.py
│   │   ├── v1/
│   │   │   ├── __init__.py
│   │   │   ├── channels.py     # CRUD + fetch + analyze + apply-mapping + validate-url
│   │   │   ├── agents.py       # CRUD + run + test-filters
│   │   │   ├── filters.py      # CRUD for ResourceFilters
│   │   │   ├── downloaders.py  # CRUD + test connection
│   │   │   ├── tasks.py        # Task detail + pause/resume/retry/delete
│   │   │   ├── decisions.py    # Confirm/skip pending decisions
│   │   │   ├── dashboard.py    # Dashboard overview
│   │   │   ├── resources.py    # FileResource listing/detail
│   │   │   ├── series.py       # TVSeries CRUD
│   │   │   └── movies.py       # Movie CRUD
│   │   └── deps.py             # Dependency injection (DB session, settings)
│   │
│   ├── services/               # Business logic layer
│   │   ├── __init__.py
│   │   ├── channel_service.py  # RSS fetching, channel CRUD, field mapping
│   │   ├── agent_service.py    # Agent orchestration, resource processing pipeline
│   │   ├── filter_service.py   # Filter matching logic, three-tier resolution
│   │   ├── download_service.py # Transmission integration, task lifecycle
│   │   ├── title_parser.py     # Mikanani fallback title parser (regex)
│   │   ├── resource_parser.py  # Dynamic field mapping parser
│   │   ├── feed_analyzer.py    # LLM-based RSS field mapping generation
│   │   ├── metadata_service.py # TVSeries/Movie metadata matching
│   │   ├── llm_service.py      # LLM integration (OpenAI-compatible)
│   │   └── scheduler.py        # APScheduler setup and job management
│   │
│   ├── clients/                # External API clients
│   │   ├── __init__.py
│   │   ├── transmission.py     # Transmission RPC wrapper
│   │   ├── rss_parser.py       # RSS feed parser (feedparser)
│   │   ├── imdb_client.py      # IMDB client (Cinemagoer)
│   │   ├── tvdb_client.py      # TVDB API v4 client
│   │   └── llm_client.py       # LLM API client (OpenAI-compatible)
│   │
│   └── utils/                  # Utility functions
│       ├── __init__.py
│       └── fuzzy_match.py      # Fuzzy string matching (Levenshtein)
│
├── frontend/                   # React frontend
│   └── src/
│       ├── main.tsx            # App entry
│       ├── App.tsx             # Router setup
│       ├── api/                # API client modules
│       │   ├── client.ts       # HTTP client (fetch wrapper)
│       │   ├── channels.ts     # Channel API calls
│       │   ├── agents.ts       # Agent API calls
│       │   ├── downloaders.ts  # Downloader API calls
│       │   └── tasks.ts        # Task API calls
│       ├── components/         # Reusable UI components
│       │   ├── Layout.tsx      # Page layout with sidebar
│       │   ├── Sidebar.tsx     # Navigation sidebar
│       │   ├── ProgressBar.tsx # Download progress visualization
│       │   ├── StatusBadge.tsx # Status indicator badges
│       │   ├── Pagination.tsx  # Pagination controls
│       │   └── Modal.tsx       # Modal dialog
│       ├── pages/              # Page components
│       │   ├── Dashboard.tsx   # Overview: agents, downloads, decisions
│       │   ├── Channels.tsx    # Channel list
│       │   ├── ChannelForm.tsx # Channel create/edit
│       │   ├── Downloaders.tsx # Downloader list
│       │   ├── DownloaderForm.tsx # Downloader create/edit
│       │   ├── Agents.tsx      # Agent list
│       │   ├── AgentForm.tsx   # Agent create/edit
│       │   └── AgentDetail.tsx # Agent detail: tasks + decisions
│       ├── hooks/              # Custom React hooks
│       │   ├── useApi.ts       # Generic API call hook
│       │   └── usePolling.ts   # Auto-refresh polling hook
│       ├── types/              # TypeScript type definitions
│       │   └── index.ts
│       └── utils/
│           └── format.ts       # Formatting utilities (bytes, dates, etc.)
│
├── tests/                      # Test suite
│   ├── unit/                   # Unit tests (title parsing, filter matching)
│   ├── api/                    # API route tests (httpx TestClient)
│   └── integration/            # End-to-end Docker tests
│
├── docker-compose.yml          # Production deployment
├── docker-compose.test.yml     # Integration test environment
├── Dockerfile                  # Multi-stage build (frontend + backend)
├── pyproject.toml              # Python project config (uv)
├── uv.lock                     # Dependency lockfile
├── PRODUCT.md                  # Product design document
├── DESIGN.md                   # Design system (Raycast-inspired dark theme)
└── ARCHITECTURE.md             # System architecture
```

## Implementation Details

### Backend Architecture

#### Layered Architecture
The backend follows a strict three-layer architecture:

1. **API Layer** (`app/api/v1/`): Route handlers that parse requests, call services, and format responses. Each route module corresponds to an entity (channels, agents, filters, etc.). All routes are registered under `/api/v1/` prefix.

2. **Service Layer** (`app/services/`): Contains all business logic. Services are pure async functions that accept parameters and return results. Key services:
   - `channel_service.py`: RSS fetching, parsing, field mapping management
   - `agent_service.py`: Agent orchestration — receives parsed resources, runs the three-tier resolution pipeline
   - `filter_service.py`: Filter matching logic — applies ResourceFilters, computes scores, handles required vs. optional semantics
   - `download_service.py`: Transmission task submission, progress polling, retry logic
   - `llm_service.py`: LLM API integration for feed analysis and decision-making
   - `scheduler.py`: APScheduler job registration and management

3. **Data Layer** (`app/models/`): SQLAlchemy 2.0 ORM models with async session support. All entities use UUID primary keys. Database migrations managed via SQLAlchemy metadata.

#### API Response Envelope
All API responses follow a standard envelope:
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

Error responses:
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

#### Concurrency Model
- **FastAPI async endpoints**: All route handlers are `async def`
- **HTTP requests**: `httpx` async client for RSS feed fetching and LLM API calls
- **Database**: `sqlalchemy.ext.asyncio` with `async_sessionmaker` for connection pooling
- **Transmission RPC**: Synchronous `transmission-rpc` library wrapped in `asyncio.run_in_executor`
- **Scheduler**: `APScheduler.AsyncIOScheduler` drives periodic RSS fetch jobs
- **Background tasks**: FastAPI `BackgroundTasks` for non-blocking post-processing

#### Scheduler Architecture
```python
scheduler = AsyncIOScheduler()

# Each active Channel registers a periodic job
scheduler.add_job(
    fetch_channel_rss,
    trigger='interval',
    seconds=channel.fetch_interval,
    args=[channel.id],
    id=f"channel-{channel.id}"
)

# After fetch, trigger Agent processing
async def fetch_channel_rss(channel_id):
    resources = await channel_service.fetch_and_parse(channel_id)
    agents = await agent_service.get_by_channel(channel_id)
    for agent in agents:
        await agent_service.process_resources(agent, resources)
```

#### Agent Processing Workflow
```
RSS Fetch (Channel) → Parse & Classify → Rule Filter (Tier 1)
  ├─ unique match → Enqueue download
  ├─ multiple matches → LLM Judge (Tier 2)
  │   ├─ decided → Enqueue download
  │   └─ undecided → Human Decision (Tier 3)
  │       ├─ chosen → Enqueue download
  │       └─ skipped → Mark as skipped
  └─ no matches → Skip
```

#### Download Task Lifecycle
```
pending → queued → downloading → completed
                ↕               ↗
             paused ── resume ─┘
                
Any state → error → retry → queued
                      └─ max retries → cancelled
```

### Frontend Architecture

#### Technology Stack
- **React 18** with TypeScript
- **TailwindCSS** for styling (Raycast-inspired dark theme per DESIGN.md)
- **Vite** for build tooling and dev server
- **React Router** for client-side routing

#### Page Structure
| Page | Route | Description |
|------|-------|-------------|
| Dashboard | `/` | Active Agents, Active Downloads, Pending Decisions overview |
| Channels | `/channels` | Paginated channel list |
| Channel Create | `/channels/new` | Create channel form with URL validation |
| Channel Detail | `/channels/:id` | Channel details (modal dialog), resource list, field mapping |
| Downloaders | `/downloaders` | Downloader instance list |
| Downloader Create | `/downloaders/new` | Create/test downloader connection |
| Agents | `/agents` | Paginated agent list |
| Agent Create | `/agents/new` | Create agent with filter configuration |
| Agent Detail | `/agents/:id` | Download tasks + pending decisions |

#### Component Architecture
- **Layout**: Sidebar navigation + main content area
- **ProgressBar**: Animated download progress with speed and ETA
- **StatusBadge**: Color-coded status indicators for tasks, channels, downloaders
- **Pagination**: Standard page/page_size controls
- **Modal**: Channel detail and confirmation dialogs

#### Data Fetching
- `useApi` hook: Generic async API call wrapper with loading/error states
- `usePolling` hook: Auto-refresh mechanism for dashboard and task progress (configurable interval)
- API client modules (`api/channels.ts`, etc.) encapsulate all HTTP calls with typed request/response

#### Design System (from DESIGN.md)
- **Dark-only theme**: 4-step surface ladder — canvas (#07080a) → surface (#0d0d0d) → surface-elevated (#101111) → surface-card (#121212)
- **Typography**: Inter with `font-feature-settings: "calt", "kern", "liga", "ss03"`
- **Elevation**: No drop shadows; depth via surface color ladder and 1px hairline borders (#242728)
- **Primary action**: White CTA pill (#ffffff) with black text
- **Spacing**: 8px base unit, 96px section rhythm
- **Border radius**: 6-16px range (sm: 6px, md: 8px, lg: 10px, xl: 16px)

### Database Schema

#### Entity Relationships
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

#### Schema Implementation Notes
- All entities use `UUID` primary keys (generated via `uuid4`)
- `created_at` and `updated_at` timestamps on all entities using `server_default=func.now()`
- JSON fields stored as SQLite TEXT with Pydantic serialization
- Enum fields stored as strings for readability
- Foreign key constraints with `ondelete=CASCADE` for parent-child relationships (e.g., deleting a Channel removes its FileResources and Agents)
- Indexed columns: `FileResource.guid` (deduplication), `FileResource.channel_id` (query by channel), `DownloadTask.status` (dashboard queries), `PendingDecision.status` (pending list)

#### Key Tables
| Table | Key Columns | Notes |
|-------|-------------|-------|
| `channels` | url, fetch_interval, field_mapping (JSON), parser_type | Central RSS source config |
| `file_resources` | guid (unique), channel_id, parsed fields | Raw parsed RSS entries |
| `agents` | channel_id, downloader_id, llm_enabled, content_type | Processing configuration |
| `resource_filters` | agent_id, field, operator, value, priority, is_required | Rule definitions |
| `download_tasks` | agent_id, file_resource_id, status, progress | Download tracking |
| `pending_decisions` | agent_id, candidates (JSON), status, llm_suggestion | Human decision queue |
| `episodes` | series_id, episode_number, preferred_profile_id | Consistency tracking |
| `series` / `movies` | title_cn, title_en, aliases (JSON), external_id | Metadata matching |
| `downloader_instances` | url, username, password, status | Transmission connections |

### RSS Parsing Architecture

#### Dynamic Field Mapping System
The core innovation for multi-source RSS support:

1. **Analysis Phase**: When a new channel is created, `POST /channels/{id}/analyze` sends sample RSS entries to the LLM, which generates a `field_mapping` JSON describing extraction rules for each FileResource field.

2. **Mapping Format**:
```json
{
  "title_cn": {"source": "title", "regex": "\\]\\s*(.+?)\\s*/", "group": 1},
  "episode": {"source": "title", "regex": "-\\s*(\\d+)\\b", "group": 1, "transform": "int"},
  "torrent_url": {"source": "enclosures[0].url"},
  "file_size": {"source": "enclosures[0].length", "transform": "int"}
}
```
   - `source`: Dotted path into feedparser entry dict (e.g., `title`, `enclosures[0].url`)
   - `regex`: Optional regex applied to the source value
   - `group`: Regex capture group index (0 = full match)
   - `transform`: Optional type coercion (`int`, `float`, `iso_datetime`, `lowercase`, `uppercase`)

3. **Application Phase**: User reviews the proposed mapping via the UI, then applies it via `POST /channels/{id}/apply-mapping`.

4. **Runtime Parsing**: `resource_parser.py` uses the channel's `field_mapping` to extract fields from each RSS entry during subsequent fetches.

#### Fallback Parser
`title_parser.py` provides a hardcoded regex parser for the mikanani title format when `parser_type: "mikanani"` (or when no `field_mapping` is set):
```
[字幕组名] 中文名 / English Name - EP## [WebRip 1080p HEVC-10bit AAC][字幕]
```
Extracts: subtitle_group, title_cn, title_en, episode, resolution, source, video_codec, audio_codec, subtitle_type, container, file_size.

#### Supported RSS Sources
- **mikanani.me**: Standard anime RSS with `<torrent>` namespace, `.torrent` enclosure URLs
- **share.dmhy.org**: Anime RSS with different title format, magnet link enclosures
- **eztv-style**: Western TV RSS with scene naming conventions (SxxExy)

### Integration Architecture

#### Docker Deployment
```yaml
services:
  app:
    build: .
    ports: ["9001:9001"]
    volumes: [./data:/app/data]
    environment:
      - DATABASE_URL=sqlite+aiosqlite:///data/rss_downloader.db
      - LLM_API_KEY=${LLM_API_KEY}
    depends_on: [transmission]

  transmission:
    image: linuxserver/transmission:latest
    ports: ["9091:9091", "51413:51413"]
    volumes:
      - ./transmission-config:/config
      - ./downloads:/downloads
```

#### Multi-Stage Dockerfile
```
Stage 1 (frontend-builder): node:20-slim → npm ci && npm run build → /frontend/dist
Stage 2 (runtime): python:3.12-slim + uv → copy app/ + static from frontend-builder → uvicorn
```

The frontend SPA is built and served as static files from the same uvicorn process, mounted at `/` while API routes are at `/api/v1/`.

#### Transmission Integration
- `clients/transmission.py` wraps the `transmission-rpc` Python library
- Supports: add torrent (by URL), get torrent status, pause, resume, remove
- Synchronous RPC calls wrapped in `asyncio.run_in_executor`
- Download progress polled periodically and synced to DownloadTask records

#### Integration Test Infrastructure
```yaml
# docker-compose.test.yml
services:
  app:           # RSSRipple in test mode (port 9001)
  test-server:   # Mock RSS feeds + BT tracker + torrent API (port 8080)
  test-runner:   # pytest suite (runs after app + test-server ready)
  transmission:  # Real Transmission (port 9092, optional)
```

**Test Server** provides:
| Endpoint | Description |
|----------|-------------|
| `GET /rss/dmhy` | Anime feed with magnet links (dmhy format) |
| `GET /rss/mikanani` | Anime feed with .torrent files (mikanani format) |
| `GET /rss/eztv` | Western TV feed (scene format) |
| `GET /rss/movies` | Movie feed with IMDB metadata |
| `GET /announce`, `GET /scrape` | Minimal HTTP tracker (BEP 3) |
| `GET /torrents/{hash}.torrent` | Serves generated .torrent files |
| `GET /files/{path}` | Serves mock test file content |
| `POST /api/torrents/create` | Create torrents via libtorrent |
| `POST /api/torrents/seed` | Seed torrents |
| `POST /api/torrents/download` | Download and verify |
| `POST /api/torrents/{hash}/assert-complete` | Verify download completion |
| `POST /api/setup/full` | One-shot: create + seed all test torrents |

**Test Data Diversity**:
- Anime (dmhy/mikanani): 4 series × 3 episodes × 3 subtitle groups, varied resolutions/codecs
- TV Shows (eztv): 4 shows × 3 episodes × 3 release groups, scene naming (SxxExy)
- Movies (IMDB-style): 3 movies × 2 release groups, IMDB IDs and genre metadata

**Integration Test Scenarios**:
1. **Torrent Lifecycle**: Create torrent → seed → download → verify file integrity
2. **RSS Subscription**: Validate feed → create Channel → verify resources → create Agent
3. **Filter & Metadata**: Create agents with filters → test filter matching → IMDB metadata config → Series/Movie CRUD

#### Testing Strategy
| Layer | Tool | Coverage |
|-------|------|----------|
| Unit | pytest + pytest-asyncio | Title parsing, filter matching logic, model validation, fuzzy matching |
| API | httpx + FastAPI TestClient | Route handlers, request/response validation, error handling |
| Integration | docker-compose + pytest | End-to-end: RSS fetch → parse → filter → Transmission download |
| Frontend | Vitest + Testing Library | Component rendering, interaction, API integration |

#### Configuration
```python
class Settings(BaseSettings):
    database_url: str = "sqlite+aiosqlite:///data/rss_downloader.db"
    default_fetch_interval: int = 1800  # 30 minutes
    llm_api_key: str = ""
    llm_model: str = "openrouter/free"
    llm_base_url: str = "https://openrouter.ai/api/v1"
    tvdb_api_key: str = ""
    app_name: str = "RSSRipple"
    debug: bool = False
    log_level: str = "INFO"
    max_retry_count: int = 3
    task_expire_days: int = 30
```

#### Environment Variables
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
