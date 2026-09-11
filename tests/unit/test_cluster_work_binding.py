"""Unit tests for app.services.cluster_work_binding.

Two layers:

* the PURE helpers (``_work_titles`` … ``_qualifier_compatible``) are asserted
  directly with ``SimpleNamespace`` works/assignment rows and crafted hints —
  form compatibility, base-name inclusion, sequel-number ambiguity and the
  unique-pick truth tables;
* the async DB/search functions are driven through ``bind_hint_clusters`` (and
  a few direct calls) against the real test DB, with FTS and external metadata
  search stubbed — asserting bound rows, season remaps and the no-op branches.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from sqlalchemy import select

import app.services.cluster_work_binding as cwb
from app.models.channel import Channel
from app.models.file_resource import FileResource
from app.models.movie import Movie
from app.models.resource_file_assignment import ResourceFileAssignment
from app.models.resource_work_link import ResourceWorkLink
from app.models.series import TVSeries
from app.models.work_collection import WorkCollection
from app.services.text_normalizer import normalize_title

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _uuid() -> str:
    return str(uuid.uuid4())


def _work(**over):
    base = dict(
        title_cn=None, title_en=None, original_title=None, aliases=None, id=_uuid(),
    )
    base.update(over)
    return SimpleNamespace(**base)


def _row(**over):
    base = dict(
        file_path="a/e01.mkv",
        work_title_hint=None,
        season=None,
        episode_start=None,
        episode_end=None,
        series_id=None,
        movie_id=None,
        source="auto",
    )
    base.update(over)
    return SimpleNamespace(**base)


async def _channel(db) -> Channel:
    ch = Channel(
        id=_uuid(),
        name=f"ch-{_uuid()[:8]}",
        type="rss_feed",
        url="https://example.com/rss",
        fetch_interval=1800,
        status="active",
        field_mapping={"list_locator": {"source": "entries"}},
        metadata_agent_enabled=False,
    )
    db.add(ch)
    await db.flush()
    return ch


async def _resource(db, channel_id: str, **over) -> FileResource:
    base = dict(
        id=_uuid(),
        channel_id=channel_id,
        guid=_uuid(),
        title_raw="[Group] Show [Batch]",
        torrent_url="https://x/pack.torrent",
        is_batch=True,
    )
    base.update(over)
    r = FileResource(**base)
    db.add(r)
    await db.flush()
    return r


def _patch_fts(monkeypatch, *, series_ids=(), movie_ids=()):
    """Deterministic FTS candidate sets (isolate the guard logic)."""
    import app.services.fts as fts

    async def _series(db, query, limit=20):
        return list(series_ids)

    async def _movies(db, query, limit=20):
        return list(movie_ids)

    monkeypatch.setattr(fts, "search_series_fts", _series)
    monkeypatch.setattr(fts, "search_movie_fts", _movies)


def _patch_meta(monkeypatch, handler, *, source="wikipedia"):
    import app.services.metadata_agent as ma
    import app.services.metadata_sources as ms

    monkeypatch.setattr(
        ma, "get_agent", lambda: SimpleNamespace(process_title_only=handler)
    )
    monkeypatch.setattr(ms, "resolve_metadata_source", lambda v: source)


def _patch_bind_search(monkeypatch, *, external=None):
    """Quiet FTS + external search so collection-member resolution is the only
    possible hit (and external never surprises a test)."""
    _patch_fts(monkeypatch)

    async def _handler(title, source):
        if external is not None:
            return external
        return SimpleNamespace(found=False, ambiguous=False, matched_entity=None)

    _patch_meta(monkeypatch, _handler)


# ===========================================================================
# Pure helpers
# ===========================================================================


def test_work_titles_and_matches():
    work = _work(
        title_cn="Foo", title_en="Bar", original_title="Baz",
        aliases=["Qux", None],
    )
    assert cwb._work_titles(work) == ["Foo", "Bar", "Baz", "Qux"]
    assert cwb._work_titles(_work()) == []
    assert cwb._matches(work, normalize_title("bar")) is True
    assert cwb._matches(work, "not-there") is False


def test_cluster_season_most_common():
    rows = [
        _row(season=2), _row(season=2), _row(season=1), _row(season=None),
    ]
    assert cwb._cluster_season(rows) == 2
    assert cwb._cluster_season([_row(season=None)]) is None
    assert cwb._cluster_season([]) is None


def test_cluster_form_branches():
    # Strong episode pattern in the path → tv.
    assert cwb._cluster_form([_row(file_path="a/S01E01.mkv")], "X") == "tv"
    # Explicit movie marker in the hint.
    assert cwb._cluster_form([_row(file_path="a/e.mkv")], "X Movie") == "movie"
    # Explicit movie marker in the path.
    assert cwb._cluster_form([_row(file_path="[Movie] X/m.mkv")], None) == "movie"
    # Single-file OVA/OAD dir with weak episode evidence stays open to movie.
    assert cwb._cluster_form(
        [_row(file_path="[OVA] X/o.mkv", episode_start=1, episode_end=1)], "X"
    ) == "unknown"
    # Episode evidence on a single non-film file → tv.
    assert cwb._cluster_form(
        [_row(file_path="a/e1.mkv", episode_end=1)], "X"
    ) == "tv"
    # Episode evidence across multiple rows → tv.
    assert cwb._cluster_form(
        [
            _row(file_path="a/e1.mkv", episode_start=1),
            _row(file_path="a/e2.mkv", episode_start=2),
        ],
        "X",
    ) == "tv"
    # No evidence at all → unknown.
    assert cwb._cluster_form([_row(file_path="a/x.mkv")], "X") == "unknown"


def test_form_allows_truth_table():
    movie = Movie(id=_uuid(), title_cn="M")
    series = TVSeries(id=_uuid(), title_cn="S", season_number=1)
    assert cwb._form_allows("tv", series) is True
    assert cwb._form_allows("tv", movie) is False
    assert cwb._form_allows("movie", movie) is True
    assert cwb._form_allows("movie", series) is False
    assert cwb._form_allows("unknown", movie) is True
    assert cwb._form_allows("unknown", series) is True


def test_base_form_match_and_exact():
    assert cwb._base_form(None) == ""
    assert cwb._base_form("") == ""
    assert cwb._base_form("[YSS] Initial D: Battle Stage Season 2") == normalize_title(
        "Initial D Battle Stage"
    )
    work = _work(
        title_cn="Initial D Battle Stage", aliases=["Initial D: Battle Stage"],
    )
    # Base hint must be at least 2 chars.
    assert cwb._base_match("a", work) is False
    assert cwb._base_exact("a", work) is False
    # Mutual containment match, exact base match.
    assert cwb._base_match("Initial D Battle Stage", work) is True
    assert cwb._base_exact("Initial D Battle Stage", work) is True
    # A too-short candidate title is skipped, not matched.
    assert cwb._base_match("Initial D Battle Stage", _work(title_cn="D")) is False


def test_base_match_containment_but_not_exact():
    sequel = _work(title_cn="Initial D Battle Stage 2")
    assert cwb._base_match("Initial D Battle Stage", sequel) is True
    assert cwb._base_exact("Initial D Battle Stage", sequel) is False


def test_sequel_ambiguous_branches():
    plain = _work(title_cn="Show")
    # No sequel number on either side.
    assert cwb._sequel_ambiguous("Show S02", plain) is False
    seq2 = _work(title_cn="Initial D Battle Stage 2")
    # Hint without a number, work with one → stripped-base hit is ambiguous.
    assert cwb._sequel_ambiguous("Initial D Battle Stage", seq2) is True
    # Same number on both sides.
    assert cwb._sequel_ambiguous("Initial D Battle Stage 2", seq2) is False
    # Work has a number but the base names differ.
    assert cwb._sequel_ambiguous(
        "Initial D Battle Stage", _work(title_cn="Completely Different 2")
    ) is False


def test_pick_unique_branches():
    s1 = TVSeries(id=_uuid(), season_number=1)
    s2 = TVSeries(id=_uuid(), season_number=2)
    m = Movie(id=_uuid())
    m2 = Movie(id=_uuid())
    # Mixed series/movie candidates never resolve.
    assert cwb._pick_unique([s1, m], None) is None
    assert cwb._pick_unique([m], None) == ("movie", m)
    assert cwb._pick_unique([m, m2], None) is None
    assert cwb._pick_unique([], None) is None
    # Series without a season hint only resolves when unique.
    assert cwb._pick_unique([s1], None) == ("series", s1)
    assert cwb._pick_unique([s1, s2], None) is None
    # A season hint selects strictly.
    assert cwb._pick_unique([s1, s2], 2) == ("series", s2)
    assert cwb._pick_unique([s1, s2], 3) is None
    # Duplicate ids are de-duplicated.
    assert cwb._pick_unique([s1, s1], None) == ("series", s1)


def test_pick_by_title_branches():
    s1 = TVSeries(id=_uuid(), season_number=1)
    s2 = TVSeries(id=_uuid(), season_number=2)
    m = Movie(id=_uuid())
    assert cwb._pick_by_title([], None) is None
    assert cwb._pick_by_title([s1], None) is s1
    assert cwb._pick_by_title([s1, s2], None) is None
    assert cwb._pick_by_title([s1, s2], 2) is s2
    # No exact season match among multiple same-title works is ambiguous.
    assert cwb._pick_by_title([s1, s2], 3) is None
    # Multiple exact-season matches are still ambiguous.
    s1b = TVSeries(id=_uuid(), season_number=1)
    assert cwb._pick_by_title([s1, s1b], 1) is None
    # A lone movie (no season_number attribute) still resolves.
    assert cwb._pick_by_title([m], 1) is m


def test_work_type_of():
    assert cwb._work_type_of(TVSeries(id=_uuid())) == "series"
    assert cwb._work_type_of(Movie(id=_uuid())) == "movie"


def test_name_tokens_and_strip_numbered_marker():
    assert cwb._name_tokens(None) == set()
    assert cwb._name_tokens("Initial D") == {"initial", "d"}
    # A numbered Stage marker is stripped…
    assert cwb._strip_numbered_marker("Initial D Fifth Stage") == "Initial D"
    assert cwb._strip_numbered_marker("Show Season 2") == "Show"
    # …but an unmappable one ("Final Stage") is kept.
    assert cwb._strip_numbered_marker("頭文字D Final Stage") == "頭文字D Final Stage"
    assert cwb._strip_numbered_marker("Show") == "Show"


def test_qualifier_compatible():
    coll_tokens = cwb._name_tokens("头文字D Initial D")
    assert cwb._qualifier_compatible(
        "initial d", coll_tokens, _work(title_cn="头文字D")
    ) is True
    assert cwb._qualifier_compatible(
        "initial d", coll_tokens, _work(title_cn="Initial D Battle Stage")
    ) is False
    # A member with no matching residue when the hint has one is rejected.
    assert cwb._qualifier_compatible(
        "initial d battle stage",
        cwb._name_tokens("initial d"),
        _work(title_cn="头文字D"),
    ) is False


# ===========================================================================
# _associated_collection_ids / _ensure_auto_link
# ===========================================================================


async def test_associated_collection_ids(db_session):
    ch = await _channel(db_session)
    coll = WorkCollection(
        id=_uuid(), title_cn="C", external_source="series_group", external_id=_uuid(),
    )
    series = TVSeries(
        id=_uuid(), title_cn="S", season_number=1, collection_id=coll.id,
    )
    movie = Movie(id=_uuid(), title_cn="M", collection_id=coll.id)
    db_session.add_all([coll, series, movie])
    await db_session.flush()

    # FK + link reach the same collection.
    r = await _resource(db_session, ch.id)
    r.collection_id = coll.id
    r.series_id = series.id
    db_session.add_all([
        ResourceWorkLink(resource_id=r.id, series_id=series.id, source="auto"),
        ResourceWorkLink(resource_id=r.id, movie_id=movie.id, source="auto"),
    ])
    await db_session.flush()
    assert await cwb._associated_collection_ids(db_session, r) == [coll.id]

    # No associations at all.
    r2 = await _resource(db_session, ch.id)
    assert await cwb._associated_collection_ids(db_session, r2) == []

    # Linked works without a collection contribute nothing.
    bare_series = TVSeries(id=_uuid(), title_cn="S2", season_number=1)
    bare_movie = Movie(id=_uuid(), title_cn="M2")
    db_session.add_all([bare_series, bare_movie])
    await db_session.flush()
    r3 = await _resource(db_session, ch.id)
    r3.movie_id = bare_movie.id
    db_session.add(ResourceWorkLink(
        resource_id=r3.id, series_id=bare_series.id, source="auto",
    ))
    await db_session.flush()
    assert await cwb._associated_collection_ids(db_session, r3) == []


async def test_ensure_auto_link(db_session):
    ch = await _channel(db_session)
    series = TVSeries(id=_uuid(), title_cn="S", season_number=1)
    movie = Movie(id=_uuid(), title_cn="M")
    db_session.add_all([series, movie])
    await db_session.flush()
    r = await _resource(db_session, ch.id)

    assert await cwb._ensure_auto_link(db_session, r.id, "series", series.id) is True
    await db_session.flush()
    assert await cwb._ensure_auto_link(db_session, r.id, "series", series.id) is False
    assert await cwb._ensure_auto_link(db_session, r.id, "movie", movie.id) is True
    await db_session.flush()
    links = (await db_session.execute(
        select(ResourceWorkLink).where(ResourceWorkLink.resource_id == r.id)
    )).scalars().all()
    assert {(link.series_id, link.movie_id) for link in links} == {
        (series.id, None), (None, movie.id),
    }


# ===========================================================================
# _match_collection_members
# ===========================================================================


async def test_match_collection_members_no_collections(db_session):
    ch = await _channel(db_session)
    r = await _resource(db_session, ch.id)
    assert await cwb._match_collection_members(
        db_session, r, "X", None, "tv", None
    ) is None


async def test_match_collection_members_exact_and_widening(db_session):
    ch = await _channel(db_session)
    coll = WorkCollection(
        id=_uuid(), title_cn="头文字D", external_source="series_group",
        external_id=_uuid(),
    )
    s1 = TVSeries(
        id=_uuid(), title_cn="头文字D First Stage", season_number=1,
        collection_id=coll.id,
    )
    s2 = TVSeries(
        id=_uuid(), title_cn="头文字D Second Stage", season_number=2,
        collection_id=coll.id,
    )
    db_session.add_all([coll, s1, s2])
    await db_session.flush()
    r = await _resource(db_session, ch.id, collection_id=coll.id)

    # Tier 1: exact member title → title evidence.
    assert await cwb._match_collection_members(
        db_session, r, "头文字D First Stage", 1, "tv", None
    ) == ("series", s1, True)
    # Tier 2: hint == collection title → season-only widening.
    assert await cwb._match_collection_members(
        db_session, r, "头文字D", 2, "tv", None
    ) == ("series", s2, False)


async def test_match_collection_members_marker_tier_success(db_session):
    ch = await _channel(db_session)
    coll = WorkCollection(
        id=_uuid(), title_cn="头文字D Initial D", external_source="franchise_pack",
        external_id=None,
    )
    s4 = TVSeries(
        id=_uuid(), title_cn="头文字D Fourth Stage", season_number=4,
        collection_id=coll.id,
    )
    s5 = TVSeries(
        id=_uuid(), title_cn="头文字D Fifth Stage", season_number=5,
        collection_id=coll.id,
    )
    db_session.add_all([coll, s4, s5])
    await db_session.flush()
    r = await _resource(db_session, ch.id, collection_id=coll.id)

    assert await cwb._match_collection_members(
        db_session, r, "Initial D Fifth Stage", 5, "tv", 5
    ) == ("series", s5, True)


async def test_match_collection_members_marker_tier_qualifier_reject(db_session):
    ch = await _channel(db_session)
    coll = WorkCollection(
        id=_uuid(), title_cn="头文字D Initial D", external_source="franchise_pack",
        external_id=None,
    )
    squatter = TVSeries(
        id=_uuid(), title_cn="Initial D Battle Stage", season_number=1,
        collection_id=coll.id,
    )
    db_session.add_all([coll, squatter])
    await db_session.flush()
    r = await _resource(db_session, ch.id, collection_id=coll.id)

    assert await cwb._match_collection_members(
        db_session, r, "Initial D First Stage", 1, "tv", 1
    ) is None


async def test_match_collection_members_marker_tier_season_mismatch(db_session):
    ch = await _channel(db_session)
    coll = WorkCollection(
        id=_uuid(), title_cn="头文字D Initial D", external_source="franchise_pack",
        external_id=None,
    )
    s4 = TVSeries(
        id=_uuid(), title_cn="头文字D Fourth Stage", season_number=4,
        collection_id=coll.id,
    )
    db_session.add_all([coll, s4])
    await db_session.flush()
    r = await _resource(db_session, ch.id, collection_id=coll.id)

    # Qualifier passes but the marker season has no member → skipped, not guessed.
    assert await cwb._match_collection_members(
        db_session, r, "Initial D Fifth Stage", 5, "tv", 5
    ) is None


async def test_match_collection_members_marker_tier_name_miss(db_session):
    ch = await _channel(db_session)
    coll = WorkCollection(
        id=_uuid(), title_cn="Totally Other IP", external_source="franchise_pack",
        external_id=None,
    )
    s5 = TVSeries(
        id=_uuid(), title_cn="头文字D Fifth Stage", season_number=5,
        collection_id=coll.id,
    )
    db_session.add_all([coll, s5])
    await db_session.flush()
    r = await _resource(db_session, ch.id, collection_id=coll.id)

    assert await cwb._match_collection_members(
        db_session, r, "Initial D Fifth Stage", 5, "tv", 5
    ) is None


async def test_match_collection_members_marker_tier_short_base(db_session):
    ch = await _channel(db_session)
    coll = WorkCollection(
        id=_uuid(), title_cn="Other Show", external_source="franchise_pack",
        external_id=None,
    )
    work = TVSeries(
        id=_uuid(), title_cn="Other Show", season_number=5, collection_id=coll.id,
    )
    db_session.add_all([coll, work])
    await db_session.flush()
    r = await _resource(db_session, ch.id, collection_id=coll.id)

    # "S5" strips back to "S5" (fallback) — base too short for the marker tier.
    assert await cwb._match_collection_members(
        db_session, r, "S5", 5, "tv", 5
    ) is None


async def test_match_collection_members_marker_tier_not_parked(db_session):
    ch = await _channel(db_session)
    coll = WorkCollection(
        id=_uuid(), title_cn="头文字D Initial D", external_source="franchise_pack",
        external_id=None,
    )
    s5 = TVSeries(
        id=_uuid(), title_cn="头文字D Fifth Stage", season_number=5,
        collection_id=coll.id,
    )
    db_session.add_all([coll, s5])
    await db_session.flush()
    # Associated through the work link, but the resource is not parked on the
    # collection itself → the marker tier deliberately refuses.
    r = await _resource(db_session, ch.id, series_id=s5.id)
    db_session.add(ResourceWorkLink(
        resource_id=r.id, series_id=s5.id, source="auto",
    ))
    await db_session.flush()

    assert await cwb._match_collection_members(
        db_session, r, "Initial D Fifth Stage", 5, "tv", 5
    ) is None


# ===========================================================================
# _match_local_fts
# ===========================================================================


async def test_match_local_fts_series_hit(db_session, monkeypatch):
    work = TVSeries(
        id=_uuid(), title_cn="头文字D Fifth Stage",
        title_en="Initial D Fifth Stage", season_number=5,
    )
    db_session.add(work)
    await db_session.flush()
    _patch_fts(monkeypatch, series_ids=[work.id])

    assert await cwb._match_local_fts(
        db_session, "Initial D Fifth Stage", 5, "tv"
    ) == ("series", work, True)


async def test_match_local_fts_fallback_and_guards(db_session, monkeypatch):
    movie = Movie(id=_uuid(), title_cn="Initial D Fifth Stage")
    unrelated = TVSeries(
        id=_uuid(), title_cn="Completely Different Show", season_number=1,
    )
    sequel = TVSeries(
        id=_uuid(), title_cn="Initial D Battle Stage 2", season_number=1,
    )
    db_session.add_all([movie, unrelated, sequel])
    await db_session.flush()
    # Empty FTS → full-table scan; the guards reject form mismatch, base
    # mismatch and sequel ambiguity respectively.
    _patch_fts(monkeypatch)

    assert await cwb._match_local_fts(
        db_session, "Initial D Battle Stage", None, "tv"
    ) is None


async def test_match_local_fts_movie_hit(db_session, monkeypatch):
    movie = Movie(
        id=_uuid(), title_cn="Initial D Battle Stage",
        title_en="Initial D Battle Stage",
    )
    db_session.add(movie)
    await db_session.flush()
    _patch_fts(monkeypatch, movie_ids=[movie.id])

    assert await cwb._match_local_fts(
        db_session, "Initial D Battle Stage", None, "unknown"
    ) == ("movie", movie, True)


async def test_match_local_fts_ambiguous_pick(db_session, monkeypatch):
    w1 = TVSeries(id=_uuid(), title_cn="Initial D Battle Stage", season_number=1)
    w2 = TVSeries(id=_uuid(), title_cn="Initial D Battle Stage", season_number=1)
    db_session.add_all([w1, w2])
    await db_session.flush()
    _patch_fts(monkeypatch, series_ids=[w1.id, w2.id])

    # Two same-title works without a season hint → ambiguous, skipped.
    assert await cwb._match_local_fts(
        db_session, "Initial D Battle Stage", None, "tv"
    ) is None


async def test_match_local_fts_containment_only(db_session, monkeypatch):
    work = TVSeries(
        id=_uuid(), title_cn="Initial D Battle Stage OVA",
        title_en="Initial D Battle Stage OVA", season_number=1,
    )
    db_session.add(work)
    await db_session.flush()
    _patch_fts(monkeypatch, series_ids=[work.id])

    # Containment-only hit (not exact) still binds; season selects the work.
    assert await cwb._match_local_fts(
        db_session, "Initial D Battle Stage", 1, "tv"
    ) == ("series", work, True)


# ===========================================================================
# _match_external
# ===========================================================================


async def test_match_external_agent_error(db_session, monkeypatch):
    async def _boom(title, source):
        raise RuntimeError("llm down")

    _patch_meta(monkeypatch, _boom)
    assert await cwb._match_external(db_session, "X", None, None, "tv") is None


async def test_match_external_rejection_paths(db_session, monkeypatch):
    async def _handler(title, source):
        if title == "notfound":
            return SimpleNamespace(found=False, ambiguous=False, matched_entity=None)
        if title == "ambiguous":
            return SimpleNamespace(
                found=True, ambiguous=True, matched_entity={"title_cn": "X"}
            )
        if title == "noentity":
            return SimpleNamespace(
                found=True, ambiguous=False, matched_entity=None
            )
        return None

    _patch_meta(monkeypatch, _handler)
    for title in ("notfound", "ambiguous", "noentity", "nothing"):
        assert await cwb._match_external(
            db_session, title, None, None, "tv"
        ) is None


async def test_match_external_form_guard(db_session, monkeypatch):
    async def _handler(title, source):
        return SimpleNamespace(
            found=True, ambiguous=False, content_type="movie",
            matched_entity={"title_cn": "X"},
        )

    _patch_meta(monkeypatch, _handler)
    assert await cwb._match_external(db_session, "X", None, None, "tv") is None


async def test_match_external_entity_without_title(db_session, monkeypatch):
    async def _handler(title, source):
        return SimpleNamespace(
            found=True, ambiguous=False, content_type="tv",
            matched_entity={"external_id": "x"},
        )

    _patch_meta(monkeypatch, _handler)
    assert await cwb._match_external(db_session, "X", None, None, "tv") is None


async def test_match_external_local_reuse(db_session, monkeypatch):
    import app.services.metadata_service as ms

    movie = Movie(id=_uuid(), title_cn="X")
    series = TVSeries(id=_uuid(), title_cn="Y", season_number=1)
    db_session.add_all([movie, series])
    await db_session.flush()

    async def _handler(title, source):
        kind = "movie" if title == "m" else "tv"
        return SimpleNamespace(
            found=True, ambiguous=False, content_type=kind,
            matched_entity={"title_cn": "X", "_kind": kind},
        )

    async def _find(db, entity, content_type):
        return movie if entity.get("_kind") == "movie" else series

    _patch_meta(monkeypatch, _handler)
    monkeypatch.setattr(ms, "find_local_work_for_entity", _find)

    assert await cwb._match_external(
        db_session, "m", None, None, "movie"
    ) == ("movie", movie, True)
    assert await cwb._match_external(
        db_session, "t", None, None, "tv"
    ) == ("series", series, True)


async def test_match_external_local_reuse_error_then_movie_upsert(
    db_session, monkeypatch
):
    import app.services.metadata_service as ms

    movie = Movie(id=_uuid(), title_cn="X")
    db_session.add(movie)
    await db_session.flush()

    async def _handler(title, source):
        return SimpleNamespace(
            found=True, ambiguous=False, content_type="movie",
            matched_entity={"title_cn": "X", "_platform": "tmdb"},
        )

    async def _find(db, entity, content_type):
        raise RuntimeError("lookup blew up")

    async def _upsert(db, entity):
        return movie

    _patch_meta(monkeypatch, _handler)
    monkeypatch.setattr(ms, "find_local_work_for_entity", _find)
    monkeypatch.setattr(ms, "create_or_update_movie_from_external", _upsert)

    assert await cwb._match_external(
        db_session, "X", None, None, "movie"
    ) == ("movie", movie, True)


async def test_match_external_movie_upsert_returns_none(db_session, monkeypatch):
    import app.services.metadata_service as ms

    async def _handler(title, source):
        return SimpleNamespace(
            found=True, ambiguous=False, content_type="movie",
            matched_entity={"title_cn": "X"},
        )

    async def _find(db, entity, content_type):
        return None

    async def _upsert(db, entity):
        return None

    _patch_meta(monkeypatch, _handler)
    monkeypatch.setattr(ms, "find_local_work_for_entity", _find)
    monkeypatch.setattr(ms, "create_or_update_movie_from_external", _upsert)

    assert await cwb._match_external(
        db_session, "X", None, None, "movie"
    ) is None


async def test_match_external_tv_upsert_specials(db_session, monkeypatch):
    import app.services.bangumi_relations as br
    import app.services.metadata_service as ms

    series = TVSeries(id=_uuid(), title_cn="X", season_number=0)
    captured: dict = {}

    async def _handler(title, source):
        return SimpleNamespace(
            found=True, ambiguous=False, content_type="tv",
            matched_entity={"title_cn": "X", "_platform": "OVA"},
        )

    async def _find(db, entity, content_type):
        return None

    async def _upsert(db, entity, season_hint=None):
        captured["season_hint"] = season_hint
        return series

    _patch_meta(monkeypatch, _handler)
    monkeypatch.setattr(ms, "find_local_work_for_entity", _find)
    monkeypatch.setattr(ms, "create_or_update_series_from_external", _upsert)
    monkeypatch.setattr(br, "classify_work_shape", lambda relation, platform: "specials")

    assert await cwb._match_external(
        db_session, "X", 3, None, "tv"
    ) == ("series", series, True)
    # OVA/番外 shape forces the season-0 slot.
    assert captured["season_hint"] == 0


async def test_match_external_tv_upsert_none(db_session, monkeypatch):
    import app.services.bangumi_relations as br
    import app.services.metadata_service as ms

    captured: dict = {}

    async def _handler(title, source):
        return SimpleNamespace(
            found=True, ambiguous=False, content_type="tv",
            matched_entity={"title_cn": "X", "_platform": "TV"},
        )

    async def _find(db, entity, content_type):
        return None

    async def _upsert(db, entity, season_hint=None):
        captured["season_hint"] = season_hint
        return None

    _patch_meta(monkeypatch, _handler)
    monkeypatch.setattr(ms, "find_local_work_for_entity", _find)
    monkeypatch.setattr(ms, "create_or_update_series_from_external", _upsert)
    monkeypatch.setattr(br, "classify_work_shape", lambda relation, platform: None)

    assert await cwb._match_external(
        db_session, "X", 2, None, "tv"
    ) is None
    # The season hint survives for a non-specials shape.
    assert captured["season_hint"] == 2


async def test_match_external_upsert_error(db_session, monkeypatch):
    import app.services.metadata_service as ms

    async def _handler(title, source):
        return SimpleNamespace(
            found=True, ambiguous=False, content_type="movie",
            matched_entity={"title_cn": "X"},
        )

    async def _find(db, entity, content_type):
        return None

    async def _upsert(db, entity):
        raise RuntimeError("upsert blew up")

    _patch_meta(monkeypatch, _handler)
    monkeypatch.setattr(ms, "find_local_work_for_entity", _find)
    monkeypatch.setattr(ms, "create_or_update_movie_from_external", _upsert)

    assert await cwb._match_external(
        db_session, "X", None, None, "movie"
    ) is None


async def test_match_external_unsupported_content_type(db_session, monkeypatch):
    import app.services.metadata_service as ms

    async def _handler(title, source):
        return SimpleNamespace(
            found=True, ambiguous=False, content_type="audio",
            matched_entity={"title_cn": "X"},
        )

    async def _find(db, entity, content_type):
        return None

    _patch_meta(monkeypatch, _handler)
    monkeypatch.setattr(ms, "find_local_work_for_entity", _find)

    assert await cwb._match_external(
        db_session, "X", None, None, "unknown"
    ) is None


# ===========================================================================
# bind_hint_clusters — no-op gates
# ===========================================================================


async def test_bind_non_batch_is_noop(db_session):
    ch = await _channel(db_session)
    r = await _resource(db_session, ch.id, is_batch=False)
    db_session.add(ResourceFileAssignment(
        resource_id=r.id, file_path="a.mkv", source="auto",
        work_title_hint="某作品",
    ))
    await db_session.flush()
    assert await cwb.bind_hint_clusters(db_session, r, None) == cwb.BindOutcome(0, 0)
    await db_session.refresh(r, ["file_assignments"])
    assert r.file_assignments[0].series_id is None


async def test_bind_refresh_error_returns_empty(db_session):
    # A transient (unattached) resource makes db.refresh raise → graceful no-op.
    r = FileResource(
        id=_uuid(), channel_id=_uuid(), guid=_uuid(), title_raw="x",
        torrent_url="https://x/p.torrent", is_batch=True,
    )
    assert await cwb.bind_hint_clusters(db_session, r, None) == cwb.BindOutcome(0, 0)


async def test_bind_empty_hints_is_noop(db_session):
    ch = await _channel(db_session)
    r = await _resource(db_session, ch.id, batch_scope="season")
    db_session.add(ResourceFileAssignment(
        resource_id=r.id, file_path="a.mkv", source="auto", work_title_hint="   ",
    ))
    await db_session.flush()
    assert await cwb.bind_hint_clusters(db_session, r, None) == cwb.BindOutcome(0, 0)


async def test_bind_cluster_without_unbound_targets(db_session, monkeypatch):
    _patch_bind_search(monkeypatch)
    ch = await _channel(db_session)
    work = TVSeries(id=_uuid(), title_cn="X", season_number=1)
    db_session.add(work)
    await db_session.flush()
    r = await _resource(db_session, ch.id, batch_scope="multi_season")
    db_session.add(ResourceFileAssignment(
        resource_id=r.id, file_path="m.mkv", source="manual",
        work_title_hint="X", series_id=work.id, season=1,
        episode_start=1, episode_end=1,
    ))
    await db_session.flush()
    assert await cwb.bind_hint_clusters(db_session, r, None) == cwb.BindOutcome(0, 0)


# ===========================================================================
# bind_hint_clusters — reconcile + resolution failure
# ===========================================================================


async def test_bind_reconcile_follows_relocated_work(db_session, monkeypatch):
    _patch_bind_search(monkeypatch)
    ch = await _channel(db_session)
    work = TVSeries(id=_uuid(), title_cn="Final Stage", season_number=6)
    db_session.add(work)
    await db_session.flush()
    r = await _resource(db_session, ch.id, batch_scope="multi_season")
    db_session.add_all([
        # Auto row bound on title evidence: follows the relocated work.
        ResourceFileAssignment(
            resource_id=r.id, file_path="a.mkv", source="auto",
            work_title_hint="Final Stage", series_id=work.id, season=1,
            episode_start=1, episode_end=1,
        ),
        # Manual provenance never follows a relocation.
        ResourceFileAssignment(
            resource_id=r.id, file_path="m.mkv", source="manual",
            work_title_hint="Final Stage", series_id=work.id, season=1,
            episode_start=2, episode_end=2,
        ),
    ])
    await db_session.flush()

    out = await cwb.bind_hint_clusters(db_session, r, None)
    assert out.bound == 0
    assert out.remapped == 1
    rows = {row.file_path: row for row in r.file_assignments}
    assert rows["a.mkv"].season == 6
    assert rows["m.mkv"].season == 1


async def test_bind_resolution_exception_skips_cluster(db_session, monkeypatch):
    _patch_bind_search(monkeypatch)
    ch = await _channel(db_session)
    r = await _resource(db_session, ch.id, batch_scope="multi_season")
    db_session.add(ResourceFileAssignment(
        resource_id=r.id, file_path="a.mkv", source="auto",
        work_title_hint="X", season=1, episode_start=1, episode_end=1,
    ))
    await db_session.flush()

    async def _boom(*args, **kwargs):
        raise RuntimeError("resolution blew up")

    monkeypatch.setattr(cwb, "_match_collection_members", _boom)
    out = await cwb.bind_hint_clusters(db_session, r, None)
    assert out == cwb.BindOutcome(0, 0)
    assert r.file_assignments[0].series_id is None


async def test_bind_no_resolution_keeps_unbound(db_session, monkeypatch):
    _patch_bind_search(monkeypatch)
    ch = await _channel(db_session)
    r = await _resource(db_session, ch.id, batch_scope="franchise")
    db_session.add(ResourceFileAssignment(
        resource_id=r.id, file_path="Ghost/e01.mkv", source="auto",
        work_title_hint="不存在的作品 Ghost", season=1,
        episode_start=1, episode_end=1,
    ))
    await db_session.flush()

    out = await cwb.bind_hint_clusters(db_session, r, None)
    assert out == cwb.BindOutcome(0, 0)
    row = r.file_assignments[0]
    assert row.series_id is None and row.movie_id is None


# ===========================================================================
# bind_hint_clusters — full binding
# ===========================================================================


async def test_bind_series_clusters_end_to_end(db_session, monkeypatch):
    _patch_bind_search(monkeypatch)
    ch = await _channel(db_session)
    coll = WorkCollection(
        id=_uuid(), title_cn="头文字D", external_source="series_group",
        external_id=_uuid(),
    )
    s1 = TVSeries(
        id=_uuid(), title_cn="头文字D First Stage", season_number=1,
        collection_id=coll.id,
    )
    s4 = TVSeries(
        id=_uuid(), title_cn="头文字D Fourth Stage", season_number=4,
        collection_id=coll.id,
    )
    db_session.add_all([coll, s1, s4])
    await db_session.flush()
    r = await _resource(
        db_session, ch.id, batch_scope="multi_season",
        collection_id=coll.id, batch_seasons=[1],
    )
    db_session.add_all([
        # Title evidence: pack-internal season 3 remaps onto the work's s4.
        ResourceFileAssignment(
            resource_id=r.id, file_path="S3/e01.mkv", source="auto",
            work_title_hint="头文字D Fourth Stage", season=3,
            episode_start=1, episode_end=2,
        ),
        # No parsed season → filled from the work's own season.
        ResourceFileAssignment(
            resource_id=r.id, file_path="S1/e01.mkv", source="auto",
            work_title_hint="头文字D First Stage", season=None,
            episode_start=1, episode_end=1,
        ),
    ])
    await db_session.flush()

    out = await cwb.bind_hint_clusters(db_session, r, None)
    assert out.bound == 2
    assert out.remapped == 1
    rows = {row.file_path: row for row in r.file_assignments}
    assert rows["S3/e01.mkv"].series_id == s4.id
    assert rows["S3/e01.mkv"].season == 4
    assert rows["S1/e01.mkv"].series_id == s1.id
    assert rows["S1/e01.mkv"].season == 1
    assert r.season_ranges == [
        {"season": 1, "episode_start": 1, "episode_end": 1},
        {"season": 4, "episode_start": 1, "episode_end": 2},
    ]
    links = (await db_session.execute(
        select(ResourceWorkLink).where(ResourceWorkLink.resource_id == r.id)
    )).scalars().all()
    assert {link.series_id for link in links} == {s1.id, s4.id}
    assert all(link.source == "auto" for link in links)
    assert r.batch_seasons == [1, 4]
    assert r.collection_id == coll.id


async def test_bind_season_only_selection_never_remaps(db_session, monkeypatch):
    _patch_bind_search(monkeypatch)
    ch = await _channel(db_session)
    coll = WorkCollection(
        id=_uuid(), title_cn="合集B", external_source="series_group",
        external_id=_uuid(),
    )
    work = TVSeries(
        id=_uuid(), title_cn="剧集B", season_number=1, collection_id=coll.id,
    )
    db_session.add_all([coll, work])
    await db_session.flush()
    r = await _resource(
        db_session, ch.id, batch_scope="multi_season", collection_id=coll.id,
    )
    # Base-name widening (season-only, no title evidence): the individual row
    # whose parsed season disagrees with the selected work is left unbound.
    db_session.add_all([
        ResourceFileAssignment(
            resource_id=r.id, file_path="B/e01.mkv", source="auto",
            work_title_hint="合集B", season=1, episode_start=1, episode_end=1,
        ),
        ResourceFileAssignment(
            resource_id=r.id, file_path="B/e02.mkv", source="auto",
            work_title_hint="合集B", season=3, episode_start=2, episode_end=2,
        ),
    ])
    await db_session.flush()

    out = await cwb.bind_hint_clusters(db_session, r, None)
    rows = {row.file_path: row for row in r.file_assignments}
    assert rows["B/e01.mkv"].series_id == work.id
    assert rows["B/e02.mkv"].series_id is None
    assert rows["B/e02.mkv"].season == 3
    assert out.bound == 1
    assert out.remapped == 0


async def test_bind_movie_cluster_clears_season(db_session, monkeypatch):
    _patch_bind_search(monkeypatch)
    ch = await _channel(db_session)
    coll = WorkCollection(
        id=_uuid(), title_cn="头文字D", external_source="series_group",
        external_id=_uuid(),
    )
    movie = Movie(
        id=_uuid(), title_cn="Initial D Third Stage",
        title_en="Initial D Third Stage", collection_id=coll.id,
    )
    db_session.add_all([coll, movie])
    await db_session.flush()
    r = await _resource(
        db_session, ch.id, batch_scope="franchise", collection_id=coll.id,
    )
    db_session.add(ResourceFileAssignment(
        resource_id=r.id, file_path="[2001] [Season 3] Initial D Third Stage/m.mkv",
        source="auto", work_title_hint="Initial D Third Stage", season=3,
    ))
    await db_session.flush()

    out = await cwb.bind_hint_clusters(db_session, r, None)
    assert out.bound == 1
    assert out.remapped == 0
    row = r.file_assignments[0]
    assert row.movie_id == movie.id
    assert row.series_id is None
    assert row.season is None
    # Franchise packs keep their own collection semantics.
    assert r.collection_id == coll.id


async def test_bind_movie_in_season_scope_skips_batch_seasons(db_session, monkeypatch):
    """A movie cluster bound in a season-flavored scope never contributes a
    season to ``batch_seasons`` (movies are seasonless)."""
    _patch_bind_search(monkeypatch)
    ch = await _channel(db_session)
    coll = WorkCollection(
        id=_uuid(), title_cn="C", external_source="series_group", external_id=_uuid(),
    )
    movie = Movie(id=_uuid(), title_cn="The Movie", collection_id=coll.id)
    db_session.add_all([coll, movie])
    await db_session.flush()
    r = await _resource(
        db_session, ch.id, batch_scope="multi_season", collection_id=coll.id,
    )
    db_session.add(ResourceFileAssignment(
        resource_id=r.id, file_path="m.mkv", source="auto",
        work_title_hint="The Movie",
    ))
    await db_session.flush()

    out = await cwb.bind_hint_clusters(db_session, r, None)
    assert out.bound == 1
    assert r.file_assignments[0].movie_id == movie.id
    assert r.batch_seasons is None

