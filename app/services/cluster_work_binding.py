"""Cluster-level work binding — resolve ``work_title_hint`` clusters to works.

The torrent enrichment pass (``apply_auto_assignments``) tags every main
video file with its top-level directory's cluster title
(``ResourceFileAssignment.work_title_hint``) but never binds ``auto`` rows to
a work. This pass closes that gap: for each distinct hint it resolves ONE
work with deterministic confidence, then binds the cluster's whole set of
unbound ``auto`` rows to it — turning "bind 95 files one by one in the
wizard" into "~7 clusters auto-bound + manual review".

Resolution order per cluster (first hit wins, anything unsure is skipped and
the hint is kept for manual/LLM handling):

1. **Same-collection members** — when the resource is already associated
   with works (mutually-exclusive FK / ``resource_work_links``) or parked on
   a collection, the hint is matched by normalized-title equality against
   the members of those collections (titles + aliases; a hint equal to the
   collection's own title/aliases widens to ALL series members so the
   cluster's parsed season can pick the right season work). A third tier
   fires when the hint itself carries a season/Stage marker and its stripped
   base matches the resource's OWN parked collection's primary title (equal
   or containing — "Initial D Fifth Stage" vs the bilingual "头文字D Initial
   D"; sibling shells are excluded, their lookalike Latin titles would make
   the verdict ambiguous) — the marker selects the member by season and
   counts as title evidence.
2. **Local FTS** — series + movie library search; only scores at or above
   the shared auto-link threshold (``AUTO_LINK_THRESHOLD``) qualify, and the
   top score must be unique after season disambiguation. Two guards apply
   before any FTS binding (both must pass):
   - **form compatibility** — a cluster whose files carry parsed episode
     evidence never binds a Movie work; movie-form requires an explicit
     movie marker word (``movie`` / 电影 / 剧场版) in the hint or file paths
     and never binds a series work (a bare single file WITHOUT one is
     neutral — an "[OVA]" directory holds TV-form content). Movie
     candidates are only eligible when the cluster has no episode evidence.
   - **base-name containment** — the normalized hint and the candidate's
     normalized base form (bracket tags, NUMERIC season tokens and separator
     punctuation stripped; Stage ordinal words like "Fifth"/"Battle" are
     work-name words and stay) must be equal or mutually containing.
     "Initial D Fifth Stage" vs "Initial D Battle Stage" scores ~88 by
     Levenshtein but its base forms differ (fifth ≠ battle) — rejected.
     Exact base-form equality outranks mere containment ("Initial D Battle
     Stage" the movie beats the "...Battle Stage 2" sibling series).
   An empty FTS result falls back to a full-table scan (the ngram AND-match
   cannot bridge separator punctuation) — the same fallback
   ``match_series_by_title`` / ``match_movie_by_title`` use.
3. **External search** — one ``process_title_only`` call on the channel's
   metadata source (the same entry franchise member resolution uses);
   accepted only when the agent found an unambiguous, form-compatible match,
   then upserted through ``create_or_update_*_from_external`` (identity bag,
   creator-wins primary ids and manual-edit protection all apply unchanged).

Season semantics — pack-internal numbering vs work identity: some packs
number their directories in pack order (S01..S05 for six TV seasons minus a
movie) while the works are identified by their own season markers (Fourth
Stage IS season 4). Two evidence tiers decide:

- **Title evidence** (exact member title/alias match, guarded FTS hit,
  unambiguous external found): the resolution says WHICH work this cluster
  is, so the work's own ``season_number`` outranks the path-parsed season —
  a disagreeing row is REMAPPED to the work's season (episode numbers are
  season-internal and kept) and bound. The hint's own season marker
  (``season_from_title`` — Stage ordinals included) likewise outranks the
  path-mode season when picking candidates and as the external upsert's
  ``season_hint`` (a season the upsert cannot place returns None and the
  cluster is skipped).
- **Season-only selection** (no title evidence: base-name widening picks a
  member purely by the cluster's season mode): stays conservative — a
  disagreeing row is NOT bound (warning logged).

The remap count is exposed in the return value (``BindOutcome.remapped``)
and logs for audit. ``llm`` / ``manual`` rows are never touched;
already-bound ``auto`` rows are left alone for BINDING, but a reconcile pass
at the top of each run re-mirrors their season when the bound work's
``season_number`` was corrected after the fact (e.g. the bangumi series
graph relocates a "Final Stage" entry created as s1 by franchise linking to
its true s6) — season is the work's identity, so bound auto rows follow it.
Rows bound to a Movie work have their season cleared (movies are
seasonless). After binding, the derived caches are refreshed
(``compute_season_ranges``, additive ``batch_seasons`` for the
season-flavored scopes) and ``sync_resource_collection`` settles the
resource's collection identity.

All failures degrade silently to the unbound state; nothing here commits
(same convention as the torrent inspection steps it runs next to).
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from typing import TYPE_CHECKING, Any, NamedTuple

from sqlalchemy import select

from app.models.movie import Movie
from app.models.resource_work_link import ResourceWorkLink
from app.models.series import TVSeries
from app.models.work_collection import WorkCollection
from app.services.resource_parser import season_from_title, strip_season_from_title
from app.services.text_normalizer import normalize_title, similarity_score

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.channel import Channel
    from app.models.file_resource import FileResource
    from app.models.resource_file_assignment import ResourceFileAssignment

logger = logging.getLogger(__name__)

_TITLE_ATTRS = ("title_cn", "title_en", "original_title")


class BindOutcome(NamedTuple):
    """Result of one ``bind_hint_clusters`` pass: newly bound rows, and rows
    whose season was rewritten onto the bound work's own ``season_number``
    (pack-internal remap at bind time + following a later work-season
    relocation on already-bound auto rows)."""

    bound: int
    remapped: int


def _work_titles(work: Any) -> list[str]:
    titles = [getattr(work, attr, None) for attr in _TITLE_ATTRS]
    titles.extend(getattr(work, "aliases", None) or [])
    return [t for t in titles if t]


def _matches(work: Any, norm_hint: str) -> bool:
    return any(normalize_title(t) == norm_hint for t in _work_titles(work))


def _cluster_season(rows: list[ResourceFileAssignment]) -> int | None:
    """Most frequent non-null parsed season across the cluster's rows."""
    counts = Counter(r.season for r in rows if r.season is not None)
    return counts.most_common(1)[0][0] if counts else None


