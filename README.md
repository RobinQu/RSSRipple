# RSSRipple

Automated RSS subscription downloader with intelligent filtering, LLM-assisted decisions, and Transmission integration.

## Features

- **Multi-source RSS support** — Dynamic field mapping via LLM analysis works with any RSS source (mikanani.me, dmhy.org, myrss.org, etc.)
- **Intelligent resource selection** — Three-tier decision pipeline: rule-based filters → LLM judgment → manual selection
- **Metadata matching** — Automatic TVSeries/Movie identification via TMDB and TVDB APIs
- **Subtitle group consistency** — Keeps the same release group across episodes of a series
- **Transmission integration** — Push download tasks directly to your Transmission daemon
- **Web UI** — React-based dashboard for managing channels, agents, filters, and download progress

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.12, FastAPI, SQLAlchemy 2.0 (async), Pydantic v2 |
| Database | SQLite with aiosqlite |
| RSS | feedparser |
| Download | transmission-rpc |
| Scheduler | APScheduler |
| Frontend | React 18, TypeScript, TailwindCSS, Vite |
| Package manager | uv |
| Container | Docker, docker-compose |

## Quick Start

### Docker (recommended)

```bash
# 1. Clone the repository
git clone https://github.com/RobinQu/RSSRipple.git
cd RSSRipple

# 2. Configure environment
cp .env.example .env
# Edit .env and set your API keys (LLM_API_KEY is required for LLM features)

# 3. Start everything (app + Transmission)
docker-compose up --build
```

- **Web UI**: http://localhost:8000
- **API docs**: http://localhost:8000/docs
- **Transmission UI**: http://localhost:9091

### Local Development

```bash
# 1. Clone and install
git clone https://github.com/RobinQu/RSSRipple.git
cd RSSRipple
uv sync

# 2. Build frontend
cd frontend && npm install && npm run build && cd ..

# 3. Configure environment
cp .env.example .env
# Edit .env as needed

# 4. Start the server
uv run uvicorn app.main:app --reload --port 8000
```

## Configuration

All configuration is done via environment variables (or a `.env` file):

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `sqlite+aiosqlite:///data/rss_downloader.db` | Database connection |
| `LLM_API_KEY` | _(empty)_ | API key for LLM features (required for feed analysis and decisions) |
| `LLM_MODEL` | `openrouter/free` | LLM model name (OpenAI-compatible) |
| `LLM_BASE_URL` | `https://openrouter.ai/api/v1` | LLM API base URL |
| `TMDB_API_KEY` | _(empty)_ | TMDB API key for metadata matching ([get one](https://www.themoviedb.org/settings/api)) |
| `TVDB_API_KEY` | _(empty)_ | TVDB API key for metadata matching |
| `DEFAULT_FETCH_INTERVAL` | `1800` | Default RSS fetch interval in seconds |
| `MAX_RETRY_COUNT` | `3` | Max download retry count |
| `TASK_EXPIRE_DAYS` | `30` | Completed task expiry in days |
| `LOG_LEVEL` | `INFO` | Logging level |
| `DEBUG` | `false` | Debug mode |

### LLM Setup

RSSRipple uses an OpenAI-compatible API for feed analysis and decision-making. By default it points to [OpenRouter](https://openrouter.ai/) free models. To enable LLM features:

1. Get a free API key at https://openrouter.ai/keys
2. Set `LLM_API_KEY=sk-or-...` in your `.env`
3. Optionally change `LLM_MODEL` to a specific model (e.g., `google/gemini-2.0-flash-exp:free`)

You can also use any OpenAI-compatible endpoint by changing `LLM_BASE_URL` and `LLM_MODEL`.

## API Overview

All endpoints are under `/api/v1/`. Interactive docs available at `/docs` when the server is running.

| Resource | Endpoints |
|----------|-----------|
| Dashboard | `GET /dashboard` |
| Channels | CRUD + `POST /{id}/fetch` + `POST /{id}/analyze` + `POST /{id}/apply-mapping` |
| Agents | CRUD + `POST /{id}/run` |
| Filters | CRUD under `/agents/{agent_id}/filters` |
| TVSeries | CRUD |
| Movies | CRUD |
| Resources | `GET /channels/{id}/resources`, `GET /resources/{id}` |
| Download Tasks | List, detail, pause, resume, retry, delete |
| Decisions | List, confirm, skip |
| Downloaders | CRUD + `POST /{id}/test` |

## Development

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (Python package manager)
- Node.js 20+ (for frontend)
- Docker (optional, for integration tests)

### Project Structure

```
rssripple/
├── app/                    # Backend
│   ├── api/v1/             # API route handlers
│   ├── clients/            # External API clients (RSS, TMDB, TVDB, Transmission)
│   ├── models/             # SQLAlchemy ORM models
│   ├── schemas/            # Pydantic request/response schemas
│   ├── services/           # Business logic (parsers, metadata, feed analyzer)
│   ├── config.py           # Settings (pydantic-settings)
│   ├── database.py         # SQLAlchemy engine/session
│   └── main.py             # FastAPI app entry
├── frontend/               # React SPA
│   └── src/
├── tests/                  # Test suite (unit, api, integration)
├── pyproject.toml          # Project config + dependencies
├── uv.lock                 # Dependency lockfile
├── Dockerfile              # Multi-stage build (frontend + backend)
├── docker-compose.yml      # App + Transmission services
├── DESIGN.md               # Data models, filter logic, UI wireframes
├── ARCHITECTURE.md         # System architecture, module structure
└── AGENTS.md               # AI coding agent guide
```

### Running Tests

```bash
# Unit + API tests
uv run pytest tests/unit tests/api -v

# Integration tests (requires Docker)
docker-compose -f docker-compose.test.yml up --build --abort-on-container-exit
```

### Code Style

- Python: [ruff](https://docs.astral.sh/ruff/) (configured in `pyproject.toml`, line-length 120)
- TypeScript: ESLint (configured in `frontend/eslint.config.js`)

```bash
# Lint
uv run ruff check app/ tests/

# Format
uv run ruff format app/ tests/
```

### Adding a Dependency

```bash
# Production dependency
uv add <package>

# Dev dependency
uv add --dev <package>
```

## Contributing

Contributions are welcome. Here's how to get started:

1. **Fork** the repository
2. **Create a branch** for your feature or fix
3. **Install dependencies**: `uv sync`
4. **Make your changes** following the coding conventions below
5. **Run tests**: `uv run pytest tests/ -v`
6. **Run lint**: `uv run ruff check app/ tests/`
7. **Submit a pull request**

### Coding Conventions

- All async where possible (FastAPI, SQLAlchemy, httpx)
- Pydantic v2 `BaseModel` for all data structures (no `dataclass`)
- UUID primary keys for all entities
- Type hints everywhere
- Docstrings on service layer functions
- ISO 8601 datetime strings in API responses
- Structured error responses with error codes

### Key Design Documents

Before making significant changes, review:

- **[DESIGN.md](DESIGN.md)** — Data models, filter logic, RSS parsing architecture, UI wireframes
- **[ARCHITECTURE.md](ARCHITECTURE.md)** — System architecture, module structure, scheduler, Docker setup
- **[AGENTS.md](AGENTS.md)** — API endpoint reference, environment variables, coding guide for AI agents

## License

MIT
