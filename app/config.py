"""Application configuration using pydantic-settings."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Database
    database_url: str = "sqlite+aioturso:///data/rss_ripple_turso.db"

    # Scheduler
    default_fetch_interval: int = 1800  # 30 minutes

    # LLM (OpenAI-compatible API)
    llm_api_key: str = ""
    llm_model: str = "openrouter/free"
    llm_base_url: str = "https://openrouter.ai/api/v1"
    llm_enable_thinking: bool = False  # pass enable_thinking=false to disable chain-of-thought for speed

    # Multi-source metadata search agent
    tmdb_api_key: str = ""
    # Bangumi API token (https://bangumi.github.io/api/). Used by the
    # "bangumi" metadata source and the post-link is_anime verification —
    # both are enabled simply by the token being configured. Env var:
    # BANGUMI_API_KEY.
    bangumi_api_key: str = ""
    # Bangumi API base URL. Overridable so self-hosted mirrors and integration
    # tests (mock server) can point the client elsewhere. Env var:
    # BANGUMI_API_BASE.
    bangumi_api_base: str = "https://api.bgm.tv/v0"
    # Wigolo web-search fallback (self-hosted agent search engine, one daemon
    # serves REST + MCP). Used when the wikipedia/tmdb/bangumi primary sources
    # miss: the fallback searches the identity-site whitelist on the daemon.
    # Env vars: WIGOLO_BASE_URL, WIGOLO_API_TOKEN (required for non-loopback
    # binds).
    wigolo_base_url: str = "http://flash-aio:3333"
    wigolo_api_token: str = ""

    # Metadata source enable switches. A source is offered as a candidate in the
    # channel form only when its switch is on AND its credentials are configured
    # (wikipedia needs no API key). Turning a switch off hides an otherwise
    # configured source without clearing its key. ``web_fallback_enabled``
    # gates only the wigolo web-search fallback. Env vars:
    # WEB_FALLBACK_ENABLED, TMDB_ENABLED, WIKIPEDIA_ENABLED.
    web_fallback_enabled: bool = True
    tmdb_enabled: bool = True
    wikipedia_enabled: bool = True

    # Poster image cache — persist cover art to the local filesystem.
    # When set, poster URLs returned by LLM are downloaded and stored here,
    # and the DB pointer is updated to the local /posters/<file> path.
    poster_cache_dir: str = "data/posters"

    # Torrent file cache — .torrent files fetched for content inspection
    # (file-listing batch analysis) are stored here as
    # ``<resource_id>.torrent``. Env var: TORRENT_CACHE_DIR.
    torrent_cache_dir: str = "data/torrents"

    # Magnet metadata resolution — magnet: links carry no .torrent file, so a
    # background worker fetches the torrent *metadata only* via libtorrent
    # (upload_mode, no payload) and rebuilds a standard .torrent into the
    # cache dir above. Env vars: MAGNET_RESOLVE_ENABLED,
    # MAGNET_RESOLVE_TIMEOUT_SECONDS, MAGNET_RESOLVE_CONCURRENCY,
    # MAGNET_RESOLVE_MAX_ATTEMPTS.
    magnet_resolve_enabled: bool = True
    # Overall budget for one resolution attempt (metadata fetch over the
    # swarm can take a long time on quiet magnets).
    magnet_resolve_timeout_seconds: int = 900
    # Max concurrent libtorrent resolutions (semaphore in the worker pool).
    magnet_resolve_concurrency: int = 4
    # Automatic retries after the first failure (manual retry is unlimited).
    magnet_resolve_max_attempts: int = 1
    # Default public trackers injected into every resolution attempt, on top of
    # whatever trackers the magnet itself carries — harvested magnets (e.g.
    # ThePirateBay) carry no ``tr=`` params, leaving DHT as the only peer
    # source. The http:// opentrackr entry comes first on purpose: it works in
    # UDP-blocked networks (DHT and udp:// trackers all need UDP outbound,
    # frequently blocked by firewalls), and each tracker gets its own tier so
    # libtorrent tries it before timing out on the UDP entries. Env var:
    # MAGNET_RESOLVE_DEFAULT_TRACKERS (JSON list).
    magnet_resolve_default_trackers: list[str] = [
        "http://tracker.opentrackr.org:1337/announce",
        "udp://tracker.opentrackr.org:1337/announce",
        "udp://open.tracker.cl:1337/announce",
        "udp://tracker.openbittorrent.com:6969/announce",
        "udp://exodus.desync.com:6969/announce",
        "udp://tracker.torrent.eu.org:451/announce",
        "udp://open.demonii.com:1337/announce",
    ]
    # Infohash-cache mirror fast path — HTTPS GET of a cached .torrent by v1
    # SHA-1 infohash, resolving in seconds instead of up to 15 minutes of P2P
    # metadata wait. Each entry is a URL template with the literal
    # ``{infohash}`` placeholder (replaced with the lowercase 40-hex hash).
    # Empty list disables the fast path. WARNING: mirrors can return an
    # unrelated valid torrent for hashes they do not have — every response is
    # re-verified against the requested infohash before use. Env var:
    # MAGNET_RESOLVE_CACHE_MIRRORS (JSON list).
    magnet_resolve_cache_mirrors: list[str] = [
        "https://itorrents.org/torrent/{infohash}.torrent",
    ]

    # Process role for web/worker separation: "all" (default, standalone —
    # HTTP + scheduler + queue consumer in one process), "web" (HTTP API +
    # enqueue only; no scheduler, no queue consumption), "worker" (scheduler +
    # queue consumer; started via `python -m app.worker`, serves no HTTP).
    # Env var: APP_ROLE.
    app_role: str = "all"

    # Run schema creation + light migrations at process startup. Default on;
    # the distributed docker-compose stack sets DB_MIGRATE_ON_STARTUP=false on
    # web/worker and runs DDL once in the one-shot `migrate` service instead,
    # so long-running worker transactions can never gridlock startup DDL.
    db_migrate_on_startup: bool = True

    # Task queue backend: "memory" (default, single-process) or "redis" (distributed)
    queue_backend: str = "memory"
    redis_url: str = "redis://localhost:6379/0"
    # Max concurrent jobs. A metadata-refresh job (sequential, can run for a long
    # time) must not monopolize the worker - with the default 1, it starved
    # fetch_channel jobs. 4 lets a long refresh coexist with channel fetches.
    queue_max_concurrent: int = 4

    # App
    app_name: str = "RSSRipple"
    debug: bool = False
    dev_mode: bool = False  # expose stack traces in 500 responses; set True in development
    log_level: str = "INFO"

    # Download
    max_retry_count: int = 3
    task_expire_days: int = 30

    # Transmission
    transmission_timeout: int = 30

    # Scheduler / background jobs. Disable for integration tests where tests
    # explicitly trigger fetch/agent jobs and automatic scheduling causes
    # ALREADY_RUNNING races.
    scheduler_enabled: bool = True

    # Download notifications (webhook fan-out to external consumers; the
    # built-in organize subsystem below consumes the same notifications
    # in-process). Enabled by default; webhooks are registered per Agent in
    # the UI. Delivery is pure outbound POST, no token, no consumer callback.
    notify_enabled: bool = True
    # Delivery retry policy: exponential backoff base * 2^attempt (capped at
    # 30 min); after this many failed attempts the notification is "failed"
    # and can only be recovered via manual retry in the UI.
    notify_max_attempts: int = 5
    notify_retry_base_seconds: int = 30
    # Retention for consumed ("done") notifications.
    notify_retention_days: int = 30

    # Built-in file organization subsystem (organize): turns download
    # completion notifications into file-operation plans targeting scanned
    # Libraries. Always on; the planning step activates as soon as any
    # enabled organize rule exists.
    # Media server refresh addressing lives entirely in the database
    # (media_server_instances, R2); there is no global PLEX_* config anymore.

    # Application authentication. When enabled, /api/v1/* and /posters/*
    # require a TOTP-issued session cookie or an API key (env bootstrap key
    # below, or keys created via /api/v1/api-keys). Env vars: AUTH_ENABLED,
    # API_KEY.
    auth_enabled: bool = True
    # Static bootstrap key for ops recovery and integration tests.
    api_key: str | None = None

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


settings = Settings()
