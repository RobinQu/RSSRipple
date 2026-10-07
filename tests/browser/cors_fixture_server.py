"""Local-only browser fixture: temporary DB, real auth/API, no scheduler.

Run from the repository with PYTHONPATH=. and the project's Python environment.
Ports 32900/32901/32902 are reserved for this standalone test only.
"""

import asyncio
import os
import tempfile
from pathlib import Path


def main():
    # Override before app imports, so a developer's .env cannot select a real DB.
    with tempfile.TemporaryDirectory(prefix="rssripple-cors-browser-") as directory:
        os.environ["DATABASE_URL"] = "sqlite+aioturso:///" + str(Path(directory) / "test.db")
        os.environ["POSTER_CACHE_DIR"] = str(Path(directory) / "posters")
        os.environ["AUTH_ENABLED"] = "true"
        os.environ["CORS_ALLOWED_ORIGINS"] = '["http://127.0.0.1:32901"]'
        os.environ["API_KEY"] = ""
        os.environ["QUEUE_BACKEND"] = "db"

        import uvicorn
        from fastapi.responses import HTMLResponse

        from app.database import async_session_factory, create_tables, engine
        from app.main import app
        from app.services.settings_service import set_setting

        @app.get("/browser-probe")
        async def browser_probe():
            return HTMLResponse("<!doctype html><title>Synthetic CORS probe</title>")

        async def serve():
            try:
                await create_tables()
                async with async_session_factory() as db:
                    await set_setting(db, "auth_totp_secret", "JBSWY3DPEHPK3PXP")
                    await db.commit()
                await uvicorn.Server(uvicorn.Config(
                    app, host="127.0.0.1", port=32900, lifespan="off", log_level="warning",
                )).serve()
            finally:
                await engine.dispose()

        asyncio.run(serve())


if __name__ == "__main__":
    main()
