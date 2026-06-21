# API Contract: RSSRipple

## Base URL

All API endpoints are under: `/api/v1/`

## Authentication

No authentication required (single-user deployment).

## Response Format

### Success Response

```json
{
  "success": true,
  "data": { ... }
}
```

### Error Response

```json
{
  "success": false,
  "error": {
    "code": "ERROR_CODE",
    "message": "Human-readable error description"
  }
}
```

### Paginated Response

```json
{
  "success": true,
  "data": {
    "items": [ ... ],
    "total": 100,
    "page": 1,
    "page_size": 20,
    "total_pages": 5
  }
}
```

## Common Query Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `page` | int | 1 | Page number (1-indexed) |
| `page_size` | int | 20 | Items per page (max 100) |

---

## Endpoints

### Dashboard

#### GET /api/v1/dashboard

Get dashboard overview data including active agents, download tasks, and pending decisions.

**Request:** No body.

**Response:**
```json
{
  "success": true,
  "data": {
    "active_agents": [
      {
        "id": "uuid",
        "name": "番剧自动下载",
        "task_count": 3,
        "total_speed": 12582912
      }
    ],
    "active_downloads": [
      {
        "id": "uuid",
        "title_raw": "[LoliHouse] 黄泉使者 - 12 [WebRip 1080p HEVC-10bit AAC]",
        "progress": 78.5,
        "download_speed": 5242880,
        "eta": 120,
        "status": "downloading"
      }
    ],
    "pending_decisions": [
      {
        "id": "uuid",
        "agent_name": "番剧自动下载",
        "reason": "Multiple candidates found for EP12",
        "candidate_count": 3,
        "created_at": "2026-06-21T10:30:00Z"
      }
    ]
  }
}
```

---

### Channels

#### GET /api/v1/channels

List all channels with pagination.

**Query Parameters:** `page`, `page_size`

**Response:**
```json
{
  "success": true,
  "data": {
    "items": [
      {
        "id": "550e8400-e29b-41d4-a716-446655440000",
        "name": "我的番组订阅",
        "type": "rss_feed",
        "url": "https://mikanani.me/RSS/MyBangumi?token=xxx",
        "fetch_interval": 1800,
        "parser_type": "auto",
        "status": "active",
        "last_fetched_at": "2026-06-21T09:00:00Z",
        "created_at": "2026-06-21T10:00:00Z",
        "updated_at": "2026-06-21T10:00:00Z"
      }
    ],
    "total": 1,
    "page": 1,
    "page_size": 20,
    "total_pages": 1
  }
}
```

#### POST /api/v1/channels

Create a new channel.

