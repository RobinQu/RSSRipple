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

    # LLM-based metadata search: model with built-in web search (e.g. perplexity/sonar-pro).
    # Used as a final fallback when local matches fail. Empty string = fall back to llm_model.
    llm_search_model: str = ""

    # Poster image cache — persist cover art to the local filesystem.
    # When set, poster URLs returned by LLM are downloaded and stored here,
    # and the DB pointer is updated to the local /posters/<file> path.
    poster_cache_dir: str = "data/posters"

    # Task queue backend: "memory" (default, single-process) or "redis" (distributed)
    queue_backend: str = "memory"
    redis_url: str = "redis://localhost:6379/0"

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

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


settings = Settings()
