<p>
  <img src="docs/assets/rssripple-banner.svg" alt="RSSRipple - RSS subscription downloader" width="596">
</p>

**English** | [中文](README_CN.md)

[![CI Fast Gate](https://github.com/RobinQu/RSSRipple/actions/workflows/ci-fast.yml/badge.svg)](https://github.com/RobinQu/RSSRipple/actions/workflows/ci-fast.yml)
[![CI Strict Gate](https://github.com/RobinQu/RSSRipple/actions/workflows/ci-strict.yml/badge.svg)](https://github.com/RobinQu/RSSRipple/actions/workflows/ci-strict.yml)
[![Docker Publish](https://github.com/RobinQu/RSSRipple/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/RobinQu/RSSRipple/actions/workflows/docker-publish.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

RSSRipple is an RSS subscription downloader for TV / anime / movie releases. It fetches RSS feeds, parses each release with per-channel field mappings, links releases to a local metadata library, filters them through Agents, and dispatches matching torrents to Transmission — closing the loop from subscription to download.

## Highlights

- **End-to-end pipeline** — RSS fetch → field-mapping parse → metadata link → Agent filter → Transmission dispatch. Agent runs are incremental (a `last_consumed_at` watermark); rule changes go through a rules-preview / backfill flow so historical resources are never silently auto-dispatched.
- **LLM-assisted feed analysis** — point RSSRipple at a feed and the LLM proposes the `field_mapping` rules; refine them in the UI before saving.
- **Unified metadata agent** — a LangGraph ReAct agent cleans titles, infers season/episode, and searches exactly one selected source (`wikipedia`, `tmdb`, or `bangumi`; channel default `wikipedia`). Results cache locally as `TVSeries` / `Movie` to avoid re-querying.
- **Filter DSL** — boolean queries with nested `and` / `or`, field operators, per-work overrides, and first-class support for batches (`is_batch`) and multi-value subtitle languages (`zh-CN`, `zh-TW`, `ja`, `en`, `multi`).
- **Transmission integration** — multiple downloader instances, required default directory with optional per-Agent subdirectories, retry with persisted destination, and live progress sync. A `mock` downloader is included for testing.
- **React dashboard** — key metrics, top active agents with their in-progress tasks, active downloads, pending decisions, channels, agents, the works library, and downloaders, all in one place.

## Quick Start

### 1. Configure environment

```bash
cp .env.example .env
# at minimum set: LLM_API_KEY, LLM_BASE_URL, LLM_MODEL
# optional metadata sources: TMDB_API_KEY / BANGUMI_API_KEY
# legacy/env-only sources (manual search/eval): JINA_API_KEY
```

### 2. Run with Docker Compose

```bash
docker compose up --build
```

This starts the app plus its PostgreSQL database and Redis queue (bring your own Transmission — you register its RPC URL as a downloader in the UI):

| Service | URL | Purpose |
| --- | --- | --- |
| RSSRipple | http://localhost:9001 | Web UI |
| API docs | http://localhost:9001/docs | OpenAPI / Swagger |
| PostgreSQL | (internal) | Shared database |
| Redis | (internal) | Distributed task queue |

PostgreSQL (`postgres:16-alpine`) + Redis (`redis:7-alpine`) are the default backend; data is persisted in named volumes. To run a single-node, dependency-free stack (embedded Turso + in-memory queue, no PostgreSQL/Redis), use `docker compose -f docker-compose.standalone.yml up --build`.

### 3. Run manually

```bash
uv sync
cd frontend && npm install && npm run build && cd ..
uv run uvicorn app.main:app --reload --port 9001
```

The frontend build requires Node.js 20.19+ or 22.12+ (Vite 8).

## Obtaining API Credentials

RSSRipple needs an LLM and at least one metadata source. Get the keys you want, then put them in `.env`.

| Service | Where to get it | Env var | Required? |
| --- | --- | --- | --- |
| LLM (OpenAI-compatible) | [OpenRouter](https://openrouter.ai/keys) — or any OpenAI-compatible provider | `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` | Yes — feed analysis, metadata agent, suggestions |
| TMDB | [themoviedb.org/settings/api](https://www.themoviedb.org/settings/api) (apply for a v3 key) | `TMDB_API_KEY` | Optional — best for TV/movie ID matching |
| Bangumi | [bgm.tv](https://bgm.tv/) (developer token) | `BANGUMI_API_KEY` | Optional — anime-only subject search; a token enables the source |
| Wikipedia | — | — | No key (free `wikipedia` library) — the default channel source |
| Exa (MCP) | [exa.ai](https://exa.ai/) — free public MCP endpoint | `EXA_MCP_URL` | Optional — free, no key; only used as the ordered Exa fallback when the primary source misses |
| Jina Search + Reader | [jina.ai/api-dashboard](https://jina.ai/api-dashboard/) | `JINA_API_KEY` | Optional — env-only; manual search/eval only (deprecated as a channel source) |

A metadata source appears in the UI only when enabled **and** its key is set. Toggle visibility with `EXA_ENABLED` / `JINA_ENABLED` / `TMDB_ENABLED` / `WIKIPEDIA_ENABLED` (`EXA_ENABLED` gates only the Exa MCP fallback; jina is env-only — its settings-page row was removed). The `local` source needs no credentials — it matches against the local DB only.

## Configuration

Common variables (full list in [docs/design/conventions.md](docs/design/conventions.md)):

| Variable | Description |
| --- | --- |
| `DATABASE_URL` | SQLAlchemy database URL |
| `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` | OpenAI-compatible LLM for feed analysis, metadata agent, suggestions |
| `JINA_API_KEY` / `TMDB_API_KEY` / `BANGUMI_API_KEY` | Metadata source credentials — configure the sources you want |
| `QUEUE_BACKEND` | `"memory"` (default) or `"redis"` (requires `REDIS_URL`) |
| `POSTER_CACHE_DIR` | Poster image cache, served at `/posters` |

## Migrating between databases

The default stack (`docker-compose.yml`) runs PostgreSQL + Redis. If you previously ran the single-node Turso stack (`docker-compose.standalone.yml`) and switch to the default, your data stays in the `app-data` volume but is **not** loaded automatically — the new PostgreSQL starts empty. Migrate it with:

```bash
docker compose stop app
docker compose run --rm app \
  uv run --no-project python scripts/migrate_to_postgres.py \
  --source sqlite+aioturso:///data/rss_ripple_turso.db \
  --target postgresql+asyncpg://rssripple:rssripple@postgres:5432/rssripple \
  --force
docker compose start app
```

Full migration matrix (SQLite → Turso → PostgreSQL), script usage, and verification are documented in [docs/design/db-migration.md](docs/design/db-migration.md).

## Using RSSRipple

Once the app is running at http://localhost:9001:

1. **Add a channel** — *Channels → New Channel*: paste the RSS URL, click **Validate**, then let the LLM propose the field mapping (or adjust it yourself) and create the channel.
2. **Add a downloader** — *Downloaders → Add Downloader*: enter your Transmission RPC URL; the default download directory is pre-filled with `/downloads/complete`. Use **Test Connection** — it probes the values currently in the form, so you can verify before saving.
3. **Create an agent** — *Download Agents → New Agent*: pick the channel and downloader, subscribe specific works (series/movies, up to 10) or go channel-wide, and refine the filter conditions. Saving runs a rules-preview that lets you optionally backfill existing resources. On a channel's detail page you can also select a few resources and use **Generate Filter Rules** to bootstrap an agent from them.
4. **Watch the dashboard** — `/` shows the key metrics, your top active agents with their in-progress downloads, the active download list, and anything waiting for your decision (confirm/skip, with an AI suggestion when LLM is enabled).

## Feedback & Issues

Found a bug or have a feature request? Please open an issue at [GitHub Issues](https://github.com/RobinQu/RSSRipple/issues).

To help us reproduce and fix it quickly, include:

- The version or image tag you run (e.g. `ghcr.io/robinqu/rssripple:latest`) and how you deployed (Docker Compose / manual).
- Steps to reproduce, expected vs. actual behavior — screenshots help a lot.
- Relevant logs (`docker compose logs app` or the server console output) — redact any API keys before posting.

## Contributing

Developer setup, tests, branch policy, and CI/CD live in [CONTRIBUTION.md](CONTRIBUTION.md). Coding agents should start with [AGENTS.md](AGENTS.md).

## Tech Stack

| Layer | Technology |
| --- | --- |
| Backend | Python 3.11+, FastAPI, SQLAlchemy 2.0 async, Pydantic v2 |
| Database | PostgreSQL by default; Turso (embedded, MVCC concurrent writes) via `docker-compose.standalone.yml` |
| Queue / Scheduler | MemoryQueue or RedisQueue, APScheduler |
| RSS | feedparser |
| Metadata / AI | OpenAI-compatible LLM, LangGraph ReAct, Wikipedia / TMDB / Bangumi (+ ordered Exa fallback) |
| Download | Transmission RPC |
| Frontend | React, TypeScript, Vite, Ant Design |
| Package manager | uv, npm |

## License

RSSRipple is licensed under the [Apache License 2.0](LICENSE).
