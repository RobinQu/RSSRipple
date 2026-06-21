# Tasks: RSSRipple

**Input**: Design documents from `/specs/001-rssripple/`

**Prerequisites**: plan.md (required), spec.md (required), data-model.md, contracts/api-spec.md

**Tests**: Tests ARE included — the project requires comprehensive testing.

**Organization**: Tasks are grouped by user story to enable independent implementation and testing of each story.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (e.g., US1, US2, US3)
- Include exact file paths in descriptions

## Path Conventions

- Backend: `app/` at repository root
- Frontend: `frontend/src/`
- Tests: `tests/` at repository root

---

## Phase 1: Setup (Shared Infrastructure)

**Goal**: Bootstrap project structure, dependencies, and configuration so all subsequent phases can build on a working foundation.

| ID | P | Story | Description |
|----|---|-------|-------------|
| T001 | | Setup | Create `pyproject.toml` with all dependencies: FastAPI, SQLAlchemy 2.0 (async), Pydantic v2, feedparser, transmission-rpc, APScheduler, aiosqlite, httpx, pytest, pytest-asyncio, python-Levenshtein, uvicorn |
| T002 | P | Setup | Create `app/config.py` with `Settings(BaseSettings)` class: `database_url`, `llm_api_key`, `llm_model`, `llm_base_url`, `tvdb_api_key`, `default_fetch_interval`, `max_retry_count`, `task_expire_days`, `log_level`, `debug` |
| T003 | P | Setup | Create `app/database.py` with async SQLAlchemy engine, `async_sessionmaker`, `Base` declarative base, and `get_db()` dependency yielding `AsyncSession` |
| T004 | | Setup | Create `app/main.py` with FastAPI app factory: register CORS middleware, include `/api/v1/` router prefix, mount static files for frontend SPA, configure logging from settings |
| T005 | P | Setup | Create `Dockerfile` (multi-stage: node:20-slim for frontend build, python:3.12-slim + uv for runtime) and `docker-compose.yml` (app + transmission services) |

**Checkpoint**: `uv run uvicorn app.main:app` starts without errors; database file created on first run.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Goal**: All database models, Pydantic schemas, base API infrastructure, and error handling — required before any user story can be implemented.

### Database Models

| ID | P | Story | Description |
|----|---|-------|-------------|
| T006 | P | F | Create `app/models/__init__.py` with imports of all model classes for Alembic/metadata discovery |
| T007 | P | F | Create `app/models/channel.py` — `Channel` model: `id` (UUID PK), `name`, `type` (enum: rss_feed), `url`, `fetch_interval`, `field_mapping` (JSON TEXT), `parser_type` (enum: auto/mikanani/custom), `last_fetched_at`, `status` (enum: active/inactive/error), `created_at`, `updated_at` |
| T008 | P | F | Create `app/models/file_resource.py` — `FileResource` model: `id`, `channel_id` (FK → channels), `guid` (unique, indexed), `title_raw`, `title_cn`, `title_en`, `subtitle_group`, `episode`, `resolution`, `source`, `video_codec`, `audio_codec`, `subtitle_type`, `container`, `file_size`, `torrent_url`, `detail_url`, `published_at`, `parsed_at`, `episode_id` (FK → episodes), `movie_id` (FK → movies), `created_at` |
| T009 | P | F | Create `app/models/episode.py` — `Episode` model: `id`, `series_id` (FK → series), `episode_number`, `title`, `air_date`, `preferred_profile_id` (FK → episodes, self-ref for profile chain), `created_at` |
| T010 | P | F | Create `app/models/series.py` — `TVSeries` model: `id`, `title_cn`, `title_en`, `aliases` (JSON list), `external_id`, `external_source`, `description`, `genre` (JSON list), `start_date`, `content_type`, `created_at`, `updated_at` |
| T011 | P | F | Create `app/models/movie.py` — `Movie` model: `id`, `title_cn`, `title_en`, `aliases` (JSON list), `external_id`, `external_source`, `description`, `release_date`, `content_type`, `created_at`, `updated_at` |
| T012 | P | F | Create `app/models/filter.py` — `ResourceFilter` model: `id`, `agent_id` (FK → agents), `field` (enum), `operator` (enum: eq/contains/fuzzy/in/regex), `value`, `priority`, `is_required`, `created_at` |
| T013 | P | F | Create `app/models/agent.py` — `Agent` model: `id`, `name`, `channel_id` (FK → channels, ondelete=CASCADE), `downloader_id` (FK → downloader_instances), `download_dir`, `task_expire_days`, `llm_enabled`, `metadata_source` (enum: imdb/tvdb/none), `content_type` (enum: anime/tv/movie/mixed), `status` (enum: active/paused/error), `last_run_at`, `created_at`, `updated_at` |
| T014 | P | F | Create `app/models/downloader.py` — `DownloaderInstance` model: `id`, `name`, `type` (enum: transmission), `url`, `username`, `password`, `status` (enum: connected/disconnected/error), `last_checked_at`, `created_at`, `updated_at` |
| T015 | P | F | Create `app/models/download_task.py` — `DownloadTask` model: `id`, `agent_id` (FK → agents), `file_resource_id` (FK → file_resources), `downloader_id` (FK → downloader_instances), `transmission_torrent_id`, `status` (enum: pending/queued/downloading/paused/completed/error/cancelled), `progress`, `download_speed`, `eta`, `error_message`, `retry_count`, `max_retries`, `confirmed_at`, `completed_at`, `created_at`, `updated_at` |
| T016 | P | F | Create `app/models/pending_decision.py` — `PendingDecision` model: `id`, `agent_id` (FK → agents), `episode_id` (FK → episodes), `movie_id` (FK → movies), `candidates` (JSON list of UUIDs), `reason`, `llm_suggestion`, `decided_resource_id` (FK → file_resources), `status` (enum: pending/decided/expired/skipped), `created_at`, `decided_at` |

