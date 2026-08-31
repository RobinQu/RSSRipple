"""Batch content analysis — LLM refinement of torrent file listings.

Layered on top of the deterministic ``torrent_inspect`` classification. The
deterministic layer already resolves scope (season / multi_season /
franchise) and per-season episode ranges from path markers for free; this
module covers what paths cannot express:

- Distinguishing a pure-movie pack (``batch_scope="movies"``) from a
  TV+movie mixed pack within the franchise verdict.
- Mapping each cluster's files onto concrete works: movie clusters go
  through the channel metadata source (``process_title_only`` →
  ``create_or_update_movie_from_external``) and bind rows; TV clusters
  inside mixed packs stay hint-only for manual binding in the wizard.

Gating (confirmed design): LLM fires only when the deterministic layer
cannot finish the job —

1. ``batch_scope == "franchise"`` (multi-work packs), or
2. ``is_batch`` and a large share of main video files have no parseable
   episode numbers.

Season/multi-season packs never burn an LLM call. All failures degrade
silently to the deterministic result; rows marked ``source="manual"``
are never touched.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.models.episode import Episode
from app.models.movie import Movie
from app.models.resource_file_assignment import ResourceFileAssignment
from app.models.resource_work_link import ResourceWorkLink
from app.models.series import TVSeries
from app.services.resource_parser import extract_subtitle_group_from_filename
from app.services.runtime_config import runtime_config
from app.services.subtitle_groups import join_legacy_subtitle_group, normalize_subtitle_groups
from app.services.torrent_inspect import TorrentReport

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.channel import Channel
    from app.models.file_resource import FileResource

logger = logging.getLogger(__name__)

# Share of main video files without parseable episode numbers above which a
# batch qualifies for LLM refinement even outside the franchise scope.
_UNPARSED_RATIO_THRESHOLD = 0.5

# Hard cap on listing entries sent to the LLM (path + size each). Beyond
# this the prompt would be useless anyway; the deterministic layer result
# stands alone.
_MAX_LISTING_ENTRIES = 400


def llm_refinement_needed(report: TorrentReport, batch_scope: str | None) -> bool:
    """True when the report should be refined through the LLM layer."""
    if not runtime_config.llm_api_key:
        return False
    if batch_scope == "franchise":
        return True
    return (
        report.is_batch
        and report.video_file_count >= 2
        and report.unparsed_ratio >= _UNPARSED_RATIO_THRESHOLD
    )


# ---------------------------------------------------------------------------
# Deterministic assignment write-backs
# ---------------------------------------------------------------------------

def apply_auto_assignments(resource: FileResource, report: TorrentReport) -> None:
    """Upsert ``ResourceFileAssignment`` rows for every main video file.

    Rows are keyed by ``(resource_id, file_path)``:

    - new paths get a row with ``season`` / ``episode_start`` / ``episode_end``
      from the path parses (episode start == end for single-episode files)
      and ``work_title_hint`` from the top-level cluster membership;
    - existing ``auto`` rows are refreshed in place;
    - existing ``llm`` / ``manual`` rows keep their work binding and source
      (only stale placement fields are left untouched too — provenance wins);
    - previous ``auto`` rows whose path vanished from the listing are removed
      (``manual`` / ``llm`` rows are kept).

    Applies to every report with main-video parses. Single TV/movie releases
    need the same durable file identity as batches for downstream organize.
    """
    if not report.file_parses:
        return

    # Title parsing remains authoritative.  When it yielded no group, use the
    # release-name convention found in overseas filenames (``...-GROUP.mkv``).
    # Require one unambiguous group across the torrent so mixed packs cannot
    # accidentally stamp the resource with one constituent release's group.
    if not getattr(resource, "subtitle_group", None) and not getattr(resource, "subtitle_groups", None):
        filename_groups: dict[str, str] = {}
        for fp in report.file_parses:
            group = extract_subtitle_group_from_filename(fp.get("path"))
            if group:
                filename_groups.setdefault(group.casefold(), group)
        if len(filename_groups) == 1:
            group = next(iter(filename_groups.values()))
            resource.subtitle_groups = normalize_subtitle_groups(group)
            resource.subtitle_group = join_legacy_subtitle_group(resource.subtitle_groups)
            resource.subtitle_groups_source = "heuristic"

    hint_by_path: dict[str, str] = {}
    for cluster in report.clusters:
        for p in cluster.files:
            hint_by_path[p] = cluster.title

    existing = {a.file_path: a for a in resource.file_assignments}
    seen_paths: set[str] = set()
    for fp in report.file_parses:
        path = fp["path"]
        seen_paths.add(path)
        row = existing.get(path)
        if row is not None and row.source in ("llm", "manual"):
            continue
        if row is None:
            row = ResourceFileAssignment(
                resource_id=resource.id,
                file_path=path,
                source="auto",
            )
            resource.file_assignments.append(row)
        row.file_size = fp.get("size")
        row.work_title_hint = hint_by_path.get(path) or row.work_title_hint
        row.season = fp.get("season")
        episode = fp.get("episode")
        row.episode_start = episode
        row.episode_end = episode
        row.source = "auto"

    for path, row in list(existing.items()):
        if path not in seen_paths and row.source == "auto":
            resource.file_assignments.remove(row)


def compute_season_ranges(resource: FileResource) -> list[dict] | None:
    """Recompute ``resource.season_ranges`` from the current assignments.

    Flat per-season min/max across all placements (work attribution lives on
    the assignments themselves). Seasons are only recorded when known.
    """
    by_season: dict[int, list[int]] = {}
    for a in resource.file_assignments:
        if a.season is None:
            continue
        eps = [v for v in (a.episode_start, a.episode_end) if v is not None]
        if not eps:
            continue
        by_season.setdefault(a.season, []).extend(eps)
    ranges = [
        {"season": s, "episode_start": min(eps), "episode_end": max(eps)}
        for s, eps in sorted(by_season.items())
    ]
    return ranges or None


# ---------------------------------------------------------------------------
# LLM refinement
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """You are a media torrent content analyzer. You receive an RSS \
release title and the file listing of one torrent. Classify what the torrent \
bundles and map every main video file to its work.

