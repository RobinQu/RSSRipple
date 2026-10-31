"""Layer-3 movie title fallback in ``find_existing_movie_for_external``:
normalized comparison, year guard, and multi-hit ambiguity (never auto-bind).
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest

from app.models.movie import Movie
from app.services.metadata_service import (
    MetadataIdentityConflictError,
    create_or_update_movie_from_external,
    find_existing_movie_for_external,
)


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


async def test_movie_upsert_converges_through_alternate_imdb_identity(db_session):
    stored = _movie(
        title_en="Manually Renamed",
        original_title="Manually Renamed",
        external_source="imdb",
        external_id="imdb:tt26315134",
    )
    db_session.add(stored)
    await db_session.flush()

    movie = await create_or_update_movie_from_external(db_session, {
        "content_type": "movie",
        "title_en": "He's Watching You",
        "original_title": "He's Watching You",
        "external_source": "tmdb",
        "external_id": "tmdb:1370424",
        "alt_external_ids": [{"source": "imdb", "id": "imdb:tt26315134"}],
        "release_date": "2026-01-23",
        "rating": 5.8,
    })

    assert movie.id == stored.id
    assert stored.release_date == date(2026, 1, 23)
    assert stored.rating == 5.8


async def test_movie_upsert_rejects_identities_owned_by_different_works(db_session):
    tmdb_owner = _movie(title_en="TMDB Owner", external_id="tmdb:1370424")
    imdb_owner = _movie(
        title_en="IMDb Owner", external_source="imdb", external_id="imdb:tt26315134",
    )
    db_session.add_all([tmdb_owner, imdb_owner])
    await db_session.flush()

    with pytest.raises(MetadataIdentityConflictError):
        await create_or_update_movie_from_external(db_session, {
            "content_type": "movie",
            "title_en": "He's Watching You",
            "external_source": "tmdb",
            "external_id": "tmdb:1370424",
            "alt_external_ids": [{"source": "imdb", "id": "imdb:tt26315134"}],
            "rating": 5.8,
        })
    assert tmdb_owner.rating is None
    assert imdb_owner.rating is None
