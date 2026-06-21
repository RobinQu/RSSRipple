"""Application configuration using pydantic-settings."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Database
    database_url: str = "sqlite+aiosqlite:///data/rss_downloader.db"

    # Scheduler
    default_fetch_interval: int = 1800  # 30 minutes

    # LLM
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    llm_base_url: str = "https://api.openai.com/v1"

    # App
    app_name: str = "RSS Downloader"
    debug: bool = False
    log_level: str = "INFO"

    # Download
    max_retry_count: int = 3
    task_expire_days: int = 30

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
