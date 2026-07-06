"""AppSetting ORM model — a simple key/value store for runtime settings.

Settings that users can change at runtime (no restart) live here instead of
``app.config.Settings`` (which is env-var driven and read-only). Examples:
the default metadata search source for the works-page refresh action.
"""

from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )
