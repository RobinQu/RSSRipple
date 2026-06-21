# Feature Specification: RSSRipple - Automated RSS Subscription Download Service

**Feature Branch**: `001-rssripple`
**Created**: 2026-06-21
**Status**: Complete

## User Scenarios & Testing

### User Story 1 - Create RSS Subscription Channel (P1)
**Description**: As a user, I can create RSS subscription channels by providing an RSS URL. The system validates the URL's reachability and displays a preview of available resources.
**Priority**: P1 (Critical)
**Independent Test**: Create a channel via the UI, verify the system validates the RSS URL and populates a preview.
**Acceptance Scenarios**:
1. User enters a valid RSS URL (e.g., mikanani.me RSS feed) → system fetches the feed, validates it, and shows a preview of parsed entries.
2. User enters an invalid/unreachable URL → system displays an error indicating the URL is unreachable.
3. After validation, the channel is persisted with configurable fetch interval and status `active`.

### User Story 2 - Configure Agent Filter Rules (P1)
**Description**: As a user, I can configure an Agent's filter rules to set preferences for subtitle groups, resolution, format, codec, and other fields. The Agent selects resources based on these rules.
**Priority**: P1 (Critical)
**Independent Test**: Create an Agent with multiple ResourceFilters, trigger RSS processing, verify only matching resources are selected.
**Acceptance Scenarios**:
1. User creates an Agent with a required filter (`resolution eq 1080p`) and optional filters (`subtitle_group eq LoliHouse`, `container eq MKV`).
2. When RSS is fetched, only resources matching all required filters are considered; optional filters act as scoring bonuses.
3. If a unique match exists after rule filtering, it is automatically enqueued for download.
4. If multiple matches exist, the system proceeds to Tier 2 (LLM) or Tier 3 (human) resolution.

### User Story 3 - View Download Progress (P2)
**Description**: As a user, I can view the download progress on the dashboard, showing active tasks with download speed, progress percentage, and status.
**Priority**: P2 (High)
**Independent Test**: Verify the dashboard displays active download tasks with real-time progress information.
**Acceptance Scenarios**:
1. Dashboard shows all active Agents with their task counts and aggregate download speeds.
2. Active Downloads section lists each task with title, progress bar, percentage, speed, and ETA.
3. Pending Decisions section shows items awaiting user action with candidate count summaries.
4. Progress information auto-refreshes via polling.

### User Story 4 - Manual Version Selection (P2)
**Description**: As a user, when ambiguity arises (multiple matching candidates), the system presents a candidate list for manual selection.
**Priority**: P2 (High)
**Independent Test**: Trigger a scenario with ambiguous candidates, verify a PendingDecision is created and the user can confirm a selection.
**Acceptance Scenarios**:
1. When rule filtering and LLM cannot determine a unique match, a PendingDecision record is created with all candidate FileResource IDs.
2. The PendingDecision appears in the dashboard and Agent detail page with a reason explaining why decision is needed.
3. User selects a specific resource from the candidate list → a DownloadTask is created for the chosen resource.
4. User can skip a decision → the PendingDecision is marked as `skipped` and no download is initiated.

### User Story 5 - Manage Transmission Instances (P2)
**Description**: As a user, I can add, test, and modify Transmission API connection settings.
**Priority**: P2 (High)
**Independent Test**: Create a DownloaderInstance, test the connection, verify status updates.
**Acceptance Scenarios**:
1. User creates a DownloaderInstance with name, API URL, and optional credentials.
2. User can test the connection → system verifies reachability and displays `connected` or `error` status.
3. User can update credentials or URL of an existing instance.
4. User can delete an instance (with appropriate warnings if agents reference it).

### User Story 6 - Automatic Subtitle Group Consistency (P1)
**Description**: As the system, new episodes automatically match the subtitle group and parameters from previous episodes in the same series.
**Priority**: P1 (Critical)
**Independent Test**: Download episode 1 from "LoliHouse", then process episode 2 with multiple subtitle groups available; verify "LoliHouse" is preferred.
**Acceptance Scenarios**:
1. After a resource is downloaded for an episode, the system stores an EpisodeProfile with the chosen subtitle_group, resolution, container, video_codec, and subtitle_type.
2. When a new episode of the same series is processed, the EpisodeProfile from the previous episode is used as scoring bonuses (not hard filters).
3. The candidate with the highest consistency score is preferred.
4. If scores are tied, the system escalates to Tier 2 (LLM) or Tier 3 (human).