Return ONLY a JSON object with this exact shape:
{
  "scope": "<see below>",
  "works": [
    {
      "candidate_key": "<exact supplied candidate_key, or null>",
      "title": "<clean work title, no release tags>",
      "content_type": "<tv | movie>",
      "files": [
        {"path": "<exact path from the listing>", "season": <int|null>, \
"episode": <int|null>, "episode_start": <int|null>, "episode_end": <int|null>}
      ]
    }
  ]
}

Rules:
- "scope" is one of: "movies" (two or more distinct MOVIES bundled together),
  "mixed" (TV episodes AND movies in one torrent), "franchise" (multiple \
distinct TV series bundled together).
- Every video file path you output MUST be copied EXACTLY from the listing and \
appear exactly once across all works' files.
- When candidate works are supplied, reuse their exact candidate_key whenever \
the file belongs to one of them. Never invent or alter a candidate_key; title \
is only a display hint and candidate_key is the identity.
- Ignore sample/trailer/extra files that were excluded from the listing.
- For tv files, set season whenever it is determinable from the file name or \
its directory; use null only when truly unknown.
- For an ordinary file containing exactly one TV episode, set "episode" and \
leave episode_start/episode_end null. This is the normal case.
- Use episode_start and episode_end only when one physical video file really \
contains a consecutive multi-episode range (for example E01-E02 burned into \
one file); then leave episode null. Never use a range merely because the \
torrent itself is a season pack.
- Clean titles: drop bracketed release tags, resolution/codec tokens, subtitle \
group names, season/episode markers."""


def _build_listing_text(files: list[dict]) -> str:
    lines = []
    for f in files[:_MAX_LISTING_ENTRIES]:
        size_mb = (f.get("size") or 0) / (1024 * 1024)
        lines.append(f"{f['name']} ({size_mb:.0f}MB)")
    return "\n".join(lines)


def _parse_llm_json(text: str) -> dict[str, Any]:
    """Parse the analyzer's JSON reply, tolerating fences and prose."""
    text = text.strip()
    if text.startswith("```"):
        first_nl = text.index("\n") if "\n" in text else 3
        text = text[first_nl + 1:]
    if text.endswith("```"):
        text = text[:-3]
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        text = match.group(0)
    return json.loads(text)


