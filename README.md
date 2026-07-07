<p>
  <img src="docs/assets/rssripple-banner.svg" alt="RSSRipple - RSS subscription downloader" width="596">
</p>

**English** | [中文](README_CN.md)

RSSRipple is an RSS subscription downloader for TV / anime / movie releases. It fetches RSS feeds, parses each release with per-channel field mappings, links releases to a local metadata library, filters them through Agents, and dispatches matching torrents to Transmission — closing the loop from subscription to download.

## Highlights

- **End-to-end pipeline** — RSS fetch → field-mapping parse → metadata link → Agent filter → Transmission dispatch. Agent runs are incremental (a `last_consumed_at` watermark); rule changes go through a rules-preview / backfill flow so historical resources are never silently auto-dispatched.
- **LLM-assisted feed analysis** — point RSSRipple at a feed and the LLM proposes the `field_mapping` rules; refine them in the UI before saving.
- **Unified metadata agent** — a LangGraph ReAct agent cleans titles, infers season/episode, and searches exactly one selected source (`exa`, `jina`, `tmdb`, or `wikipedia`). Results cache locally as `TVSeries` / `Movie` to avoid re-querying.
- **Filter DSL** — boolean queries with nested `and` / `or`, field operators, per-work overrides, and first-class support for batches (`is_batch`) and multi-value subtitle languages (`zh-CN`, `zh-TW`, `ja`, `en`, `multi`).
- **Transmission integration** — multiple downloader instances, required default directory with optional per-Agent subdirectories, retry with persisted destination, and live progress sync. A `mock` downloader is included for testing.
- **React dashboard** — channels, resources, agents, pending decisions, download tasks, the works library, and downloaders, all in one place.

## Quick Start

### 1. Configure environment

```bash
cp .env.example .env
# at minimum set: LLM_API_KEY, LLM_BASE_URL, LLM_MODEL
# optional metadata sources: EXA_API_KEY / JINA_API_KEY / TMDB_API_KEY
```

### 2. Run with Docker Compose

```bash
docker compose up --build
```

This starts the app **and** a Transmission instance:

| Service | URL | Purpose |
| --- | --- | --- |
| RSSRipple | http://localhost:9001 | Web UI |
| API docs | http://localhost:9001/docs | OpenAPI / Swagger |
| Transmission | http://localhost:9091 | Download backend |

SQLite + in-memory queue by default; data is persisted under `./data/`.

### 3. Run manually

```bash
uv sync
cd frontend && npm install && npm run build && cd ..
uv run uvicorn app.main:app --reload --port 9001
```

## Obtaining API Credentials

RSSRipple needs an LLM and at least one metadata source. Get the keys you want, then put them in `.env`.