### User Story 7 - LLM-Assisted Decision Making (P3)
**Description**: As the system, when rule-based filtering cannot uniquely match a resource, invoke LLM for intelligent judgment.
**Priority**: P3 (Medium)
**Independent Test**: Configure an Agent with `llm_enabled: true`, create an ambiguous scenario, verify LLM is called and provides a recommendation.
**Acceptance Scenarios**:
1. When Tier 1 rule filtering produces multiple matches and the Agent has `llm_enabled`, the LLM service is invoked with candidate details.
2. LLM returns a recommendation with reasoning.
3. If LLM makes a definitive choice, the selected resource is enqueued for download.
4. If LLM cannot decide or the API call fails, the system falls back to Tier 3 (human decision) and records the LLM suggestion on the PendingDecision.

### Edge Cases
1. **RSS feed is temporarily unreachable**: System retries with exponential backoff and marks channel status as `error` after repeated failures.
2. **Duplicate RSS entries**: The system deduplicates FileResources by `guid` to avoid processing the same entry twice.
3. **Missing parsed fields**: If title parsing fails to extract key fields (e.g., episode number), the resource is still stored with `title_raw` but may not match any filters.
4. **Transmission connection drops mid-download**: DownloadTask transitions to `error` status and retries up to `max_retries` with exponential backoff.
5. **Multiple Agents on same Channel**: Each Agent processes resources independently with its own filter set and downloader.
6. **LLM API rate limits or failures**: LLM failures are non-fatal; the system gracefully falls back to human decision (Tier 3).
7. **Task expiry**: Completed DownloadTasks are retained for `task_expire_days` before being eligible for cleanup.
8. **Concurrent RSS fetches**: APScheduler ensures only one fetch job per channel runs at a time; overlapping triggers are skipped.
9. **Non-standard RSS formats**: The dynamic field mapping system (with LLM analysis) handles varied RSS sources; the fallback mikanani parser handles the standard format.

## Requirements

### Functional Requirements

#### Channel Management
- **FR-001**: System shall support creating RSS subscription channels with a name, type (`rss_feed`), URL, and configurable fetch interval.
- **FR-002**: System shall validate RSS URL reachability before channel creation (`POST /channels/validate-url`).
- **FR-003**: System shall support CRUD operations on channels (create, read, update, delete) with pagination.
- **FR-004**: System shall support manual RSS fetch triggering per channel (`POST /channels/{id}/fetch`).
- **FR-005**: System shall support LLM-based RSS feed analysis to generate field mappings (`POST /channels/{id}/analyze`).
- **FR-006**: System shall support applying generated field mappings to a channel (`POST /channels/{id}/apply-mapping`).
- **FR-007**: System shall automatically fetch RSS feeds at configurable intervals using APScheduler.
- **FR-008**: System shall track channel status (`active`, `inactive`, `error`) and last fetched timestamp.

#### RSS Parsing
- **FR-009**: System shall parse RSS feeds using feedparser, extracting items into FileResource records.
- **FR-010**: System shall support a fallback title parser (`title_parser.py`) for mikanani-format titles with regex extraction of subtitle_group, title_cn, title_en, episode, resolution, source, video_codec, audio_codec, subtitle_type, container, and file_size.
- **FR-011**: System shall support dynamic field mapping (`resource_parser.py`) where each channel can have custom extraction rules specifying source path, regex, capture group, and transform.
- **FR-012**: System shall support transforms on extracted fields: `int`, `float`, `iso_datetime`, `lowercase`, `uppercase`.
- **FR-013**: System shall deduplicate FileResources by GUID to prevent duplicate processing.
- **FR-014**: System shall support multiple RSS sources including mikanani.me, share.dmhy.org, and eztv-style feeds.

#### Agent Management
- **FR-015**: System shall support creating Agents linked to a Channel and a DownloaderInstance.
- **FR-016**: System shall support CRUD operations on Agents with pagination.
- **FR-017**: System shall support manual Agent processing triggers (`POST /agents/{id}/run`).
- **FR-018**: System shall support Agent status management (`active`, `paused`, `error`).
- **FR-019**: System shall support configuring Agent content type (`anime`, `tv`, `movie`, `mixed`).
- **FR-020**: System shall support enabling/disabling LLM-assisted decisions per Agent.
- **FR-021**: System shall support configuring external metadata source per Agent (`imdb`, `tvdb`, `none`).
- **FR-022**: System shall support filter testing against channel resources (`POST /agents/{id}/test-filters`).

#### Resource Filtering (Three-Tier Resolution)
- **FR-023**: System shall implement Tier 1 rule-based filtering: apply ResourceFilters to candidate FileResources.
  - Unique match → automatically enqueue for download.
  - Multiple matches → escalate to Tier 2.
  - No matches → skip the resource (or mark as unmatched).
