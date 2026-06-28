# Deepwork: Exa AI Agent + Metadata Repository UI

## Phase 0: Research Findings

### Exa AI Agent API (ses_0f22c6380ffezOnDirnPy62TUW)
- Package: `exa_py` v2.15.0, `AsyncExa` class for async
- `exa.agent.runs.create(query, output_schema, effort)` → run ID
- `exa.agent.runs.poll_until_finished(run_id)` → completed run with `.output.structured`
- `output.structured` is the schema-validated JSON dict (None until completed)
- Effort levels: `minimal` ($0.012) / `low` ($0.025) / `medium` ($0.10) / `high` ($0.50) / `xhigh` ($1.00)
- `output_schema` is JSON Schema draft-07 format — enforces types, NOT semantics
- `query` parameter carries behavioral instructions (source preferences, poster URL format, disambiguation)
- Poster URL: `"format": "uri"` in schema + reinforced in query text
- Replaces Jina completely — Exa handles search + reading + reasoning + structured extraction in one API call
- No multi-result search per run — each call returns one structured object
- User provided key: `68067938-677f-424a-bf55-0350e5c799c6`

### Current Codebase (ses_0f22c54d5ffev4lgIMkZQ47cGj)
- `_search_jina()` at `metadata_search_agent.py:497-555` — Phase 2 fallback, called only when TMDB+TVDB miss
- `jina_api_key` already in config. Need to add `exa_api_key`
- Frontend routes: 16 routes, no `/works` route, no edit routes for series/movies
- Series/Movies list pages: Ant Design Table, read-only, no create/delete
- Series/Movies detail pages: read-only display, no edit form, no delete button
- API: DELETE silently nullifies FKs — needs 409 constraint check
- Movie API missing `agent_work_count` (series has it)

## Design Plan

### Work 1: Replace Jina with Exa AI Agent

**Config changes:**
- `app/config.py`: add `exa_api_key: str = ""`
- `.env.example`: add `EXA_API_KEY`
- `.env`: add `EXA_API_KEY=68067938-677f-424a-bf55-0350e5c799c6`
- `pyproject.toml`: add `exa-py>=2.15.0`

