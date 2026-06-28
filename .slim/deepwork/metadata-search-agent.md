# Deepwork: Metadata Search Agent Refactor

## Goal
Replace single LLM web-search call in `search_metadata_via_llm()` with a multi-source metadata search agent:
1. TMDB API (tmdbsimple) — primary, best structured data, free
2. TVDB v4 API (tvdb-v4-python) — complementary, requires subscription
3. Jina Search API — fallback only

## Files Involved

### Code to modify
- `app/config.py` — add `tmdb_api_key`, `tvdb_api_key`, `jina_api_key`
- `app/services/metadata_service.py` — update `search_metadata_via_llm()` → new agent
- `pyproject.toml` — add `tmdbsimple` dependency

### Code to create
- `app/services/metadata_search_agent.py` — new multi-source agent module
- `tests/unit/test_metadata_search_agent.py` — unit tests
- `tests/integration/test_metadata_search_agent_integration.py` — integration tests with 20+ test data

## Research Context (from @librarian ses_0f2554382ffeLSMrk2aJGATVXS)

### TMDB (tmdbsimple)
- `tmdbsimple.Search().multi(query, language)` → `results[]` with `{id, media_type, title/name, overview, poster_path, vote_average, genre_ids, release_date/first_air_date}`
- `tmdbsimple.TV(id).info(language)` → full series details
- `tmdbsimple.Movies(id).info(language)` → full movie details
- Poster: `f"{IMAGE_BASE}w500{poster_path}"`
- Rate limit: ~50 req/sec

### TVDB (tvdb-v4-python)
- `tvdb.search(query, type="series", limit=5)` → list of SearchResult
- `tvdb.get_series_extended(id)` → full details with translations
- `tvdb.get_movie_extended(id)` → full movie details
- Chinese title from translations: `nameTranslations` → filter `language=="zho"`
- Requires $12/yr subscription; API key + optional PIN

### Jina Search
- POST `https://s.jina.ai/` with `{"q": "...", "num": 10}`, Bearer auth
- Returns web search results with title, url, content, description
- `X-Site` header for domain-restricted search

## Architecture Plan

### New module: `app/services/metadata_search_agent.py`

```
search_metadata(title: str) -> list[dict]
  ├── _search_tmdb(title) -> list[dict]       # Primary
  │     ├── search.multi(query)
  │     ├── filter media_type in (tv, movie)
  │     ├── for each hit: fetch full details
  │     └── normalize to dict matching existing RSSRipple format
  │
  ├── _search_tvdb(title) -> list[dict]       # Secondary (if API key set)
  │     ├── search(query, type="series"/"movie")
  │     ├── get_series_extended / get_movie_extended
  │     └── normalize
  │
  └── _search_jina_fallback(title) -> list[dict]  # Tertiary
        ├── POST https://s.jina.ai/ q=title
        ├── optional X-Site restrictions
        └── minimal normalization from web snippets
```

### Validation gate
Before returning a result, the agent checks it has enough data to construct metadata:
- Must have at least `title_en` or `original_title`
- Must have `content_type` (tv/movie) determined
- Should have at least one of: description, poster_url, year, rating

### Integration with existing metadata_service
- `search_metadata_via_llm()` signature stays same → returns `list[dict]`
- Implementation replaces LLM call with agent call
- `create_or_update_series/movie_from_external()` logic unchanged
- `external_source` field changes: `"llm_search"` → `"tmdb"` / `"tvdb"` / `"jina"`
- `external_id` format: `"tmdb:12345"` / `"tvdb:67890"` / `"jina:<hash>"`

### Config additions
```python
tmdb_api_key: str = ""
tvdb_api_key: str = ""
jina_api_key: str = ""
```

### Dependencies
- `tmdbsimple>=0.9.0` — add to pyproject.toml
- tvdb-v4-python — optional (only if TVDB_API_KEY set)
- httpx already available

## Test Plan

### Unit tests (`tests/unit/test_metadata_search_agent.py`)
- Mock TMDB API responses → verify normalization
- Mock TVDB API responses → verify normalization
- Mock Jina responses → verify normalization
- Test validation gate (missing fields → skip result)
- Test empty results handling
- Test error handling (API errors, timeouts)

### Integration test dataset (20+ items)
Curated list of real titles spanning TV, movie, anime, Chinese titles:
1. "Breaking Bad" — TV, high confidence
2. "The Dark Knight" — Movie, high confidence
3. "Spirited Away" — Movie/Anime
4. "Attack on Titan" — TV/Anime
5. "Inception" — Movie
6. "Game of Thrones" — TV
7. "Parasite" — Movie
8. "Stranger Things" — TV
9. "Interstellar" — Movie
10. "Death Note" — TV/Anime
11. "The Matrix" — Movie
12. "Friends" — TV
13. "Pulp Fiction" — Movie
14. "Demon Slayer" — TV/Anime
15. "The Godfather" — Movie
16. "Better Call Saul" — TV
17. "Your Name" — Movie/Anime
18. "The Office" — TV
19. "John Wick" — Movie
20. "Fullmetal Alchemist Brotherhood" — TV/Anime
21. "Oppenheimer" — Movie
22. "Rick and Morty" — TV

### Integration tests (`tests/integration/test_metadata_search_agent_integration.py`)
- For each title in dataset: search and verify response format
- Verify at least 80% of titles return valid metadata
- Verify content_type is correctly identified
- Verify poster_url validity
- Measure search latency

## Oracle Review Key Decisions (ses_0f25220c9ffetqFydl1wOwbGZd)

### P0 (mandatory)
- [x] Use `httpx.AsyncClient` directly — **drop `tmdbsimple` dependency** (sync lib adds no value)
- [x] `external_source` backward compat: upsert queries use `.in_([source, "llm_search"])`
- [x] Add `tmdb_api_key`, `tvdb_api_key`, `tvdb_pin`, `jina_api_key` to Settings
- [x] Document TMDB poster URL: `f"https://image.tmdb.org/t/p/w500{poster_path}"`
- [x] Status field mappings: TMDB TV "Returning Series"/"Ended"/"Canceled", TMDB Movie "Released"/etc.

### P1 (important)
- [x] Session-level in-memory LRU cache for search results (per `search_title`)
- [x] `number_of_episodes`/`number_of_seasons` omitted from search results (detail call skipped)
- [x] `asyncio.gather(*tasks, return_exceptions=True)` for parallel source calls
- [x] TMDB zh-CN + en-US calls run in parallel (3-way gather with TVDB)

### P2 (nice-to-have)
- [x] `MetadataCandidate` TypedDict in new module
- [x] Keep LLM web-search as ultimate fallback (not Jina-only)
- [x] Contract test: agent dicts pass through upsert functions

## Implementation Phases

### Phase 2: Config + Dependencies
- Add settings fields: tmdb_api_key, tvdb_api_key, tvdb_pin, jina_api_key
- Update .env.example

### Phase 3: Build metadata_search_agent module
- httpx.AsyncClient with TMDB endpoints (search/multi, search/tv, search/movie)
- zh-CN + en-US parallel search, merge by TMDB ID
- TVDB source (if configured)
- Jina source (if configured)
- LLM fallback as safety net
- Session-level in-memory LRU cache
- Validation gate per result

### Phase 4: Integrate with metadata_service
- Replace `search_metadata_via_llm()` body with agent call
- Fix upsert queries for external_source backward compat
- Wire up caching

### Phase 5: Tests
- Unit tests with mocked httpx
- Integration test dataset (22 titles)
- Coverage verification
