"""In-process integration coverage for app.services.batch_content_analysis.

Targets the branches the integration run misses: the LLM gate
(``llm_refinement_needed``), the listing/prompt helpers, ``analyze_listing``
and ``analyze_listing_stream`` (success, malformed payload, transport
failure — the OpenAI client is stubbed at ``openai.AsyncOpenAI``), the
deterministic write-back ``apply_auto_assignments`` (cluster hints, manual/llm
provenance, stale-row pruning), ``bind_single_work_assignments`` (binding,
season backfill, fractional-special mapping, orphan auto-link cleanup),
``sync_resource_collection`` disagreement, ``resolve_fractional_specials``
cardinality rules, ``build_candidate_works``, the ``_valid_paths`` clamp,
``refine_batch_content`` (movies upgrade, hint-only TV clusters, season-scope
gate), ``_resolve_movie`` / ``_bind_work`` / ``_apply_hint``, and
``suggest_batch_content``.

DB-backed paths run against the per-test Turso fixture from
``tests/unit/conftest.py``; all network/LLM access is stubbed.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

import app.services.batch_content_analysis as bca
from app.models.channel import Channel
from app.models.episode import Episode
from app.models.file_resource import FileResource
from app.models.movie import Movie
from app.models.resource_file_assignment import ResourceFileAssignment
from app.models.resource_work_link import ResourceWorkLink
from app.models.series import TVSeries
from app.models.work_collection import WorkCollection
from app.services.torrent_inspect import TorrentReport, WorkCluster

MB = 1024 * 1024


def _uuid() -> str:
    return str(uuid.uuid4())


async def _make_resource(db_session, **over):
    """Persist a minimal Channel + FileResource; relationships refreshed."""
    channel = Channel(
        id=_uuid(),
        name="Batch Coverage Channel",
        type="rss_feed",
        url="https://example.com/rss",
        fetch_interval=1800,
        status="active",
        field_mapping={"list_locator": {"source": "entries"}},
        metadata_agent_enabled=False,
    )
    base = dict(
        id=_uuid(),
        channel_id=channel.id,
        guid=_uuid(),
        title_raw="[Group] Pack [1080p]",
        torrent_url="https://x/pack.torrent",
    )
    base.update(over)
    resource = FileResource(**base)
    db_session.add_all([channel, resource])
    await db_session.commit()
    await db_session.refresh(resource, ["file_assignments"])
    return resource


async def _links(db_session, resource_id):
    return (await db_session.execute(
        select(ResourceWorkLink).where(ResourceWorkLink.resource_id == resource_id)
    )).scalars().all()


def _no_llm_key(monkeypatch):
    monkeypatch.setattr("app.services.runtime_config._overrides", {"llm_api_key": ""})


def _llm_key(monkeypatch):
    monkeypatch.setattr("app.services.runtime_config._overrides", {
        "llm_api_key": "sk-test",
        "llm_model": "test-model",
        "llm_base_url": "https://llm.local/v1",
        "llm_enable_thinking": "false",
    })


class _FakeStream:
    """Async iterator of OpenAI-style streaming chunks."""

    def __init__(self, chunks):
        self._chunks = list(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._chunks:
            raise StopAsyncIteration
        content = self._chunks.pop(0)
        return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=content))])


def _install_fake_openai(monkeypatch, *, content=None, chunks=None, exc=None):
    """Stub ``openai.AsyncOpenAI``; returns the list of captured create() kwargs."""

    captured = []

    class _FakeCompletions:
        async def create(self, **kwargs):
            captured.append(kwargs)
            if exc is not None:
                raise exc
            if kwargs.get("stream"):
                return _FakeStream(chunks or [])
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
            )

    class _FakeOpenAI:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=_FakeCompletions())

    monkeypatch.setattr("openai.AsyncOpenAI", _FakeOpenAI)
    return captured


def _franchise_report():
    return TorrentReport(
        scope="franchise",
        is_batch=True,
        clusters=[
            WorkCluster(title="MovieA", files=["MovieA/a.mkv"]),
            WorkCluster(title="MovieB", files=["MovieB/b.mkv"]),
        ],
        file_parses=[
            {"path": "MovieA/a.mkv", "size": 500 * MB, "season": None, "episode": None},
            {"path": "MovieB/b.mkv", "size": 600 * MB, "season": None, "episode": None},
        ],
        video_file_count=2,
    )


# =============================================================================
# llm_refinement_needed — gating
# =============================================================================

def test_llm_refinement_needed_gate(monkeypatch):
    report = TorrentReport(
        scope="season", is_batch=True, video_file_count=4, unparsed_ratio=0.75
    )
    _no_llm_key(monkeypatch)
    # Without an API key nothing ever reaches the LLM, even franchise packs.
    assert bca.llm_refinement_needed(report, "franchise") is False

    _llm_key(monkeypatch)
    assert bca.llm_refinement_needed(report, "franchise") is True
    # High unparsed ratio on a batch qualifies even outside franchise scope.
    assert bca.llm_refinement_needed(report, "season") is True
    low = TorrentReport(
        scope="season", is_batch=True, video_file_count=4, unparsed_ratio=0.25
    )
    assert bca.llm_refinement_needed(low, "season") is False
    single = TorrentReport(
        scope="single", is_batch=False, video_file_count=1, unparsed_ratio=1.0
    )
    assert bca.llm_refinement_needed(single, None) is False


# =============================================================================
# Prompt/parsing helpers
# =============================================================================

def test_build_listing_text_formats_and_caps(monkeypatch):
    files = [{"name": "a.mkv", "size": 500 * MB}, {"name": "b.mkv"}]
    assert bca._build_listing_text(files) == "a.mkv (500MB)\nb.mkv (0MB)"
    monkeypatch.setattr(bca, "_MAX_LISTING_ENTRIES", 1)
    assert bca._build_listing_text(files) == "a.mkv (500MB)"


def test_parse_llm_json_tolerates_fences_and_prose():
    payload = {"scope": "movies", "works": []}
    assert bca._parse_llm_json(json.dumps(payload)) == payload
    fenced = "```json\n" + json.dumps(payload) + "\n```"
    assert bca._parse_llm_json(fenced) == payload
    prose = "Here is the analysis:\n" + json.dumps(payload) + "\nHope this helps."
    assert bca._parse_llm_json(prose) == payload
    with pytest.raises(json.JSONDecodeError):
        bca._parse_llm_json("no json here at all")


# =============================================================================
# apply_auto_assignments — deterministic write-back
# =============================================================================

async def test_apply_auto_assignments_hints_provenance_and_pruning(db_session):
    resource = await _make_resource(db_session, is_batch=True, batch_scope="franchise")
    manual = ResourceFileAssignment(
        resource_id=resource.id, file_path="MovieA/a.mkv", source="manual",
        work_title_hint="人工标注", season=9, episode_start=9, episode_end=9,
    )
    stale = ResourceFileAssignment(
        resource_id=resource.id, file_path="gone.mkv", source="auto",
    )
    db_session.add_all([manual, stale])
    await db_session.commit()
    await db_session.refresh(resource, ["file_assignments"])

    report = TorrentReport(
        scope="franchise",
        is_batch=True,
        clusters=[WorkCluster(title="MovieA", files=["MovieA/a.mkv", "MovieA/b.mkv"])],
        file_parses=[
            {"path": "MovieA/a.mkv", "size": 100, "season": 1, "episode": 1},
            {"path": "MovieA/b.mkv", "size": 200, "season": 1, "episode": 2},
        ],
    )
    bca.apply_auto_assignments(resource, report)
    await db_session.flush()

    rows = {a.file_path: a for a in resource.file_assignments}
    # The stale auto row whose path vanished from the listing is pruned.
    assert set(rows) == {"MovieA/a.mkv", "MovieA/b.mkv"}
    # Manual provenance wins: the row is left completely untouched.
    assert rows["MovieA/a.mkv"].source == "manual"
    assert (rows["MovieA/a.mkv"].season, rows["MovieA/a.mkv"].episode_start) == (9, 9)
    assert rows["MovieA/a.mkv"].work_title_hint == "人工标注"
    # The new path gets an auto row with cluster hint + placement.
    new = rows["MovieA/b.mkv"]
    assert new.source == "auto"
    assert new.work_title_hint == "MovieA"
    assert (new.season, new.episode_start, new.episode_end) == (1, 2, 2)
    assert new.file_size == 200
    # The pruned row is really gone from the database.
    leftover = (await db_session.execute(
        select(ResourceFileAssignment).where(ResourceFileAssignment.file_path == "gone.mkv")
    )).scalars().all()
    assert leftover == []


# =============================================================================
# analyze_listing — one-shot LLM classification
# =============================================================================

async def test_analyze_listing_requires_api_key(monkeypatch):
    _no_llm_key(monkeypatch)
    out = await bca.analyze_listing("Title", [{"name": "a.mkv", "size": 1}], [])
    assert out is None


async def test_analyze_listing_success_includes_anchors_and_candidates(monkeypatch):
    _llm_key(monkeypatch)
    payload = {
        "scope": "movies",
        "works": [{"title": "MovieA", "content_type": "movie", "files": []}],
    }
    captured = _install_fake_openai(monkeypatch, content=json.dumps(payload))
    out = await bca.analyze_listing(
        "Movies Pack",
        [{"name": "MovieA/a.mkv", "size": 500 * MB}],
        ["MovieA"],
        candidate_works=[{
            "candidate_key": "movie:m1", "work_type": "movie",
            "work_id": "m1", "titles": ["MovieA"],
        }],
    )
    assert out == payload
    request = captured[0]
    assert request["model"] == "test-model"
    user_msg = request["messages"][1]["content"]
    assert "Release title: Movies Pack" in user_msg
    assert "MovieA/a.mkv (500MB)" in user_msg
    assert "- MovieA" in user_msg  # deterministic cluster anchor hint
    assert "movie:m1" in user_msg  # candidate identities JSON


async def test_analyze_listing_failures_degrade_to_none(monkeypatch):
    _llm_key(monkeypatch)
    files = [{"name": "a.mkv", "size": 1}]
    # Transport / API error.
    _install_fake_openai(monkeypatch, exc=RuntimeError("llm down"))
    assert await bca.analyze_listing("T", files, []) is None
    # Unparseable reply body.
    _install_fake_openai(monkeypatch, content="not json at all")
    assert await bca.analyze_listing("T", files, []) is None
    # Valid JSON but not a dict.
    _install_fake_openai(monkeypatch, content=json.dumps(["not", "a", "dict"]))
    assert await bca.analyze_listing("T", files, []) is None
    # Dict without a works list.
    _install_fake_openai(monkeypatch, content=json.dumps({"works": "oops"}))
    assert await bca.analyze_listing("T", files, []) is None


# =============================================================================
# analyze_listing_stream — streaming variant
# =============================================================================

async def test_analyze_listing_stream_requires_api_key(monkeypatch):
    _no_llm_key(monkeypatch)
    events = [e async for e in bca.analyze_listing_stream("T", [{"name": "a.mkv"}], [])]
    assert events == [("result", None)]


async def test_analyze_listing_stream_yields_deltas_then_result(monkeypatch):
    _llm_key(monkeypatch)
    payload = {"scope": "movies", "works": []}
    text = json.dumps(payload)
    # The empty chunk exercises the falsy-delta skip.
    _install_fake_openai(monkeypatch, chunks=[text[:10], text[10:], ""])
    events = [
        e async for e in bca.analyze_listing_stream(
            "T", [{"name": "a.mkv", "size": 1}], ["Anchor"]
        )
    ]
    assert events[:-1] == [("delta", text[:10]), ("delta", text[10:])]
    assert events[-1] == ("result", payload)


async def test_analyze_listing_stream_failures_are_visible_and_nonfatal(monkeypatch):
    _llm_key(monkeypatch)
    _install_fake_openai(monkeypatch, exc=RuntimeError("boom"))
    events = [e async for e in bca.analyze_listing_stream("T", [{"name": "a.mkv"}], [])]
    assert events == [("error", "boom"), ("result", None)]
    # Well-formed JSON without a works list degrades the same way: raw deltas
    # were already streamed, then the parse failure surfaces as error + None.
    chunk = json.dumps({"scope": "movies"})
    _install_fake_openai(monkeypatch, chunks=[chunk])
    events = [e async for e in bca.analyze_listing_stream("T", [{"name": "a.mkv"}], [])]
    assert events[0] == ("delta", chunk)
    assert events[1][0] == "error"
    assert "works list" in events[1][1]
    assert events[2] == ("result", None)


# =============================================================================
# bind_single_work_assignments
# =============================================================================

async def test_bind_single_work_without_work_fk_is_noop(db_session):
    resource = SimpleNamespace(series_id=None, movie_id=None)
    assert await bca.bind_single_work_assignments(db_session, resource) == 0


async def test_bind_single_work_refresh_failure_returns_zero(db_session, monkeypatch):
    resource = SimpleNamespace(series_id="s-1", movie_id=None)

    async def _boom(obj, attribute_names=None):
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(db_session, "refresh", _boom)
    assert await bca.bind_single_work_assignments(db_session, resource) == 0


async def test_bind_single_work_binds_rows_backfills_season_and_links(db_session):
    collection = WorkCollection(
        id=_uuid(), title_cn="合集", external_source="series_group", external_id=_uuid(),
    )
    work = TVSeries(
        id=_uuid(), title_cn="第二季", season_number=2, collection_id=collection.id,
    )
    db_session.add_all([collection, work])
    await db_session.commit()
    resource = await _make_resource(
        db_session, is_batch=True, batch_scope="season", season=2, series_id=work.id,
    )
    db_session.add_all([
        ResourceFileAssignment(
            resource_id=resource.id, file_path="S2/e01.mkv", source="auto",
            episode_start=1, episode_end=1,
        ),
        ResourceFileAssignment(
            resource_id=resource.id, file_path="S2/e02.mkv", source="auto",
            episode_start=2, episode_end=2,
        ),
        ResourceFileAssignment(
            resource_id=resource.id, file_path="S2/e03.mkv", source="manual",
            episode_start=3, episode_end=3,
        ),
    ])
    await db_session.commit()

    changed = await bca.bind_single_work_assignments(db_session, resource)
    # Only the two auto rows are bound; manual provenance is never touched.
    assert changed == 2
    rows = {r.file_path: r for r in resource.file_assignments}
    assert rows["S2/e01.mkv"].series_id == work.id
    assert rows["S2/e02.mkv"].series_id == work.id
    # Placement backfill: rows without a season inherit the resource's.
    assert rows["S2/e01.mkv"].season == 2
    assert rows["S2/e02.mkv"].season == 2
    assert rows["S2/e03.mkv"].series_id is None
    assert rows["S2/e03.mkv"].season is None
    # The work link is created once for the FK-identified work.
    links = await _links(db_session, resource.id)
    assert [(link.series_id, link.source) for link in links] == [(work.id, "auto")]
    # Derived season_ranges recomputed; collection identity settled.
    assert resource.season_ranges == [{"season": 2, "episode_start": 1, "episode_end": 2}]
    assert resource.collection_id == collection.id


async def test_bind_single_work_maps_fractional_specials(db_session):
    work = TVSeries(id=_uuid(), title_cn=" specials 作品", season_number=1)
    db_session.add(work)
    await db_session.commit()
    db_session.add_all([
        Episode(series_id=work.id, season=0, episode=1, title="SP1"),
        Episode(series_id=work.id, season=0, episode=2, title="SP2"),
    ])
    resource = await _make_resource(
        db_session, is_batch=True, batch_scope="season", season=None, series_id=work.id,
    )
    db_session.add_all([
        ResourceFileAssignment(
            resource_id=resource.id, file_path="Show/SP/Show - 22.5 [1080p].mkv", source="auto",
        ),
        ResourceFileAssignment(
            resource_id=resource.id, file_path="Show/SP/Show - 11.5 [1080p].mkv", source="auto",
        ),
    ])
    await db_session.commit()

    changed = await bca.bind_single_work_assignments(db_session, resource)
    assert changed == 2
    rows = {r.file_path: r for r in resource.file_assignments}
    # Sorted fractional labels map onto ascending Season 0 episodes.
    sp1 = rows["Show/SP/Show - 11.5 [1080p].mkv"]
    sp2 = rows["Show/SP/Show - 22.5 [1080p].mkv"]
    assert (sp1.season, sp1.episode_start, sp1.episode_end) == (0, 1, 1)
    assert (sp2.season, sp2.episode_start, sp2.episode_end) == (0, 2, 2)
    assert resource.season_ranges == [{"season": 0, "episode_start": 1, "episode_end": 2}]


async def test_bind_single_work_deletes_orphan_auto_links(db_session):
    old = TVSeries(id=_uuid(), title_cn="旧作品", season_number=1)
    work = TVSeries(id=_uuid(), title_cn="新作品", season_number=1)
    db_session.add_all([old, work])
    await db_session.commit()
    resource = await _make_resource(
        db_session, is_batch=True, batch_scope="season", season=1, series_id=work.id,
    )
    db_session.add_all([
        ResourceFileAssignment(
            resource_id=resource.id, file_path="e01.mkv", source="auto",
            episode_start=1, episode_end=1,
        ),
        # Orphan: the FK was re-pointed away from `old`.
        ResourceWorkLink(resource_id=resource.id, series_id=old.id, source="auto"),
        ResourceWorkLink(resource_id=resource.id, series_id=work.id, source="auto"),
    ])
    await db_session.commit()

    changed = await bca.bind_single_work_assignments(db_session, resource)
    assert changed == 1
    links = await _links(db_session, resource.id)
    # Orphan auto link removed; the matching link is kept and not duplicated.
    assert [(link.series_id, link.source) for link in links] == [(work.id, "auto")]


# =============================================================================
# resolve_fractional_specials
# =============================================================================

async def test_resolve_fractional_specials_fallbacks_and_ambiguity(db_session):
    work = TVSeries(id=_uuid(), title_cn="w", season_number=1)
    db_session.add(work)
    await db_session.commit()

    # No fractional labels in the listing → nothing to resolve.
    assert await bca.resolve_fractional_specials(db_session, work.id, ["a/e01.mkv"]) == {}
    # No Season 0 rows: release-order decimals become the canonical S00E01..N.
    out = await bca.resolve_fractional_specials(
        db_session, work.id, ["Show - 22.5.mkv", "Show - 11.5.mkv"]
    )
    assert out == {"Show - 11.5.mkv": 1, "Show - 22.5.mkv": 2}
    # Cardinality mismatch (1 special row vs 2 labels) is ambiguous → no guess.
    db_session.add(Episode(series_id=work.id, season=0, episode=1))
    await db_session.commit()
    out = await bca.resolve_fractional_specials(
        db_session, work.id, ["Show - 11.5.mkv", "Show - 22.5.mkv"]
    )
    assert out == {}


# =============================================================================
# sync_resource_collection — disagreement branch
# =============================================================================

async def test_sync_resource_collection_clears_on_disagreement(db_session):
    c1 = WorkCollection(
        id=_uuid(), title_cn="合集一", external_source="series_group", external_id=_uuid(),
    )
    c2 = WorkCollection(
        id=_uuid(), title_cn="合集二", external_source="series_group", external_id=_uuid(),
    )
    s1 = TVSeries(id=_uuid(), title_cn="S1", season_number=1, collection_id=c1.id)
    s2 = TVSeries(id=_uuid(), title_cn="S2", season_number=2, collection_id=c2.id)
    free = TVSeries(id=_uuid(), title_cn="游离作品", season_number=1, collection_id=None)
    db_session.add_all([c1, c2, s1, s2, free])
    await db_session.commit()

    # Works spanning two collections → resource collection cleared.
    resource = await _make_resource(
        db_session, is_batch=True, batch_scope="multi_season",
        series_id=s1.id, collection_id=c1.id,
    )
    db_session.add(ResourceWorkLink(resource_id=resource.id, series_id=s2.id, source="auto"))
    await db_session.commit()
    await bca.sync_resource_collection(db_session, resource)
    assert resource.collection_id is None

    # A null collection next to a non-null one also counts as disagreement.
    resource2 = await _make_resource(
        db_session, is_batch=True, batch_scope="season",
        series_id=s1.id, collection_id=c1.id,
    )
    db_session.add(ResourceWorkLink(resource_id=resource2.id, series_id=free.id, source="auto"))
    await db_session.commit()
    await bca.sync_resource_collection(db_session, resource2)
    assert resource2.collection_id is None


# =============================================================================
# build_candidate_works
# =============================================================================

async def test_build_candidate_works_collects_aliases_sorted(db_session):
    series = TVSeries(
        id=_uuid(), title_cn="系列", title_en="Series", original_title="系列",
        canonical_name="系列 Canonical", season_number=1,
    )
    movie = Movie(id=_uuid(), title_cn="电影", title_en="Movie")
    db_session.add_all([series, movie])
    await db_session.commit()
    resource = await _make_resource(
        db_session, is_batch=True, batch_scope="franchise", series_id=series.id,
    )
    db_session.add(ResourceWorkLink(resource_id=resource.id, movie_id=movie.id, source="llm"))
    await db_session.commit()

    candidates = await bca.build_candidate_works(db_session, resource)
    # Sorted by (work_type, work_id): movie before series.
    assert [c["candidate_key"] for c in candidates] == [
        f"movie:{movie.id}", f"series:{series.id}",
    ]
    assert candidates[0]["titles"] == ["电影", "Movie"]
    # Duplicate alias (original_title == title_cn) is deduped, order preserved.
    assert candidates[1]["titles"] == ["系列", "Series", "系列 Canonical"]
    assert candidates[1]["work_type"] == "series"
    assert candidates[1]["work_id"] == series.id

    # Mirror image: movie carried by the FK, series carried by a link.
    resource2 = await _make_resource(
        db_session, is_batch=True, batch_scope="franchise", movie_id=movie.id,
    )
    db_session.add(ResourceWorkLink(resource_id=resource2.id, series_id=series.id, source="auto"))
    await db_session.commit()
    candidates2 = await bca.build_candidate_works(db_session, resource2)
    assert [c["candidate_key"] for c in candidates2] == [
        f"movie:{movie.id}", f"series:{series.id}",
    ]


class _ExecResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _StubDb:
    """Minimal async db double for read-only candidate building."""

    def __init__(self, links=(), works=None):
        self._links = list(links)
        self._works = dict(works or {})

    async def execute(self, stmt):
        return _ExecResult(self._links)

    async def get(self, model, pk):
        return self._works.get((model, pk))


async def test_build_candidate_works_skips_missing_work_rows():
    resource = SimpleNamespace(id="r1", series_id="s-gone", movie_id=None)
    db = _StubDb(links=[SimpleNamespace(series_id=None, movie_id="m-gone")])
    # Both refs point at work rows that no longer exist → no candidates.
    assert await bca.build_candidate_works(db, resource) == []


# =============================================================================
# _valid_paths — clamping the LLM reply against the real listing
# =============================================================================

def test_valid_paths_normalizes_episode_ranges():
    known = {"a.mkv", "b.mkv", "c.mkv"}
    data = {"works": [{
        "title": "Show", "content_type": "tv",
        "files": [
            # Single-episode file: episode → inclusive start/end.
            {"path": "a.mkv", "season": 1, "episode": 3},
            # Genuine multi-episode range is kept.
            {"path": "b.mkv", "season": 1, "episode_start": 4, "episode_end": 5},
            # Inverted range is reset; non-int season dropped.
            {"path": "c.mkv", "season": "1", "episode_start": 9, "episode_end": 6},
            # Hallucinated path and non-dict entries are dropped.
            {"path": "missing.mkv", "season": 1, "episode": 1},
            "not-a-dict",
        ],
    }]}
    out = bca._valid_paths(data, known)
    assert out == [{
        "candidate_key": None, "title": "Show", "content_type": "tv",
        "files": [
            {"path": "a.mkv", "season": 1, "episode_start": 3, "episode_end": 3},
            {"path": "b.mkv", "season": 1, "episode_start": 4, "episode_end": 5},
            {"path": "c.mkv", "season": None, "episode_start": None, "episode_end": None},
        ],
    }]


def test_valid_paths_drops_invalid_entries():
    known = {"a.mkv", "b.mkv"}
    data = {"works": [
        "not-a-dict",
        # Empty title.
        {"title": "", "content_type": "tv", "files": [{"path": "a.mkv"}]},
        # Unsupported content type.
        {"title": "X", "content_type": "book", "files": [{"path": "a.mkv"}]},
        # No valid files at all → the whole work entry is dropped.
        {"title": "Y", "content_type": "tv", "files": [{"path": "nope.mkv"}]},
        # Partial range (start without end) is normalized to null.
        {"title": "Z", "content_type": "tv", "files": [{"path": "b.mkv", "episode_start": 2}]},
    ]}
    out = bca._valid_paths(data, known)
    assert out == [{
        "candidate_key": None, "title": "Z", "content_type": "tv",
        "files": [{"path": "b.mkv", "season": None, "episode_start": None, "episode_end": None}],
    }]


def test_valid_paths_candidate_key_enforcement():
    known = {"a.mkv", "b.mkv"}
    candidates = [{
        "candidate_key": "series:s1", "work_type": "series",
        "work_id": "s1", "titles": ["番剧"],
    }]
    data = {"works": [
        # Candidate identity wins: content_type forced to tv, empty title
        # falls back to the candidate's first title.
        {"candidate_key": "series:s1", "title": "", "content_type": "movie",
         "files": [{"path": "a.mkv", "episode": 1}]},
        # An invented/altered key is rejected while candidates were supplied.
        {"candidate_key": "series:fake", "title": "Fake", "content_type": "tv",
         "files": [{"path": "b.mkv"}]},
    ]}
    out = bca._valid_paths(data, known, candidates)
    assert out == [{
        "candidate_key": "series:s1", "title": "番剧", "content_type": "tv",
        "files": [{"path": "a.mkv", "season": None, "episode_start": 1, "episode_end": 1}],
    }]


# =============================================================================
# refine_batch_content — LLM refinement during fetch
# =============================================================================

async def test_refine_batch_content_empty_listing(db_session):
    resource = await _make_resource(db_session, is_batch=True, batch_scope="franchise")
    report = TorrentReport(scope="franchise", is_batch=True, file_parses=[])
    assert await bca.refine_batch_content(db_session, resource, report, None) is False


async def test_refine_batch_content_llm_failures_degrade(db_session, monkeypatch):
    resource = await _make_resource(db_session, is_batch=True, batch_scope="franchise")
    report = _franchise_report()

    async def _none(*a, **k):
        return None

    monkeypatch.setattr(bca, "analyze_listing", _none)
    assert await bca.refine_batch_content(db_session, resource, report, None) is False

    async def _garbage(*a, **k):
        # Every file path is hallucinated → nothing survives the clamp.
        return {"works": [{"title": "X", "content_type": "movie",
                           "files": [{"path": "hallucinated.mkv"}]}]}

    monkeypatch.setattr(bca, "analyze_listing", _garbage)
    assert await bca.refine_batch_content(db_session, resource, report, None) is False
    assert resource.batch_scope == "franchise"


async def test_refine_batch_content_upgrades_movie_pack_and_binds(db_session, monkeypatch):
    movie_a = Movie(id=_uuid(), title_cn="电影A")
    movie_b = Movie(id=_uuid(), title_cn="电影B")
    db_session.add_all([movie_a, movie_b])
    await db_session.commit()
    resource = await _make_resource(
        db_session, is_batch=True, batch_scope="franchise", title_raw="Movies Pack",
    )
    db_session.add_all([
        ResourceFileAssignment(resource_id=resource.id, file_path="MovieA/a.mkv", source="auto"),
        ResourceFileAssignment(resource_id=resource.id, file_path="MovieB/b.mkv", source="auto"),
    ])
    await db_session.commit()
    await db_session.refresh(resource, ["file_assignments"])
    report = _franchise_report()

    async def _fake_analyze(title, listing, anchors, candidate_works=None):
        assert title == "Movies Pack"
        return {"scope": "movies", "works": [
            {"title": "电影A", "content_type": "movie", "files": [{"path": "MovieA/a.mkv"}]},
            {"title": "电影B", "content_type": "movie", "files": [{"path": "MovieB/b.mkv"}]},
        ]}

    monkeypatch.setattr(bca, "analyze_listing", _fake_analyze)
    resolved = {"电影A": movie_a, "电影B": movie_b}

    async def _fake_resolve(db, res, channel, title):
        return resolved[title]

    monkeypatch.setattr(bca, "_resolve_movie", _fake_resolve)

    assert await bca.refine_batch_content(db_session, resource, report, None) is True
    # Pure multi-movie pack: franchise → movies scope upgrade.
    assert resource.batch_scope == "movies"
    rows = {a.file_path: a for a in resource.file_assignments}
    assert rows["MovieA/a.mkv"].movie_id == movie_a.id
    assert rows["MovieA/a.mkv"].series_id is None
    assert rows["MovieA/a.mkv"].source == "llm"
    assert rows["MovieB/b.mkv"].movie_id == movie_b.id
    assert rows["MovieB/b.mkv"].source == "llm"
    links = await _links(db_session, resource.id)
    assert sorted(link.movie_id for link in links) == sorted([movie_a.id, movie_b.id])
    assert all(link.source == "llm" for link in links)


async def test_refine_batch_content_tv_clusters_stay_hint_only(db_session, monkeypatch):
    resource = await _make_resource(db_session, is_batch=True, batch_scope="franchise")
    db_session.add(ResourceFileAssignment(
        resource_id=resource.id, file_path="ShowA/e01.mkv", source="auto",
    ))
    await db_session.commit()
    await db_session.refresh(resource, ["file_assignments"])
    report = TorrentReport(
        scope="franchise", is_batch=True,
        clusters=[WorkCluster(title="ShowA", files=["ShowA/e01.mkv"])],
        file_parses=[{"path": "ShowA/e01.mkv", "size": 100, "season": None, "episode": None}],
    )

    async def _fake_analyze(*a, **k):
        return {"scope": "franchise", "works": [{
            "title": "ShowA", "content_type": "tv",
            "files": [{"path": "ShowA/e01.mkv", "season": 1, "episode": 5}],
        }]}

    monkeypatch.setattr(bca, "analyze_listing", _fake_analyze)

    async def _resolve_boom(*a):
        raise AssertionError("TV clusters must never hit movie resolution")

    monkeypatch.setattr(bca, "_resolve_movie", _resolve_boom)

    assert await bca.refine_batch_content(db_session, resource, report, None) is False
    row = resource.file_assignments[0]
    # Hint-only: title + placement filled, no binding, provenance untouched.
    assert row.work_title_hint == "ShowA"
    assert (row.season, row.episode_start, row.episode_end) == (1, 5, 5)
    assert row.series_id is None
    assert row.source == "auto"


async def test_refine_batch_content_season_scope_never_binds_movies(db_session, monkeypatch):
    """High-unparsed season packs get hint fills only, never movie binding."""
    resource = await _make_resource(db_session, is_batch=True, batch_scope="season")
    db_session.add(ResourceFileAssignment(
        resource_id=resource.id, file_path="a.mkv", source="auto",
    ))
    await db_session.commit()
    await db_session.refresh(resource, ["file_assignments"])
    report = TorrentReport(
        scope="unknown", is_batch=True,
        file_parses=[{"path": "a.mkv", "size": 1, "season": None, "episode": None}],
    )

    async def _fake_analyze(*a, **k):
        return {"works": [{"title": "Some Movie", "content_type": "movie",
                           "files": [{"path": "a.mkv"}]}]}

    monkeypatch.setattr(bca, "analyze_listing", _fake_analyze)

    async def _resolve_boom(*a):
        raise AssertionError("season-scope packs never resolve movies")

    monkeypatch.setattr(bca, "_resolve_movie", _resolve_boom)

    assert await bca.refine_batch_content(db_session, resource, report, None) is False
    # A single work is not a multi-work pack: no movies upgrade either.
    assert resource.batch_scope == "season"
    row = resource.file_assignments[0]
    assert row.work_title_hint == "Some Movie"
    assert row.movie_id is None


async def test_refine_batch_content_unresolved_movie_falls_back_to_hint(
    db_session, monkeypatch
):
    resource = await _make_resource(db_session, is_batch=True, batch_scope="franchise")
    db_session.add_all([
        ResourceFileAssignment(resource_id=resource.id, file_path="MovieA/a.mkv", source="auto"),
        ResourceFileAssignment(resource_id=resource.id, file_path="MovieB/b.mkv", source="auto"),
    ])
    await db_session.commit()
    await db_session.refresh(resource, ["file_assignments"])
    report = _franchise_report()

    async def _fake_analyze(*a, **k):
        return {"scope": "movies", "works": [
            {"title": "电影A", "content_type": "movie", "files": [{"path": "MovieA/a.mkv"}]},
            {"title": "电影B", "content_type": "movie", "files": [{"path": "MovieB/b.mkv"}]},
        ]}

    monkeypatch.setattr(bca, "analyze_listing", _fake_analyze)

    async def _none(db, res, channel, title):
        return None

    monkeypatch.setattr(bca, "_resolve_movie", _none)

    assert await bca.refine_batch_content(db_session, resource, report, None) is False
    # The scope upgrade is verdict-driven and stands even when binding fails.
    assert resource.batch_scope == "movies"
    rows = {a.file_path: a for a in resource.file_assignments}
    assert rows["MovieA/a.mkv"].work_title_hint == "电影A"
    assert rows["MovieA/a.mkv"].movie_id is None
    assert rows["MovieB/b.mkv"].work_title_hint == "电影B"
    assert await _links(db_session, resource.id) == []


# =============================================================================
# _apply_hint / _bind_work / _resolve_movie
# =============================================================================

async def test_apply_hint_respects_manual_and_bound_rows(db_session):
    movie = Movie(id=_uuid(), title_cn="已绑定电影")
    db_session.add(movie)
    await db_session.commit()
    resource = await _make_resource(db_session, is_batch=True, batch_scope="franchise")
    db_session.add_all([
        ResourceFileAssignment(
            resource_id=resource.id, file_path="manual.mkv", source="manual",
            work_title_hint="人工",
        ),
        ResourceFileAssignment(
            resource_id=resource.id, file_path="bound.mkv", source="llm",
            movie_id=movie.id,
        ),
        ResourceFileAssignment(resource_id=resource.id, file_path="free.mkv", source="auto"),
    ])
    await db_session.commit()
    await db_session.refresh(resource, ["file_assignments"])

    bca._apply_hint(resource, {"title": "LLM Title", "files": [
        {"path": "manual.mkv", "season": 1, "episode_start": 1, "episode_end": 1},
        {"path": "bound.mkv", "season": 1, "episode_start": 2, "episode_end": 2},
        {"path": "free.mkv", "season": 2, "episode_start": 7, "episode_end": 8},
        # Path without an assignment row is ignored without error.
        {"path": "absent.mkv", "season": 1, "episode_start": 1, "episode_end": 1},
    ]})

    rows = {a.file_path: a for a in resource.file_assignments}
    assert rows["manual.mkv"].work_title_hint == "人工"
    assert rows["manual.mkv"].season is None
    assert rows["bound.mkv"].work_title_hint is None
    assert rows["bound.mkv"].season is None
    assert rows["free.mkv"].work_title_hint == "LLM Title"
    assert (rows["free.mkv"].season, rows["free.mkv"].episode_start,
            rows["free.mkv"].episode_end) == (2, 7, 8)


async def test_bind_work_series_branch_replaces_movie_and_skips_manual(db_session):
    series = TVSeries(id=_uuid(), title_cn="剧集", season_number=1)
    movie = Movie(id=_uuid(), title_cn="误绑电影")
    db_session.add_all([series, movie])
    await db_session.commit()
    resource = await _make_resource(db_session, is_batch=True, batch_scope="franchise")
    db_session.add_all([
        ResourceFileAssignment(
            resource_id=resource.id, file_path="e01.mkv", source="auto", movie_id=movie.id,
        ),
        ResourceFileAssignment(resource_id=resource.id, file_path="e02.mkv", source="manual"),
    ])
    await db_session.commit()
    await db_session.refresh(resource, ["file_assignments"])

    files = [
        {"path": "e01.mkv", "season": 1, "episode_start": 3, "episode_end": 3},
        {"path": "e02.mkv", "season": 1, "episode_start": 4, "episode_end": 4},
    ]
    await bca._bind_work(db_session, resource, "series", series.id, files)
    await db_session.flush()

    rows = {a.file_path: a for a in resource.file_assignments}
    # Series binding clears a stale movie FK and stamps llm provenance.
    assert rows["e01.mkv"].series_id == series.id
    assert rows["e01.mkv"].movie_id is None
    assert rows["e01.mkv"].source == "llm"
    assert (rows["e01.mkv"].season, rows["e01.mkv"].episode_start) == (1, 3)
    # Manual rows are never rebound.
    assert rows["e02.mkv"].source == "manual"
    assert rows["e02.mkv"].series_id is None
    links = await _links(db_session, resource.id)
    assert [(link.series_id, link.source) for link in links] == [(series.id, "llm")]


async def test_bind_work_existing_link_not_duplicated(db_session):
    series = TVSeries(id=_uuid(), title_cn="剧集", season_number=1)
    movie = Movie(id=_uuid(), title_cn="电影")
    db_session.add_all([series, movie])
    await db_session.commit()
    resource = await _make_resource(db_session, is_batch=True, batch_scope="franchise")
    db_session.add_all([
        ResourceFileAssignment(resource_id=resource.id, file_path="e01.mkv", source="auto"),
        ResourceWorkLink(resource_id=resource.id, series_id=series.id, source="manual"),
    ])
    await db_session.commit()
    await db_session.refresh(resource, ["file_assignments"])

    await bca._bind_work(
        db_session, resource, "series", series.id,
        [{"path": "e01.mkv", "season": 1, "episode_start": 1, "episode_end": 1}],
    )
    await db_session.flush()
    # The pre-existing link short-circuits insertion — still exactly one row.
    links = await _links(db_session, resource.id)
    assert [(link.series_id, link.source) for link in links] == [(series.id, "manual")]
    # The file binding itself still lands.
    assert resource.file_assignments[0].series_id == series.id

    # Same early return on the movie side of the link scan.
    resource2 = await _make_resource(db_session, is_batch=True, batch_scope="franchise")
    db_session.add_all([
        ResourceFileAssignment(resource_id=resource2.id, file_path="m1.mkv", source="auto"),
        ResourceWorkLink(resource_id=resource2.id, movie_id=movie.id, source="auto"),
    ])
    await db_session.commit()
    await db_session.refresh(resource2, ["file_assignments"])
    await bca._bind_work(
        db_session, resource2, "movie", movie.id, [{"path": "m1.mkv"}],
    )
    await db_session.flush()
    links2 = await _links(db_session, resource2.id)
    assert [(link.movie_id, link.source) for link in links2] == [(movie.id, "auto")]
    assert resource2.file_assignments[0].movie_id == movie.id


async def test_resolve_movie_match_failures(db_session, monkeypatch):
    import app.services.metadata_agent as ma

    resource = await _make_resource(db_session)

    async def _raise(title, source):
        raise RuntimeError("metadata source down")

    monkeypatch.setattr(ma, "get_agent", lambda: SimpleNamespace(process_title_only=_raise))
    assert await bca._resolve_movie(db_session, resource, None, "T") is None

    async def _not_found(title, source):
        return SimpleNamespace(found=False, matched_entity=None, content_type="movie")

    monkeypatch.setattr(ma, "get_agent", lambda: SimpleNamespace(process_title_only=_not_found))
    assert await bca._resolve_movie(db_session, resource, None, "T") is None

    async def _wrong_type(title, source):
        return SimpleNamespace(found=True, matched_entity={"title": "X"}, content_type="tv")

    monkeypatch.setattr(ma, "get_agent", lambda: SimpleNamespace(process_title_only=_wrong_type))
    assert await bca._resolve_movie(db_session, resource, None, "T") is None


async def test_resolve_movie_success_and_upsert_failure(db_session, monkeypatch):
    import app.services.metadata_agent as ma
    import app.services.metadata_service as ms

    resource = await _make_resource(db_session)
    movie = Movie(id=_uuid(), title_cn="电影")
    db_session.add(movie)
    await db_session.commit()

    async def _found(title, source):
        return SimpleNamespace(
            found=True, matched_entity={"title": "电影"}, content_type="movie",
        )

    monkeypatch.setattr(ma, "get_agent", lambda: SimpleNamespace(process_title_only=_found))

    async def _upsert(db, entity):
        return movie

    monkeypatch.setattr(ms, "create_or_update_movie_from_external", _upsert)
    assert await bca._resolve_movie(db_session, resource, None, "电影") is movie

    async def _boom(db, entity):
        raise RuntimeError("identity conflict")

    monkeypatch.setattr(ms, "create_or_update_movie_from_external", _boom)
    assert await bca._resolve_movie(db_session, resource, None, "电影") is None


# =============================================================================
# suggest_batch_content — wizard suggestions (non-persistent)
# =============================================================================

async def test_suggest_batch_content_deterministic_plus_llm(db_session, monkeypatch):
    resource = await _make_resource(
        db_session, title_raw="Pack", search_title=None, title_cn=None,
    )
    files = [
        {"name": "MovieA/a.mkv", "size": 500 * MB},
        {"name": "MovieB/b.mkv", "size": 600 * MB},
    ]

    async def _fake_analyze(title, listing, anchors, candidate_works=None):
        assert title == "Pack"
        assert anchors == ["MovieA", "MovieB"]
        return {"works": [
            {"title": "MovieA", "content_type": "movie", "files": [{"path": "MovieA/a.mkv"}]},
            # Hallucinated path: clamped away by _valid_paths.
            {"title": "Ghost", "content_type": "movie", "files": [{"path": "ghost.mkv"}]},
        ]}

    monkeypatch.setattr(bca, "analyze_listing", _fake_analyze)
    out = await bca.suggest_batch_content(db_session, resource, files=files)

    det = out["deterministic"]
    assert det["scope_hint"] == "franchise"
    assert [f["path"] for f in det["files"]] == ["MovieA/a.mkv", "MovieB/b.mkv"]
    assert {c["title"] for c in det["clusters"]} == {"MovieA", "MovieB"}
    assert [w["title"] for w in out["works"]] == ["MovieA"]


async def test_suggest_batch_content_without_torrent_file(db_session, monkeypatch):
    resource = await _make_resource(db_session, torrent_file=None)

    async def _boom(*a, **k):
        raise AssertionError("no listing → the LLM must not be called")

    monkeypatch.setattr(bca, "analyze_listing", _boom)
    out = await bca.suggest_batch_content(db_session, resource)
    assert out["deterministic"]["scope_hint"] == "unknown"
    assert out["deterministic"]["files"] == []
    assert out["deterministic"]["clusters"] == []
    assert out["works"] == []


async def test_suggest_batch_content_torrent_parse_failure(db_session, monkeypatch):
    import app.services.torrent_inspect as ti

    resource = await _make_resource(db_session, torrent_file="/nonexistent/pack.torrent")

    def _corrupt(path):
        raise OSError("corrupt torrent")

    monkeypatch.setattr(ti, "parse_torrent_files", _corrupt)
    out = await bca.suggest_batch_content(db_session, resource)
    assert out["deterministic"]["files"] == []
    assert out["works"] == []


async def test_suggest_batch_content_llm_absent_returns_empty_works(db_session, monkeypatch):
    resource = await _make_resource(db_session)
    files = [
        {"name": "MovieA/a.mkv", "size": 500 * MB},
        {"name": "MovieB/b.mkv", "size": 600 * MB},
    ]

    async def _none(*a, **k):
        return None

    monkeypatch.setattr(bca, "analyze_listing", _none)
    out = await bca.suggest_batch_content(db_session, resource, files=files)
    # The deterministic layer is always returned; only the LLM block is empty.
    assert out["deterministic"]["scope_hint"] == "franchise"
    assert out["works"] == []