def _cluster_form(rows: list[ResourceFileAssignment], hint: str | None) -> str:
    """Form evidence of a cluster: ``tv`` / ``movie`` / ``unknown``.

    Only EXPLICIT episode patterns in the file path (SxxEyy, Exx, 第N话,
    ``[NN]``) count as TV evidence — a bare number ("Battle Stage 3") or an
    LLM-suggested episode on a row is not decisive. Movie-form requires an
    explicit movie marker word (``movie`` / 电影 / 剧场版) in the hint or the
    file paths. A SINGLE-FILE cluster in an OVA/OAD/剧场版/Movie/Film-marked
    directory stays open to a movie verdict even with weak numeric evidence
    (the number is a sequel ordinal, not an episode). Season-only parses
    stay neutral: a "[Season N]" directory tag is pack-internal numbering
    that movie directories also carry.
    """
    paths = [r.file_path or "" for r in rows]
    if any(_STRONG_EPISODE_RE.search(p) for p in paths):
        return "tv"
    if _MOVIE_MARKER_RE.search(hint or "") or any(
        _MOVIE_MARKER_RE.search(p) for p in paths
    ):
        return "movie"
    if any(r.episode_start is not None or r.episode_end is not None for r in rows):
        if len(rows) == 1 and any(_OVA_FILM_MARKER_RE.search(p) for p in paths):
            return "unknown"
        return "tv"
    return "unknown"


