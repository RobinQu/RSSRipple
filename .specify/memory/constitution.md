# RSSRipple Constitution

## Core Principles

### I. Async-First Architecture
All I/O-bound operations MUST be asynchronous. Every FastAPI endpoint handler, SQLAlchemy database query, HTTP client call (httpx), Transmission RPC invocation, and RSS feed fetch MUST use `async`/`await`. Blocking calls in the request path are strictly prohibited. When a library does not support async natively, wrap it with `asyncio.to_thread()` or an equivalent executor. The APScheduler `AsyncIOScheduler` is the sole scheduler; never use `BlockingScheduler`. Database sessions are always `AsyncSession`; never instantiate a synchronous `Session`. This principle ensures the service can handle concurrent RSS fetches, multiple Transmission pushes, and simultaneous Web UI requests without thread starvation.

### II. Type Safety & Validation
Pydantic v2 is the single source of truth for all data entering or leaving the application. Every API request body, query parameter, and response payload MUST be validated through a Pydantic model. Type hints are required on every function signature—parameters and return types. All database entities use UUID v4 primary keys; never use auto-incrementing integers. All datetime fields in API responses MUST be ISO 8601 strings with timezone information. Error responses follow a structured format: `{"success": false, "error": {"code": "NOT_FOUND", "message": "..."}}` — every error carries a machine-readable error code. Pydantic `ConfigDict` with `from_attributes=True` replaces the legacy `orm_mode`. SQLAlchemy models and Pydantic schemas are kept in separate modules (`models/` vs `schemas/`); never expose an ORM model directly through an endpoint.

### III. Test-First Development
All new features and bug fixes MUST include tests. The testing stack is pytest with pytest-asyncio; every async test function is decorated with `@pytest.mark.asyncio`. Test data is constructed via pytest fixtures defined in `conftest.py` files at appropriate scope levels. Unit tests cover service-layer logic, title parsing, and filter scoring in isolation. API tests use httpx `AsyncClient` against the FastAPI test client — no real network calls. Integration tests run inside Docker Compose (`docker-compose.test.yml`) with a mock RSS feed server, a minimal BitTorrent tracker, and a Transmission daemon, validating the full subscription-to-download lifecycle. Test coverage must not decrease with any merge; new code paths require new tests.

### IV. Dynamic RSS Parsing
RSS sources vary widely in title format and XML structure. The system MUST NOT hardcode parsers for each source. Instead, each Channel stores a `field_mapping` JSON document that describes how to extract FileResource fields (subtitle_group, title_cn, title_en, episode, resolution, source, video_codec, audio_codec, subtitle_type, container, torrent_url, file_size) from a feedparser entry. New channels undergo LLM-assisted analysis (`POST /channels/{id}/analyze`) to generate a proposed mapping, which the user reviews before applying (`POST /channels/{id}/apply-mapping`). A fallback title parser exists for the known mikanani.me format. Adding support for a new RSS source requires zero code changes — only a new field mapping configuration. This principle keeps the parser extensible without forking the codebase.

### V. Three-Tier Filter Resolution
Each Agent ensures that every episode downloads exactly one best-matching version through a strict three-tier pipeline. First, required filters (`is_required: true`) act as hard gates — any resource failing a required filter is immediately excluded. Second, optional filters contribute weighted scores based on their `priority` value; resources are ranked by cumulative score. Third, when multiple resources tie or remain ambiguous after scoring, the LLM is invoked as a tiebreaker (if `llm_enabled: true` on the Agent). If the LLM is disabled, ties result in a Pending Decision that requires manual user confirmation. This pipeline is non-negotiable; no shortcut that bypasses required filters or scoring is permitted.

### VI. Simplicity & YAGNI
Start with the simplest implementation that satisfies the requirement. Avoid speculative abstractions, premature optimization, and features that are not in the current product scope. Each module has a single responsibility: `services/` holds business logic, `api/v1/` holds route handlers (thin delegation to services), `clients/` wraps external APIs, `utils/` holds stateless helpers. Prefer Python standard library solutions over third-party dependencies. When a new dependency is introduced, it must be justified by a concrete need that the standard library cannot fulfill. SQLite is the default database; do not introduce PostgreSQL or another RDBMS without a demonstrated concurrency or scale requirement that SQLite cannot meet.

