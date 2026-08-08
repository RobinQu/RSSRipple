"""Unit tests for scripts/fix_double_tmdb_prefix.py.

Covers the pure normalization/decision helpers (``normalize_double_prefix``,
``normalize_collection_id``, ``plan_fix``) and the DB-level ``fix_model``
loop including collision handling, driven with ORM rows against a test DB.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.models.movie import Movie
from app.models.series import TVSeries
from app.models.work_collection import WorkCollection
from scripts.fix_double_tmdb_prefix import (
    OUTCOME_COLLISION,
    OUTCOME_FIXED,
    OUTCOME_UNCHANGED,
    fix_model,
    normalize_collection_id,
    normalize_double_prefix,
    plan_fix,
)


def _uuid() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# normalize_double_prefix
# ---------------------------------------------------------------------------


def test_normalize_double_tmdb_prefix_via_canonicalizer():
    assert normalize_double_prefix("tmdb:tmdb:1430077", "tmdb") == "tmdb:1430077"


def test_normalize_double_prefix_case_insensitive():
    assert normalize_double_prefix("TMDB:TMDB:1430077", "exa") == "tmdb:1430077"


def test_normalize_non_tmdb_double_prefix_fallback():
    """Shapes the canonicalizer doesn't know fall back to stripping one prefix."""
    assert normalize_double_prefix("mal:mal:5114", "exa") == "mal:5114"


def test_normalize_single_prefix_untouched():
    assert normalize_double_prefix("tmdb:1430077", "tmdb") is None
    assert normalize_double_prefix("imdb:tt1234567", None) is None
    assert normalize_double_prefix("1430077", "tmdb") is None
    assert normalize_double_prefix(None, "tmdb") is None
    assert normalize_double_prefix("", "tmdb") is None


def test_normalize_triple_prefix_collapses_to_single():
    assert normalize_double_prefix("tmdb:tmdb:tmdb:1430077", "tmdb") == "tmdb:1430077"


# ---------------------------------------------------------------------------
# normalize_collection_id (WorkCollection raw-digit convention)
# ---------------------------------------------------------------------------


def test_normalize_collection_id_strips_to_digits():
    assert normalize_collection_id("tmdb:tmdb:131295") == "131295"
    assert normalize_collection_id("tmdb_collection:tmdb_collection:131295") == "131295"


def test_normalize_collection_id_ignores_clean_rows():
    assert normalize_collection_id("131295") is None
    assert normalize_collection_id(None) is None


# ---------------------------------------------------------------------------
# plan_fix
# ---------------------------------------------------------------------------


def test_plan_fix_fixed():
    outcome, normalized = plan_fix("tmdb:tmdb:1", "tmdb", {"tmdb:2"})
    assert (outcome, normalized) == (OUTCOME_FIXED, "tmdb:1")


def test_plan_fix_collision_when_taken():
    outcome, normalized = plan_fix("tmdb:tmdb:1", "tmdb", {"tmdb:1", "tmdb:2"})
    assert (outcome, normalized) == (OUTCOME_COLLISION, "tmdb:1")


def test_plan_fix_unchanged_for_clean_rows():
    outcome, normalized = plan_fix("tmdb:1", "tmdb", set())
    assert (outcome, normalized) == (OUTCOME_UNCHANGED, None)


# ---------------------------------------------------------------------------
# fix_model against a test DB
# ---------------------------------------------------------------------------


async def test_fix_model_rewrites_movie_and_series(db_session):
    bad_movie = Movie(
        id=_uuid(), title_cn="坏电影", external_id="tmdb:tmdb:1430077",
        external_source="tmdb",
    )
    good_movie = Movie(
        id=_uuid(), title_cn="好电影", external_id="tmdb:999", external_source="tmdb",
    )
    bad_series = TVSeries(
        id=_uuid(), title_cn="坏剧", external_id="tmdb:tmdb:82684",
        external_source="exa",
    )
    db_session.add_all([bad_movie, good_movie, bad_series])
    await db_session.flush()

    await fix_model(db_session, Movie, "Movie", apply=True)
    await fix_model(db_session, TVSeries, "TVSeries", apply=True)

    assert bad_movie.external_id == "tmdb:1430077"
    assert bad_series.external_id == "tmdb:82684"
    assert good_movie.external_id == "tmdb:999"  # untouched


async def test_fix_model_flags_collision_and_leaves_both_rows(db_session):
    """The normalized id already belongs to another row -> no rewrite."""
    dup = Movie(
        id=_uuid(), title_cn="重复", external_id="tmdb:tmdb:1430077",
        external_source="tmdb",
    )
    owner = Movie(
        id=_uuid(), title_cn="正主", external_id="tmdb:1430077",
        external_source="tmdb",
    )
    db_session.add_all([dup, owner])
    await db_session.flush()

    await fix_model(db_session, Movie, "Movie", apply=True)

    assert dup.external_id == "tmdb:tmdb:1430077"  # untouched
    assert owner.external_id == "tmdb:1430077"


async def test_fix_model_work_collection_raw_digits(db_session):
    bad = WorkCollection(
        id=_uuid(), title_cn="合集", external_id="tmdb:tmdb:131295",
        external_source="tmdb_collection",
    )
    good = WorkCollection(
        id=_uuid(), title_cn="好合集", external_id="10",
        external_source="tmdb_collection",
    )
    db_session.add_all([bad, good])
    await db_session.flush()

    await fix_model(db_session, WorkCollection, "WorkCollection", apply=True,
                    keep_prefix=False)

    assert bad.external_id == "131295"
    assert good.external_id == "10"


async def test_fix_model_dry_run_does_not_mutate(db_session):
    bad = Movie(
        id=_uuid(), title_cn="坏电影", external_id="tmdb:tmdb:1",
        external_source="tmdb",
    )
    db_session.add(bad)
    await db_session.flush()

    await fix_model(db_session, Movie, "Movie", apply=False)

    assert bad.external_id == "tmdb:tmdb:1"
    rows = (await db_session.execute(
        select(Movie).where(Movie.id == bad.id)
    )).scalars().all()
    assert rows[0].external_id == "tmdb:tmdb:1"