### Pydantic Schemas

| ID | P | Story | Description |
|----|---|-------|-------------|
| T017 | P | F | Create `app/schemas/common.py` — `APIResponse[T]` envelope (`success`, `data`, `error`, `meta`), `PaginationMeta` (`page`, `page_size`, `total`), `ErrorResponse` (`code`, `message`), `ListQueryParams` (`page`, `page_size`) |
| T018 | P | F | Create `app/schemas/channel.py` — `ChannelCreate`, `ChannelUpdate`, `ChannelResponse`, `ChannelListResponse`, `ValidateUrlRequest`, `ValidateUrlResponse`, `FieldMappingProposal`, `ApplyMappingRequest` |
| T019 | P | F | Create `app/schemas/agent.py` — `AgentCreate` (with nested `filters` list), `AgentUpdate`, `AgentResponse`, `AgentListResponse`, `TestFiltersRequest`, `TestFiltersResponse` |
| T020 | P | F | Create `app/schemas/filter.py` — `ResourceFilterCreate`, `ResourceFilterUpdate`, `ResourceFilterResponse` |
| T021 | P | F | Create `app/schemas/downloader.py` — `DownloaderCreate`, `DownloaderUpdate`, `DownloaderResponse`, `DownloaderTestResponse` |
| T022 | P | F | Create `app/schemas/download_task.py` — `DownloadTaskResponse`, `DownloadTaskListResponse` |
| T023 | P | F | Create `app/schemas/pending_decision.py` — `PendingDecisionResponse`, `PendingDecisionListResponse`, `ConfirmDecisionRequest` |
| T024 | P | F | Create `app/schemas/file_resource.py` — `FileResourceResponse`, `FileResourceListResponse` |
| T025 | P | F | Create `app/schemas/series.py` — `SeriesCreate`, `SeriesUpdate`, `SeriesResponse`; `app/schemas/movie.py` — `MovieCreate`, `MovieUpdate`, `MovieResponse`; `app/schemas/episode.py` — `EpisodeResponse` |
| T026 | | F | Create `app/schemas/__init__.py` re-exporting all schema classes |

### Base Infrastructure

| ID | P | Story | Description |
|----|---|-------|-------------|
| T027 | | F | Create `app/api/deps.py` — FastAPI dependency providers: `get_db()` (async session), `get_settings()` (Settings singleton) |
| T028 | | F | Create `app/api/__init__.py` and `app/api/v1/__init__.py` — register all v1 route modules into a single `APIRouter` with `/api/v1` prefix |
| T029 | | F | Implement structured error handling: custom exceptions in `app/utils/errors.py` (`NotFoundError`, `ValidationError`, `ConflictError`), global exception handlers in `app/main.py` returning `APIResponse` envelope with error codes |
| T030 | P | F | Create `app/utils/__init__.py` and `app/utils/fuzzy_match.py` — `fuzzy_ratio(a, b) -> float` using Levenshtein distance, `is_fuzzy_match(a, b, threshold=0.7) -> bool` |

### Database Initialization

