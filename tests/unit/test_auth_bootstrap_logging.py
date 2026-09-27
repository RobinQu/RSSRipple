"""Exercise production startup using an isolated real database and synthetic secrets."""

import logging

from app import database, main
from app.models.app_setting import AppSetting
from app.services import task_queue
from app.services.auth_service import SETTING_COOKIE_SECRET, SETTING_TOTP_SECRET


async def test_startup_does_not_publish_authenticator_secret(
    db_engine, tmp_path, monkeypatch, caplog,
):
    db_session_factory = database.async_session_factory
    monkeypatch.setattr(task_queue, "task_queue", task_queue.task_queue)
    monkeypatch.setattr(main, "async_session_factory", db_session_factory)
    monkeypatch.setattr(main.settings, "db_migrate_on_startup", False)
    monkeypatch.setattr(main.settings, "poster_cache_dir", str(tmp_path / "posters"))
    monkeypatch.setattr(main.settings, "app_role", "web")
    monkeypatch.setattr(main.settings, "queue_backend", "memory")
    monkeypatch.setattr(main.settings, "log_level", "INFO")
    caplog.set_level(logging.INFO, logger="app")
    secrets = []
    for _ in range(2):
        async with main.lifespan(main.app):
            async with db_session_factory() as db:
                totp = (await db.get(AppSetting, SETTING_TOTP_SECRET)).value
                cookie = (await db.get(AppSetting, SETTING_COOKIE_SECRET)).value
                secrets.append((totp, cookie))
    assert secrets[0] == secrets[1]
    assert all(secrets[0])
    assert "otpauth://" not in caplog.text
    assert secrets[0][0] not in caplog.text
    assert secrets[0][1] not in caplog.text