## Technology Stack

| Layer | Technology | Version/Notes |
|-------|-----------|---------------|
| Language | Python | 3.12+ |
| Web Framework | FastAPI | Latest stable |
| ORM | SQLAlchemy | 2.0, async mode only (`AsyncSession`, `asyncpg`-style API) |
| Validation | Pydantic | v2 (`model_config`, `ConfigDict`) |
| Database | SQLite | via `aiosqlite` driver |
| RSS Parsing | feedparser | Per-channel dynamic field mapping |
| Download Client | transmission-rpc | Python client for Transmission daemon |
| Task Scheduling | APScheduler | `AsyncIOScheduler` only |
| HTTP Client | httpx | Async, for LLM API and external calls |
| Frontend Framework | React | 18, functional components + hooks |
| Frontend Language | TypeScript | Strict mode enabled |
| Frontend Styling | TailwindCSS | Raycast-inspired dark theme (see DESIGN.md) |
| Frontend Build | Vite | Dev server + production build |
| Containerization | Docker | Multi-stage Dockerfile |
| Orchestration | docker-compose | Dev, production, and test compose files |
| Testing | pytest | pytest-asyncio, httpx AsyncClient |
| Package Manager | uv | PEP 621 compliant, `pyproject.toml` + `uv.lock` |

## Development Workflow

1. **Branching**: Feature branches off `main`. Branch naming: `feature/<description>`, `fix/<description>`, `chore/<description>`.
2. **Local Development**: Run `uv sync` to install dependencies, build the frontend (`cd frontend && npm install && npm run build`), then start the backend with `uv run uvicorn app.main:app --reload --port 9001`. Use Docker Compose (`docker-compose up --build`) for full-stack development with Transmission.
3. **Code Review**: Every change requires at least one approving review. Reviewers verify constitution compliance: async correctness, type hints, Pydantic validation, test coverage, and error response structure.
4. **Testing Gates**: All unit and API tests (`uv run pytest tests/unit tests/api -v`) must pass before merge. Integration tests (`docker-compose -f docker-compose.test.yml up --build --abort-on-container-exit`) must pass for any change touching RSS parsing, filter logic, download task lifecycle, or Transmission integration.
5. **Commit Hygiene**: Conventional Commits format (`feat:`, `fix:`, `refactor:`, `test:`, `docs:`, `chore:`). Commits must be atomic — one logical change per commit.
6. **No Secrets in Code**: All configuration comes from environment variables (see AGENTS.md for the full list). Never commit API keys, tokens, or credentials. The `.env` file is gitignored.

## Governance

This constitution is the supreme authority for all technical decisions in the RSSRipple project. When a practice, pattern, or convenience conflicts with a principle above, the principle wins. Exceptions require explicit documentation of the rationale and an amendment to this constitution.

- **Compliance Verification**: All pull requests must be reviewed against each principle. Reviewers check async usage, type completeness, test coverage, filter pipeline integrity, and dynamic parsing extensibility.
- **Runtime Guidance**: `AGENTS.md` provides day-to-day development instructions (project structure, API endpoints, environment variables, integration test infrastructure). It is subordinate to this constitution and must not contradict it.
- **Design Reference**: `DESIGN.md` governs the visual design system (colors, typography, components). `ARCHITECTURE.md` governs module structure and Docker topology. Both are subordinate to this constitution.
- **Amendment Process**: Any principle change requires a version bump, updated `Last Amended` date, and a brief rationale recorded in the commit message. Breaking changes to principles require migration of existing code that relied on the old principle.
- **Complexity Justification**: Any deviation from Simplicity & YAGNI (Principle VI) must include a written justification in the PR description explaining why the simpler approach was insufficient.

**Version**: 1.0.0 | **Ratified**: 2026-06-21 | **Last Amended**: 2026-06-21
