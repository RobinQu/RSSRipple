"""Tests for settings_service.py: runtime key/value settings stored in
``app_settings`` (upsert/delete semantics) plus the typed readers with
defaults, boolean parsing, and integer clamping.
"""

from sqlalchemy import select

from app.models.app_setting import AppSetting
from app.services.settings_service import (
    get_bool_setting,
    get_int_setting,
    get_setting,
    set_setting,
)


async def test_set_and_get_roundtrip(db_session):
    await set_setting(db_session, "default_metadata_source", "wikipedia")
    await db_session.commit()
    assert await get_setting(db_session, "default_metadata_source") == "wikipedia"


async def test_get_unset_returns_none(db_session):
    assert await get_setting(db_session, "never-set") is None


async def test_set_existing_key_updates_value(db_session):
    await set_setting(db_session, "some_key", "first")
    await db_session.commit()
    await set_setting(db_session, "some_key", "second")
    await db_session.commit()
    assert await get_setting(db_session, "some_key") == "second"
    rows = (await db_session.execute(
        select(AppSetting).where(AppSetting.key == "some_key")
    )).scalars().all()
    assert len(rows) == 1


async def test_set_empty_string_deletes_row(db_session):
    await set_setting(db_session, "gone", "value")
    await db_session.commit()
    await set_setting(db_session, "gone", "")
    await db_session.commit()
    assert await get_setting(db_session, "gone") is None


async def test_set_none_is_noop_when_unset(db_session):
    await set_setting(db_session, "absent", None)
    await db_session.commit()
    assert await get_setting(db_session, "absent") is None
    assert (await db_session.execute(select(AppSetting))).scalars().all() == []


async def test_get_bool_setting_defaults(db_session):
    assert await get_bool_setting(db_session, "unset-flag", default=True) is True
    assert await get_bool_setting(db_session, "unset-flag", default=False) is False


async def test_get_bool_setting_parses_truey_falsey_values(db_session):
    cases = [
        ("1", True), ("true", True), ("TRUE", True), (" yes ", True), ("on", True),
        ("0", False), ("false", False), ("off", False), ("no", False), ("whatever", False),
    ]
    for raw, expected in cases:
        await set_setting(db_session, "flag", raw)
        await db_session.commit()
        assert await get_bool_setting(db_session, "flag") is expected


async def test_get_int_setting_default_when_unset_or_invalid(db_session):
    assert await get_int_setting(db_session, "unset-int", default=7) == 7

    await set_setting(db_session, "interval", "not-a-number")
    await db_session.commit()
    assert await get_int_setting(db_session, "interval", default=42) == 42


async def test_get_int_setting_parses_and_clamps(db_session):
    await set_setting(db_session, "interval", "5000")
    await db_session.commit()
    assert await get_int_setting(db_session, "interval", default=0) == 5000

    # min clamp
    assert await get_int_setting(db_session, "interval", default=0, minimum=6000) == 6000
    # max clamp
    assert await get_int_setting(db_session, "interval", default=0, maximum=100) == 100
    # default value is clamped too
    assert await get_int_setting(db_session, "absent", default=5, minimum=10) == 10
    assert await get_int_setting(db_session, "absent", default=500, maximum=100) == 100
