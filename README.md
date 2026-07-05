<p>
  <img src="docs/assets/rssripple-banner.svg" alt="RSSRipple - RSS subscription downloader" width="596">
</p>

RSSRipple is an RSS subscription downloader for TV/anime/movie releases. It fetches RSS feeds, parses resources with per-channel field mappings, links resources to local metadata, filters them through Agents, and sends matching torrents to Transmission.

## Features

- Multi-source RSS support through required per-channel `field_mapping` rules.
- LLM-assisted feed analysis, unified metadata agent (title cleaning + single-source Exa/TMDB/Wikipedia metadata search), and pending-decision suggestions.
- Local metadata cache for `TVSeries` and `Movie`, populated by manual metadata linking or MetadataAgent search.
- Agent-based subscriptions with channel-wide or selected-work scope.
- Bool-query Filter DSL with nested `and`/`or`, field operators, per-work overrides, and dedicated support for boolean (`is_batch`) and multi-value list (`subtitle_langs` — BCP-47 tags like `zh-CN`, `zh-TW`, `ja`, `en`, plus the `multi` sentinel) fields.
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
| Metadata/AI | OpenAI-compatible LLM APIs, Exa Agent API, TMDB API, Wikipedia Python library |
| Download | Transmission RPC |
| Frontend | React 18, TypeScript, Vite, Ant Design 5 |
| Package manager | uv, npm |

## Key Concepts

- **Channel**: RSS feed configuration. `field_mapping` is required and defines how RSS entries become `FileResource` records. `metadata_agent_enabled` defaults to `true`; if set to `false`, the unified metadata agent is disabled and only local DB matching is used.
- **FileResource**: One parsed RSS release. TV episode numbering uses the `episode` field directly. Multi-episode batches (Season Pack / `S01E01~13` / `[01-12 合集]` …) are marked with `is_batch=true` and best-effort `episode_start` / `episode_end`; batches bypass per-episode dedup and never produce PendingDecisions — filter them via the Filter DSL when needed.
- **TVSeries / Movie**: Local metadata cache. External metadata search results are stored here rather than in a separate search cache.
- **MetadataAgent**: LangGraph ReAct agent that cleans titles, infers season/episode fields, and searches exactly one selected metadata source. Supported sources are `exa` (default Exa Agent Search), `tmdb`, and `wikipedia`; it does not perform multi-source fallback.
- **Agent**: Watches one Channel, requires a Downloader, applies Filter DSL, and dispatches matching resources. It may specify a relative `download_subdir` under the Downloader's default directory.
- **AgentWork**: A selected TV series or movie for scoped Agents, with optional filter overrides.
- **AgentSuggestion**: Persisted groups of unrecognized resources for later manual metadata linking.
- **PendingDecision**: Multiple valid candidates for the same movie or episode when `conflict_resolution="ask"`.
- **DownloaderInstance**: Transmission RPC connection plus a required default `download_dir`, interpreted on the download server where Transmission runs. Its connection test also checks the directory with Transmission's free-space RPC. A second `type="mock"` variant is available for testing — an in-process simulator whose connection test always succeeds and whose accepted tasks complete after a random 1–10 s delay. Both types share the same client interface via `app.clients.downloader.get_downloader_client`.
- **Download directory resolution**: A task's effective directory is `DownloaderInstance.download_dir` plus `Agent.download_subdir` when set. Agent subdirectories must be relative paths and must not escape the Downloader root.

## API Overview

All API routes are under `/api/v1`.

| Area | Main endpoints |
| --- | --- |
| Dashboard | `GET /dashboard` |
| Channels | CRUD, fetch, fetch-status, analyze/analyze-stream, preview-feed, validate-url, summarize-filters |
| Resources | Channel resources, resource detail, metadata search/link |
| Agents | CRUD, run/run-status, test-filters, persisted suggestions |
| Agent Works | CRUD under `/agents/{agent_id}/works` |
| Downloaders | CRUD with required default download directory, connection test, local tasks, live Transmission torrents |
| Tasks | Detail, pause, resume, retry, delete |
| Decisions | List, confirm, skip |
| Series / Movies | CRUD and search/list views |

