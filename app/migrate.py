"""One-shot schema migration entry point (``python -m app.migrate``).

Runs ``create_tables`` (schema creation + light migrations) and exits. The
distributed docker-compose stack runs this as a ``migrate`` service that web
and worker services depend on (``service_completed_successfully``), with
``DB_MIGRATE_ON_STARTUP=false`` on the long-lived processes — so their slow
jobs can never hold transactions that gridlock startup DDL.
"""

import asyncio
import logging

# Import models for SQLAlchemy discovery
import app.models  # noqa: F401
from app.config import settings
from app.database import create_tables

logger = logging.getLogger("app.migrate")


def main() -> None:  # pragma: no cover - process wiring
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("app").setLevel(settings.log_level)
    logger.info("Running database migrations...")
    asyncio.run(create_tables())
    logger.info("Database migrations complete.")


if __name__ == "__main__":
    main()