| Service | Where to get it | Env var | Required? |
| --- | --- | --- | --- |
| LLM (OpenAI-compatible) | [OpenRouter](https://openrouter.ai/keys) — or any OpenAI-compatible provider | `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` | Yes — feed analysis, metadata agent, suggestions |
| Exa Agent Search | [dashboard.exa.ai](https://dashboard.exa.ai/) | `EXA_API_KEY` | Optional — default metadata source |
| Jina Search + Reader | [jina.ai/api-dashboard](https://jina.ai/api-dashboard/) | `JINA_API_KEY` | Optional — strong CJK coverage |
| TMDB | [themoviedb.org/settings/api](https://www.themoviedb.org/settings/api) (apply for a v3 key) | `TMDB_API_KEY` | Optional — best for TV/movie ID matching |
| Wikipedia | — | — | No key (free `wikipedia` library) |

A metadata source appears in the UI only when enabled **and** its key is set. Toggle visibility with `EXA_ENABLED` / `JINA_ENABLED` / `TMDB_ENABLED` / `WIKIPEDIA_ENABLED`. The `local` source needs no credentials — it matches against the local DB only.

## Configuration

Common variables (full list in [AGENTS.md](AGENTS.md), under "Other Conventions"):

| Variable | Description |
| --- | --- |
| `DATABASE_URL` | SQLAlchemy database URL |
| `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` | OpenAI-compatible LLM for feed analysis, metadata agent, suggestions |
| `EXA_API_KEY` / `JINA_API_KEY` / `TMDB_API_KEY` | Metadata source credentials — configure the sources you want |
| `QUEUE_BACKEND` | `"memory"` (default) or `"redis"` (requires `REDIS_URL`) |
| `POSTER_CACHE_DIR` | Poster image cache, served at `/posters` |

Each metadata source appears as a candidate only when enabled **and** its API key is set (`wikipedia` needs no key). Toggle visibility with `EXA_ENABLED` / `JINA_ENABLED` / `TMDB_ENABLED` / `WIKIPEDIA_ENABLED`.

## Developer Guide

### Local development

The compose file watches `./app` and hot-reloads Python. Frontend changes are **not** hot-reloaded — run `npm run build` in `frontend/`, or `docker compose build app` to bake a new bundle into the image.

### Tests

**Unit & API tests** (fast, local SQLite):

```bash
uv run pytest tests/unit tests/api -v
```

**Integration tests** (docker-compose) — two profiles:

Single-node (SQLite + MemoryQueue) — fast, no external dependencies:

```bash
rm -rf data/ && mkdir -p data   # stale SQLite files persist across `down -v`
docker compose -f docker-compose.test.yml run --rm test-runner
# single module:
docker compose -f docker-compose.test.yml run --rm test-runner \
  uv run pytest tests/integration/test_channel_workflow.py -v --tb=short
```

Distributed (PostgreSQL + Redis, two app replicas) — exercises multi-instance queue dedup:

```bash
docker compose -f docker-compose.test-distributed.yml run --rm test-runner
```

Tests requiring a persistent network client (E2E, torrent lifecycle) are excluded from both profiles; Redis-specific job-queue tests are skipped in single-node mode.

### Contributing

Branch naming follows [Conventional Branch](https://conventionalbranch.org/) v1.1.0. See [CONTRIBUTION.md](CONTRIBUTION.md) for the workflow and [AGENTS.md](AGENTS.md) (branch policy section) for the full branch specification.

### CI/CD

GitHub Actions handles continuous integration and delivery:

- **CI Fast Gate** (`ci-fast.yml`) — feature/fix branches and their PRs: lint + unit/API tests.
- **CI Strict Gate** (`ci-strict.yml`) — `main`, `develop`, `release/**` and their PRs: lint + unit/API + integration tests.
- **Docker Publish** (`docker-publish.yml`) — on push to `main` or a `v*` tag, builds a multi-arch (`linux/amd64` + `linux/arm64`) image and pushes it to `ghcr.io/robinqu/rssripple`. Tags: `main` → `:latest`, `:main`, `:sha-<short>`; `v1.2.3` → `:1.2.3`, `:1.2`, `:1`. The build is gated on lint + unit/API tests.

See [CONTRIBUTION.md](CONTRIBUTION.md) for the full workflow and the recommended release flow.

## Specs for Coding Agents

If you are a coding agent (Claude Code, Cursor, Copilot, Codex, …) working on this repo, read these in order:

- **[AGENTS.md](AGENTS.md)** — the authoritative spec: data models, Filter DSL, API endpoints, business logic, frontend routes, error handling, and the branch policy. This is the single source of truth for *how the system works*.
- **[ARCHITECTURE.md](ARCHITECTURE.md)** — module layout and runtime data flow.
- **[overview.md](overview.md)** — design-logic analysis of the channel & metadata library.
- **[DESIGN.md](DESIGN.md)** — design tokens and visual guidance (frontend only).

Implementation must follow AGENTS.md. When code and AGENTS.md disagree, AGENTS.md describes the intended behavior — fix the code, or update AGENTS.md if the design has genuinely changed.

## Tech Stack

| Layer | Technology |
| --- | --- |
| Backend | Python 3.11+, FastAPI, SQLAlchemy 2.0 async, Pydantic v2 |
| Database | SQLite (aiosqlite) by default; PostgreSQL-compatible architecture |
| Queue / Scheduler | MemoryQueue or RedisQueue, APScheduler |
| RSS | feedparser |
| Metadata / AI | OpenAI-compatible LLM, LangGraph ReAct, Exa / Jina / TMDB / Wikipedia |
| Download | Transmission RPC |
| Frontend | React, TypeScript, Vite, Ant Design |
| Package manager | uv, npm |