# Explicit episode patterns that lock a cluster to TV form. Everything else
# numeric (sequel ordinals, years, bare digits) is not episode evidence.
_STRONG_EPISODE_RE = re.compile(
    r"(?:S\d{1,2}\s*E\d{1,3}\b|\bE\d{1,3}\b|第\s*\d{1,3}\s*[话話集]|\[\d{1,3}(?:v\d+)?\])",
    re.IGNORECASE,
)
# Directory markers that keep a single-file cluster open to a movie verdict.
_OVA_FILM_MARKER_RE = re.compile(
    r"(?:\bOVA\b|\bOAD\b|剧场版|劇場版|电影|電影|\bMovie\b|\bFilm\b)",
    re.IGNORECASE,
)

# Explicit movie-form markers in a directory / cluster title.
_MOVIE_MARKER_RE = re.compile(r"(?:\bmovie\b|电影|電影|剧场版|劇場版)", re.IGNORECASE)


def _form_allows(form: str, work: Any) -> bool:
    if form == "tv":
        return not isinstance(work, Movie)
    if form == "movie":
        return not isinstance(work, TVSeries)
    return True


# Base-form comparison for the FTS guard: bracketed release tags and NUMERIC
# season tokens (S01 / Season 3 / 第3季) are pack decoration and stripped;
# Stage ordinal words ("Fifth Stage" vs "Battle Stage") are part of the work
# name and deliberately kept so lookalike titles stay distinct. Separator
# punctuation ("Initial D: Battle Stage" vs "Initial D Battle Stage") is
# stripped too — it carries no identity, and the ngram FTS AND-match cannot
# bridge it.
_BRACKET_BLOCK_RE = re.compile(r"[\[【\(（][^\]】\)）]*[\]】\)）]")
_NUMERIC_SEASON_RE = re.compile(
    r"(?:\bS\d{1,2}\b|\bSeason\s*\d+\b|第\s*\d+\s*季)", re.IGNORECASE
)
_BASE_SEP_RE = re.compile(r"[:：·/／\-–—]+")


def _base_form(title: str | None) -> str:
    if not title:
        return ""
    t = _BRACKET_BLOCK_RE.sub(" ", title)
    t = _NUMERIC_SEASON_RE.sub(" ", t)
    t = _BASE_SEP_RE.sub(" ", t)
    return normalize_title(t)


def _base_match(hint: str, work: Any) -> bool:
    """Normalized base forms of hint and one of the work's titles must be
    equal or mutually containing."""
    base_hint = _base_form(hint)
    if len(base_hint) < 2:
        return False
    for title in _work_titles(work):
        base = _base_form(title)
        if len(base) < 2:
            continue
        if base == base_hint or base in base_hint or base_hint in base:
            return True
    return False


def _base_exact(hint: str, work: Any) -> bool:
    """Whether one of the work's titles base-matches the hint EXACTLY (not
    merely containment — "Initial D Battle Stage" vs "...Battle Stage 2")."""
    base_hint = _base_form(hint)
    if len(base_hint) < 2:
        return False
    return any(_base_form(t) == base_hint for t in _work_titles(work))


def _sequel_ambiguous(hint: str, work: Any) -> bool:
    """Sequel-number ambiguity between a hint and a candidate work.

    Base-form comparison strips nothing after the last word, so "Initial D
    Battle Stage" is containment-matched by "...Battle Stage 2". When the
    trailing sequel numbers of the two sides DIFFER (one side has none, or a
    different number), the titles are siblings, not the same work: only an
    exact normalized ORIGINAL-title match may bind (handled by the caller);
    the stripped-fuzzy hit is sequel-ambiguous. Season tokens ("Show S02")
    are not sequel numbers — that path resolves by season selection, never
    through this guard.
    """
    from app.services.metadata_episode_reconcile import _trailing_sequel_number

    hint_seq = _trailing_sequel_number(hint)
    work_seqs = {
        n for n in (_trailing_sequel_number(t) for t in _work_titles(work))
        if n is not None
    }
    if hint_seq is None and not work_seqs:
        return False
    if hint_seq is not None and hint_seq in work_seqs:
        return False
    if not _base_match(hint, work):
        return False
    norm_hint = normalize_title(hint)
    return not any(normalize_title(t) == norm_hint for t in _work_titles(work))


