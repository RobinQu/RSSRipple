"""Layer-3 movie title fallback in ``find_existing_movie_for_external``:
normalized comparison, year guard, and multi-hit ambiguity (never auto-bind).
"""

from __future__ import annotations

import uuid
from datetime import date

from app.models.movie import Movie
from app.services.metadata_service import find_existing_movie_for_external


def _uuid() -> str:
    return str(uuid.uuid4())


def _movie(**kw) -> Movie:
    defaults = {"id": _uuid(), "content_type": "movie", "external_source": "tmdb"}
    defaults.update(kw)
    return Movie(**defaults)


async def test_title_fallback_matches_normalized_titles(db_session):
    """Trad/simp and half/full-width variants of a stored title converge with
    the incoming entity's titles."""
    stored = _movie(title_cn="測試電影", title_en="Ｔｅｓｔ Ｍｏｖｉｅ", external_id="tmdb:222")
    db_session.add(stored)
    await db_session.flush()

    found = await find_existing_movie_for_external(db_session, {
        "content_type": "movie",
        "title_cn": "测试电影",  # simplified twin of the stored title
        "external_id": "tmdb:111",
        "external_source": "tmdb",
    })
    assert found is not None and found.id == stored.id

    found2 = await find_existing_movie_for_external(db_session, {
        "content_type": "movie",
        "title_en": "Test Movie",  # half-width twin of the stored title
        "external_id": "tmdb:333",
        "external_source": "tmdb",
    })
    assert found2 is not None and found2.id == stored.id


async def test_title_fallback_year_guard_excludes_remake(db_session):
    """Same normalized title but a year conflict (beyond ±1) → not the same
    work; the fallback must not bind it."""
    stored = _movie(
        title_cn="重生", external_id="tmdb:222", release_date=date(1995, 4, 1),
    )
    db_session.add(stored)
    await db_session.flush()

    found = await find_existing_movie_for_external(db_session, {
        "content_type": "movie",
        "title_cn": "重生",
        "release_date": "2026-01-01",
        "external_id": "tmdb:111",
        "external_source": "tmdb",
    })
    assert found is None

    # Within the ±1 slack the guard does not fire.
    found_ok = await find_existing_movie_for_external(db_session, {
        "content_type": "movie",
        "title_cn": "重生",
        "release_date": "1996-03-01",
        "external_id": "tmdb:333",
        "external_source": "tmdb",
    })
    assert found_ok is not None and found_ok.id == stored.id

    # No entity year → no evidence either way → still binds.
    found_no_year = await find_existing_movie_for_external(db_session, {
        "content_type": "movie",
        "title_cn": "重生",
        "external_id": "tmdb:444",
        "external_source": "tmdb",
    })
    assert found_no_year is not None and found_no_year.id == stored.id


async def test_title_fallback_ambiguity_does_not_auto_bind(db_session):
    """Multiple rows sharing the normalized title (no disqualifying evidence)
    are ambiguous — the fallback returns None instead of an arbitrary row."""
    a = _movie(title_cn="同名电影", external_id="tmdb:222")
    b = _movie(title_cn="同名电影", external_id="tmdb:333", title_en="Same Name Film")
    db_session.add_all([a, b])
    await db_session.flush()

    found = await find_existing_movie_for_external(db_session, {
        "content_type": "movie",
        "title_cn": "同名电影",
        "external_id": "tmdb:111",
        "external_source": "tmdb",
    })
    assert found is None


async def test_title_fallback_year_guard_disambiguates(db_session):
    """Two same-title rows: the year-conflicting one is excluded, the
    remaining single hit binds."""
    old = _movie(title_cn="重生", external_id="tmdb:222", release_date=date(1995, 1, 1))
    new = _movie(title_cn="重生", external_id="tmdb:333", release_date=date(2026, 2, 1))
    db_session.add_all([old, new])
    await db_session.flush()

    found = await find_existing_movie_for_external(db_session, {
        "content_type": "movie",
        "title_cn": "重生",
        "release_date": "2025-11-01",
        "external_id": "tmdb:111",
        "external_source": "tmdb",
    })
    assert found is not None and found.id == new.id
