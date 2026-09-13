"""Transactionally version configuration that can change organize plans."""
from sqlalchemy import BigInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

CONFIGURATION_ID = "b72934dd-04bd-4cc3-8a68-ab1957b78027"


class OrganizeConfiguration(Base):
    __tablename__ = "organize_configuration"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    lock_domain: Mapped[str | None] = mapped_column(String(36), nullable=True)