def _pick_unique(
    candidates: list[Any], season_hint: int | None
) -> tuple[str, Any] | None:
    """Deterministic single-winner selection among same-title candidates.

    A season hint only ever SELECTS (exact ``season_number`` match) — when it
    matches nothing the cluster is skipped rather than guessed onto another
    season's work.
    """
    seen: dict[str, Any] = {w.id: w for w in candidates}
    series = [w for w in seen.values() if isinstance(w, TVSeries)]
    movies = [w for w in seen.values() if isinstance(w, Movie)]
    if series and movies:
        return None
    if movies:
        return ("movie", movies[0]) if len(movies) == 1 else None
    if not series:
        return None
    if season_hint is not None:
        exact = [w for w in series if w.season_number == season_hint]
        return ("series", exact[0]) if len(exact) == 1 else None
    return ("series", series[0]) if len(series) == 1 else None


def _pick_by_title(candidates: list[Any], season_hint: int | None) -> Any | None:
    """Title-evidence selection among matched candidates.

    The title match already says which work the cluster IS, so a lone
    candidate is accepted even when its ``season_number`` differs from the
    path-parsed season (pack-internal numbering remaps onto the work's
    season at bind time). An exact season match still selects among
    multiple same-title works; multiple candidates with no exact match are
    ambiguous and skipped.
    """
    works = list({w.id: w for w in candidates}.values())
    if not works:
        return None
    if season_hint is not None:
        exact = [w for w in works if getattr(w, "season_number", None) == season_hint]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            return None
    return works[0] if len(works) == 1 else None


def _work_type_of(work: Any) -> str:
    return "series" if isinstance(work, TVSeries) else "movie"


async def _associated_collection_ids(
    db: AsyncSession, resource: FileResource
) -> list[str]:
    """Collections reachable from the resource's current associations."""
    collection_ids: set[str] = set()
    if resource.collection_id:
        collection_ids.add(resource.collection_id)
    refs: list[tuple[str, str]] = []
    if resource.series_id:
        refs.append(("series", resource.series_id))
    if resource.movie_id:
        refs.append(("movie", resource.movie_id))
    links = (
        await db.execute(
            select(ResourceWorkLink).where(ResourceWorkLink.resource_id == resource.id)
        )
    ).scalars().all()
    for link in links:
        if link.series_id:
            refs.append(("series", link.series_id))
        elif link.movie_id:
            refs.append(("movie", link.movie_id))
    for work_type, work_id in refs:
        work = await db.get(TVSeries if work_type == "series" else Movie, work_id)
        if work is not None and work.collection_id:
            collection_ids.add(work.collection_id)
    return sorted(collection_ids)