async def analyze_listing(
    resource_title: str, files: list[dict], anchor_titles: list[str],
    candidate_works: list[dict] | None = None,
) -> dict[str, Any] | None:
    """One LLM call classifying the listing. Returns None on any failure.

    Uses a dedicated short-timeout client (20s) instead of the shared
    ``call_llm`` helper (120s): the deterministic layer already covers the
    wizard, so a slow/unreachable LLM must never stall the analyze-batch
    request long enough for browsers/proxies to cut the connection.
    """
    if not runtime_config.llm_api_key:
        return None

    anchors = ""
    if anchor_titles:
        anchors = (
            "\nDeterministic top-level clusters detected by path analysis "
            "(hints, verify them):\n" + "\n".join(f"- {t}" for t in anchor_titles)
        )
    candidates = (
        "\nCandidate works (reuse exact candidate_key):\n"
        + json.dumps(candidate_works, ensure_ascii=False)
        if candidate_works else ""
    )
    user = (
        f"Release title: {resource_title}\n\n"
        f"File listing ({min(len(files), _MAX_LISTING_ENTRIES)} entries):\n"
        f"{_build_listing_text(files)}{anchors}{candidates}"
    )
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]
    try:
        import httpx as _httpx
        from openai import AsyncOpenAI as _AsyncOpenAI

        client = _AsyncOpenAI(
            api_key=runtime_config.llm_api_key,
            base_url=runtime_config.llm_base_url,
            timeout=_httpx.Timeout(20.0, connect=5.0),
        )
        response = await client.chat.completions.create(
            model=runtime_config.llm_model,
            messages=messages,
            temperature=0.1,
            timeout=20,
            extra_body={"enable_thinking": runtime_config.llm_enable_thinking},
        )
        raw = response.choices[0].message.content or ""
        data = _parse_llm_json(raw)
    except Exception as e:  # noqa: BLE001 — best-effort refinement
        logger.warning("[batch] LLM analysis failed (deterministic layer still returned): %s", e)
        return None
    if not isinstance(data, dict) or not isinstance(data.get("works"), list):
        return None
    return data


async def analyze_listing_stream(
    resource_title: str, files: list[dict], anchor_titles: list[str],
    candidate_works: list[dict] | None = None,
):
    """Stream the optional LLM's raw output, then yield its parsed result.

    Events are ``("delta", text)``, ``("result", dict|None)`` and
    ``("error", message)``.  The caller already owns the deterministic
    report, so every LLM failure is a visible, non-fatal degradation.
    """
    if not runtime_config.llm_api_key:
        yield "result", None
        return
    anchors = ""
    if anchor_titles:
        anchors = (
            "\nDeterministic top-level clusters detected by path analysis "
            "(hints, verify them):\n" + "\n".join(f"- {t}" for t in anchor_titles)
        )
    candidates = (
        "\nCandidate works (reuse exact candidate_key):\n"
        + json.dumps(candidate_works, ensure_ascii=False)
        if candidate_works else ""
    )
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": (
            f"Release title: {resource_title}\n\n"
            f"File listing ({min(len(files), _MAX_LISTING_ENTRIES)} entries):\n"
            f"{_build_listing_text(files)}{anchors}{candidates}"
        )},
    ]
    raw_parts: list[str] = []
    try:
        import httpx as _httpx
        from openai import AsyncOpenAI as _AsyncOpenAI

        client = _AsyncOpenAI(
            api_key=runtime_config.llm_api_key,
            base_url=runtime_config.llm_base_url,
            timeout=_httpx.Timeout(60.0, connect=5.0),
        )
        stream = await client.chat.completions.create(
            model=runtime_config.llm_model,
            messages=messages,
            temperature=0.1,
            stream=True,
            extra_body={"enable_thinking": runtime_config.llm_enable_thinking},
        )
        async for chunk in stream:
            delta = chunk.choices[0].delta.content or ""
            if delta:
                raw_parts.append(delta)
                yield "delta", delta
        data = _parse_llm_json("".join(raw_parts))
        if not isinstance(data, dict) or not isinstance(data.get("works"), list):
            raise ValueError("LLM output does not contain a works list")
        yield "result", data
    except Exception as e:  # noqa: BLE001 — deterministic result remains valid
        logger.warning("[batch] streaming LLM analysis failed: %s", e)
        yield "error", str(e)
        yield "result", None


