"""Bangumi related-subject expansion — the series graph pass (B1).

A Bangumi subject is ONE season of a series, so a franchise/multi_season
batch pack linked to a base entry (e.g. "頭文字D First Stage") only covers
the first season. This pass unfolds the rest of the graph through the
related-subjects endpoint (``GET /v0/subjects/{id}/subjects``):

  * 续集 / 前传 / 主线故事 → sibling per-season works — gated by a same-IP
    check first: the relation candidate's season-stripped normalized base
    name (both ``name`` and ``name_cn`` are tried) must equal or contain /
    be contained in a seed work's base name. Bangumi's 续集 edges cross IPs
    (頭文字D Final Stage → MFゴースト); a node that fails the gate is never
    upserted, never linked, and BFS does NOT continue from it, so the whole
    foreign sub-graph stays out. The season number comes from the subject's
    own title marker (``season_from_title`` — Stage ordinals included) or,
    for unmarked sequels dated strictly after every dated marked entry, the
    next free season number in air-date order. The air-date threshold and
    ordering are computed over the GATED node set only — a rejected node's
    date can no longer push the threshold past a legitimate unmarked sequel
    (Final Stage 2014 vs MFゴースト 3rd Season 2026). Anything still
    indeterminate is SKIPPED with a warning — never guessed.
  * 剧场版 → Movie works.
  * 番外篇 (OVA/spinoff) → the collection's season-0 specials work. Only ONE
    work can occupy the ``(collection, season 0)`` slot: the earliest-aired
    specials subject takes it; every further DISTINCT specials subject is
    upserted as its own single-season shell work (its own ``series_group``
    collection, ``season_number=1``) and still linked to the resource —
    cross-collection links are legal and ``sync_resource_collection``
    settling ``collection_id`` to NULL faithfully reports the span.
  * 不同演绎 (different adaptation / remake) → an independent Movie work when
    the subject is movie-form; TV-form adaptations are skipped (their season
    is unknowable from the relation alone).

Every member is upserted through the existing per-season entry points
(``create_or_update_series_from_external`` with a season hint /
``create_or_update_movie_from_external``), so identity-bag, creator-wins
primary ids and manual-edit protection all apply unchanged. Works that land
in a single-member ``series_group`` shell are absorbed into the resource's
collection (``try_absorb_shell_collection``); works that resolve to a
DIFFERENT real collection are kept but not linked. Resource-side association
goes through ``resource_work_links`` (``source="auto"``; manual/llm rows are
never touched) and ``sync_resource_collection`` settles
``resource.collection_id`` for the season-flavored scopes.

Expansion-only, never a downgrade: any relation-fetch or per-member failure
is logged and the original link verdict stands. Triggered solely for batch
resources (``batch_scope ∈ {franchise, multi_season}``) already linked to a
Bangumi identity, so single-episode resources never pay the extra API calls.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import httpx
from sqlalchemy import select

from app.models.movie import Movie
from app.models.resource_work_link import ResourceWorkLink
from app.models.series import TVSeries
from app.models.work_collection import WorkCollection
from app.models.work_external_id import WorkExternalId
from app.services.bangumi_client import (
    SUBJECT_TYPE_ANIME,
    bangumi_configured,
    get_subject,
    get_subject_relations,
)
from app.services.resource_parser import season_from_title, strip_season_from_title
from app.services.text_normalizer import normalize_title

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.file_resource import FileResource

logger = logging.getLogger(__name__)

_EXPANSION_SCOPES = frozenset({"franchise", "multi_season"})
# Graph-walk cap: franchise relations stay in-IP, but the cap bounds API
# calls when a hub entry (e.g. a long-running franchise) fans out widely.
_MAX_GRAPH_NODES = 24

_SEQUEL_RELATIONS = frozenset({"续集", "前传", "主线故事"})
_MOVIE_RELATIONS = frozenset({"剧场版"})
_SPECIALS_RELATIONS = frozenset({"番外篇"})
_ADAPTATION_RELATIONS = frozenset({"不同演绎"})
_KNOWN_RELATIONS = (
    _SEQUEL_RELATIONS | _MOVIE_RELATIONS | _SPECIALS_RELATIONS | _ADAPTATION_RELATIONS
)
_MOVIE_PLATFORMS = frozenset({"剧场版", "电影"})
# OVA/OAD-platform subjects are specials-form works (番外篇 semantics) even
# when no relation label is available (franchise member resolution).
_OVA_PLATFORMS = frozenset({"OVA", "OAD"})


def classify_work_shape(relation: str | None, platform: str) -> str | None:
    """Unified work-shape classification shared by the graph walk and
    franchise member resolution (F4): TV sequels → per-season works, movie
    form → Movie works, 番外篇/OVA → the season-0 specials work.

    ``relation`` is the Bangumi relation label on the graph walk, or None for
    relation-less contexts (a cluster title resolved through the metadata
    source carries only the subject's platform). Returns ``"season"`` /
    ``"specials"`` / ``"movie"`` or None (skipped — e.g. a TV-form 不同演绎
    whose season cannot be derived).
    """
    is_movie = platform in _MOVIE_PLATFORMS
    if relation is None:
        if is_movie:
            return "movie"
        if platform in _OVA_PLATFORMS:
            return "specials"
        return "season"
    if relation in _MOVIE_RELATIONS:
        return "movie"
    if relation in _SPECIALS_RELATIONS:
        return "movie" if is_movie else "specials"
    if relation in _ADAPTATION_RELATIONS:
        return "movie" if is_movie else None
    if relation in _SEQUEL_RELATIONS:
        return "movie" if is_movie else "season"
    return None


def _base_names(titles) -> set[str]:
    """Season-stripped normalized base names for a set of titles."""
    out: set[str] = set()
    for title in titles:
        if not title:
            continue
        for cand in {title, strip_season_from_title(title)}:
            norm = normalize_title(cand)
            if norm:
                out.add(norm)
    return out


def _same_ip(seed_bases: set[str], titles) -> bool:
    """Same-IP gate for sequel-line relation nodes (F1).

    The candidate's base name must equal a seed's base name or contain / be
    contained by it ("頭文字D Final Stage" carries no numeric marker but still
    contains the seed base "頭文字D"). Anything else — "MFゴースト 2nd Season"
    reached via 頭文字D's 续集 edge — is foreign IP.
    """
    for node_base in _base_names(titles):
        for seed_base in seed_bases:
            if node_base == seed_base:
                return True
            if min(len(node_base), len(seed_base)) >= 2 and (
                node_base in seed_base or seed_base in node_base
            ):
                return True
    return False


def _bangumi_id_of(external_id: str | None) -> int | None:
    """Parse the numeric subject id out of a canonical ``bangumi:{id}``."""
    if not external_id or not external_id.startswith("bangumi:"):
        return None
    digits = external_id.split(":", 1)[1]
    return int(digits) if digits.isdigit() else None


async def _work_subject_id(db: AsyncSession, work_type: str, work) -> int | None:
    """Bangumi subject id for a work — primary column first, then the bag."""
    sid = _bangumi_id_of(
        work.external_id if work.external_source == "bangumi" else None
    )
    if sid is not None:
        return sid
    rows = (
        await db.execute(
            select(WorkExternalId.external_id).where(
                WorkExternalId.work_type == work_type,
                WorkExternalId.work_id == work.id,
                WorkExternalId.source == "bangumi",
            )
        )
    ).scalars().all()
    for external_id in rows:
        sid = _bangumi_id_of(external_id)
        if sid is not None:
            return sid
    return None


async def _seed_subjects(db: AsyncSession, resource: FileResource) -> list[dict[str, Any]]:
    """Bangumi-linked works already associated with the resource.

    Seeds come from the legacy FK (multi_season packs keep their base-entry
    link there), the work-link table, and — for franchise packs, whose works
    are attached to the pack's collection without link rows — the collection
    members.
    """
    works: list[tuple[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def _add(work_type: str, work) -> None:
        if work is None or (work_type, work.id) in seen:
            return
        seen.add((work_type, work.id))
        works.append((work_type, work))

    if resource.series_id:
        _add("series", await db.get(TVSeries, resource.series_id))
    if resource.movie_id:
        _add("movie", await db.get(Movie, resource.movie_id))
    links = (
        await db.execute(
            select(ResourceWorkLink).where(ResourceWorkLink.resource_id == resource.id)
        )
    ).scalars().all()
    for link in links:
        if link.series_id:
            _add("series", await db.get(TVSeries, link.series_id))
        elif link.movie_id:
            _add("movie", await db.get(Movie, link.movie_id))
    if resource.batch_scope == "franchise" and resource.collection_id:
        for work in (
            await db.execute(
                select(TVSeries).where(TVSeries.collection_id == resource.collection_id)
            )
        ).scalars().all():
            _add("series", work)
        for work in (
            await db.execute(
                select(Movie).where(Movie.collection_id == resource.collection_id)
            )
        ).scalars().all():
            _add("movie", work)

    seeds: list[dict[str, Any]] = []
    for work_type, work in works:
        sid = await _work_subject_id(db, work_type, work)
        if sid is None:
            continue
        start = getattr(work, "start_date", None) or getattr(work, "release_date", None)
        seeds.append({
            "sid": sid,
            "work": work,
            "work_type": work_type,
            "season": getattr(work, "season_number", None),
            "date": start.isoformat() if start else None,
        })
    return seeds


def _season_is_default_guess(work: TVSeries) -> bool:
    """Whether a seed work's season_number is a single-season DEFAULT rather
    than evidence: the title is qualified ("頭文字D Final Stage", "Initial D
    Battle Stage 2") but carries no season marker. Member resolution creates
    such works at season 1 in their own shells; the graph must not treat that
    guess as marked evidence — the subject re-enters the walk as an ordinary
    node and gets the deterministic assignment (marker / gated air-date
    order) instead."""
    titles = [
        t for t in (
            getattr(work, "title_cn", None),
            getattr(work, "title_en", None),
            getattr(work, "original_title", None),
        ) if t
    ]
    if any(season_from_title(t) is not None for t in titles):
        return False
    from app.services.metadata_service import _has_unresolved_title_qualifier

    return _has_unresolved_title_qualifier({
        "title_cn": getattr(work, "title_cn", None),
        "title_en": getattr(work, "title_en", None),
        "original_title": getattr(work, "original_title", None),
    })


def _node_date(node: dict[str, Any]) -> str | None:
    date = str(node.get("detail", {}).get("date") or node["summary"].get("date") or "")
    return date if date[:4].isdigit() else None


def _assign_seasons(
    season_nodes: list[dict[str, Any]],
    seeds: list[dict[str, Any]],
) -> dict[int, int]:
    """Pin a season number for every season-shaped graph node.

    Deterministic evidence only: title markers first (Stage ordinals
    included), then air-date order for unmarked sequels dated strictly after
    every dated marked entry (e.g. "Final Stage" → the number after the
    latest known season). ``season_nodes`` is already the same-IP-gated set
    (F1/F2): the threshold and the date ordering never see foreign nodes, so
    a younger cross-IP sequel cannot push the threshold past a legitimate
    unmarked entry. Unmarked entries that cannot be ordered this way
    are skipped — the skip is logged here and by the caller.
    """
    from app.services.metadata_bangumi import _subject_season

    marked: dict[int, int] = {}
    for seed in seeds:
        if seed["work_type"] == "series" and seed["season"] is not None:
            marked[seed["sid"]] = seed["season"]
    for node in season_nodes:
        titles = {**node["summary"], **node.get("detail", {})}
        marker = _subject_season(titles)
        if marker is not None and marker >= 1:
            marked[node["sid"]] = marker

    dated_marked = [d for seed in seeds if (d := seed.get("date"))]
    dated_marked += [
        d for node in season_nodes
        if node["sid"] in marked and (d := _node_date(node))
    ]
    assignments = dict(marked)
    unmarked = [node for node in season_nodes if node["sid"] not in marked]
    if not unmarked:
        return assignments
    if not dated_marked:
        for node in unmarked:
            logger.warning(
                "[bangumi-graph] subject %s (%r) has no season marker and the "
                "relation chain carries no air dates; skipped",
                node["sid"], node["summary"].get("name"),
            )
        return assignments
    threshold = max(dated_marked)
    inferable = [node for node in unmarked if (d := _node_date(node)) and d > threshold]
    if len({_node_date(node) for node in inferable}) != len(inferable):
        # Duplicate air dates make the order unknowable — skip all of them.
        for node in inferable:
            logger.warning(
                "[bangumi-graph] subject %s (%r) shares an air date with another "
                "unmarked sequel; season order indeterminate, skipped",
                node["sid"], node["summary"].get("name"),
            )
        inferable = []
    inferable.sort(key=lambda node: (_node_date(node), node["sid"]))
    next_season = max(assignments.values(), default=0) + 1
    for node in inferable:
        assignments[node["sid"]] = next_season
        next_season += 1
    for node in unmarked:
        if node["sid"] not in assignments:
            logger.warning(
                "[bangumi-graph] subject %s (%r) season indeterminate; skipped",
                node["sid"], node["summary"].get("name"),
            )
    return assignments


async def _ensure_link(
    db: AsyncSession, resource: FileResource, work_type: str, work_id: str
) -> bool:
    """Add a ``source="auto"`` work link unless one already exists.

    Additive only: existing rows (manual/llm/auto) are never modified.
    """
    column = ResourceWorkLink.series_id if work_type == "series" else ResourceWorkLink.movie_id
    existing = (
        await db.execute(
            select(ResourceWorkLink.id).where(
                ResourceWorkLink.resource_id == resource.id,
                column == work_id,
            )
        )
    ).first()
    if existing:
        return False
    db.add(ResourceWorkLink(
        resource_id=resource.id,
        series_id=work_id if work_type == "series" else None,
        movie_id=work_id if work_type == "movie" else None,
        source="auto",
    ))
    return True


async def _collection_member_by_season(
    db: AsyncSession, collection_id: str, season: int | None
) -> TVSeries | None:
    """The work occupying ``(collection_id, season_number)``, if any.

    The pair is application-level unique; used both to reuse an existing
    member instead of upserting a duplicate season work (F3b) and to guard
    the season slot before attaching/absorbing (F4).
    """
    if season is None:
        return None
    return (
        await db.execute(
            select(TVSeries).where(
                TVSeries.collection_id == collection_id,
                TVSeries.season_number == season,
            )
        )
    ).scalars().first()


async def _attach_and_link(
    db: AsyncSession,
    resource: FileResource,
    work_type: str,
    work,
    target_collection: WorkCollection,
    *,
    link_foreign: bool = False,
) -> bool:
    """Bring ``work`` into the target collection (when possible) and link it.

    Attach order for a work sitting in another collection: single-member
    ``series_group`` shell absorption first, then — same normalized base
    name, auto sources only, no manual edits — whole-collection absorption
    of the duplicate same-IP collection (F3b, multi-member allowed; members
    whose season collides with an existing target member stay behind). The
    ``(collection, season)`` slot is never double-occupied: a season work
    whose slot is taken is not moved.

    Works that still resolve to a DIFFERENT collection afterwards are kept
    but — unless ``link_foreign`` — not linked: for season-flavored scopes a
    cross-collection link unsets the resource's derived collection identity.
    ``link_foreign=True`` is used for the season-1 shell specials (F4), where
    the cross-collection span is the faithful representation.
    """
    from app.services.collection_service import (
        try_absorb_same_name_collection,
        try_absorb_shell_collection,
    )

    if work.collection_id != target_collection.id:
        if work_type == "series":
            # The (collection, season) slot is application-unique: never move
            # or absorb a season work whose slot is already taken.
            occupant = await _collection_member_by_season(
                db, target_collection.id, getattr(work, "season_number", None)
            )
            if occupant is not None and occupant.id != work.id:
                if link_foreign:
                    return await _ensure_link(db, resource, work_type, work.id)
                logger.info(
                    "[bangumi-graph] work %s (season %s) slot already occupied in "
                    "collection %s; not linked to %s",
                    work.id, work.season_number, target_collection.id, resource.id,
                )
                return False
        attached = False
        if work.collection_id is None:
            work.collection_id = target_collection.id
            attached = True
        else:
            attached = await try_absorb_shell_collection(db, target_collection, work)
            if not attached:
                attached = await try_absorb_same_name_collection(
                    db, target_collection, work
                )
        if not attached:
            if link_foreign:
                return await _ensure_link(db, resource, work_type, work.id)
            logger.info(
                "[bangumi-graph] work %s stays in collection %s; not linked to %s",
                work.id, work.collection_id, resource.id,
            )
            return False
    return await _ensure_link(db, resource, work_type, work.id)


async def _correct_work_season(
    db: AsyncSession, work: TVSeries, season: int, node: dict[str, Any]
) -> bool:
    """Apply the graph's deterministic season assignment to an existing work.

    A bag-hit or hint-created work can carry a stale DEFAULT season (a
    no-marker member-resolution guess or a pack-internal cluster number).
    The node's assignment is deterministic (title marker or gated air-date
    order), so it corrects the row — never a user edit, never onto an
    occupied ``(collection, season)`` slot. Returns True when the work's
    season now equals ``season``.
    """
    from app.services.metadata_service import field_manually_edited

    if work.season_number == season:
        return True
    if field_manually_edited(work, "season_number"):
        logger.warning(
            "[bangumi-graph] work %s season manually edited (%s); graph "
            "assignment %s not applied",
            work.id, work.season_number, season,
        )
        return False
    occupant = await _collection_member_by_season(db, work.collection_id, season)
    if occupant is not None and occupant.id != work.id:
        logger.warning(
            "[bangumi-graph] cannot correct work %s to season %s: slot "
            "occupied by %s; node %s skipped",
            work.id, season, occupant.id, node["sid"],
        )
        return False
    logger.info(
        "[bangumi-graph] correcting work %s season %s -> %s "
        "(deterministic graph assignment)",
        work.id, work.season_number, season,
    )
    work.season_number = season
    return True


async def _season_zero_holder(db: AsyncSession, collection_id: str) -> TVSeries | None:
    """The work currently occupying the collection's season-0 slot, if any."""
    return (
        await db.execute(
            select(TVSeries).where(
                TVSeries.collection_id == collection_id,
                TVSeries.season_number == 0,
            )
        )
    ).scalars().first()


async def expand_bangumi_series_graph(db: AsyncSession, resource: FileResource) -> int:
    """Expand the Bangumi series graph for a batch pack resource.

    Returns the number of NEW resource↔work links created. Never raises for
    source-side failures; the caller commits (same convention as the torrent
    inspection / metadata linking steps it runs next to).
    """
    if not (
        getattr(resource, "is_batch", False)
        and getattr(resource, "batch_scope", None) in _EXPANSION_SCOPES
    ):
        return 0
    if not bangumi_configured():
        return 0
    seeds = await _seed_subjects(db, resource)
    if not seeds:
        return 0
    target_collection_id = resource.collection_id or next(
        (seed["work"].collection_id for seed in seeds if seed["work"].collection_id),
        None,
    )
    if target_collection_id is None:
        return 0
    target_collection = await db.get(WorkCollection, target_collection_id)
    if target_collection is None:
        return 0

    from app.services.metadata_bangumi import _build_matched_entity
    from app.services.metadata_service import (
        create_or_update_movie_from_external,
        create_or_update_series_from_external,
    )

    seed_bases = _base_names(
        t
        for seed in seeds
        for t in (
            getattr(seed["work"], "title_cn", None),
            getattr(seed["work"], "title_en", None),
            getattr(seed["work"], "original_title", None),
        )
    )

    created_links = 0
    linked_ids: set[tuple[str, str]] = set()
    # Guessed-season seeds (qualified-but-unmarked member works) are not
    # season evidence: they may re-enter the walk as ordinary nodes for a
    # deterministic assignment, and stay out of the marked/dated sets.
    guessed_sids = {
        seed["sid"]
        for seed in seeds
        if seed["work_type"] == "series"
        and seed["season"] is not None
        and seed["season"] >= 1  # season 0 comes from OVA/番外 shape evidence
        and _season_is_default_guess(seed["work"])
    }
    evidence_seeds = [s for s in seeds if s["sid"] not in guessed_sids]
    async with httpx.AsyncClient(timeout=15) as client:
        # 1. Walk the relation graph (BFS from the seed subjects).
        visited: set[int] = {seed["sid"] for seed in evidence_seeds}
        nodes: dict[int, dict[str, Any]] = {}
        queue = [seed["sid"] for seed in seeds]
        while queue and len(visited) < _MAX_GRAPH_NODES:
            sid = queue.pop(0)
            try:
                relations = await get_subject_relations(client, sid)
            except Exception as e:
                logger.warning(
                    "[bangumi-graph] relations fetch failed for subject %s: %s", sid, e
                )
                continue
            for rel in relations:
                rid = rel.get("id")
                if not isinstance(rid, int) or rid in visited:
                    continue
                if rel.get("type") != SUBJECT_TYPE_ANIME:
                    continue
                relation = str(rel.get("relation") or "")
                if relation not in _KNOWN_RELATIONS:
                    continue
                if relation in _SEQUEL_RELATIONS and not _same_ip(
                    seed_bases, (rel.get("name"), rel.get("name_cn"))
                ):
                    # F1 cross-IP guard: Bangumi 续集 edges jump franchises
                    # (頭文字D Final Stage → MFゴースト). The node is marked
                    # visited so it is never upserted/linked, and — not being
                    # queued — its own relations are never fetched.
                    visited.add(rid)
                    logger.warning(
                        "[bangumi-graph] subject %s (%r, relation=%r) fails the "
                        "same-IP gate; skipped and not traversed",
                        rid, rel.get("name"), relation,
                    )
                    continue
                visited.add(rid)
                nodes[rid] = {"sid": rid, "summary": rel, "relation": relation}
                queue.append(rid)
                if len(visited) >= _MAX_GRAPH_NODES:
                    break

        if not nodes:
            return 0

        # 2. Fetch details (platform drives the movie/TV split) + classify.
        season_nodes: list[dict[str, Any]] = []
        specials_nodes: list[dict[str, Any]] = []
        movie_nodes: list[dict[str, Any]] = []
        for node in nodes.values():
            try:
                node["detail"] = await get_subject(client, node["sid"])
            except Exception as e:
                logger.warning(
                    "[bangumi-graph] subject %s details failed: %s", node["sid"], e
                )
                continue
            platform = str(node["detail"].get("platform") or "")
            shape = classify_work_shape(node["relation"], platform)
            if shape == "season":
                season_nodes.append(node)
            elif shape == "specials":
                specials_nodes.append(node)
            elif shape == "movie":
                movie_nodes.append(node)
            else:
                logger.warning(
                    "[bangumi-graph] subject %s (%r, relation=%r, platform=%r) "
                    "has no deterministic work shape; skipped",
                    node["sid"], node["summary"].get("name"),
                    node["relation"], platform,
                )

        # 3. Upsert season works (marker-first, then air-date inference).
        assignments = _assign_seasons(season_nodes, evidence_seeds)

        # Phase A (C1): correct stale default/hint-derived seasons on
        # EXISTING works before admitting any new season work. A misplaced
        # entry ("Final Stage" parked at s5 by a cluster hint) frees its slot
        # now, so the rightful owner ("Fifth Stage") admitted in phase B
        # never sees the conflict. Runs to a fixpoint so swap-shaped
        # misplacements unwind too.
        for _ in range(3):
            corrected_any = False
            for node in season_nodes:
                season = assignments.get(node["sid"])
                if season is None:
                    continue
                from app.services.external_ids import find_work_by_external_id

                existing = await find_work_by_external_id(
                    db, "series", "bangumi", f"bangumi:{node['sid']}"
                )
                if existing is None or existing.season_number in (None, season):
                    continue
                if await _correct_work_season(db, existing, season, node):
                    corrected_any = True
            if not corrected_any:
                break

        # Phase B: admit season works (member reuse → upsert → attach/link).
        for node in season_nodes:
            season = assignments.get(node["sid"])
            if season is None:
                continue
            # F3b: prefer the resource's own collection — when the target
            # already holds a member for this season (e.g. a member-resolution
            # upsert), reuse it and only bag this node's identity instead of
            # materializing a duplicate season work in a fresh shell.
            work = await _collection_member_by_season(db, target_collection.id, season)
            if work is not None:
                member_sid = await _work_subject_id(db, "series", work)
                if member_sid in (None, node["sid"]):
                    from app.services.external_ids import add_external_id

                    await add_external_id(
                        db, "series", work.id, "bangumi", f"bangumi:{node['sid']}"
                    )
                else:
                    # A DIFFERENT Bangumi subject already occupies the slot —
                    # fall through to the normal upsert, never merge identities.
                    work = None
            if work is None:
                work = await _upsert_season_node(
                    db, client, node, season, create_or_update_series_from_external,
                    _build_matched_entity, movie_nodes,
                )
            if work is None:
                continue
            if work.season_number is not None and work.season_number != season:
                # Phase A already corrected bag-hit works; a slot may only
                # have freed since (an absorption moved the occupant), so
                # retry the correction here — never a user edit, never onto
                # an occupied (collection, season) slot.
                if not await _correct_work_season(db, work, season, node):
                    continue
            if await _attach_and_link(db, resource, "series", work, target_collection):
                linked_ids.add(("series", work.id))
                created_links += 1

        # 4. Upsert specials works. The earliest-aired subject takes the
        # collection's season-0 slot; every further DISTINCT specials subject
        # becomes its own single-season shell work (season_number=1) and is
        # still linked — the (collection, s0) slot is never shared (F4).
        for node in sorted(
            specials_nodes, key=lambda n: (_node_date(n) or "9999-99-99", n["sid"])
        ):
            holder = await _season_zero_holder(db, target_collection.id)
            holder_sid = (
                await _work_subject_id(db, "series", holder)
                if holder is not None
                else None
            )
            if holder is None or holder_sid == node["sid"]:
                work = await _upsert_season_node(
                    db, client, node, 0, create_or_update_series_from_external,
                    _build_matched_entity, movie_nodes,
                )
                if work is None:
                    continue
                if await _attach_and_link(db, resource, "series", work, target_collection):
                    linked_ids.add(("series", work.id))
                    created_links += 1
                continue
            logger.info(
                "[bangumi-graph] subject %s (%r): the collection's season-0 slot "
                "is taken by subject %s; upserting as its own season-1 shell work",
                node["sid"], node["summary"].get("name"), holder_sid,
            )
            work = await _upsert_season_node(
                db, client, node, 1, create_or_update_series_from_external,
                _build_matched_entity, movie_nodes,
            )
            if work is None:
                continue
            if await _attach_and_link(
                db, resource, "series", work, target_collection, link_foreign=True
            ):
                linked_ids.add(("series", work.id))
                created_links += 1

        # 5. Upsert movie works (剧场版 / movie-form 番外篇·不同演绎·续集).
        for node in movie_nodes:
            subject = {**node["summary"], **node.get("detail", {})}
            try:
                entity = await _build_matched_entity(
                    client, subject, season=1, detail=node.get("detail")
                )
                entity.pop("_content_type", None)
                entity["content_type"] = "movie"
                # The movie upsert reads release_date, not start_date.
                entity["release_date"] = entity.get("start_date")
                work = await create_or_update_movie_from_external(db, entity)
            except Exception as e:
                logger.warning(
                    "[bangumi-graph] movie upsert failed for subject %s: %s",
                    node["sid"], e,
                )
                continue
            if await _attach_and_link(db, resource, "movie", work, target_collection):
                linked_ids.add(("movie", work.id))
                created_links += 1

    # 6. The seed works themselves join the link set (the multi_season
    # coverage key reads the links table, so the base entry must be there
    # too); foreign-collection seeds are left untouched.
    for seed in seeds:
        work = seed["work"]
        if work.collection_id != target_collection.id:
            continue
        if await _ensure_link(db, resource, seed["work_type"], work.id):
            linked_ids.add((seed["work_type"], work.id))
            created_links += 1

    # 7. Settle the derived state from the expanded link set.
    if linked_ids:
        await _settle_resource(db, resource, linked_ids)
    return created_links


async def _upsert_season_node(
    db: AsyncSession,
    client: httpx.AsyncClient,
    node: dict[str, Any],
    season: int,
    create_or_update_series_from_external,
    build_matched_entity,
    movie_nodes: list[dict[str, Any]],
):
    """Build the entity for a season-shaped node and upsert its season work.

    A node whose details turn out to be movie-form (platform changed between
    the relation summary and the details fetch) is deferred to the movie
    pass instead of being forced into a season work.
    """
    subject = {**node["summary"], **node.get("detail", {})}
    try:
        entity = await build_matched_entity(
            client, subject, season=season, detail=node.get("detail")
        )
        content_type = entity.pop("_content_type")
        if content_type != "tv":
            movie_nodes.append(node)
            return None
        entity["content_type"] = "tv"
        return await create_or_update_series_from_external(
            db, entity, season_hint=season
        )
    except Exception as e:
        logger.warning(
            "[bangumi-graph] season work upsert failed for subject %s: %s",
            node["sid"], e,
        )
        return None


async def _settle_resource(
    db: AsyncSession, resource: FileResource, linked_ids: set[tuple[str, str]]
) -> None:
    """Mirror the derived caches after auto links were added.

    ``batch_seasons`` gains the linked season works' numbers (season 0 kept —
    it is part of the reported coverage set); ``sync_resource_collection``
    settles ``collection_id`` for the season-flavored scopes (franchise packs
    own their collection semantics and are left alone).
    """
    from app.services.batch_content_analysis import sync_resource_collection

    if resource.batch_scope in (None, "season", "multi_season"):
        seasons = {s for s in (resource.batch_seasons or []) if s is not None}
        for work_type, work_id in linked_ids:
            if work_type != "series":
                continue
            work = await db.get(TVSeries, work_id)
            if work is not None and work.season_number is not None:
                seasons.add(work.season_number)
        if seasons:
            resource.batch_seasons = sorted(seasons)
    await sync_resource_collection(db, resource)
