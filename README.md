# RSSRipple

RSSRipple is an RSS subscription downloader for TV/anime/movie releases. It fetches RSS feeds, parses resources with per-channel field mappings, links resources to local metadata, filters them through Agents, and sends matching torrents to Transmission.

## Features

- Multi-source RSS support through required per-channel `field_mapping` rules.
- LLM-assisted feed analysis, title cleaning, title regex generation, metadata search, and pending-decision suggestions.
- Local metadata cache for `TVSeries` and `Movie`, populated by manual metadata linking or LLM web search.
- Agent-based subscriptions with channel-wide or selected-work scope.
- Bool-query Filter DSL with nested `and`/`or`, field operators, and per-work overrides.
- Persistent suggestions for resources that cannot be linked to metadata yet.
- Transmission RPC integration for torrent add/pause/resume/retry/delete, per-downloader default directories, optional per-Agent subdirectories, and progress sync.
- React dashboard for channels, resources, agents, decisions, download tasks, series, movies, and downloaders.

## Architecture

The system is organized around two design documents:

- [AGENTS.md](AGENTS.md) is the authoritative product/API/data-model specification.
- [ARCHITECTURE.md](ARCHITECTURE.md) describes the module layout and runtime data flow.

`DESIGN.md` is reserved for design tokens and visual guidance only.

## Tech Stack

| Layer | Technology |
| --- | --- |
| Backend | Python 3.11+, FastAPI, SQLAlchemy 2.0 async, Pydantic v2 |
| Database | SQLite with aiosqlite by default; PostgreSQL-compatible architecture |
| Queue/Scheduler | MemoryQueue or RedisQueue, APScheduler |
| RSS | feedparser |
| Metadata/AI | OpenAI-compatible LLM APIs, optional web-search-capable model |
| Download | Transmission RPC |
| Frontend | React 18, TypeScript, Vite, Ant Design 5 |
| Package manager | uv, npm |

## Key Concepts

- **Channel**: RSS feed configuration. `field_mapping` is required and defines how RSS entries become `FileResource` records. `metadata_source` defaults to `llm`; if set to `none`, automatic web metadata search is disabled and users must manually link metadata.
- **FileResource**: One parsed RSS release. TV episode numbering uses the `episode` field directly.
- **TVSeries / Movie**: Local metadata cache. External metadata search results are stored here rather than in a separate search cache.
- **Agent**: Watches one Channel, requires a Downloader, applies Filter DSL, and dispatches matching resources. It may specify a relative `download_subdir` under the Downloader's default directory.
- **AgentWork**: A selected TV series or movie for scoped Agents, with optional filter overrides.
- **AgentSuggestion**: Persisted groups of unrecognized resources for later manual metadata linking.
- **PendingDecision**: Multiple valid candidates for the same movie or episode when `conflict_resolution="ask"`.
- **DownloaderInstance**: Transmission RPC connection plus a required default `download_dir`, interpreted on the download server where Transmission runs. Its connection test also checks the directory with Transmission's free-space RPC.
- **Download directory resolution**: A task's effective directory is `DownloaderInstance.download_dir` plus `Agent.download_subdir` when set. Agent subdirectories must be relative paths and must not escape the Downloader root.

## API Overview

All API routes are under `/api/v1`.

| Area | Main endpoints |
| --- | --- |
| Dashboard | `GET /dashboard` |
| Channels | CRUD, fetch, fetch-status, analyze/analyze-stream, preview-feed, validate-url, title-regex, summarize-filters |
| Resources | Channel resources, resource detail, metadata search/link |
| Agents | CRUD, run/run-status, test-filters, persisted suggestions |
| Agent Works | CRUD under `/agents/{agent_id}/works` |
| Downloaders | CRUD with required default download directory, connection test, local tasks, live Transmission torrents |
| Tasks | Detail, pause, resume, retry, delete |
| Decisions | List, confirm, skip |
| Series / Movies | CRUD and search/list views |

## Download Directories

Each Downloader must define a default `download_dir`, using the path as seen by the Transmission server. The path may use the native absolute-path style of that server, such as `/volume1/downloads/rssripple`, `D:\Downloads\RSSRipple`, or a UNC path if supported by the Transmission daemon.

Each Agent may optionally define a relative `download_subdir`; RSSRipple joins it under the Downloader directory when creating tasks.

The resolved directory is stored on each `DownloadTask`, so retries keep using the original destination even if the Downloader or Agent configuration changes later. Agent subdirectories are validated as relative paths and cannot escape the Downloader root.

RSSRipple does not change Transmission's global session download directory. It passes the resolved directory per torrent when calling Transmission.

## Configuration

Common environment variables:

| Variable | Description |
| --- | --- |
| `DATABASE_URL` | SQLAlchemy database URL |
| `REDIS_URL` | Optional Redis backend for queue locks |
| `LLM_API_KEY` | API key for LLM features |
| `LLM_BASE_URL` | OpenAI-compatible base URL |
| `LLM_MODEL` | Model for feed analysis, title cleaning, regex generation, and suggestions |
| `LLM_SEARCH_MODEL` | Web-search-capable model for metadata search |
| `POSTER_CACHE_DIR` | Local poster cache mounted at `/posters` |
| `TRANSMISSION_TIMEOUT` | Transmission RPC timeout |
| `DEV_MODE` | Include stack traces in internal error responses when true |

## Local Development

```bash
uv sync
cd frontend && npm install && npm run build && cd ..
uv run uvicorn app.main:app --reload --port 9001
```

Web UI: [http://localhost:9001](http://localhost:9001)

API docs: [http://localhost:9001/docs](http://localhost:9001/docs)

## Tests

```bash
uv run pytest tests/unit tests/api -v
```

Integration tests use `docker-compose.test.yml`.