- **FR-024**: System shall implement Tier 2 LLM-assisted decision making when rule filtering is ambiguous and LLM is enabled.
  - LLM makes a definitive choice → enqueue selected resource.
  - LLM cannot decide or fails → escalate to Tier 3.
- **FR-025**: System shall implement Tier 3 human decision: create a PendingDecision record with all candidates, reason, and optional LLM suggestion.
  - User confirms a candidate → enqueue for download.
  - User skips → mark decision as skipped.
- **FR-026**: System shall support filter fields: `subtitle_group`, `resolution`, `container`, `video_codec`, `audio_codec`, `subtitle_type`, `source`, `title_cn`, `title_en`.
- **FR-027**: System shall support filter operators: `eq` (exact match), `contains`, `fuzzy` (fuzzy match), `in` (list membership), `regex`.
- **FR-028**: System shall support required filters (hard gate, must pass) and optional filters (scoring bonuses).
- **FR-029**: System shall support filter priority ordering (higher priority = evaluated first / weighted more).
- **FR-030**: System shall support CRUD operations on ResourceFilters within an Agent.

#### Consistency Matching
- **FR-031**: System shall maintain an EpisodeProfile per series episode recording the chosen subtitle_group, resolution, container, video_codec, and subtitle_type.
- **FR-032**: System shall use the previous episode's EpisodeProfile as scoring bonuses when processing new episodes of the same series.
- **FR-033**: System shall break ties in consistency scoring by escalating to Tier 2/3.

#### Title Fuzzy Matching
- **FR-034**: System shall support title matching via exact match on `title_cn` or `title_en`.
- **FR-035**: System shall support title matching via aliases (TVSeries.aliases list).
- **FR-036**: System shall support fuzzy title matching using Levenshtein distance (ratio > 0.7).
- **FR-037**: System shall support external data source association for title matching (TMDB/Bangumi API).

#### Download Management
- **FR-038**: System shall create DownloadTasks when a resource is selected for download (through any tier).
- **FR-039**: System shall submit download tasks to Transmission via the transmission-rpc client.
- **FR-040**: System shall support the download task lifecycle: `pending` → `queued` → `downloading` → `completed`.
- **FR-041**: System shall support pausing and resuming download tasks.
- **FR-042**: System shall support retrying failed download tasks up to `max_retries` with exponential backoff.
- **FR-043**: System shall support cancelling/deleting download tasks.
- **FR-044**: System shall track download progress (0-100%), speed (bytes/s), and ETA (seconds) by polling Transmission.
- **FR-045**: System shall support task expiry based on `task_expire_days` for completed tasks.
- **FR-046**: System shall store Transmission torrent ID on the DownloadTask for cross-referencing.

#### Downloader Instance Management
- **FR-047**: System shall support creating DownloaderInstance records with name, type (`transmission`), URL, and optional credentials.
- **FR-048**: System shall support testing DownloaderInstance connections (`POST /downloaders/{id}/test`).
- **FR-049**: System shall support CRUD operations on DownloaderInstances.
- **FR-050**: System shall track DownloaderInstance status (`connected`, `disconnected`, `error`) and last checked timestamp.

#### TVSeries & Movie Metadata
- **FR-051**: System shall support CRUD operations on TVSeries with fields: title_cn, title_en, aliases, external_id, external_source, description, genre, start_date, content_type.
- **FR-052**: System shall support CRUD operations on Movie with fields: title_cn, title_en, aliases, external_id, external_source, description, release_date, content_type.
- **FR-053**: System shall associate FileResources with Episodes (via episode_id) and Movies (via movie_id).
- **FR-054**: System shall support Episodes with episode_number, title, air_date, and preferred_profile_id for consistency matching.

#### Dashboard
- **FR-055**: System shall provide a dashboard endpoint (`GET /api/v1/dashboard`) returning active Agents, active download tasks, and pending decisions.
- **FR-056**: Dashboard shall show aggregate download speeds per Agent.

#### Web UI
- **FR-057**: System shall provide a React SPA with pages: Dashboard, Channels, Channel Create/Detail, Downloaders, Downloader Create, Agents, Agent Create, Agent Detail.
- **FR-058**: System shall provide channel management UI with RSS URL validation and field mapping workflow.
- **FR-059**: System shall provide Agent management UI with filter configuration.
- **FR-060**: System shall provide download progress visualization with real-time polling.
- **FR-061**: System shall provide pending decision UI with candidate selection and skip actions.