| ID | | Story | Description |
|----|---|-------|-------------|
| T031 | | F | Add `startup` event in `app/main.py` to run `Base.metadata.create_all(engine)` on app start, creating all tables in SQLite |
| T032 | | F | Create `tests/conftest.py` — shared pytest fixtures: `async_client` (httpx AsyncClient with TestClient), `db_session` (in-memory SQLite async session), `sample_channel`, `sample_agent`, `sample_resource` factory fixtures |

**Checkpoint**: All models create tables in SQLite; `pytest tests/` runs with fixtures available; API returns `{ "success": true, "data": null }` for a health-check route.

---

## Phase 3: US1 — Create RSS Subscription Channel

**Goal**: Users can create channels, validate RSS URLs, trigger manual fetches, and preview parsed resources.

**Independent Test**: Create a channel via API with a valid RSS URL, verify the feed is fetched, entries are parsed into FileResources, and the channel is persisted with status `active`.

### Tests (Write First)

| ID | P | Story | Description |
|----|---|-------|-------------|
| T033 | | US1 | Write `tests/unit/test_title_parser.py` — test `app/services/title_parser.py` against mikanani-format titles: extract subtitle_group, title_cn, title_en, episode, resolution, source, video_codec, audio_codec, subtitle_type, container; test edge cases (missing fields, non-standard formats) |
| T034 | P | US1 | Write `tests/api/test_channels.py` — test channel CRUD endpoints: create with valid URL returns 201 + channel data; create with invalid URL returns 400; list with pagination; get by id; update fetch_interval; delete cascades FileResources; validate-url endpoint returns reachable/unreachable |
| T035 | P | US1 | Write `tests/api/test_resources.py` — test `GET /api/v1/channels/{id}/resources` returns paginated FileResources; test `GET /api/v1/resources/{id}` returns resource detail |

### Implementation

| ID | P | Story | Description |
|----|---|-------|-------------|
| T036 | | US1 | Create `app/clients/rss_parser.py` — `fetch_feed(url) -> feedparser.FeedParserDict` using httpx async GET with timeout + retries; `parse_entries(feed, field_mapping) -> list[dict]` dispatching to title_parser or resource_parser |
| T037 | | US1 | Create `app/services/title_parser.py` — `parse_mikanani_title(title_raw: str) -> dict` with regex extraction for all fields: `subtitle_group`, `title_cn`, `title_en`, `episode`, `resolution`, `source`, `video_codec`, `audio_codec`, `subtitle_type`, `container` |
| T038 | | US1 | Create `app/services/resource_parser.py` — `parse_with_mapping(entry: dict, mapping: dict) -> dict` implementing dotted-path source resolution, regex capture groups, and transforms (int, float, iso_datetime, lowercase, uppercase) |
| T039 | | US1 | Create `app/services/channel_service.py` — `create_channel(db, data)`, `get_channel(db, id)`, `list_channels(db, page, page_size)`, `update_channel(db, id, data)`, `delete_channel(db, id)`, `validate_url(url) -> bool`, `fetch_and_parse(db, channel_id)` (fetch RSS, deduplicate by guid, persist FileResources) |
| T040 | | US1 | Create `app/api/v1/channels.py` — POST/GET/PUT/DELETE `/channels`, POST `/channels/validate-url`, POST `/channels/{id}/fetch`, POST `/channels/{id}/analyze`, POST `/channels/{id}/apply-mapping` |
| T041 | P | US1 | Create `app/api/v1/resources.py` — GET `/channels/{channel_id}/resources` (paginated), GET `/resources/{id}` |

**Checkpoint**: `POST /api/v1/channels` with a valid mikanani RSS URL creates channel + populates FileResources; `GET /api/v1/channels` returns paginated list; title parser unit tests pass.

---

## Phase 4: US2 — Configure Agent Filter Rules

**Goal**: Users can create Agents with filter rules; the system applies three-tier resolution to select resources for download.

**Independent Test**: Create an Agent with required filter `resolution eq 1080p` and optional filters; trigger RSS processing; verify only matching resources are selected and unique matches auto-enqueue.

### Tests (Write First)

| ID | P | Story | Description |
|----|---|-------|-------------|
| T042 | | US2 | Write `tests/unit/test_filter_service.py` — test filter matching: required `eq` filter passes/fails; optional filter adds score; `contains` operator; `fuzzy` operator using Levenshtein; `in` operator (list membership); `regex` operator; priority ordering; required gate eliminates non-matching; scoring ranks candidates correctly |
| T043 | P | US2 | Write `tests/api/test_agents.py` — test Agent CRUD: create with nested filters; list with pagination; get includes filters + tasks count; update; delete cascades filters + tasks; `POST /agents/{id}/run` triggers processing; `POST /agents/{id}/test-filters` returns match results |
| T044 | P | US2 | Write `tests/api/test_filters.py` — test ResourceFilter CRUD: add filter to agent; update filter value; delete filter; list filters by agent |