async def _match_collection_members(
    db: AsyncSession,
    resource: FileResource,
    hint: str,
    season_hint: int | None,
    form: str,
    season_marker: int | None,
) -> tuple[str, Any, bool] | None:
    """Match within associated collections. Returns ``(work_type, work,
    title_evidence)``. Three tiers, in order:

    1. exact normalized title/alias equality with a member — title evidence;
    2. hint == the collection's own title/aliases (base name after the
       directory's season token was stripped): widens to all series members,
       selected STRICTLY by the parsed season mode — not title evidence;
    3. the hint carries its own season/Stage marker and its stripped base
       matches the resource's OWN parked collection's primary title (equal or
       containing, e.g. the Latin "Initial D Fifth Stage" vs the bilingual
       collection "头文字D Initial D"; sibling shell collections are excluded —
       their lookalike Latin titles would make the verdict ambiguous): the
       marker selects the member by season — the directory's own Stage word
       IS title evidence, so remapping is allowed.
    """
    collection_ids = await _associated_collection_ids(db, resource)
    if not collection_ids:
        return None
    norm_hint = normalize_title(hint)
    coll_entries: list[tuple[Any, list[Any], list[Any]]] = []
    members: list[Any] = []
    base_match_series: list[Any] = []
    for cid in collection_ids:
        series_members = (
            await db.execute(select(TVSeries).where(TVSeries.collection_id == cid))
        ).scalars().all()
        movie_members = (
            await db.execute(select(Movie).where(Movie.collection_id == cid))
        ).scalars().all()
        collection = await db.get(WorkCollection, cid)
        coll_entries.append((collection, series_members, movie_members))
        members.extend(series_members)
        members.extend(movie_members)
        if collection is not None and _matches(collection, norm_hint):
            base_match_series.extend(series_members)
    picked = _pick_by_title(
        [w for w in members if _matches(w, norm_hint) and _form_allows(form, w)],
        season_hint,
    )
    if picked is not None:
        logger.info(
            "[cluster-bind] hint %r matched collection member %s (exact title)",
            hint[:80], picked.id,
        )
        return (_work_type_of(picked), picked, True)
    picked = _pick_unique(
        [w for w in base_match_series if _form_allows(form, w)], season_hint
    )
    if picked is not None:
        logger.info(
            "[cluster-bind] hint %r matched collection member %s "
            "(base-name widening, season-only)",
            hint[:80], picked[1].id,
        )
        return (picked[0], picked[1], False)
    if season_marker is not None:
        base = normalize_title(strip_season_from_title(hint))
        if len(base) >= 3:
            # The marker tier answers "this cluster is season N of THE pack's
            # collection" — so it considers ONLY the resource's own parked
            # collection. Linked sibling shells carry lookalike Latin primary
            # titles ("Initial D Battle Stage 2") that would otherwise match
            # the containment test and make the verdict ambiguous.
            parked = next(
                (
                    (collection, series_members)
                    for collection, series_members, _ in coll_entries
                    if collection is not None
                    and resource.collection_id is not None
                    and collection.id == resource.collection_id
                ),
                None,
            )
            if parked is not None:
                collection, series_members = parked
                name_hit = any(
                    (nt := normalize_title(n))
                    and (nt == base or (len(nt) >= 3 and (base in nt or nt in base)))
                    for n in (collection.title_cn, getattr(collection, "title_en", None))
                    if n
                )
                if name_hit:
                    base_tokens = set()
                    for n in (
                        collection.title_cn, getattr(collection, "title_en", None),
                    ):
                        base_tokens |= _name_tokens(strip_season_from_title(n))
                    picked = _pick_unique(
                        [
                            w for w in series_members
                            if _form_allows(form, w)
                            and _qualifier_compatible(base, base_tokens, w)
                        ],
                        season_marker,
                    )
                    if picked is not None:
                        logger.info(
                            "[cluster-bind] hint %r matched collection member %s "
                            "(season marker tier)",
                            hint[:80], picked[1].id,
                        )
                        return (picked[0], picked[1], True)
    return None


def _name_tokens(title: str | None) -> set[str]:
    """Whitespace tokens of a normalized title (empty for falsy input)."""
    norm = normalize_title(title)
    return set(norm.split()) if norm else set()


def _strip_numbered_marker(title: str) -> str:
    """Strip a trailing season suffix ONLY when it maps to a number.

    The suffix table also covers words it cannot number ("Final Stage" —
    deliberately unmapped): stripping those would erase the very qualifier
    the compatibility check needs ("頭文字D Final Stage" must not collapse
    to the base "頭文字D").
    """
    return (
        strip_season_from_title(title)
        if season_from_title(title) is not None
        else title
    )