#### API Architecture
- **FR-062**: All API endpoints shall be under `/api/v1/` prefix with RESTful JSON request/response bodies.
- **FR-063**: All API responses shall use a standard envelope: `{ success, data, error, meta }`.
- **FR-064**: Error responses shall include structured error codes and messages.
- **FR-065**: List endpoints shall support pagination with page, page_size, and total count in meta.

### Key Entities

#### Channel (Subscription Channel)
| Field | Type | Description |
|-------|------|-------------|
| id | UUID | Primary key |
| name | str | Channel name |
| type | enum | Channel type (currently only `rss_feed`) |
| url | str | RSS subscription URL |
| fetch_interval | int | Fetch interval in seconds |
| field_mapping | JSON? | Dynamic field mapping rules (LLM-generated) |
| parser_type | enum | Parser type: `auto`, `mikanani`, `custom` |
| last_fetched_at | datetime | Last fetch timestamp |
| status | enum | `active`, `inactive`, `error` |
| created_at | datetime | Creation time |
| updated_at | datetime | Update time |

#### FileResource (Resource Entry)
| Field | Type | Description |
|-------|------|-------------|
| id | UUID | Primary key |
| channel_id | UUID | Parent channel |
| guid | str | RSS item GUID |
| title_raw | str | Raw title string |
| title_cn | str? | Parsed Chinese title |
| title_en | str? | Parsed English title |
| subtitle_group | str? | Parsed subtitle/release group |
| episode | int? | Parsed episode number |
| resolution | str? | Parsed resolution (e.g., `1080p`) |
| source | str? | Parsed source (e.g., `WebRip`, `WEB-DL`) |
| video_codec | str? | Parsed video codec (e.g., `HEVC-10bit`) |
| audio_codec | str? | Parsed audio codec (e.g., `AAC`) |
| subtitle_type | str? | Parsed subtitle type (e.g., `简繁内封字幕`) |
| container | str? | Parsed container format (e.g., `MKV`) |
| file_size | int? | File size in bytes |
| torrent_url | str | .torrent download URL |
| detail_url | str | Detail page URL |
| published_at | datetime | RSS publish time |
| parsed_at | datetime | Parse timestamp |
| episode_id | UUID? | Associated Episode |
| movie_id | UUID? | Associated Movie |
| created_at | datetime | Creation time |

#### Episode
| Field | Type | Description |
|-------|------|-------------|
| id | UUID | Primary key |
| series_id | UUID | Parent TVSeries |
| episode_number | int | Episode number |
| title | str? | Episode title |
| air_date | date? | Air date |
| preferred_profile_id | UUID? | Preferred download profile (from history) |
| created_at | datetime | Creation time |

#### TVSeries
| Field | Type | Description |
|-------|------|-------------|
| id | UUID | Primary key |
| title_cn | str? | Chinese title |
| title_en | str? | English title |
| aliases | list[str] | Alias list |
| external_id | str? | External database ID (e.g., TMDB) |
| external_source | str? | External data source name |
| description | str? | Synopsis |
| genre | list[str] | Genre tags |
| start_date | date? | Premiere date |
| content_type | str? | Content type (`anime`, `tv`, `movie`) |
| created_at | datetime | Creation time |
| updated_at | datetime | Update time |

#### Movie
| Field | Type | Description |
|-------|------|-------------|
| id | UUID | Primary key |
| title_cn | str? | Chinese title |
| title_en | str? | English title |
| aliases | list[str] | Alias list |
| external_id | str? | External database ID (TMDB, TVDB) |
| external_source | str? | External data source name |
| description | str? | Synopsis |
| release_date | date? | Release date |
| content_type | str? | Content type |
| created_at | datetime | Creation time |
| updated_at | datetime | Update time |

#### ResourceFilter
| Field | Type | Description |
|-------|------|-------------|
| id | UUID | Primary key |
| agent_id | UUID | Parent Agent |
| field | enum | Match field: `subtitle_group`, `resolution`, `container`, `video_codec`, `audio_codec`, `subtitle_type`, `source`, `title_cn`, `title_en` |
| operator | enum | Operator: `eq`, `contains`, `fuzzy`, `in`, `regex` |
| value | str | Match value |
| priority | int | Priority (higher = more important) |
| is_required | bool | Required (hard gate) vs. scoring bonus |
| created_at | datetime | Creation time |