### Implementation

| ID | P | Story | Description |
|----|---|-------|-------------|
| T045 | | US2 | Create `app/services/filter_service.py` — `apply_filters(resources: list[FileResource], filters: list[ResourceFilter]) -> FilterResult` implementing: (1) required filter gate — reject if any required filter fails, (2) optional filter scoring — sum weighted scores by priority, (3) return `FilterResult` with `unique_match`, `candidates` (sorted by score), `no_match` flag |
| T046 | | US2 | Create `app/services/agent_service.py` — `create_agent(db, data)` (with nested filter creation), `get_agent(db, id)`, `list_agents(db, page, page_size)`, `update_agent(db, id, data)`, `delete_agent(db, id)`, `process_resources(db, agent, resources)` (orchestrates Tier 1 → Tier 2 → Tier 3), `test_filters(db, agent_id)` (dry-run filter matching against channel resources) |
| T047 | | US2 | Create `app/api/v1/agents.py` — POST/GET/PUT/DELETE `/agents`, POST `/agents/{id}/run`, POST `/agents/{id}/test-filters` |
| T048 | P | US2 | Create `app/api/v1/filters.py` — GET/POST `/agents/{agent_id}/filters`, PUT/DELETE `/agents/{agent_id}/filters/{id}` |

**Checkpoint**: Agent with `resolution eq 1080p` (required) + `subtitle_group eq LoliHouse` (optional) correctly selects unique match from test resources; filter unit tests pass; Agent API CRUD works.

---

## Phase 5: US5 — Manage Transmission Instances

**Goal**: Users can add, test, and manage Transmission downloader instances.

**Independent Test**: Create a DownloaderInstance, test the connection, verify status updates to `connected` or `error`.

### Tests (Write First)

| ID | P | Story | Description |
|----|---|-------|-------------|
| T049 | | US5 | Write `tests/api/test_downloaders.py` — test DownloaderInstance CRUD: create returns 201; list; get by id; update URL/credentials; delete (with warning check for referencing agents); test-connection endpoint updates status |

### Implementation

| ID | P | Story | Description |
|----|---|-------|-------------|
| T050 | | US5 | Create `app/clients/transmission.py` — `TransmissionClient` wrapping transmission-rpc: `__init__(url, username, password)`, `test_connection() -> bool`, `add_torrent(url, download_dir) -> torrent_id`, `get_torrent(torrent_id) -> TorrentStatus`, `pause_torrent(torrent_id)`, `resume_torrent(torrent_id)`, `remove_torrent(torrent_id)`; all sync calls wrapped in `asyncio.run_in_executor` |
| T051 | | US5 | Create `app/services/downloader_service.py` — `create_downloader(db, data)`, `get_downloader(db, id)`, `list_downloaders(db)`, `update_downloader(db, id, data)`, `delete_downloader(db, id)` (check for referencing agents), `test_connection(db, id)` (instantiate client, call test, update status) |
| T052 | | US5 | Create `app/api/v1/downloaders.py` — POST/GET/PUT/DELETE `/downloaders`, POST `/downloaders/{id}/test` |

**Checkpoint**: DownloaderInstance CRUD works; test-connection endpoint returns `connected`/`error` status.

---

## Phase 6: US3 — View Download Progress

**Goal**: Dashboard shows active Agents, download tasks with progress/speed/ETA, and pending decisions.

**Independent Test**: Create download tasks with varying statuses; verify `GET /api/v1/dashboard` returns aggregated data with speeds and progress.

### Tests (Write First)

| ID | P | Story | Description |
|----|---|-------|-------------|
| T053 | | US3 | Write `tests/api/test_dashboard.py` — test `GET /api/v1/dashboard` returns active agents with task counts, active downloads with progress/speed/ETA, pending decisions with candidate counts |
| T054 | P | US3 | Write `tests/api/test_tasks.py` — test task endpoints: get task detail; pause transitions to `paused`; resume transitions to `downloading`; retry resets error state; delete removes task |

### Implementation