## Metadata Search

Metadata search is single-source by design. Each MetadataAgent run uses exactly one of:

- `exa` — default, backed by Exa Agent Search.
- `tmdb` — TMDB API search/detail.
- `wikipedia` — Wikipedia search/page lookup.

The agent does not chain sources or fall back from one source to another. Eval datasets should be created with an explicit source type and use that source as the dataset-name prefix, such as `exa-eval-...`. The legacy `combined` value is accepted only for old datasets and is normalized to `exa`.

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
| `REDIS_URL` | Optional Redis backend for queue distribution |
| `QUEUE_BACKEND` | Queue backend: `"memory"` (default) or `"redis"` |
| `LLM_API_KEY` | API key for LLM features |
| `LLM_BASE_URL` | OpenAI-compatible base URL |
| `LLM_MODEL` | Model for feed analysis, metadata agent, and suggestions. The metadata agent uses the same `LLM_MODEL` for title understanding and interpreting the selected metadata source. |
| `EXA_API_KEY` | API key for default Exa Agent Search metadata source |
| `EXA_EFFORT_LEVEL` | Exa Agent effort level: `minimal`, `low` (default), `medium`, `high`, or `xhigh` |
| `TMDB_API_KEY` | Optional API key for the `tmdb` metadata source |
| `POSTER_CACHE_DIR` | Local poster cache mounted at `/posters` |
| `TRANSMISSION_TIMEOUT` | Transmission RPC timeout |
| `MAX_RETRY_COUNT` | Max retry attempts for failed downloads (default `3`) |
| `TASK_EXPIRE_DAYS` | Auto-cleanup completed tasks after N days (default `30`) |
| `DEV_MODE` | Include stack traces in internal error responses when true |
| `DEBUG` | Enable debug logging (default `false`) |
| `LOG_LEVEL` | Logging level: `"DEBUG"`, `"INFO"` (default), `"WARNING"`, `"ERROR"` |

## Local Development

### Quick start (docker-compose)

```bash
docker compose up --build
```

Web UI: [http://localhost:9001](http://localhost:9001)

API docs: [http://localhost:9001/docs](http://localhost:9001/docs)

The compose file starts the app with a MemoryQueue backend and an SQLite database stored in `./data/`.

### Manual run

```bash
uv sync
cd frontend && npm install && npm run build && cd ..
uv run uvicorn app.main:app --reload --port 9001
```

## Tests

### Unit and API tests (fast, local SQLite)

```bash
uv run pytest tests/unit tests/api -v
```

573 tests, typically finish in under 60 seconds.

### Integration tests (docker-compose)

Two test profiles are available:

**Single-node (SQLite + MemoryQueue)** — fast, no external dependencies:

```bash
# Clean stale database files from previous runs
rm -rf data/ && mkdir -p data

# Run all integration tests suitable for single-node
docker compose -f docker-compose.test.yml run --rm test-runner

# Run a single integration test module
docker compose -f docker-compose.test.yml run --rm test-runner \
  uv run pytest tests/integration/test_channel_workflow.py -v --tb=short
```

**Distributed (PostgreSQL + Redis, two app replicas)** — exercises multi-instance queue dedup:

```bash
docker compose -f docker-compose.test-distributed.yml run --rm test-runner
```

Tests that require a persistent network client (E2E, torrent lifecycle) are excluded from both profiles. Redis-specific job-queue tests are automatically skipped in single-node mode.

**Note:** `./data` is bind-mounted in the single-node profile, so stale SQLite files persist across `docker compose down -v`. Always run `rm -rf data/ && mkdir -p data` before a clean test run.

## Development Collaboration

See [CONTRIBUTION.md](CONTRIBUTION.md) for branch naming conventions and development workflow. See [AGENTS.md](AGENTS.md#分支与协作规范) for the full branch specification (intended for AI agents).
