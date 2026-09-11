"""Franchise pack member linking (torrent content detection, final layer).

When ``maybe_inspect_torrent`` classifies a torrent as ``batch_scope ==
"franchise"`` (two or more distinct work clusters, e.g. "作品X TV" +
"作品X 剧场版"), this service resolves each cluster title in
``TorrentReport.work_titles`` to a real work row and groups them under a
``WorkCollection``:

1. Each cluster title is matched via the channel-configured
   ``UnifiedMetadataAgent.process_title_only`` (the same title-only matching
   used by manual search / eval — no new matching logic here). A hit
   (``found`` + ``matched_entity`` carrying a title) first claims any LOCAL
   work it identifies (identity-bag reverse lookup + normalized title match
   at the auto-link threshold — a hit only bags the new identity, creator-
   wins, never a duplicate row), then upserts through the existing
   ``create_or_update_series_from_external`` /
   ``create_or_update_movie_from_external`` path, so identity-bag /
   canonical-id / title convergence and idempotency come for free. TV-form
   members carrying an OVA/番外 platform are upserted with
   ``season_hint=0`` (the season-0 specials work — the same shape mapping
   the Bangumi series graph applies by relation label).
2. A ``WorkCollection`` is fetched or created immediately from the clean pack
   title (get-or-create key: ``external_source="franchise_pack"`` +
   normalized title equality — these collections carry no external id, so
   ``external_id`` stays NULL and the title is the identity). The pack title
   is the resource's cleaned main title or the raw title scrubbed of release
   tokens (batch markers, language/subtitle and quality/codec segments) —
   never the raw release string.  Child-member
   resolution is best-effort enrichment and never gates the parent
   collection. Member works are attached via ``collection_id``; a work
   already belonging to another collection is never stolen — but a
   single-member ``series_group`` shell or a same-base-name auto collection
   (series_group/franchise_pack, no manual edits) is ABSORBED (they are the
   same IP's duplicate groupings). A season work whose
   ``(collection, season)`` slot is already taken keeps its own shell and
   is linked to the resource instead of being attached.
3. The resource itself links to the collection (``resource.collection_id``)
   and the FK-exclusivity invariant is enforced — ``series_id`` /
   ``movie_id`` / ``audio_work_id`` are all cleared.

When every member fails to resolve, the resource still links to the parent
collection and satisfies the franchise-shape invariant; a later pass may
enrich its members.

The function does NOT commit: like ``maybe_inspect_torrent`` it runs inside
``fetch_service._process_resource_metadata``'s task session, whose own
commit persists the changes. Re-running it is idempotent (upserts +
get-or-create converge on the same rows).
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.models.series import TVSeries
from app.models.work_collection import WorkCollection
from app.services.collection_service import (
    try_absorb_same_name_collection,
    try_absorb_shell_collection,
)
from app.services.resource_parser import (
    extract_compilation_work_title,
    season_from_title,
    strip_season_from_title,
)
from app.services.text_normalizer import normalize_title

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.channel import Channel
    from app.models.file_resource import FileResource
    from app.models.movie import Movie
    from app.models.series import TVSeries
    from app.services.torrent_inspect import TorrentReport

logger = logging.getLogger(__name__)

# ``external_source`` for collections created from franchise torrent packs.
# ``external_id`` stays NULL (no upstream identity) — get-or-create keys off
# the normalized title instead; the (source, external_id) unique constraint
# allows multiple NULLs on both SQLite/Turso and PostgreSQL.
FRANCHISE_PACK_SOURCE = "franchise_pack"

_TITLE_KEYS = ("title_cn", "title_en", "original_title")

# Pack-title cleanup, mirroring the cluster-title normalization in
# torrent_inspect: bracketed release tags are decoration, not the work name.
_BRACKET_BLOCK_RE = re.compile(r"[\[【\(（][^\]】\)）]*[\]】\)）]")

# Multi-token release markers removed BEFORE tokenization (they span words):
# whole-run season markers (全六季 / 1-6季 / 第3季), season tokens
# (Season 2 / S01 / S01-S06) and the alt-title tail after " / ".
_PACK_SEASON_MARKER_RE = re.compile(
    r"全[一二三四五六七八九十\d]{1,2}季"
    r"|\d{1,2}\s*[-~–～〜]\s*\d{1,2}\s*季"
    r"|第[一二三四五六七八九十\d]{1,2}\s*季"
    r"|Season\s*\d{1,2}(?:\s*[-~–]\s*(?:Season\s*)?\d{1,2})?"
    r"|S\d{1,2}(?:\s*[-~–]\s*S?\d{1,2})?",
    re.IGNORECASE,
)
# Single-token release info dropped from the tokenized title: audio/subtitle
# languages, subtitle form, batch keywords and quality/codec/source tokens.
_PACK_DROP_TOKEN_RE = re.compile(
    r"^(?:"
    r"[中日英粤韩法德俄][语語文]"
    r"|国[语語]|粤[语語配]|国配|台配|日中|中英|中日"
    r"|[中日英粤韩国]{0,2}双语|[中日英粤韩国]{0,2}雙語"
    r"|(?:[中日英粤韩][语語文]?|简|繁|簡|简繁|簡繁|繁简|繁簡|内封|內封|内嵌|外挂|外掛|双语|雙語|硬|软|軟)*字幕"
    r"|(?:内封|內封|内嵌|外挂|外掛|硬|软|軟)?[中英日]字"
    r"|全集|全季|合集|完整|完结|完結|打包|整理搬运|合集整理|资源整合|全集整理"
    r"|Batch|Complete(?:\s*Series)?|Fin"
    r"|\d{3,4}p|\d+[kK]"
    r"|BD|BD-?Rip|BDMV|BDRemux|BD-?BOX|Blu-?ray|WEB-?DL|WEBRip|Web|HDTV|DVDRip|DVD|TVRip"
    r"|x26[45]|[hH]\.?26[45]|HEVC|AVC|AV1|VP9|XviD|DivX"
    r"|AAC|AC3|AC-?3|EAC3|E-?AC-?3|FLAC|DTS(?:-?HD)?|Opus|MP3|TrueHD|Atmos|LPCM"
    r"|Hi10P|10bit|8bit|HDR(?:10\+?)?|SDR|DV|DoVi"
    r"|Ma10p|Mini-?HD|REMUX|Remux|RAW|VCB-?S"
    r")$",
    re.IGNORECASE,
)
_TOKEN_SPLIT_RE = re.compile(r"[._\s　]+")


def _clean_pack_name(raw: str) -> str:
    """Best-effort work name out of a raw franchise-pack release title.

    Fansub pack titles pile release metadata between dot/space segments
    ("头文字D.全六季.日语.英文字幕.Initial D"): bracket blocks, whole-run
    season markers and single-token language/subtitle/quality/codec noise are
    dropped so the collection is named after the WORK, never the release.
    Falls back to the bracket-stripped input when every segment is dropped.
    """
    text = _BRACKET_BLOCK_RE.sub(" ", raw)
    text = re.split(r"\s*/\s*", text, maxsplit=1)[0]
    text = _PACK_SEASON_MARKER_RE.sub(" ", text)
    parts = [
        p for p in (_TOKEN_SPLIT_RE.split(text))
        if p and not _PACK_DROP_TOKEN_RE.fullmatch(p)
    ]
    cleaned = " ".join(parts)
    cleaned = strip_season_from_title(cleaned).strip(" -_.·　")
    return cleaned or _BRACKET_BLOCK_RE.sub(" ", raw).strip()


def is_franchise_resource(resource: FileResource) -> bool:
    """Whether the resource uses collection-owned franchise semantics."""
    return bool(
        getattr(resource, "is_batch", False)
        and getattr(resource, "batch_scope", None) == "franchise"
    )


def enforce_franchise_resource_invariant(resource: FileResource) -> bool:
    """Clear flat work FKs from a franchise-shaped resource.

    Returns whether anything changed.  This is intentionally reusable at the
    end of the whole metadata pipeline because cache/title-index shortcuts do
    not pass through the repository's normal write-back branch.
    """
    if not is_franchise_resource(resource):
        return False
    changed = bool(
        getattr(resource, "series_id", None)
        or getattr(resource, "movie_id", None)
        or getattr(resource, "audio_work_id", None)
    )
    resource.series_id = None
    resource.movie_id = None
    resource.audio_work_id = None
    return changed


def _pack_title(resource: FileResource) -> str:
    """Display title for the pack's WorkCollection.

    Prefers the resource's cleaned main title (``search_title``/``title_cn``);
    falls back to the compilation-style extraction of the raw title
    (``extract_compilation_work_title`` handles "[整理搬运] 作品：TV+剧场版…"
    shapes), then to release-token cleanup of the raw title (F3a). The
    pre-parser's ``search_title`` may itself still carry dot-separated release
    tokens ("头文字D.全六季.日语.英文字幕.Initial D"), so every base passes
    through the same cleanup — it is idempotent on already-clean names.
    """
    base = (resource.search_title or resource.title_cn or "").strip()
    if base:
        base = _clean_pack_name(base)
    if not base:
        base = extract_compilation_work_title(resource.title_raw) or ""
    if not base and resource.title_raw:
        base = _clean_pack_name(resource.title_raw)
    t = _BRACKET_BLOCK_RE.sub(" ", base)
    t = re.sub(r"\s+", " ", t).strip(" -_.·　")
    return (t or base or "franchise pack")[:512]


async def _ensure_auto_link(
    db: AsyncSession, resource_id: str, work_type: str, work_id: str
) -> bool:
    """Add a ``source="auto"`` resource↔work link unless one already exists.

    Used for members kept in their own shell collection (the season slot in
    the pack's collection is occupied): the link is what makes the shell
    work's collection reachable to the later cluster-binding pass.
    """
    from app.models.resource_work_link import ResourceWorkLink

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


async def _get_or_create_franchise_collection(
    db: AsyncSession, pack_title: str
) -> WorkCollection:
    """Get-or-create a franchise-pack WorkCollection by normalized title."""
    norm = normalize_title(pack_title)
    rows = (
        await db.execute(
            select(WorkCollection).where(
                WorkCollection.external_source == FRANCHISE_PACK_SOURCE
            )
        )
    ).scalars().all()
    for coll in rows:
        if normalize_title(coll.title_cn) == norm:
            return coll
        if coll.title_en and normalize_title(coll.title_en) == norm:
            return coll
    collection = WorkCollection(
        title_cn=pack_title,
        external_id=None,
        external_source=FRANCHISE_PACK_SOURCE,
    )
    db.add(collection)
    await db.flush()
    return collection


async def _resolve_member(
    db: AsyncSession, agent, source: str, title: str,
    collection_base_names: frozenset[str] | None = None,
) -> tuple[TVSeries | Movie, bool] | None:
    """Resolve one cluster title to a work row, or None on any failure.

    Returns ``(work, attach_to_pack)``. Local-first (F5): before the matched
    entity is upserted, the local library gets the first claim (identity-bag
    reverse lookup + guarded normalized title match); a hit only bags the
    new identity — no duplicate row. Shape classification is shared with the
    Bangumi series graph (F4): movie form → Movie upsert; OVA/番外 platform
    → season-0 specials work; anything else TV → a per-season work.

    ``attach_to_pack`` is False only for TV members with NO season evidence
    whose title is a VARIANT of the pack's own base name ("頭文字D Final
    Stage", "Initial D Battle Stage" — same name family, no title/Stage
    marker pinning a season): they must never take the main collection's
    season-1 slot by default, or the true first season is stranded in a
    shell and every season-marked cluster binds onto the squatter (D1).
    Marker-less members whose title belongs to a DIFFERENT work ("作品A" in
    a "某大IP" pack) attach normally — the (collection, season) occupant
    guard stays the backstop for genuine slot conflicts.
    """
    try:
        meta = await agent.process_title_only(title, source)
    except Exception as e:
        logger.warning("[franchise] member match failed for %r: %s", title[:80], e)
        return None
    if not meta or not meta.found or not meta.matched_entity:
        logger.info("[franchise] member %r not found (source=%s)", title[:80], source)
        return None
    entity = dict(meta.matched_entity)
    if not any(entity.get(k) for k in _TITLE_KEYS):
        logger.warning("[franchise] member %r matched entity has no title; skipped", title[:80])
        return None

    from app.services.metadata_service import (
        create_or_update_movie_from_external,
        create_or_update_series_from_external,
        find_local_work_for_entity,
    )

    platform = str(entity.pop("_platform", "") or "")
    attach = True
    if meta.content_type == "tv":
        from app.services.bangumi_relations import classify_work_shape

        if classify_work_shape(None, platform) == "season":
            marked = any(
                season_from_title(t) is not None
                for t in (entity.get("title_cn"), entity.get("title_en"),
                          entity.get("original_title"))
                if t
            )
            if not marked and not _is_pack_base_name(entity, collection_base_names):
                attach = False
    try:
        local = await find_local_work_for_entity(db, entity, meta.content_type)
    except Exception as e:
        logger.warning("[franchise] local reuse check failed for %r: %s", title[:80], e)
        local = None
    if local is not None:
        return local, attach

    try:
        movie_form = meta.content_type == "movie" or (
            meta.content_type == "tv"
            and any(
                _MOVIE_FORM_TITLE_RE.search(t)
                for t in (entity.get("title_cn"), entity.get("title_en"),
                          entity.get("original_title"))
                if t
            )
        )
        if movie_form:
            work = await create_or_update_movie_from_external(db, entity)
            return (work, True) if work is not None else None
        if meta.content_type == "tv":
            from app.services.bangumi_relations import classify_work_shape

            shape = classify_work_shape(None, platform)
            # 番外/OVA-form members occupy the collection's season-0 slot
            # (the graph path applies the same mapping via the relation
            # label); ordinary TV members keep the upsert's own season
            # resolution (no hint — never a guess).
            season_hint = 0 if shape == "specials" else None
            work = await create_or_update_series_from_external(
                db, entity, season_hint=season_hint
            )
            if work is None:
                return None
            return work, attach
    except Exception as e:
        logger.warning("[franchise] member upsert failed for %r: %s", title[:80], e)
        return None
    logger.info(
        "[franchise] member %r resolved to non-tv/movie content_type=%r; skipped",
        title[:80], meta.content_type,
    )
    return None


def _shares_name_token(a: str, b: str) -> bool:
    """Whether two normalized names share a distinctive token (len ≥ 2, not a
    stopword) — the IP-family signal distinguishing a season-less VARIANT of
    the pack's work ("Initial D Battle Stage" ~ "头文字D Initial D") from a
    DIFFERENT work of the franchise ("作品A" ~ "某大IP")."""
    tokens_a = {t for t in a.split() if len(t) >= 2} - _NAME_TOKEN_STOPWORDS
    tokens_b = {t for t in b.split() if len(t) >= 2} - _NAME_TOKEN_STOPWORDS
    return bool(tokens_a & tokens_b)


_NAME_TOKEN_STOPWORDS = frozenset({
    "the", "a", "an", "no", "season", "movie", "film", "part",
})


# A tv-typed entity whose title is explicitly movie-form ("作品A 剧场版") is
# a movie member: web-fallback/stub entities frequently lack platform
# evidence, and the 剧场版 title marker is the deterministic movie-form
# signal (TV series never carry it in their name).
_MOVIE_FORM_TITLE_RE = re.compile(r"剧场版|劇場版")


def _is_pack_base_name(
    entity: dict, collection_base_names: frozenset[str] | None
) -> bool:
    """Whether a marker-less TV member may attach to the pack collection.

    Compared RAW (normalized, NOT season-stripped — this function is only
    consulted for marker-less entities, where the suffix table's
    "Final Stage"-style words are qualifiers, not seasons). Three cases:

    - the title shares NO name token with the collection's base names
      ("作品A" vs "某大IP") — a DIFFERENT work of the franchise, which
      attaches normally (slot conflicts are handled by the occupant guard);
    - same name family and the title IS a base name (equal to or contained
      in a collection base name — "头文字D" / "Initial D" inside the
      bilingual "头文字D Initial D") — the true base entry, allowed to take
      the (collection, s1) slot;
    - same name family but a decorated variant ("Initial D Battle Stage",
      "頭文字D Final Stage") — NOT attachable: the caller keeps it in its
      own named shell (D1).

    Unknown collection base names (None/empty) cannot establish a family
    relation — normal attach.
    """
    if not collection_base_names:
        return True
    for title in (
        entity.get("title_cn"),
        entity.get("title_en"),
        entity.get("original_title"),
    ):
        raw = normalize_title(title)
        if len(raw) < 2:
            continue
        for coll_base in collection_base_names:
            if not _shares_name_token(raw, coll_base):
                return True  # a different work of the franchise
            if raw == coll_base or (len(coll_base) >= 2 and raw in coll_base):
                return True  # the base entry itself
    return False


async def link_franchise_pack(
    db: AsyncSession,
    resource: FileResource,
    report: TorrentReport,
    channel: Channel,
) -> None:
    """Link a franchise-pack resource to a WorkCollection of member works.

    See the module docstring for the full contract.  The parent collection is
    deterministic from the release title and is linked before any network
    member lookup, so a timeout or an unhelpful ``TV/OVA/Film`` directory name
    cannot create a spurious metadata confirmation.
    """
    collection = await _get_or_create_franchise_collection(db, _pack_title(resource))
    resource.collection_id = collection.id
    # FK-exclusivity invariant: a collection-linked franchise resource carries
    # no flat per-work FK, even if an earlier partial pass left one behind.
    enforce_franchise_resource_invariant(resource)

    if not report.work_titles:
        logger.warning(
            "[franchise] resource %s: linked parent collection %r but report "
            "has no member titles",
            resource.id, collection.title_cn,
        )
        return

    # Local import at call time so tests can patch
    # ``app.services.metadata_agent.get_agent`` (same pattern as fetch_service).
    from app.services.metadata_agent import get_agent
    from app.services.metadata_sources import resolve_metadata_source

    source = resolve_metadata_source(getattr(channel, "metadata_source", None))
    agent = get_agent()

    from app.services.collection_service import _collection_base_names

    collection_base_names = frozenset(_collection_base_names(collection))

    works: list[tuple[TVSeries | Movie, bool]] = []
    seen: set[tuple[str, str]] = set()
    for title in report.work_titles:
        resolved = await _resolve_member(
            db, agent, source, title, collection_base_names
        )
        if resolved is None:
            continue
        work, attach = resolved
        key = (type(work).__name__, work.id)
        if key not in seen:
            seen.add(key)
            works.append((work, attach))

    if not works:
        logger.warning(
            "[franchise] resource %s: all %d members failed to resolve; "
            "keeping parent collection %r",
            resource.id, len(report.work_titles), collection.title_cn,
        )
        return

    for work, attach in works:
        if work.collection_id == collection.id:
            continue
        if isinstance(work, TVSeries) and not attach:
            # Qualified-but-unmarked member ("Final Stage", "Battle Stage 2"):
            # its season is indeterminate — it keeps its own shell collection
            # and is only linked; the series graph may place it later.
            await _ensure_auto_link(db, resource.id, "series", work.id)
            continue
        if isinstance(work, TVSeries):
            # (collection, season) is application-unique (F4): when the slot
            # is taken — e.g. a second 番外/specials work once season 0 is
            # occupied — the work keeps its own shell collection and is
            # linked to the resource instead (cross-collection links legal).
            occupant = (
                await db.execute(
                    select(TVSeries).where(
                        TVSeries.collection_id == collection.id,
                        TVSeries.season_number == work.season_number,
                    )
                )
            ).scalars().first()
            if occupant is not None and occupant.id != work.id:
                logger.info(
                    "[franchise] work %s (season %s) slot occupied in %r; "
                    "kept in its shell collection and linked",
                    work.id, work.season_number, collection.title_cn,
                )
                await _ensure_auto_link(db, resource.id, "series", work.id)
                continue
        if work.collection_id:
            # Per-season model (P3): a TV member upsert auto-creates a
            # ``series_group`` shell collection for its series. When that
            # shell contains ONLY this work, the franchise pack's collection
            # is the stronger IP grouping — absorb it (bag rows move over,
            # the empty shell is removed) instead of refusing the attach.
            # F3b: a MULTI-member auto collection with the same normalized
            # base name is the same IP's duplicate — it is absorbed whole
            # (season-colliding members stay behind), manual edits aside.
            if await try_absorb_shell_collection(db, collection, work):
                continue
            if await try_absorb_same_name_collection(db, collection, work):
                continue
            logger.warning(
                "[franchise] work %s already belongs to collection %s; "
                "not re-attaching to %s",
                work.id, work.collection_id, collection.id,
            )
            continue
        work.collection_id = collection.id

    logger.info(
        "[franchise] resource %s linked to collection %r (%d/%d members resolved)",
        resource.id, collection.title_cn, len(works), len(report.work_titles),
    )


# ---------------------------------------------------------------------------
# C4 — same-resource same-date movie dedup
# ---------------------------------------------------------------------------

# Survivor preference for movie dedup: the channel's metadata source first
# (a main-source identity outranks web-fallback ones), then registry order.
_SOURCE_RANK = {
    "bangumi": 10, "tmdb": 20, "wikipedia": 30, "douban": 40,
    "mal": 50, "anilist": 60, "imdb": 70, "exa_web": 90, "llm_search": 95,
}


def _movie_base_names(work) -> set[str]:
    out: set[str] = set()
    for title in (work.title_cn, work.title_en, work.original_title, *(work.aliases or [])):
        if not title:
            continue
        for cand in {title, strip_season_from_title(title)}:
            norm = normalize_title(cand)
            if norm:
                out.add(norm)
    return out


def _same_ip_movies(a, b) -> bool:
    """Same-IP test for the movie dedup: shared collection, or Round-A
    base-name family (equal / mutually containing, 2-char floor)."""
    if a.collection_id and a.collection_id == b.collection_id:
        return True
    for base_a in _movie_base_names(a):
        for base_b in _movie_base_names(b):
            if base_a == base_b:
                return True
            if min(len(base_a), len(base_b)) >= 2 and (
                base_a in base_b or base_b in base_a
            ):
                return True
    return False


async def _movie_year(db: AsyncSession, resource_id: str, work) -> int | None:
    """Date evidence for one movie work: its ``release_date`` year, else the
    single distinct ``[YYYY]`` year across the file paths bound to it (web-
    fallback rows carry no content fields, but the release's directories
    carry the year)."""
    if work.release_date is not None:
        return work.release_date.year
    from app.models.resource_file_assignment import ResourceFileAssignment
    from app.services.resource_parser import extract_title_year

    years = set()
    rows = (await db.execute(
        select(ResourceFileAssignment.file_path).where(
            ResourceFileAssignment.resource_id == resource_id,
            ResourceFileAssignment.movie_id == work.id,
        )
    )).scalars().all()
    for path in rows:
        year = extract_title_year(path or "")
        if year is not None:
            years.add(year)
    return years.pop() if len(years) == 1 else None


async def dedupe_resource_movies(
    db: AsyncSession, resource, channel=None
) -> int:
    """Deterministic same-resource movie dedup (C4).

    Member resolution (web fallback, English titles) and the Bangumi series
    graph (Japanese titles) can each create a row for the SAME film — no
    shared title bridges them. Two movie works linked to the same resource
    merge when they are same-IP (shared collection or base-name family) AND
    carry identical date evidence (equal ``release_date`` years; a row
    without one borrows the ``[YYYY]`` year from its bound file paths). No
    date evidence → never merged.

    The survivor is the channel-source row (a main-source identity outranks
    web-fallback/exa_web/mal rows); creator-wins keeps its primary id. The
    shared merge machine re-points child rows (file assignments / work links
    / decisions / agent works), unions the identity bags and deletes the
    duplicate. Returns the number of rows merged away.
    """
    from app.models.movie import Movie
    from app.models.resource_work_link import ResourceWorkLink

    movie_ids = set(
        (await db.execute(
            select(ResourceWorkLink.movie_id).where(
                ResourceWorkLink.resource_id == resource.id,
                ResourceWorkLink.movie_id.is_not(None),
            )
        )).scalars().all()
    )
    if resource.movie_id:
        movie_ids.add(resource.movie_id)
    if len(movie_ids) < 2:
        return 0
    movies = []
    for mid in movie_ids:
        m = await db.get(Movie, mid)
        if m is not None:
            movies.append(m)
    if len(movies) < 2:
        return 0

    channel_source = None
    if channel is not None:
        from app.services.metadata_sources import resolve_metadata_source

        channel_source = resolve_metadata_source(
            getattr(channel, "metadata_source", None)
        )

    def _rank(work) -> tuple[int, str]:
        source = work.external_source or ""
        if channel_source and source == channel_source:
            return (0, work.id)
        return (_SOURCE_RANK.get(source, 80), work.id)

    years = {m.id: await _movie_year(db, resource.id, m) for m in movies}
    by_year: dict[int, list] = {}
    for m in movies:
        year = years[m.id]
        if year is not None:
            by_year.setdefault(year, []).append(m)

    from app.services.metadata_dedup import DedupReport, _merge_movie_group

    merged = 0
    for group in by_year.values():
        group.sort(key=_rank)
        while len(group) >= 2:
            survivor = group[0]
            partner = next(
                (m for m in group[1:] if _same_ip_movies(survivor, m)), None,
            )
            if partner is None:
                break
            rows = [survivor, partner]
            # creator-wins: the merge machine re-picks a canonical primary;
            # restore the survivor's own identity afterwards.
            keep_id, keep_source = survivor.external_id, survivor.external_source
            report = DedupReport()
            await _merge_movie_group(db, rows, report, survivor=survivor)
            survivor.external_id = keep_id
            survivor.external_source = keep_source
            merged += 1
            group = [survivor, *[m for m in group[1:] if m is not partner]]
            logger.info(
                "[franchise] deduped same-date movie rows for resource %s: "
                "kept %s (%s:%s), merged %s",
                resource.id, survivor.id, keep_source, keep_id, partner.id,
            )
    return merged