| ID | P | Story | Description |
|----|---|-------|-------------|
| T055 | | US3 | Create `app/services/download_service.py` — `create_task(db, agent_id, resource_id, downloader_id)`, `get_task(db, id)`, `list_tasks_by_agent(db, agent_id, page, page_size)`, `pause_task(db, id)`, `resume_task(db, id)`, `retry_task(db, id)`, `delete_task(db, id)`, `sync_progress(db, task_id)` (poll Transmission, update progress/speed/eta/status), `submit_to_transmission(db, task_id)` (add torrent to Transmission, store torrent_id) |
| T056 | | US3 | Create `app/services/dashboard_service.py` — `get_dashboard(db)` returning: active agents (with task counts + aggregate download speed), active downloads (tasks in downloading/queued status with progress), pending decisions (count + latest items) |
| T057 | | US3 | Create `app/api/v1/dashboard.py` — GET `/dashboard` |
| T058 | P | US3 | Create `app/api/v1/tasks.py` — GET `/agents/{agent_id}/tasks` (paginated), GET `/tasks/{id}`, POST `/tasks/{id}/pause`, POST `/tasks/{id}/resume`, POST `/tasks/{id}/retry`, DELETE `/tasks/{id}` |

### Frontend — Dashboard & Download Pages

| ID | P | Story | Description |
|----|---|-------|-------------|
| T059 | P | US3 | Create `frontend/src/types/index.ts` — TypeScript interfaces: `Channel`, `Agent`, `ResourceFilter`, `Downloader`, `DownloadTask`, `PendingDecision`, `FileResource`, `Dashboard`, `APIResponse<T>`, `PaginationMeta` |
| T060 | P | US3 | Create `frontend/src/api/client.ts` — generic `apiRequest<T>(method, path, body?)` with base URL, JSON headers, error handling; `frontend/src/api/channels.ts`, `agents.ts`, `downloaders.ts`, `tasks.ts` — typed API call functions |
| T061 | P | US3 | Create `frontend/src/hooks/useApi.ts` — generic async state hook with loading/error/data; `frontend/src/hooks/usePolling.ts` — auto-refresh hook with configurable interval, start/stop control |
| T062 | P | US3 | Create `frontend/src/components/Layout.tsx` (sidebar + main content), `Sidebar.tsx` (nav links: Dashboard, Channels, Downloaders, Agents), `ProgressBar.tsx` (animated bar with % + speed + ETA), `StatusBadge.tsx` (color-coded status pills), `Pagination.tsx`, `Modal.tsx` |
| T063 | | US3 | Create `frontend/src/pages/Dashboard.tsx` — three sections: Active Agents (card grid with task counts + aggregate speed), Active Downloads (task list with ProgressBar components), Pending Decisions (list with candidate count badges); uses `usePolling` for auto-refresh |
| T064 | P | US3 | Create `frontend/src/App.tsx` — React Router setup with Layout wrapper and routes: `/` → Dashboard, `/channels` → Channels, `/channels/new` → ChannelForm, `/channels/:id` → ChannelDetail, `/downloaders` → Downloaders, `/downloaders/new` → DownloaderForm, `/agents` → Agents, `/agents/new` → AgentForm, `/agents/:id` → AgentDetail |
| T065 | P | US3 | Create `frontend/src/main.tsx` — React 18 `createRoot`, render `<App />` with StrictMode; `frontend/src/utils/format.ts` — `formatBytes`, `formatSpeed`, `formatETA`, `formatDate` utilities |

**Checkpoint**: Dashboard page renders with mock data; API returns correct aggregated dashboard data; frontend builds with Vite.

---

## Phase 7: US4 — Manual Version Selection

**Goal**: When rule filtering + LLM cannot determine a unique match, a PendingDecision is created; users can confirm or skip.

**Independent Test**: Create an ambiguous filter scenario producing multiple candidates; verify PendingDecision is created; confirm a candidate creates a DownloadTask; skip marks decision as `skipped`.

### Tests (Write First)

| ID | P | Story | Description |
|----|---|-------|-------------|
| T066 | | US4 | Write `tests/api/test_decisions.py` — test: confirm decision with resource_id creates DownloadTask + marks decision `decided`; skip decision marks `skipped`; list decisions by agent (paginated, only `pending` status) |

### Implementation