async def bind_single_work_assignments(db: AsyncSession, resource: FileResource) -> int:
    """Bind deterministic batch rows after metadata linked one concrete work.

    Torrent inspection intentionally runs before metadata matching.  This
    second pass closes that ordering gap without guessing: it only acts when
    the resource FK identifies exactly one series or movie, and never
    overwrites manual/LLM provenance.
    """
    work_type = "series" if resource.series_id else "movie" if resource.movie_id else None
    work_id = resource.series_id or resource.movie_id
    if work_type is None or work_id is None:
        return 0
    try:
        await db.refresh(resource, ["file_assignments"])
    except Exception:  # noqa: BLE001
        return 0
    special_overrides = (
        await resolve_fractional_specials(
            db, work_id, [row.file_path for row in resource.file_assignments],
        )
        if work_type == "series"
        else {}
    )
    changed = 0
    placement_changed = False
    special_changed = False
    for row in resource.file_assignments:
        if row.source != "auto":
            continue
        if not row.series_id and not row.movie_id:
            row.series_id = work_id if work_type == "series" else None
            row.movie_id = work_id if work_type == "movie" else None
            changed += 1
        if (
            work_type == "series"
            and row.season is None
            and resource.season is not None
        ):
            row.season = resource.season
            placement_changed = True
        if row.file_path in special_overrides:
            row.season = 0
            row.episode_start = special_overrides[row.file_path]
            row.episode_end = special_overrides[row.file_path]
            special_changed = True
            placement_changed = True
    if special_changed or placement_changed:
        resource.season_ranges = compute_season_ranges(resource)
    if changed:
        links = (await db.execute(
            select(ResourceWorkLink).where(ResourceWorkLink.resource_id == resource.id)
        )).scalars().all()
        found = any(
            (work_type == "series" and link.series_id == work_id)
            or (work_type == "movie" and link.movie_id == work_id)
            for link in links
        )
        if not found:
            db.add(ResourceWorkLink(
                resource_id=resource.id,
                series_id=work_id if work_type == "series" else None,
                movie_id=work_id if work_type == "movie" else None,
                source="auto",
            ))
    return changed


_FRACTIONAL_SPECIAL_RE = re.compile(
    r"(?:^|[\s._\-])(\d{1,3})\.5(?=[;\s._\-\[(]|$)", re.IGNORECASE,
)


async def resolve_fractional_specials(
    db: AsyncSession,
    series_id: str,
    paths: list[str],
) -> dict[str, int]:
    """Resolve release-style ``11.5`` labels against canonical Season 0 rows.

    A decimal label is not itself a Plex episode number.  We only map when the
    linked work supplies exactly as many Season 0 episodes as the listing has
    fractional video labels; ordering then gives a deterministic bijection.
    Ambiguous cardinalities return no mapping rather than guessing.
    """
    candidates: list[tuple[int, str]] = []
    for path in paths:
        match = _FRACTIONAL_SPECIAL_RE.search(path.rsplit("/", 1)[-1])
        if match:
            candidates.append((int(match.group(1)), path))
    if not candidates:
        return {}
    specials = (await db.execute(
        select(Episode).where(Episode.series_id == series_id, Episode.season == 0)
        .order_by(Episode.episode.asc())
    )).scalars().all()
    candidates.sort(key=lambda item: (item[0], item[1]))
    if len(specials) == len(candidates):
        return {path: special.episode for (_, path), special in zip(candidates, specials)}
    # Release-order decimals (11.5, 22.5, …) explicitly mean interstitial
    # specials.  When the metadata source has no Season 0 rows, preserve their
    # order and assign the canonical Specials sequence S00E01..N.
    if not specials:
        return {path: index for index, (_, path) in enumerate(candidates, start=1)}
    return {}


async def build_candidate_works(db: AsyncSession, resource: FileResource) -> list[dict]:
    """Build server-owned candidate identities with all useful title aliases."""
    refs: set[tuple[str, str]] = set()
    if resource.series_id:
        refs.add(("series", resource.series_id))
    if resource.movie_id:
        refs.add(("movie", resource.movie_id))
    links = (await db.execute(select(ResourceWorkLink).where(
        ResourceWorkLink.resource_id == resource.id,
    ))).scalars().all()
    for link in links:
        if link.series_id:
            refs.add(("series", link.series_id))
        elif link.movie_id:
            refs.add(("movie", link.movie_id))

    candidates = []
    for work_type, work_id in sorted(refs):
        model = TVSeries if work_type == "series" else Movie
        work = await db.get(model, work_id)
        if work is None:
            continue
        titles = []
        for value in (work.title_cn, work.title_en, work.original_title, work.canonical_name):
            if value and value not in titles:
                titles.append(value)
        candidates.append({
            "candidate_key": f"{work_type}:{work_id}",
            "work_type": work_type,
            "work_id": work_id,
            "titles": titles,
        })
    return candidates