**Code changes:**
- `app/services/metadata_search_agent.py`:
  - Replace `_search_jina` function with `_search_exa`
  - New function signature: `async def _search_exa(title: str) -> list[dict[str, Any]]`
  - Uses `AsyncExa`, sets `output_schema` with all MetadataCandidate fields
  - Adds poster URL post-validation: HEAD request to verify Content-Type is image/*
  - `effort="medium"` (best cost-quality ratio)
  - Update `search_metadata()` Phase 2 to call `_search_exa` instead of `_search_jina`
  - Keep same cache pattern (keyed by "exa")

**Poster validation:**
- After Exa returns poster_url, do async HEAD request (3 retries, 5s timeout each)
- Check `Content-Type` starts with `image/`
- If validation fails, drop poster_url (set to None) — don't discard entire result
- Valid poster URL passes through to existing `download_and_cache_poster` pipeline

**Dependency resolution:**
- `exa-py` replaces `requests` dependency (Jina used httpx directly, Exa uses its own SDK)
- Remove jina-specific code (`_is_tv_indicative`, `_extract_title_from_snippet`, `JINA_ENDPOINT`)
- Keep `jina_api_key` config for backward compat but mark deprecated

### Work 2: Metadata Repository UI

**Backend API changes:**

1. `GET /api/v1/works` — Unified poster wall
   - Query params: `?page=1&page_size=20&search=&content_type=tv|movie|all`
   - UNION TVSeries + Movie, add `content_type` discriminator field
   - Response: paginated with normalized fields (title, poster_url, rating, status, year, etc.)

2. `DELETE /api/v1/series/{id}` — Add constraint checks
   - Pre-delete: count FileResource with `series_id=id`, AgentWork with `series_id=id`
   - If any AgentWork references exist → 409 with `DELETE_BLOCKED` error + detail
   - FileResource references: nullify FKs (existing behavior)
   - No constraint on FileResource (only AgentWork blocks deletion per user requirement)

3. `DELETE /api/v1/movies/{id}` — Add constraint checks
   - Same pattern as series

4. `GET /api/v1/movies/{id}` — Add `agent_work_count` to response (parity with series)

**Frontend changes:**

1. New route: `/works` → `WorksPage` — Poster wall
   - Responsive CSS grid of poster cards (180×270px poster + title overlay)
   - Filter tabs: All / TV Series / Movies
   - Search bar
   - Click poster → navigate to detail page

2. New route: `/works/:type/:id/edit` → `WorkEditPage`
   - Renders form fields based on content_type (TV vs Movie)
   - Fields: title_cn, title_en, original_title, description, rating, genre, status
   - TV-specific: number_of_episodes, number_of_seasons, start_date, end_date
   - Movie-specific: release_date, runtime
   - Save → PUT to `/series/{id}` or `/movies/{id}`

3. Enhanced SeriesDetail/MovieDetail — Add delete button
   - "Delete" button with confirmation modal
   - Modal shows: Agent refs count, FileResource refs count
   - On confirm → DELETE API call
   - If 409 → show blocking details in error message

4. Add "Repository" nav item to sidebar (between Agents and Downloaders)

**Route structure:**
```
/works              → WorksPage (poster wall)
/works/:type/:id    → redirect to /series/:id or /movies/:id
/works/:type/:id/edit → WorkEditPage
/series/:id         → SeriesDetail (enhanced with delete)
/movies/:id         → MovieDetail (enhanced with delete)
```

### Oracle Review Decisions (ses_0f2291d9bffeLaA7TkmsPOQOA7)

### Accepted changes:
1. **DELETE now blocks on AgentWork references** → 409 with DELETE_BLOCKED (breaking change, better UX)
2. **`agent_work_count` needed in Movie frontend type** + backend endpoint
3. **Phase 2 gate: `jina_api_key` → `exa_api_key`**
4. **Exa effort default: `"low"`** → add `EXA_EFFORT_LEVEL` env var
5. **Route simplification**: drop `/works/:type/:id` redirect; poster cards link directly to `/series/:id` or `/movies/:id`
6. **Sidebar placement**: Repository after Dashboard, before Channels
7. **Remove Jina dead code**: `_search_jina`, `_is_tv_indicative`, `_extract_title_from_snippet`, `JINA_ENDPOINT`; keep `jina_api_key` config (ignored)
8. **Python-side merge** for works API (not raw SQL UNION)

## Revised Execution Plan

### Phase 1: Backend — Exa + API changes
- Config: add `exa_api_key`, `exa_effort_level: str = "low"`; deprecate `jina_api_key` comment
- Dependencies: `pip install exa-py>=2.15.0`
- metadata_search_agent.py: replace `_search_jina()` → `_search_exa()`, remove Jina helpers
  - POST to Exa Agent with structured output_schema
  - Poster URL post-validation (HEAD request, 3 retries)
  - Update `search_metadata()` Phase 2 gate
- New endpoint: `GET /api/v1/works` (Python-side merge, paginated, searchable)
- DELETE /series/{id}, DELETE /movies/{id}: add AgentWork constraint check → 409
- GET /movies/{id}: add `agent_work_count`
- .env: add `EXA_API_KEY=68067938-677f-424a-bf55-0350e5c799c6`

### Phase 2: Frontend — UI/UX (@designer)
- `/works` → WorksPage: poster wall grid, filter tabs, search
- `/works/:type/:id/edit` → WorkEditPage: unified edit form
- Enhanced SeriesDetail/MovieDetail: delete button + constraint dialog
- Sidebar: "Repository" nav item after Dashboard
- TypeScript: Movie type + `agent_work_count`, new Works types

### Phase 3: Tests + Verify
- Update test_metadata_search_agent.py: remove Jina tests, add Exa tests
- New tests for works endpoint
- Full test suite pass