**Request Body:**
```json
{
  "name": "我的番组订阅",
  "type": "rss_feed",
  "url": "https://mikanani.me/RSS/MyBangumi?token=xxx",
  "fetch_interval": 1800
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | str | Yes | Channel name |
| `type` | enum | Yes | Channel type (`rss_feed`) |
| `url` | str | Yes | RSS feed URL |
| `fetch_interval` | int | No | Fetch interval in seconds (default: 1800) |

**Response:** `201 Created`
```json
{
  "success": true,
  "data": {
    "id": "550e8400-e29b-41d4-a716-446655440000",
    "name": "我的番组订阅",
    "type": "rss_feed",
    "url": "https://mikanani.me/RSS/MyBangumi?token=xxx",
    "fetch_interval": 1800,
    "field_mapping": null,
    "parser_type": "auto",
    "status": "active",
    "last_fetched_at": null,
    "created_at": "2026-06-21T10:00:00Z",
    "updated_at": "2026-06-21T10:00:00Z"
  }
}
```

#### GET /api/v1/channels/{id}

Get channel details by ID.

**Path Parameters:** `id` (UUID)

**Response:** `200 OK` — Same structure as POST response `data` field.

**Error:** `404 Not Found` if channel does not exist.

#### PUT /api/v1/channels/{id}

Update a channel.

**Path Parameters:** `id` (UUID)

**Request Body:** (all fields optional)
```json
{
  "name": "更新后的频道名称",
  "url": "https://mikanani.me/RSS/NewBangumi?token=yyy",
  "fetch_interval": 3600,
  "parser_type": "custom",
  "status": "inactive"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | str | No | Updated channel name |
| `url` | str | No | Updated RSS URL |
| `fetch_interval` | int | No | Updated fetch interval |
| `parser_type` | enum | No | Updated parser type |
| `status` | enum | No | Updated status |

**Response:** `200 OK` — Full channel object.

#### DELETE /api/v1/channels/{id}

Delete a channel and its associated resources.

**Path Parameters:** `id` (UUID)

**Response:** `200 OK`
```json
{
  "success": true,
  "data": null
}
```

**Error:** `404 Not Found` if channel does not exist.

#### POST /api/v1/channels/{id}/fetch

Manually trigger an RSS fetch for the channel. Fetches the RSS feed, parses new entries, and creates FileResource records.

**Path Parameters:** `id` (UUID)

**Request:** No body.

**Response:** `200 OK`
```json
{
  "success": true,
  "data": {
    "new_resources_count": 5,
    "fetched_at": "2026-06-21T10:30:00Z"
  }
}
```

#### POST /api/v1/channels/{id}/analyze

Use LLM to analyze sample RSS entries and generate a field mapping. Returns a proposed `field_mapping` JSON for review.

**Path Parameters:** `id` (UUID)

**Request:** No body.

**Response:** `200 OK`
```json
{
  "success": true,
  "data": {
    "proposed_mapping": {
      "title_cn": {"source": "title", "regex": "\\]\\s*(.+?)\\s*/", "group": 1},
      "episode": {"source": "title", "regex": "-\\s*(\\d+)\\b", "group": 1, "transform": "int"},
      "torrent_url": {"source": "enclosures[0].url"},
      "file_size": {"source": "enclosures[0].length", "transform": "int"}
    },
    "sample_entries": [
      {
        "title": "[LoliHouse] 黄泉使者 / Yomi no Tsugai - 12 [WebRip 1080p HEVC-10bit AAC][简繁内封字幕]",
        "parsed_fields": {
          "subtitle_group": "LoliHouse",
          "title_cn": "黄泉使者",
          "title_en": "Yomi no Tsugai",
          "episode": 12,
          "resolution": "1080p",
          "video_codec": "HEVC-10bit",
          "audio_codec": "AAC",
          "subtitle_type": "简繁内封字幕"
        }
      }
    ]
  }
}
```

#### POST /api/v1/channels/{id}/apply-mapping

Apply a field mapping to the channel. This sets the channel's `field_mapping` and updates `parser_type` to `custom`.

**Path Parameters:** `id` (UUID)

**Request Body:**
```json
{
  "field_mapping": {
    "title_cn": {"source": "title", "regex": "\\]\\s*(.+?)\\s*/", "group": 1},
    "episode": {"source": "title", "regex": "-\\s*(\\d+)\\b", "group": 1, "transform": "int"},
    "torrent_url": {"source": "enclosures[0].url"},
    "file_size": {"source": "enclosures[0].length", "transform": "int"}
  }
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `field_mapping` | JSON object | Yes | The field mapping rules to apply |

**Response:** `200 OK` — Full updated channel object.

#### POST /api/v1/channels/validate-url

Validate that an RSS URL is reachable and returns valid RSS content.

**Request Body:**
```json
{
  "url": "https://mikanani.me/RSS/MyBangumi?token=xxx"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `url` | str | Yes | RSS URL to validate |

**Response:** `200 OK`
```json
{
  "success": true,
  "data": {
    "valid": true,
    "feed_title": "Mikan Project - 我的番组",
    "item_count": 50,
    "response_time_ms": 230
  }
}
```

**Error case:**
```json
{
  "success": true,
  "data": {
    "valid": false,
    "error": "URL returned HTTP 403"
  }
}
```

---

### Agents

#### GET /api/v1/agents

List all agents with pagination.

**Query Parameters:** `page`, `page_size`

**Response:**
```json
{
  "success": true,
  "data": {
    "items": [
      {
        "id": "uuid",
        "name": "番剧自动下载",
        "channel_id": "uuid",
        "downloader_id": "uuid",
        "download_dir": "/downloads/anime",
        "task_expire_days": 30,
        "llm_enabled": true,
        "metadata_source": null,
        "content_type": "anime",
        "status": "active",
        "last_run_at": "2026-06-21T10:00:00Z",
        "created_at": "2026-06-21T10:00:00Z",
        "updated_at": "2026-06-21T10:00:00Z"
      }
    ],
    "total": 1,
    "page": 1,
    "page_size": 20,
    "total_pages": 1
  }
}
```

#### POST /api/v1/agents

Create a new agent. Optionally includes initial filter definitions.

**Request Body:**
```json
{
  "name": "番剧自动下载",
  "channel_id": "550e8400-e29b-41d4-a716-446655440000",
  "downloader_id": "660e8400-e29b-41d4-a716-446655440001",
  "download_dir": "/downloads/anime",
  "task_expire_days": 30,
  "llm_enabled": true,
  "content_type": "anime",
  "filters": [
    {"field": "resolution", "operator": "eq", "value": "1080p", "priority": 10, "is_required": true},
    {"field": "subtitle_group", "operator": "eq", "value": "LoliHouse", "priority": 20, "is_required": false},
    {"field": "container", "operator": "eq", "value": "MKV", "priority": 5, "is_required": false}
  ]
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | str | Yes | Agent name |
| `channel_id` | UUID | Yes | Associated channel ID |
| `downloader_id` | UUID | Yes | Associated downloader instance ID |
| `download_dir` | str | No | Target download directory |
| `task_expire_days` | int | No | Completed task expiry days (default: 30) |
| `llm_enabled` | bool | No | Enable LLM-assisted decisions (default: false) |
| `content_type` | enum | No | Content type: `anime`, `tv`, `movie`, `mixed` (default: `anime`) |
| `metadata_source` | enum | No | External metadata source: `imdb`, `tvdb`, `none` |
| `filters` | list | No | Initial ResourceFilter definitions |

**Response:** `201 Created` — Full agent object including created filters.

#### GET /api/v1/agents/{id}

Get agent details by ID.

**Path Parameters:** `id` (UUID)

**Response:** `200 OK` — Full agent object.

**Error:** `404 Not Found` if agent does not exist.

#### PUT /api/v1/agents/{id}

Update an agent.

**Path Parameters:** `id` (UUID)

**Request Body:** (all fields optional)
```json
{
  "name": "更新后的Agent名称",
  "download_dir": "/downloads/new-path",
  "llm_enabled": false,
  "status": "paused"
}
```

**Response:** `200 OK` — Full updated agent object.

#### DELETE /api/v1/agents/{id}

Delete an agent and its associated filters.

**Path Parameters:** `id` (UUID)

**Response:** `200 OK`
```json
{
  "success": true,
  "data": null
}
```

#### POST /api/v1/agents/{id}/run

Manually trigger the agent to process new resources from its channel. Runs the full filter pipeline (Tier 1 → Tier 2 → Tier 3).

**Path Parameters:** `id` (UUID)

**Request:** No body.

**Response:** `200 OK`
```json
{
  "success": true,
  "data": {
    "processed_count": 15,
    "matched_count": 3,
    "tasks_created": 2,
    "decisions_created": 1,
    "skipped_count": 12,
    "run_at": "2026-06-21T10:30:00Z"
  }
}
```

#### POST /api/v1/agents/{id}/test-filters

Test the agent's filters against current channel resources without creating tasks. Returns which resources match and their scores.

**Path Parameters:** `id` (UUID)

**Request:** No body.

**Response:** `200 OK`
```json
{
  "success": true,
  "data": {
    "results": [
      {
        "resource_id": "uuid",
        "title_raw": "[LoliHouse] 黄泉使者 - 12 [WebRip 1080p HEVC-10bit AAC]",
        "matched_required": true,
        "score": 35,
        "matched_filters": [
          {"field": "resolution", "value": "1080p", "matched": true},
          {"field": "subtitle_group", "value": "LoliHouse", "matched": true},
          {"field": "container", "value": "MKV", "matched": true}
        ]
      }
    ],
    "total_resources": 50,
    "passing_resources": 3
  }
}
```

---

### Resource Filters

#### GET /api/v1/agents/{agent_id}/filters

Get all filters for an agent.

**Path Parameters:** `agent_id` (UUID)

**Response:** `200 OK`
```json
{
  "success": true,
  "data": [
    {
      "id": "uuid",
      "agent_id": "uuid",
      "field": "resolution",
      "operator": "eq",
      "value": "1080p",
      "priority": 10,
      "is_required": true,
      "created_at": "2026-06-21T10:00:00Z"
    },
    {
      "id": "uuid",
      "agent_id": "uuid",
      "field": "subtitle_group",
      "operator": "eq",
      "value": "LoliHouse",
      "priority": 20,
      "is_required": false,
      "created_at": "2026-06-21T10:00:00Z"
    }
  ]
}
```

#### POST /api/v1/agents/{agent_id}/filters

Add a new filter to an agent.

**Path Parameters:** `agent_id` (UUID)

**Request Body:**
```json
{
  "field": "resolution",
  "operator": "eq",
  "value": "1080p",
  "priority": 10,
  "is_required": true
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `field` | enum | Yes | Target field: `subtitle_group`, `resolution`, `container`, `video_codec`, `audio_codec`, `subtitle_type`, `source`, `title_cn`, `title_en` |
| `operator` | enum | Yes | Operator: `eq`, `contains`, `fuzzy`, `in`, `regex` |
| `value` | str | Yes | Value to match |
| `priority` | int | No | Priority weight (default: 0) |
| `is_required` | bool | No | Is hard requirement (default: false) |

**Response:** `201 Created` — Full filter object.

#### PUT /api/v1/agents/{agent_id}/filters/{id}

Update an existing filter.

**Path Parameters:** `agent_id` (UUID), `id` (UUID)

**Request Body:** (all fields optional)
```json
{
  "value": "720p",
  "priority": 15,
  "is_required": false
}
```

**Response:** `200 OK` — Full updated filter object.

#### DELETE /api/v1/agents/{agent_id}/filters/{id}

Delete a filter.

**Path Parameters:** `agent_id` (UUID), `id` (UUID)

**Response:** `200 OK`
```json
{
  "success": true,
  "data": null
}
```

---

### Downloader Instances

#### GET /api/v1/downloaders

List all downloader instances.

**Response:** `200 OK`
```json
{
  "success": true,
  "data": [
    {
      "id": "uuid",
      "name": "Home Transmission",
      "type": "transmission",
      "url": "http://localhost:9091/transmission/rpc",
      "username": "admin",
      "status": "connected",
      "last_checked_at": "2026-06-21T10:00:00Z",
      "created_at": "2026-06-21T10:00:00Z",
      "updated_at": "2026-06-21T10:00:00Z"
    }
  ]
}
```

> Note: `password` is never returned in responses.

#### POST /api/v1/downloaders

Create a new downloader instance.

**Request Body:**
```json
{
  "name": "Home Transmission",
  "type": "transmission",
  "url": "http://localhost:9091/transmission/rpc",
  "username": "admin",
  "password": "secret"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | str | Yes | Instance name |
| `type` | enum | Yes | Downloader type (currently only `transmission`) |
| `url` | str | Yes | API endpoint URL |
| `username` | str | No | Auth username |
| `password` | str | No | Auth password |

**Response:** `201 Created` — Full downloader object (without password).

#### GET /api/v1/downloaders/{id}

Get downloader instance details.

**Path Parameters:** `id` (UUID)

**Response:** `200 OK` — Full downloader object (without password).

#### PUT /api/v1/downloaders/{id}

Update a downloader instance.

**Path Parameters:** `id` (UUID)

**Request Body:** (all fields optional)
```json
{
  "name": "Updated Name",
  "url": "http://newhost:9091/transmission/rpc",
  "username": "newuser",
  "password": "newpass"
}
```

**Response:** `200 OK` — Full updated downloader object (without password).

#### DELETE /api/v1/downloaders/{id}

Delete a downloader instance.

**Path Parameters:** `id` (UUID)

**Response:** `200 OK`
```json
{
  "success": true,
  "data": null
}
```

**Error:** `409 Conflict` if the downloader is referenced by active agents.

#### POST /api/v1/downloaders/{id}/test

Test the connection to a downloader instance.

**Path Parameters:** `id` (UUID)

**Request:** No body.

**Response:** `200 OK`
```json
{
  "success": true,
  "data": {
    "connected": true,
    "version": "4.0.5",
    "response_time_ms": 45
  }
}
```

**Error case:**
```json
{
  "success": true,
  "data": {
    "connected": false,
    "error": "Connection refused"
  }
}
```

---

### Download Tasks

#### GET /api/v1/agents/{agent_id}/tasks

Get download tasks for an agent with pagination.

**Path Parameters:** `agent_id` (UUID)

**Query Parameters:** `page`, `page_size`, `status` (optional filter)

**Response:** `200 OK`
```json
{
  "success": true,
  "data": {
    "items": [
      {
        "id": "uuid",
        "agent_id": "uuid",
        "file_resource_id": "uuid",
        "downloader_id": "uuid",
        "transmission_torrent_id": 42,
        "status": "downloading",
        "progress": 78.5,
        "download_speed": 5242880,
        "eta": 120,
        "error_message": null,
        "retry_count": 0,
        "max_retries": 3,
        "confirmed_at": "2026-06-21T10:00:00Z",
        "completed_at": null,
        "created_at": "2026-06-21T10:00:00Z",
        "updated_at": "2026-06-21T10:15:00Z",
        "file_resource": {
          "title_raw": "[LoliHouse] 黄泉使者 - 12 [WebRip 1080p HEVC-10bit AAC]",
          "title_cn": "黄泉使者",
          "episode": 12,
          "resolution": "1080p",
          "subtitle_group": "LoliHouse",
          "file_size": 718442304
        }
      }
    ],
    "total": 5,
    "page": 1,
    "page_size": 20,
    "total_pages": 1
  }
}
```

#### GET /api/v1/tasks/{id}

Get a single download task by ID.

**Path Parameters:** `id` (UUID)

**Response:** `200 OK` — Full task object (same structure as items above).

#### POST /api/v1/tasks/{id}/pause

Pause a download task.

**Path Parameters:** `id` (UUID)

**Request:** No body.

**Response:** `200 OK`
```json
{
  "success": true,
  "data": {
    "id": "uuid",
    "status": "paused"
  }
}
```

**Error:** `409 Conflict` if task is not in a pausable state.

#### POST /api/v1/tasks/{id}/resume

Resume a paused download task.

**Path Parameters:** `id` (UUID)

**Request:** No body.

**Response:** `200 OK`
```json
{
  "success": true,
  "data": {
    "id": "uuid",
    "status": "downloading"
  }
}
```

#### POST /api/v1/tasks/{id}/retry

Retry a failed download task. Resets `retry_count` and resubmits to the downloader.

**Path Parameters:** `id` (UUID)

**Request:** No body.

**Response:** `200 OK`
```json
{
  "success": true,
  "data": {
    "id": "uuid",
    "status": "pending",
    "retry_count": 0
  }
}
```

#### DELETE /api/v1/tasks/{id}

Delete a download task. Also removes the torrent from the downloader if still active.

**Path Parameters:** `id` (UUID)

**Response:** `200 OK`
```json
{
  "success": true,
  "data": null
}
```

---

### Pending Decisions

#### GET /api/v1/agents/{agent_id}/decisions

Get pending decisions for an agent with pagination.

**Path Parameters:** `agent_id` (UUID)

**Query Parameters:** `page`, `page_size`, `status` (optional filter, default: `pending`)

**Response:** `200 OK`
```json
{
  "success": true,
  "data": {
    "items": [
      {
        "id": "uuid",
        "agent_id": "uuid",
        "episode_id": "uuid",
        "movie_id": null,
        "candidates": ["uuid-1", "uuid-2", "uuid-3"],
        "reason": "3 candidates match filters for EP12",
        "llm_suggestion": "Recommend [LoliHouse] version for best quality/size ratio",
        "decided_resource_id": null,
        "status": "pending",
        "created_at": "2026-06-21T10:30:00Z",
        "decided_at": null,
        "candidate_details": [
          {
            "id": "uuid-1",
            "title_raw": "[LoliHouse] 黄泉使者 - 12 [WebRip 1080p HEVC-10bit AAC][简繁内封字幕]",
            "subtitle_group": "LoliHouse",
            "resolution": "1080p",
            "video_codec": "HEVC-10bit",
            "container": "MKV",
            "file_size": 718442304
          },
          {
            "id": "uuid-2",
            "title_raw": "[ANi] 黄泉使者 - 12 [1080p][Baha][WEB-DL][AAC][CHT]",
            "subtitle_group": "ANi",
            "resolution": "1080p",
            "video_codec": null,
            "container": null,
            "file_size": 524288000
          }
        ]
      }
    ],
    "total": 2,
    "page": 1,
    "page_size": 20,
    "total_pages": 1
  }
}
```

#### POST /api/v1/decisions/{id}/confirm

Confirm a decision by selecting a specific candidate resource. Creates a DownloadTask for the chosen resource.

**Path Parameters:** `id` (UUID)

**Request Body:**
```json
{
  "resource_id": "770e8400-e29b-41d4-a716-446655440002"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `resource_id` | UUID | Yes | The FileResource ID to download |

**Response:** `200 OK`
```json
{
  "success": true,
  "data": {
    "id": "uuid",
    "status": "decided",
    "decided_resource_id": "770e8400-e29b-41d4-a716-446655440002",
    "decided_at": "2026-06-21T10:30:00Z"
  }
}
```

**Error:** `400 Bad Request` if `resource_id` is not in the candidates list.

#### POST /api/v1/decisions/{id}/skip

Skip a pending decision. No download task is created.

**Path Parameters:** `id` (UUID)

**Request:** No body.

**Response:** `200 OK`
```json
{
  "success": true,
  "data": {
    "id": "uuid",
    "status": "skipped",
    "decided_at": "2026-06-21T10:30:00Z"
  }
}
```

---

### TVSeries

#### GET /api/v1/series

List all TV series with pagination.

**Query Parameters:** `page`, `page_size`

**Response:** `200 OK`
```json
{
  "success": true,
  "data": {
    "items": [
      {
        "id": "uuid",
        "title_cn": "黄泉使者",
        "title_en": "Yomi no Tsugai",
        "aliases": ["黄泉のツガイ"],
        "external_id": "12345",
        "external_source": "tmdb",
        "description": "...",
        "genre": ["anime", "fantasy"],
        "start_date": "2026-01-10",
        "content_type": "anime",
        "created_at": "2026-06-21T10:00:00Z",
        "updated_at": "2026-06-21T10:00:00Z"
      }
    ],
    "total": 1,
    "page": 1,
    "page_size": 20,
    "total_pages": 1
  }
}
```

#### POST /api/v1/series

Create a new TV series.

**Request Body:**
```json
{
  "title_cn": "黄泉使者",
  "title_en": "Yomi no Tsugai",
  "aliases": ["黄泉のツガイ"],
  "external_id": "12345",
  "external_source": "tmdb",
  "description": "A fantasy anime about...",
  "genre": ["anime", "fantasy"],
  "start_date": "2026-01-10",
  "content_type": "anime"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `title_cn` | str | No | Chinese title |
| `title_en` | str | No | English title |
| `aliases` | list[str] | No | Alternative title aliases |
| `external_id` | str | No | External database ID |
| `external_source` | str | No | External data source name |
| `description` | str | No | Synopsis |
| `genre` | list[str] | No | Genre tags |
| `start_date` | date | No | Premiere date |
| `content_type` | str | No | Content type: `anime`, `tv`, `movie` |

**Response:** `201 Created` — Full series object.

#### GET /api/v1/series/{id}

Get a TV series by ID.

**Path Parameters:** `id` (UUID)

**Response:** `200 OK` — Full series object.

#### PUT /api/v1/series/{id}

Update a TV series.

**Path Parameters:** `id` (UUID)

**Request Body:** (all fields optional)
```json
{
  "title_cn": "更新后的中文名",
  "aliases": ["alias1", "alias2"]
}
```

**Response:** `200 OK` — Full updated series object.

#### DELETE /api/v1/series/{id}

Delete a TV series and its episodes.

**Path Parameters:** `id` (UUID)

**Response:** `200 OK`
```json
{
  "success": true,
  "data": null
}
```

---

### Movies

#### GET /api/v1/movies

List all movies with pagination.

**Query Parameters:** `page`, `page_size`

**Response:** `200 OK`
```json
{
  "success": true,
  "data": {
    "items": [
      {
        "id": "uuid",
        "title_cn": "你的名字",
        "title_en": "Your Name",
        "aliases": ["君の名は"],
        "external_id": "372058",
        "external_source": "tmdb",
        "description": "A body-swapping romance...",
        "release_date": "2016-08-26",
        "content_type": "movie",
        "created_at": "2026-06-21T10:00:00Z",
        "updated_at": "2026-06-21T10:00:00Z"
      }
    ],
    "total": 1,
    "page": 1,
    "page_size": 20,
    "total_pages": 1
  }
}
```

#### POST /api/v1/movies

Create a new movie.

**Request Body:**
```json
{
  "title_cn": "你的名字",
  "title_en": "Your Name",
  "aliases": ["君の名は"],
  "external_id": "372058",
  "external_source": "tmdb",
  "description": "A body-swapping romance...",
  "release_date": "2016-08-26",
  "content_type": "movie"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `title_cn` | str | No | Chinese title |
| `title_en` | str | No | English title |
| `aliases` | list[str] | No | Alternative title aliases |
| `external_id` | str | No | External database ID |
| `external_source` | str | No | External data source name |
| `description` | str | No | Synopsis |
| `release_date` | date | No | Release date |
| `content_type` | str | No | Content type |

**Response:** `201 Created` — Full movie object.

#### GET /api/v1/movies/{id}

Get a movie by ID.

**Path Parameters:** `id` (UUID)

**Response:** `200 OK` — Full movie object.

#### PUT /api/v1/movies/{id}

Update a movie.

**Path Parameters:** `id` (UUID)

**Request Body:** (all fields optional)
```json
{
  "title_cn": "更新后的中文名",
  "description": "Updated description"
}
```

**Response:** `200 OK` — Full updated movie object.

#### DELETE /api/v1/movies/{id}

Delete a movie.

**Path Parameters:** `id` (UUID)

**Response:** `200 OK`
```json
{
  "success": true,
  "data": null
}
```

---

### File Resources

#### GET /api/v1/channels/{channel_id}/resources

Get file resources for a channel with pagination.

**Path Parameters:** `channel_id` (UUID)

**Query Parameters:** `page`, `page_size`

**Response:** `200 OK`
```json
{
  "success": true,
  "data": {
    "items": [
      {
        "id": "uuid",
        "channel_id": "uuid",
        "guid": "[字幕组] 标题 - EP [质量信息]",
        "title_raw": "[LoliHouse] 黄泉使者 / Yomi no Tsugai - 12 [WebRip 1080p HEVC-10bit AAC][简繁内封字幕]",
        "title_cn": "黄泉使者",
        "title_en": "Yomi no Tsugai",
        "subtitle_group": "LoliHouse",
        "episode": 12,
        "resolution": "1080p",
        "source": "WebRip",
        "video_codec": "HEVC-10bit",
        "audio_codec": "AAC",
        "subtitle_type": "简繁内封字幕",
        "container": "MKV",
        "file_size": 718442304,
        "torrent_url": "https://mikanani.me/Download/20260621/abc123.torrent",
        "detail_url": "https://mikanani.me/Home/Episode/abc123",
        "published_at": "2026-06-21T09:40:23Z",
        "parsed_at": "2026-06-21T10:00:00Z",
        "episode_id": null,
        "movie_id": null,
        "created_at": "2026-06-21T10:00:00Z"
      }
    ],
    "total": 50,
    "page": 1,
    "page_size": 20,
    "total_pages": 3
  }
}
```

#### GET /api/v1/resources/{id}

Get a single file resource by ID.

**Path Parameters:** `id` (UUID)

**Response:** `200 OK` — Full resource object (same structure as items above).

---

## Status Codes Summary

| Status Code | Meaning | Usage |
|-------------|---------|-------|
| `200 OK` | Success | GET, PUT, DELETE, action endpoints |
| `201 Created` | Resource created | POST create endpoints |
| `400 Bad Request` | Invalid input | Validation errors, invalid parameters |
| `404 Not Found` | Resource not found | Invalid ID in path |
| `409 Conflict` | State conflict | Deleting referenced entities, invalid state transitions |
| `500 Internal Server Error` | Server error | Unexpected failures |

## Enumerations

### Channel Type
- `rss_feed`

### Channel Status
- `active`, `inactive`, `error`

### Parser Type
- `auto`, `mikanani`, `custom`

### Agent Status
- `active`, `paused`, `error`

### Agent Content Type
- `anime`, `tv`, `movie`, `mixed`

### Metadata Source
- `imdb`, `tvdb`, `none`

### Downloader Type
- `transmission`

### Downloader Status
- `connected`, `disconnected`, `error`

### Download Task Status
- `pending`, `queued`, `downloading`, `paused`, `completed`, `error`, `cancelled`

### Pending Decision Status
- `pending`, `decided`, `expired`, `skipped`

### Filter Field
- `subtitle_group`, `resolution`, `container`, `video_codec`, `audio_codec`, `subtitle_type`, `source`, `title_cn`, `title_en`

### Filter Operator
- `eq`, `contains`, `fuzzy`, `in`, `regex`