def _valid_paths(
    data: dict[str, Any], known: set[str], candidate_works: list[dict] | None = None,
) -> list[dict]:
    """Clamp the LLM reply against the actual listing (drop hallucinations)."""
    out: list[dict] = []
    candidates = {
        candidate["candidate_key"]: candidate for candidate in (candidate_works or [])
    }
    for w in data.get("works", []):
        if not isinstance(w, dict):
            continue
        candidate_key = w.get("candidate_key")
        candidate = candidates.get(candidate_key)
        title = str(w.get("title") or "").strip()
        ctype = w.get("content_type")
        if candidate is not None:
            ctype = "tv" if candidate["work_type"] == "series" else "movie"
            title = title or next(iter(candidate["titles"]), candidate["work_id"])
        elif candidate_key is not None and candidates:
            # A model-generated or altered ID is never accepted.
            continue
        if not title or ctype not in ("tv", "movie"):
            continue
        files: list[dict] = []
        for f in w.get("files", []):
            if not isinstance(f, dict):
                continue
            path = f.get("path")
            if path not in known:
                continue
            episode = f.get("episode") if isinstance(f.get("episode"), int) else None
            episode_start = (
                f.get("episode_start") if isinstance(f.get("episode_start"), int) else None
            )
            episode_end = (
                f.get("episode_end") if isinstance(f.get("episode_end"), int) else None
            )
            # Persistence uses a normalized inclusive range. Keep the LLM
            # contract unambiguous while remaining schema-compatible.
            if episode is not None:
                episode_start = episode
                episode_end = episode
            elif (
                episode_start is None
                or episode_end is None
                or episode_start > episode_end
            ):
                episode_start = None
                episode_end = None
            files.append({
                "path": path,
                "season": f.get("season") if isinstance(f.get("season"), int) else None,
                "episode_start": episode_start,
                "episode_end": episode_end,
            })
        if files:
            out.append({
                "candidate_key": candidate_key if candidate is not None else None,
                "title": title,
                "content_type": ctype,
                "files": files,
            })
    return out


async def refine_batch_content(
    db: AsyncSession,
    resource: FileResource,
    report: TorrentReport,
    channel: Channel | None,
) -> bool:
    """LLM refinement during fetch. Returns True when movies were bound.

    Writes ``llm``-sourced assignments / work links and upgrades a pure-movie
    pack's ``batch_scope`` to ``movies``. Failures degrade silently.
    """
    listing = [
        {"name": fp["path"], "size": fp.get("size")} for fp in report.file_parses
    ]
    if not listing:
        return False
    title = resource.search_title or resource.title_cn or resource.title_raw
    data = await analyze_listing(title, listing, [c.title for c in report.clusters])
    if not data:
        return False

    known = {fp["path"] for fp in report.file_parses}
    works = _valid_paths(data, known)
    if not works:
        return False

    # Scope upgrade: pure-movie packs become 'movies'; everything else stays
    # under 'franchise' (mixed tv+movie AND multi-series packs alike).
    ctypes = {w["content_type"] for w in works}
    multi_work = len(works) >= 2 or len({w["title"] for w in works}) >= 2
    is_movie_pack = ctypes == {"movie"} and multi_work
    if is_movie_pack and resource.batch_scope == "franchise":
        resource.batch_scope = "movies"

    # Movie resolution/binding only applies to genuine multi-work packs;
    # season/multi-season packs (high-unparsed gate) get hint fills only.
    allow_movie_binding = resource.batch_scope in ("franchise", "movies")

    bound_any = False
    for w in works:
        if w["content_type"] != "movie" or not allow_movie_binding:
            # TV clusters inside multi-work packs stay hint-only: concrete tv
            # binding happens manually in the wizard (per confirmed design).
            _apply_hint(resource, w)
            continue
        movie = await _resolve_movie(db, resource, channel, w["title"])
        if movie is None:
            _apply_hint(resource, w)
            continue
        bound_any = True
        await _bind_work(db, resource, "movie", movie.id, w["files"])

    if bound_any:
        logger.info(
            "[batch] LLM refinement bound movie works for resource %s (%d clusters)",
            resource.id, len(works),
        )
    return bound_any


def _apply_hint(resource: FileResource, work: dict) -> None:
    """Hint-only refinement: tag unbound rows with the suggested title."""
    by_path = {a.file_path: a for a in resource.file_assignments}
    for f in work["files"]:
        row = by_path.get(f["path"])
        if row is None or row.source == "manual":
            continue
        if row.series_id or row.movie_id:
            continue
        row.work_title_hint = work["title"]
        if f.get("season") is not None:
            row.season = f["season"]
        if f.get("episode_start") is not None:
            row.episode_start = f["episode_start"]
        if f.get("episode_end") is not None:
            row.episode_end = f["episode_end"]