| ID | P | Story | Description |
|----|---|-------|-------------|
| T067 | | US4 | Extend `app/services/agent_service.py` — in `process_resources`, when Tier 1 produces multiple matches and Tier 2 (LLM) is disabled or fails: create `PendingDecision` with all candidate resource IDs, reason string, and optional LLM suggestion |
| T068 | | US4 | Create decision handling in `app/services/download_service.py` — `confirm_decision(db, decision_id, resource_id)`: validate resource is in candidates, create DownloadTask, update decision status to `decided`; `skip_decision(db, decision_id)`: update status to `skipped` |
| T069 | | US4 | Create `app/api/v1/decisions.py` — GET `/agents/{agent_id}/decisions` (paginated, filter by status), POST `/decisions/{id}/confirm`, POST `/decisions/{id}/skip` |

### Frontend — Decision UI

| ID | P | Story | Description |
|----|---|-------|-------------|
| T070 | | US4 | Create `frontend/src/pages/AgentDetail.tsx` — two tabs: Download Tasks (paginated task list with status badges + ProgressBar) and Pending Decisions (candidate list with resource details, confirm/skip buttons per decision); uses `usePolling` for task progress |
| T071 | P | US4 | Add `frontend/src/api/decisions.ts` — `confirmDecision(id, resourceId)`, `skipDecision(id)`, `listDecisions(agentId, page)` |

**Checkpoint**: Ambiguous scenario creates PendingDecision visible in AgentDetail; confirming a candidate creates a DownloadTask; skipping updates status.

---

## Phase 8: US6 — Automatic Subtitle Group Consistency

**Goal**: New episodes automatically prefer the subtitle group and parameters from previous episodes in the same series.

**Independent Test**: Download episode 1 from "LoliHouse" at 1080p MKV; process episode 2 with multiple subtitle groups; verify "LoliHouse 1080p MKV" candidate scores highest.

### Tests (Write First)

| ID | P | Story | Description |
|----|---|-------|-------------|
| T072 | | US6 | Write `tests/unit/test_consistency.py` — test consistency scoring: EpisodeProfile from episode 1 applied as bonuses to episode 2 candidates; matching subtitle_group adds score; matching resolution adds score; matching container adds score; combined matches rank highest; tied scores escalate to Tier 2/3 |

### Implementation

| ID | P | Story | Description |
|----|---|-------|-------------|
| T073 | | US6 | Extend `app/services/agent_service.py` — after successful download, store EpisodeProfile (subtitle_group, resolution, container, video_codec, subtitle_type) on the Episode record; update `preferred_profile_id` |
| T074 | | US6 | Extend `app/services/filter_service.py` — add `apply_consistency_bonus(candidates, episode_profile) -> scored_candidates`: compare each candidate's fields against the EpisodeProfile, add weighted bonus scores for matching fields; integrate into Tier 1 pipeline before final ranking |
| T075 | | US6 | Extend `app/services/metadata_service.py` — `match_resource_to_series(db, resource)`: fuzzy-match resource title against TVSeries titles + aliases, associate FileResource with Episode (create Episode if needed), return matched Episode for consistency lookup |
| T076 | P | US6 | Create `app/api/v1/series.py` — POST/GET/PUT/DELETE `/series` (CRUD with pagination) |
| T077 | P | US6 | Create `app/api/v1/movies.py` — POST/GET/PUT/DELETE `/movies` (CRUD with pagination) |

### Frontend — Channel & Agent Management

| ID | P | Story | Description |
|----|---|-------|-------------|
| T078 | P | US6 | Create `frontend/src/pages/Channels.tsx` — paginated channel list with status badges, last-fetched timestamps, fetch-now buttons |
| T079 | P | US6 | Create `frontend/src/pages/ChannelForm.tsx` — create/edit channel form: name, URL input with validate button, fetch interval, parser type selector; field mapping review section after analyze |
| T080 | P | US6 | Create `frontend/src/pages/Agents.tsx` — paginated agent list with channel name, status, task count |
| T081 | P | US6 | Create `frontend/src/pages/AgentForm.tsx` — create/edit agent form: name, channel selector, downloader selector, download dir, LLM toggle, content type; filter builder section (add/remove/edit filters with field, operator, value, priority, required toggle) |
| T082 | P | US6 | Create `frontend/src/pages/Downloaders.tsx` — downloader list with status badges, test-connection buttons; `frontend/src/pages/DownloaderForm.tsx` — create/edit form with name, URL, credentials, test button |

**Checkpoint**: Consistency scoring unit tests pass; Series/Movie CRUD works; all frontend pages render and connect to API.

---

## Phase 9: US7 — LLM-Assisted Decision Making

**Goal**: When rule filtering produces multiple matches and LLM is enabled, invoke LLM for intelligent selection.