def _qualifier_compatible(hint_base: str, collection_tokens: set[str], work: Any) -> bool:
    """Qualifier compatibility for the season marker tier (D2).

    The tier already requires the hint's season-stripped base to match the
    parked collection's title — but that only proves the IP, not the work:
    an "Initial D First Stage" hint (marker 1) must never select the
    collection's season-1 member when that member is "Initial D Battle
    Stage" or "頭文字D Final Stage" — same base, CONFLICTING qualifier.
    Rule: the candidate member must carry the same qualifier residue as the
    hint (both measured against the collection's name tokens; only NUMBERED
    markers are stripped): a base work (empty residue on both sides) always
    passes; a member whose titles carry extra qualifier tokens the hint
    does not share is rejected.
    """
    hint_residue = _name_tokens(hint_base) - collection_tokens
    member_base_tokens = collection_tokens | _name_tokens(hint_base)
    for title in _work_titles(work):
        residue = _name_tokens(_strip_numbered_marker(title)) - member_base_tokens
        if residue == hint_residue:
            return True
    return False


async def _match_local_fts(
    db: AsyncSession, hint: str, season_hint: int | None, form: str
) -> tuple[str, Any, bool] | None:
    """Library-wide FTS match at the shared auto-link confidence threshold.

    Both guards (form compatibility + base-name containment) must pass
    before a candidate may bind; the hit counts as title evidence.
    """
    from app.services import fts as fts_service
    from app.services.metadata_service import AUTO_LINK_THRESHOLD

    norm_hint = normalize_title(hint)
    candidates: list[tuple[int, Any, bool]] = []
    for model, search in (
        (TVSeries, fts_service.search_series_fts),
        (Movie, fts_service.search_movie_fts),
    ):
        ids = await search(db, hint, limit=20)
        if ids:
            rows = (
                await db.execute(select(model).where(model.id.in_(ids)))
            ).scalars().all()
        else:
            # The ngram AND-match cannot bridge separator punctuation
            # ("Initial D: Battle Stage" never matches an "initial d battle
            # stage" query), so an empty FTS result falls back to the same
            # full-table scan match_series_by_title / match_movie_by_title
            # use; the threshold + guards below do the filtering.
            rows = (await db.execute(select(model))).scalars().all()
        for work in rows:
            if (
                not _form_allows(form, work)
                or not _base_match(hint, work)
                or _sequel_ambiguous(hint, work)
            ):
                continue
            score = max(
                (similarity_score(norm_hint, t) for t in _work_titles(work)),
                default=0,
            )
            if score >= AUTO_LINK_THRESHOLD:
                candidates.append((score, work, _base_exact(hint, work)))
    if not candidates:
        return None
    # Exact base-name equality outranks mere containment: "Initial D Battle
    # Stage" (the movie) beats the "...Battle Stage 2" sibling series that
    # only contains the hint's base.
    exact = [c for c in candidates if c[2]]
    pool = exact or candidates
    best = max(score for score, _, _ in pool)
    top = [work for score, work, _ in pool if score == best]
    picked = _pick_by_title(top, season_hint)
    if picked is None:
        return None
    logger.info("[cluster-bind] hint %r matched local FTS %s score=%d", hint[:80], picked.id, best)
    return (_work_type_of(picked), picked, True)