async def _resolve_movie(
    db: AsyncSession,
    resource: FileResource,
    channel: Channel | None,
    title: str,
):
    """Resolve one cluster title to a Movie row via the channel source."""
    from app.services.metadata_agent import get_agent
    from app.services.metadata_sources import resolve_metadata_source

    agent = get_agent()
    source = resolve_metadata_source(getattr(channel, "metadata_source", None))
    try:
        meta = await agent.process_title_only(title, source)
    except Exception as e:  # noqa: BLE001 — best-effort member match
        logger.warning("[batch] movie match failed for %r: %s", title[:80], e)
        return None
    if not meta or not meta.found or not meta.matched_entity:
        return None
    entity = dict(meta.matched_entity)
    if meta.content_type != "movie":
        return None
    from app.services.metadata_service import create_or_update_movie_from_external

    try:
        return await create_or_update_movie_from_external(db, entity)
    except Exception as e:  # noqa: BLE001
        logger.warning("[batch] movie upsert failed for %r: %s", title[:80], e)
        return None


async def _bind_work(
    db: AsyncSession,
    resource: FileResource,
    work_type: str,
    work_id: str,
    files: list[dict],
) -> None:
    """Bind assignment rows to a work and ensure the ResourceWorkLink row."""
    by_path = {a.file_path: a for a in resource.file_assignments}
    for f in files:
        row = by_path.get(f["path"])
        if row is None or row.source == "manual":
            continue
        if work_type == "movie":
            row.movie_id = work_id
            row.series_id = None
        else:
            row.series_id = work_id
            row.movie_id = None
        if f.get("season") is not None:
            row.season = f["season"]
        if f.get("episode_start") is not None:
            row.episode_start = f["episode_start"]
        if f.get("episode_end") is not None:
            row.episode_end = f["episode_end"]
        row.source = "llm"

    link_model = ResourceWorkLink
    existing_links = (await db.execute(
        select(link_model).where(link_model.resource_id == resource.id)
    )).scalars().all()
    for link in existing_links:
        if work_type == "movie" and link.movie_id == work_id:
            return
        if work_type == "series" and link.series_id == work_id:
            return
    db.add(
        link_model(
            resource_id=resource.id,
            movie_id=work_id if work_type == "movie" else None,
            series_id=work_id if work_type == "series" else None,
            source="llm",
        )
    )


async def suggest_batch_content(
    db: AsyncSession,
    resource: FileResource,
    channel: Channel | None = None,
    files: list[dict] | None = None,
) -> dict:
    """Suggestions for the edit wizard's file-mapping step (non-persistent).

    Always returns the DETERMINISTIC layer (per-file season/episode parses +
    top-level clusters) so the wizard can prefill mappings without any LLM;
    the LLM ``works`` block is attached only when the analyzer succeeds.
    Nothing is written to the database — works are title-keyed and binding to
    concrete work rows happens client-side against the step-1 association set.
    """
    from app.services.torrent_inspect import analyze_torrent_files, parse_torrent_files

    if files is None:
        path = resource.torrent_file
        if not path:
            files = []
        else:
            try:
                files = parse_torrent_files(path)
            except Exception:  # noqa: BLE001
                files = None
            files = files or []

    report = analyze_torrent_files(files)
    deterministic = {
        "scope_hint": report.scope,
        "seasons": report.seasons,
        "season_ranges": report.season_ranges,
        "files": [
            {
                "path": fp["path"],
                "size": fp.get("size"),
                "season": fp["season"],
                "episode": fp["episode"],
            }
            for fp in report.file_parses
        ],
        "clusters": [
            {"title": c.title, "files": list(c.files)} for c in report.clusters
        ],
    }

    listing = [
        {"name": fp["path"], "size": fp.get("size")} for fp in report.file_parses
    ]
    llm_works: list[dict] = []
    if listing:
        title = resource.search_title or resource.title_cn or resource.title_raw
        data = await analyze_listing(title, listing, [c.title for c in report.clusters])
        if data:
            known = {fp["path"] for fp in report.file_parses}
            llm_works = _valid_paths(data, known)

    return {
        "deterministic": deterministic,
        "works": llm_works,
    }