**Independent Test**: Configure Agent with `llm_enabled: true`; create ambiguous scenario; verify LLM client is called with candidate details; verify definitive choice enqueues download; verify failure falls back to PendingDecision.

### Tests (Write First)

| ID | P | Story | Description |
|----|---|-------|-------------|
| T083 | | US7 | Write `tests/unit/test_llm_service.py` — test LLM service with mocked client: successful response parses recommendation; API failure returns `None` gracefully; timeout handled; response includes reasoning string |
| T084 | P | US7 | Write `tests/unit/test_feed_analyzer.py` — test feed analyzer: given sample RSS entries, LLM returns valid field_mapping JSON; malformed response handled gracefully |

### Implementation

| ID | P | Story | Description |
|----|---|-------|-------------|
| T085 | | US7 | Create `app/clients/llm_client.py` — `LLMClient` using httpx async: `__init__(base_url, api_key, model)`, `chat(messages: list[dict]) -> str` (OpenAI-compatible `/chat/completions` endpoint), timeout + retry logic, error handling |
| T086 | | US7 | Create `app/services/llm_service.py` — `decide_candidates(client, candidates: list[dict], context: dict) -> LLMDecision` (send candidate details + series context to LLM, parse response for selected resource ID + reasoning); handle failures by returning `None` |
| T087 | | US7 | Create `app/services/feed_analyzer.py` — `analyze_feed(client, sample_entries: list[dict]) -> FieldMappingProposal` (send sample entries to LLM, parse proposed field_mapping JSON, validate structure) |
| T088 | | US7 | Extend `app/services/agent_service.py` — integrate Tier 2: when Tier 1 produces multiple matches and `agent.llm_enabled`, call `llm_service.decide_candidates()`; if definitive choice → enqueue download; if undecided/failed → create PendingDecision with `llm_suggestion` field populated |
| T089 | | US7 | Wire `POST /channels/{id}/analyze` in `app/api/v1/channels.py` to call `feed_analyzer.analyze_feed()` and return proposed mapping; wire `POST /channels/{id}/apply-mapping` to persist `field_mapping` on Channel |

**Checkpoint**: LLM service unit tests pass (with mocks); feed analyzer produces valid mappings; Agent with LLM enabled processes ambiguous candidates through Tier 2.

---

## Phase 10: Scheduler & Background Processing

**Goal**: APScheduler automatically fetches RSS feeds at configured intervals and triggers Agent processing.

| ID | P | Story | Description |
|----|---|-------|-------------|
| T090 | | SCH | Create `app/services/scheduler.py` — `init_scheduler()`: create `AsyncIOScheduler`, register periodic jobs for each active Channel (`fetch_interval` trigger); `register_channel_job(channel_id, interval)`, `remove_channel_job(channel_id)`; `fetch_channel_rss(channel_id)` fetches + parses + triggers all agents for that channel |
| T091 | | SCH | Wire scheduler into `app/main.py` startup/shutdown: `init_scheduler()` on startup, `scheduler.shutdown()` on shutdown; register jobs for all active channels on boot |
| T092 | P | SCH | Add download progress polling: background task in `app/services/scheduler.py` that periodically queries Transmission for active DownloadTasks and syncs progress/speed/eta/status to database |
| T093 | P | SCH | Implement retry logic in `app/services/download_service.py`: on `error` status, check `retry_count < max_retries`, apply exponential backoff, re-submit to Transmission; on max retries exceeded, set status to `cancelled` |

**Checkpoint**: Scheduler registers jobs on startup; RSS fetches run at configured intervals; download progress auto-syncs; failed tasks retry with backoff.

---

## Phase 11: Polish & Cross-Cutting Concerns

**Goal**: Integration tests, frontend polish, Docker packaging, and final validation.

### Integration Tests

| ID | P | Story | Description |
|----|---|-------|-------------|
| T094 | | INT | Create `docker-compose.test.yml` — services: `app` (RSSRipple in test mode), `test-server` (mock RSS feeds + BT tracker + torrent API on port 8080), `test-runner` (pytest after health checks), `transmission` (real instance on port 9092) |
| T095 | P | INT | Create `tests/integration/server/` — mock RSS feed server: `GET /rss/mikanani` (mikanani-format feed with .torrent enclosures), `GET /rss/dmhy` (dmhy-format with magnet links), `GET /rss/eztv` (scene-format TV feed); minimal HTTP tracker (`/announce`, `/scrape`); torrent file serving |
| T096 | | INT | Write `tests/integration/test_torrent_lifecycle.py` — create torrent via test-server → seed → submit as DownloadTask → verify Transmission downloads → assert file integrity |
| T097 | P | INT | Write `tests/integration/test_rss_subscription.py` — validate RSS URL against test-server → create Channel → trigger fetch → verify FileResources parsed correctly → create Agent → verify filtering |
| T098 | P | INT | Write `tests/integration/test_filter_metadata.py` — create agents with varied filters → test filter matching against test-server resources → create Series/Movie records → verify metadata association |

### Docker & Deployment

| ID | P | Story | Description |
|----|---|-------|-------------|
| T099 | P | POL | Finalize `Dockerfile` multi-stage build: Stage 1 builds frontend (node:20-slim → npm ci → npm run build → /frontend/dist); Stage 2 copies app/ + frontend dist into python:3.12-slim with uv, runs uvicorn serving both API and static files |
| T100 | P | POL | Finalize `docker-compose.yml` with volume mounts for SQLite data directory and Transmission downloads; environment variable passthrough for LLM_API_KEY, DATABASE_URL |

### Frontend Polish

| ID | P | Story | Description |
|----|---|-------|-------------|
| T101 | P | POL | Apply DESIGN.md styling to all frontend pages: Raycast-inspired dark theme (canvas #07080a, surface ladder, hairline borders #242728), Inter font with feature settings, white CTA pills, 8px spacing unit, 6-16px border radius range |

### Final Validation

| ID | | POL | Description |
|----|---|-------|-------------|
| T102 | | POL | Run full test suite: `uv run pytest tests/unit tests/api -v` — all unit + API tests pass; verify test coverage for filter matching, title parsing, channel CRUD, agent processing pipeline, decision flow |

**Checkpoint**: All tests pass; Docker image builds and runs; frontend serves from same container; integration tests validate end-to-end flow.

---

## Dependencies & Execution Order

```
Phase 1 (Setup)
  └─→ Phase 2 (Foundational)
        ├─→ Phase 3 (US1 - Channels)
        │     └─→ Phase 4 (US2 - Filters/Agents) ← also needs Phase 5 (Downloaders)
        ├─→ Phase 5 (US5 - Downloaders)
        │     └─→ Phase 6 (US3 - Dashboard) ← also needs Phase 4
        ├─→ Phase 6 (US3 - Dashboard)
        │     └─→ Phase 7 (US4 - Decisions) ← needs Phase 4 + Phase 6
        ├─→ Phase 8 (US6 - Consistency) ← needs Phase 4
        ├─→ Phase 9 (US7 - LLM) ← needs Phase 3 (feed_analyzer) + Phase 4
        └─→ Phase 10 (Scheduler) ← needs Phase 3 + Phase 4
              └─→ Phase 11 (Polish/Integration)
```

**Critical Path**: Setup → Foundational → US1 (Channels) → US2 (Filters) → US5 (Downloaders) → US3 (Dashboard) → US4 (Decisions) → Scheduler → Integration Tests

## Parallel Opportunities

| Group | Tasks | Reason |
|-------|-------|--------|
| **Phase 2 Models** | T007–T016 (all 10 models) | Independent files, no cross-dependencies |
| **Phase 2 Schemas** | T018–T025 (all 8 schema files) | Independent files, depend only on models |
| **Phase 5 vs Phase 3** | T049–T052 and T033–T041 | Downloaders and Channels are independent entities |
| **Phase 6 Frontend** | T059–T065 (frontend infra + pages) | Independent from backend once API contracts are defined |
| **Phase 8 Frontend** | T078–T082 (all management pages) | Independent pages, parallel development |
| **Phase 11 Integration** | T095, T097, T098 | Independent test scenarios |

## Implementation Strategy

### MVP First (Phases 1–5)
Get the core pipeline working end-to-end:
1. Create a channel with a valid RSS URL → resources are parsed
2. Create a downloader instance → connection tested
3. Create an agent with filter rules → rule-based matching selects a unique resource
4. Download task created → submitted to Transmission

### Incremental Delivery (Phases 6–9)
Add intelligence and visibility:
1. Dashboard shows real-time download progress
2. Ambiguous matches create PendingDecisions for human resolution
3. Consistency matching prefers subtitle groups from previous episodes
4. LLM integration handles complex ambiguous scenarios

### Production Ready (Phases 10–11)
Automate and harden:
1. Scheduler drives periodic RSS fetches without manual triggers
2. Retry logic handles transient failures
3. Integration tests validate the full stack in Docker
4. Frontend polished with design system