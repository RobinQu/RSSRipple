"""Episode ORM model."""

import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKeyConstraint, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.utils.sql_time import UTCNow


class Episode(Base):
    __tablename__ = "episodes"
    __table_args__ = (
        UniqueConstraint("series_id", "season", "episode", name="uq_episode_series_season_episode"),
        # DB-level enforcement of "Episode.season 恒等于所属作品的
        # season_number": the composite FK targets the redundant
        # uq_tv_series_id_season_number unique constraint. DEFERRABLE
        # INITIALLY DEFERRED so season corrections (parent season_number +
        # child season re-tagged within one flush/transaction, e.g.
        # bangumi_relations._correct_work_season) check at commit instead of
        # mid-flush. ON DELETE CASCADE replaces the former plain series_id FK.
        ForeignKeyConstraint(
            ["series_id", "season"],
            ["tv_series.id", "tv_series.season_number"],
            name="fk_episodes_series_season",
            ondelete="CASCADE",
            deferrable=True,
            initially="DEFERRED",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    series_id: Mapped[str] = mapped_column(String(36), nullable=False)
    season: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
    )
    episode: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    air_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=UTCNow(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=UTCNow(), onupdate=UTCNow(), nullable=False
    )

    # Relationships
    series = relationship("TVSeries", back_populates="episodes")