async def _match_external(
    db: AsyncSession,
    hint: str,
    season_hint: int | None,
    channel: Channel | None,
    form: str,
) -> tuple[str, Any, bool] | None:
    """Channel-source title match, upserted through the standard entry points."""
    from app.services.metadata_agent import get_agent
    from app.services.metadata_sources import resolve_metadata_source

    source = resolve_metadata_source(getattr(channel, "metadata_source", None))
    try:
        meta = await get_agent().process_title_only(hint, source)
    except Exception as e:  # noqa: BLE001 — best-effort cluster resolution
        logger.warning("[cluster-bind] external match failed for %r: %s", hint[:80], e)
        return None
    if not meta or not meta.found or meta.ambiguous or not meta.matched_entity:
        return None
    if (
        (form == "tv" and meta.content_type == "movie")
        or (form == "movie" and meta.content_type == "tv")
    ):
        # Form guard: an episode-bearing cluster never binds a movie verdict
        # (and vice versa), even on an unambiguous external hit.
        logger.info(
            "[cluster-bind] external %r verdict %r rejected by cluster form %r",
            hint[:80], meta.content_type, form,
        )
        return None
    entity = dict(meta.matched_entity)
    if not any(entity.get(k) for k in _TITLE_ATTRS):
        return None

    from app.services.metadata_service import (
        create_or_update_movie_from_external,
        create_or_update_series_from_external,
        find_local_work_for_entity,
    )

    platform = str(entity.pop("_platform", "") or "")
    try:
        # F5: local-first reuse — a local hit only bags the new identity
        # (creator-wins); no duplicate row is created.
        local = await find_local_work_for_entity(db, entity, meta.content_type)
    except Exception as e:  # noqa: BLE001
        logger.warning("[cluster-bind] local reuse check failed for %r: %s", hint[:80], e)
        local = None
    if local is not None:
        return (
            "movie" if isinstance(local, Movie) else "series",
            local,
            True,
        )

    try:
        if meta.content_type == "movie":
            work = await create_or_update_movie_from_external(db, entity)
            return ("movie", work, True) if work is not None else None
        if meta.content_type == "tv":
            from app.services.bangumi_relations import classify_work_shape

            # F4: OVA/番外-form entities occupy the season-0 specials slot,
            # the same shape mapping the series graph applies by relation.
            if classify_work_shape(None, platform) == "specials":
                season_hint = 0
            work = await create_or_update_series_from_external(
                db, entity, season_hint=season_hint
            )
            # None = the season could not be placed — never guess one.
            return ("series", work, True) if work is not None else None
    except Exception as e:  # noqa: BLE001
        logger.warning("[cluster-bind] work upsert failed for %r: %s", hint[:80], e)
        return None
    return None


async def _ensure_auto_link(
    db: AsyncSession, resource_id: str, work_type: str, work_id: str
) -> bool:
    """Add a ``source="auto"`` work link unless one already exists."""
    column = ResourceWorkLink.series_id if work_type == "series" else ResourceWorkLink.movie_id
    existing = (
        await db.execute(
            select(ResourceWorkLink.id).where(
                ResourceWorkLink.resource_id == resource_id,
                column == work_id,
            )
        )
    ).first()
    if existing:
        return False
    db.add(ResourceWorkLink(
        resource_id=resource_id,
        series_id=work_id if work_type == "series" else None,
        movie_id=work_id if work_type == "movie" else None,
        source="auto",
    ))
    return True


