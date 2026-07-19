"""Application configuration using pydantic-settings."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Database
    database_url: str = "sqlite+aiosqlite:///data/rss_ripple_dev.db"

    # Scheduler
    default_fetch_interval: int = 1800  # 30 minutes

    # LLM (OpenAI-compatible API)
    llm_api_key: str = ""
    llm_model: str = "openrouter/free"
    llm_base_url: str = "https://openrouter.ai/api/v1"
    llm_enable_thinking: bool = False  # pass enable_thinking=false to disable chain-of-thought for speed

    # Multi-source metadata search agent
    tmdb_api_key: str = ""
    # Jina Search + Reader API: https://jina.ai/api-dashboard/
    # Used by the "jina" metadata source — cheap web-native search with strong
    # CJK/JA/EN coverage. Empty string disables the jina source at runtime.
    jina_api_key: str = ""

    # Exa AI Agent API (deeper, structured-agent web metadata source)
    exa_api_key: str = ""
    exa_effort_level: str = "low"  # "minimal" | "low" | "medium" | "high" | "xhigh"

    # Metadata source enable switches. A source is offered as a candidate in the
    # channel form only when its switch is on AND its credentials are configured
    # (wikipedia needs no API key). Turning a switch off hides an otherwise
    # configured source without clearing its key. Env vars: EXA_ENABLED,
    # JINA_ENABLED, TMDB_ENABLED, WIKIPEDIA_ENABLED.
    exa_enabled: bool = True
    jina_enabled: bool = True
    tmdb_enabled: bool = True
    wikipedia_enabled: bool = True

    # Poster image cache — persist cover art to the local filesystem.
    # When set, poster URLs returned by LLM are downloaded and stored here,
    # and the DB pointer is updated to the local /posters/<file> path.
    poster_cache_dir: str = "data/posters"

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

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


settings = Settings()