#### Agent
| Field | Type | Description |
|-------|------|-------------|
| id | UUID | Primary key |
| name | str | Agent name |
| channel_id | UUID | Associated channel |
| downloader_id | UUID | Associated downloader instance |
| task_expire_days | int | Completed task expiry days |
| llm_enabled | bool | Enable LLM-assisted decisions |
| metadata_source | enum? | External metadata: `imdb`, `tvdb`, `none` |
| content_type | enum | Content type: `anime`, `tv`, `movie`, `mixed` |
| status | enum | `active`, `paused`, `error` |
| last_run_at | datetime | Last run timestamp |
| created_at | datetime | Creation time |
| updated_at | datetime | Update time |

#### DownloaderInstance
| Field | Type | Description |
|-------|------|-------------|
| id | UUID | Primary key |
| name | str | Instance name |
| type | enum | Downloader type (currently only `transmission`) |
| url | str | API URL (e.g., `http://localhost:9091/transmission/rpc`) |
| username | str? | Auth username |
| password | str? | Auth password |
| status | enum | `connected`, `disconnected`, `error` |
| last_checked_at | datetime | Last connection check |
| created_at | datetime | Creation time |
| updated_at | datetime | Update time |

#### DownloadTask
| Field | Type | Description |
|-------|------|-------------|
| id | UUID | Primary key |
| agent_id | UUID | Parent Agent |
| file_resource_id | UUID | Associated resource |
| downloader_id | UUID | Downloader used |
| transmission_torrent_id | int? | Transmission internal torrent ID |
| status | enum | `pending`, `queued`, `downloading`, `paused`, `completed`, `error`, `cancelled` |
| progress | float | Download progress (0-100) |
| download_speed | int | Download speed (bytes/s) |
| eta | int? | ETA in seconds |
| error_message | str? | Error details |
| retry_count | int | Current retry count |
| max_retries | int | Max retry limit |
| confirmed_at | datetime? | Confirmation timestamp |
| completed_at | datetime? | Completion timestamp |
| created_at | datetime | Creation time |
| updated_at | datetime | Update time |

#### PendingDecision
| Field | Type | Description |
|-------|------|-------------|
| id | UUID | Primary key |
| agent_id | UUID | Parent Agent |
| episode_id | UUID? | Associated Episode |
| movie_id | UUID? | Associated Movie |
| candidates | list[UUID] | Candidate FileResource IDs |
| reason | str | Reason decision is needed |
| llm_suggestion | str? | LLM recommendation |
| decided_resource_id | UUID? | User's chosen resource |
| status | enum | `pending`, `decided`, `expired`, `skipped` |
| created_at | datetime | Creation time |
| decided_at | datetime? | Decision timestamp |

## Success Criteria

### Measurable Outcomes
1. **Automated Download**: The system can automatically select and download the best-matching resource for each episode from an RSS feed, requiring zero human intervention when filters produce a unique match.
2. **Subtitle Group Consistency**: Once a subtitle group is selected for a series, subsequent episodes prefer the same group, achieving consistency without manual intervention.
3. **Multi-Source Support**: The dynamic field mapping system supports at least 3 different RSS sources (mikanani.me, dmhy.org, eztv-style) through LLM-generated mapping rules.
4. **Intelligent Fallback**: When rule-based filtering is ambiguous, the three-tier system (Rules → LLM → Human) ensures every matchable episode gets a resolution path.
5. **Download Visibility**: Users can monitor all active downloads with real-time progress, speed, and ETA from the dashboard.
6. **Configuration Flexibility**: Users can configure fine-grained filter rules with required/optional semantics, multiple operators, and priority ordering.
7. **Reliable Delivery**: Download tasks retry automatically on failure (up to max_retries with exponential backoff), and Transmission integration is robust to connection interruptions.
8. **Operational Simplicity**: Single Docker container deployment with SQLite storage, requiring no external database or complex infrastructure.

## Assumptions
1. RSS feeds are accessible without authentication (token-based URLs like mikanani's are supported via the URL itself).
2. Transmission is deployed separately (as a Docker container or external service) and accessible via its RPC API.
3. SQLite is sufficient for the expected data volume (personal/small-team use, not high-concurrency scenarios).
4. LLM API (OpenAI-compatible) is available when `llm_enabled` is set; the system degrades gracefully when LLM is unavailable.
5. The primary use case is anime/TV series with episodic content; movie support is secondary.
6. Frontend is served as a static SPA bundled in the same Docker container as the backend.
7. Network access to RSS feed URLs and Transmission API is available from the Docker container.
8. Users are technically proficient enough to configure RSS URLs, filter rules, and Transmission connections.
9. The mikanani title format (`[字幕组] CN / EN - EP [quality]`) is the primary fallback format; other formats require dynamic field mapping setup.
10. APScheduler's AsyncIOScheduler is sufficient for scheduling needs (no distributed task queue required).