async def bind_hint_clusters(
    db: AsyncSession, resource: FileResource, channel: Channel | None = None
) -> BindOutcome:
    """Bind each hint cluster's unbound ``auto`` rows to its resolved work.

    Returns a :class:`BindOutcome` (bound rows + season-remapped rows). No-op
    for non-batch resources, missing listings (no assignment rows / no hints)
    and clusters whose auto rows are all bound already.
    """
    none = BindOutcome(0, 0)
    if not getattr(resource, "is_batch", False):
        return none
    try:
        await db.refresh(resource, ["file_assignments"])
    except Exception:  # noqa: BLE001 — pending row edge
        return none
    rows = list(resource.file_assignments or [])
    clusters: dict[str, list[ResourceFileAssignment]] = {}
    hint_display: dict[str, str] = {}
    for row in rows:
        hint = (row.work_title_hint or "").strip()
        norm = normalize_title(hint)
        if not norm:
            continue
        clusters.setdefault(norm, []).append(row)
        hint_display.setdefault(norm, hint)
    if not clusters:
        return none

    from app.services.batch_content_analysis import (
        compute_season_ranges,
        sync_resource_collection,
    )

    # Reconcile pass: rows bound on title evidence mirror their work's season
    # identity, but a work's season_number can be CORRECTED after binding (the
    # bangumi series graph relocates an unmappable entry — e.g. "Final Stage"
    # created as s1 by franchise linking, later relocated to s6). Auto rows
    # already bound to such a work follow the relocation; llm/manual rows are
    # never touched.
    reconciled = 0
    work_cache: dict[str, Any] = {}
    for row in rows:
        if row.source != "auto" or not row.series_id:
            continue
        if row.series_id not in work_cache:
            work_cache[row.series_id] = await db.get(TVSeries, row.series_id)
        work = work_cache[row.series_id]
        if (
            work is not None
            and work.season_number is not None
            and row.season != work.season_number
        ):
            logger.debug(
                "[cluster-bind] %s: bound season %s follows relocated work "
                "season %s",
                row.file_path, row.season, work.season_number,
            )
            row.season = work.season_number
            reconciled += 1

    bound = 0
    remapped = 0
    bound_works: set[tuple[str, str]] = set()
    for norm, cluster_rows in clusters.items():
        targets = [
            r for r in cluster_rows
            if r.source == "auto" and not r.series_id and not r.movie_id
        ]
        if not targets:
            continue
        hint = hint_display[norm]
        # The hint's own season marker (Stage ordinals included) is title
        # evidence and outranks the pack-internal path numbering; only
        # marker-less hints fall back to the parsed season mode.
        marker = season_from_title(hint)
        season_hint = marker if marker is not None else _cluster_season(cluster_rows)
        form = _cluster_form(cluster_rows, hint)
        try:
            resolved = await _match_collection_members(
                db, resource, hint, season_hint, form, marker
            )
            if resolved is None:
                resolved = await _match_local_fts(db, hint, season_hint, form)
            if resolved is None:
                resolved = await _match_external(db, hint, season_hint, channel, form)
        except Exception as e:  # noqa: BLE001 — one cluster never blocks the rest
            logger.warning(
                "[cluster-bind] resolution failed for hint %r: %s", hint[:80], e,
            )
            continue
        if resolved is None:
            continue
        work_type, work, title_evidence = resolved
        logger.info(
            "[cluster-bind] hint %r resolved to %s:%s (title_evidence=%s, form=%r)",
            hint[:80], work_type, getattr(work, "id", None), title_evidence, form,
        )
        work_season = getattr(work, "season_number", None) if work_type == "series" else None
        bound_work = False
        for row in targets:
            if work_type == "series" and work_season is not None:
                if row.season is not None and row.season != work_season:
                    if not title_evidence:
                        logger.warning(
                            "[cluster-bind] %s: parsed season %s disagrees with "
                            "work season %s; row left unbound",
                            row.file_path, row.season, work_season,
                        )
                        continue
                    # Title evidence says which work this cluster IS: the
                    # pack-internal directory numbering (S03 = Fourth Stage)
                    # remaps onto the work's own season identity. Episode
                    # numbers are season-internal and stay unchanged.
                    logger.debug(
                        "[cluster-bind] %s: pack-internal season %s remapped to "
                        "work season %s (%r)",
                        row.file_path, row.season, work_season, hint[:80],
                    )
                    row.season = work_season
                    remapped += 1
                elif row.season is None:
                    row.season = work_season
                row.series_id = work.id
                row.movie_id = None
            else:
                row.movie_id = work.id
                row.series_id = None
                # Movies are seasonless: drop any pack-internal season token
                # the path parse picked up from a "[Season N]" directory tag.
                row.season = None
            bound += 1
            bound_work = True
        if bound_work:
            bound_works.add((work_type, work.id))
            await _ensure_auto_link(db, resource.id, work_type, work.id)

    total_remapped = remapped + reconciled
    if not bound and not reconciled:
        return BindOutcome(0, 0)
    resource.season_ranges = compute_season_ranges(resource)
    if bound and resource.batch_scope in (None, "season", "multi_season"):
        seasons = {s for s in (resource.batch_seasons or []) if s is not None}
        for work_type, work_id in bound_works:
            if work_type != "series":
                continue
            work = await db.get(TVSeries, work_id)
            if work is not None and work.season_number is not None:
                seasons.add(work.season_number)
        if seasons:
            resource.batch_seasons = sorted(seasons)
    if bound:
        await sync_resource_collection(db, resource)
    logger.info(
        "[cluster-bind] resource %s: bound %d rows across %d clusters "
        "(%d season-remapped, %d reconciled)",
        resource.id, bound, len(bound_works), remapped, reconciled,
    )
    return BindOutcome(bound, total_remapped)
