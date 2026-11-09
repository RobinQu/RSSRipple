"""Torrent file-listing inspection — the third batch (合集) detection layer.

The existing layers are the raw-title regexes (``resource_parser.detect_batch``)
and the LLM. This module adds a deterministic third layer: fetch the .torrent
file itself, parse its file listing with bencodepy, and infer the resource
scope (single episode / season pack / multi-season pack / franchise bundle)
from the video files' paths and sizes.

``maybe_inspect_torrent`` is the fetch-period wiring ("channel A"): given a
fresh FileResource it downloads/parses/analyzes the .torrent and
reclassifies the resource (``is_batch`` / ``batch_scope`` / episode range)
when the listing proves a batch the title regexes could not see. Resources
already judged as batches but still missing ``batch_scope`` / episode range
(e.g. title-regex batches) are likewise enriched from the file listing —
an existing batch verdict is never downgraded.
"""

import asyncio
import hashlib
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import bencodepy
import httpx

from app.clients import outbound_http
from app.config import settings
from app.services.file_roles import assignment_file_role, classify_file
from app.services.resource_parser import (
    extract_release_title,
    extract_season_episode_from_path,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.channel import Channel
    from app.models.file_resource import FileResource

logger = logging.getLogger(__name__)

# Hard caps for the .torrent download: a real torrent is typically < 1 MB;
# 50 MB is far above anything legitimate and guards against abusive payloads.
_MAX_TORRENT_BYTES = 50 * 1024 * 1024
_DOWNLOAD_TIMEOUT = 20

# Retry budget: one automatic retry (2 attempts total) with a short backoff,
# only for transient failures (timeout / connection error / non-200 status).
_MAX_ATTEMPTS = 2
_RETRY_BACKOFF_SECONDS = 1

@dataclass
class WorkCluster:
    """One top-level work cluster of a multi-work torrent.

    ``title`` is the normalized cluster title (see ``_cluster_title``);
    ``files`` holds the relative paths of the main video files under that
    top-level directory. Root-level files (no directory component) never
    form a cluster.
    """

    title: str
    files: list[str] = field(default_factory=list)


@dataclass
class TorrentReport:
    """Result of :func:`analyze_torrent_files`.

    ``scope`` semantics:

    - ``"single"``: at most one main video file — a single episode or movie.
      ``is_batch`` is False.
    - ``"season"``: one season's episode run — every parsed video file maps
      to the same season (explicit marker or one implicit flat season) with
      at least two distinct episode numbers. ``episode_start``/``episode_end``
      are the min/max episode numbers. ``is_batch`` is True.
    - ``"multi_season"``: video files span two or more explicit season
      numbers. ``episode_start``/``episode_end`` are None (per-season episode
      numbers are not comparable across seasons). ``is_batch`` is True.
    - ``"franchise"``: the torrent bundles two or more distinct work clusters
      (top-level directories whose normalized names differ significantly,
      e.g. "作品X TV" vs "作品X 剧场版"). ``work_titles`` holds the cluster
      titles (sorted). ``is_batch`` is True.
    - ``"unknown"``: cannot be classified confidently — empty listing, no
      main video files, or too many video files without parseable episode
      numbers. ``is_batch`` is False.

    ``seasons`` is the sorted set of explicitly detected season numbers (from
    ``SxxEyy`` / season-directory markers); files without a season marker do
    not contribute. ``unparsed_ratio`` is the share of main video files whose
    episode number could not be extracted (0.0 when there are none).

    ``season_ranges`` carries per-season episode runs for explicitly marked
    seasons ([{season, episode_start, episode_end}, ...], sorted by season) —
    computed for free from the same (season, episode) parses that drive the
    scope verdict, no LLM needed.

    ``clusters`` lists every work cluster with its member file paths (sorted
    alphabetically), giving the LLM refinement and the edit wizard a stable
    grouping anchor.
    """

    scope: str  # "single" | "season" | "multi_season" | "franchise" | "unknown"
    is_batch: bool
    episode_start: int | None = None
    episode_end: int | None = None
    seasons: list[int] = field(default_factory=list)
    work_titles: list[str] = field(default_factory=list)
    season_ranges: list[dict] = field(default_factory=list)
    clusters: list[WorkCluster] = field(default_factory=list)
    # Per-main-video-file parse results in listing order:
    # [{"path", "size", "season", "episode"}]. Drives deterministic file
    # assignment write-backs without re-parsing the torrent.
    file_parses: list[dict] = field(default_factory=list)
    # Complete role-classified listing, including subtitles, extras and other
    # files which never participate in scope or episode-range detection.
    file_entries: list[dict] = field(default_factory=list)
    video_file_count: int = 0
    unparsed_ratio: float = 0.0


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

class TorrentFetchError(Exception):
    """A classified torrent acquisition failure suitable for retry state."""

    def __init__(self, message: str, *, retryable: bool):
        super().__init__(message)
        self.retryable = retryable


async def fetch_torrent_file_detailed(url: str, resource_id: str) -> str:
    """Fetch and cache a torrent, raising :class:`TorrentFetchError` on failure."""
    if not url or not url.startswith(("http://", "https://")):
        raise TorrentFetchError("torrent URL must use HTTP or HTTPS", retryable=False)
    if not settings.torrent_cache_dir:
        raise TorrentFetchError("torrent cache directory is not configured", retryable=False)

    cache_dir = Path(settings.torrent_cache_dir)
    try:
        await asyncio.to_thread(cache_dir.mkdir, parents=True, exist_ok=True)
    except Exception as error:
        logger.warning("[torrent] cache dir not writable %s: %s", cache_dir, error)
        raise TorrentFetchError(
            f"torrent cache directory is not writable: {error}", retryable=False
        ) from error

    def _download() -> bytes:
        ua = (
            f"{settings.app_name}/0.1.0 "
            "(https://github.com/RobinQu/RSSRipple) torrent-inspect"
        )
        try:
            with outbound_http.client(
                timeout=_DOWNLOAD_TIMEOUT, headers={"User-Agent": ua}
            ) as client, client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    retryable = (
                        resp.status_code in (408, 429) or 500 <= resp.status_code <= 599
                    )
                    raise TorrentFetchError(
                        f"torrent download returned HTTP {resp.status_code}", retryable=retryable
                    )
                chunks: list[bytes] = []
                total = 0
                for chunk in resp.iter_bytes():
                    total += len(chunk)
                    if total > _MAX_TORRENT_BYTES:
                        raise TorrentFetchError(
                            "torrent response exceeds 50MB", retryable=False
                        )
                    chunks.append(chunk)
                return b"".join(chunks)
        except TorrentFetchError:
            raise
        except outbound_http.DestinationDenied as error:
            raise TorrentFetchError(str(error), retryable=False) from error
        except (httpx.TimeoutException, httpx.TransportError) as error:
            raise TorrentFetchError(
                f"torrent download failed: {error}", retryable=True
            ) from error

    for attempt in range(_MAX_ATTEMPTS):
        try:
            content = await asyncio.to_thread(_download)
            break
        except TorrentFetchError as error:
            if not error.retryable or attempt == _MAX_ATTEMPTS - 1:
                logger.warning("[torrent] download failed %s: %s", url[:80], error)
                raise
            await asyncio.sleep(_RETRY_BACKOFF_SECONDS)

    if not content or await asyncio.to_thread(parse_torrent_payload, content) is None:
        logger.debug("[torrent] rejecting non-bencode or unusable payload %s", url[:80])
        raise TorrentFetchError("response is not a usable bencoded torrent", retryable=False)

    dest = cache_dir / f"{resource_id}-{hashlib.sha256(content).hexdigest()}.torrent"

    def publish():
        temporary = dest.with_name(f".{uuid.uuid4().hex}.torrent.tmp")
        try:
            temporary.write_bytes(content)
            os.replace(temporary, dest)
        finally:
            temporary.unlink(missing_ok=True)

    try:
        # A cancelled caller cannot stop its thread. Different bytes use
        # different final paths, and readers never observe a partial write.
        await asyncio.to_thread(publish)
        return str(dest)
    except Exception as error:
        logger.warning("[torrent] write failed %s: %s", dest, error)
        raise TorrentFetchError(f"torrent cache write failed: {error}", retryable=False) from error


async def fetch_torrent_file(url: str, resource_id: str) -> str | None:
    """Compatibility wrapper returning ``path|None`` instead of classified errors."""
    try:
        return await fetch_torrent_file_detailed(url, resource_id)
    except Exception as error:  # noqa: BLE001 - preserve the legacy best-effort contract
        logger.debug("[torrent] fetch failed for %s: %s", resource_id, error)
        return None


async def ensure_torrent_cached(resource: "FileResource") -> str | None:
    """Legacy cache helper for callers that do not manage HTTP retry state.

    The fetch pipeline uses ``fetch_resource_torrent`` instead. This downloads
    ``resource.torrent_url`` into the cache dir when it is a plain
    http(s) direct link and no usable cache exists yet (``torrent_file`` is
    empty or points at a file that has since been deleted). On success the
    local path is written to ``resource.torrent_file``. Magnets and any
    download/write failure are silent (returns None). Does NOT commit — the
    caller's session owns the transaction.
    """
    path = resource.torrent_file
    if path and await asyncio.to_thread(Path(path).exists):
        if await asyncio.to_thread(parse_torrent_files, path) is not None:
            return path
        # A previous implementation persisted HTTP error pages as .torrent
        # files. Remove those poison entries before attempting a fresh fetch.
        try:
            await asyncio.to_thread(Path(path).unlink)
        except OSError:
            pass
        resource.torrent_file = None
    url = resource.torrent_url or ""
    if not (url.startswith("http://") or url.startswith("https://")):
        return None
    try:
        new_path = await fetch_torrent_file(url, resource.id)
    except Exception as e:
        logger.debug("[torrent] cache failed for %s: %s", resource.id, e)
        return None
    if new_path:
        resource.torrent_file = new_path
    return new_path


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _decode_text(value) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def parse_torrent_payload(raw: bytes) -> list[dict] | None:
    """Decode and validate torrent bytes before they are persisted."""
    try:
        decoded = bencodepy.decode(raw)
    except Exception as e:
        logger.debug("[torrent] bencode decode failed in payload: %s", e)
        return None

    if not isinstance(decoded, dict):
        return None
    info = decoded.get(b"info")
    if not isinstance(info, dict):
        return None

    out: list[dict] = []
    files = info.get(b"files")
    if isinstance(files, list):
        for entry in files:
            if not isinstance(entry, dict):
                return None
            length = entry.get(b"length")
            parts = entry.get(b"path.utf-8") or entry.get(b"path")
            if not isinstance(length, int) or not isinstance(parts, list) or not parts:
                return None
            name = "/".join(_decode_text(p) for p in parts)
            if name:
                out.append({"name": name, "size": length})
        return out or None

    name = info.get(b"name.utf-8") or info.get(b"name")
    length = info.get(b"length")
    if name is None or not isinstance(length, int):
        return None
    return [{"name": _decode_text(name), "size": length}]


def parse_torrent_files(path: str) -> list[dict] | None:
    """Parse a .torrent file's file listing.

    Handles both multi-file torrents (``info/files``, path components joined
    with ``/``; paths are kept relative to the torrent root, i.e. the
    ``info/name`` root directory is NOT prepended, so the first path component
    is meaningful for clustering) and single-file torrents (``info/name``).
    Returns ``[{"name": <relative path>, "size": int}, ...]``, or None when
    the file cannot be read/decoded or has no usable ``info`` dict.
    """
    try:
        raw = Path(path).read_bytes()
    except Exception as e:
        logger.debug("[torrent] read failed %s: %s", path, e)
        return None
    return parse_torrent_payload(raw)


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def read_torrent_root_name(path: str) -> str | None:
    """Return a multi-file torrent's ``info/name`` root directory name.

    Returns None for single-file torrents (``info/name`` IS the file, already
    returned verbatim by :func:`parse_torrent_files`) and on any read/decode
    failure. Used by the organize manifest fallback: ``parse_torrent_files``
    keeps paths relative to the torrent root, but on disk the download client
    materializes them under ``download_dir/<info/name>/`` — the root component
    is needed to locate the files.
    """
    try:
        decoded = bencodepy.decode(Path(path).read_bytes())
    except Exception:
        return None
    if not isinstance(decoded, dict):
        return None
    info = decoded.get(b"info")
    if not isinstance(info, dict) or not isinstance(info.get(b"files"), list):
        return None
    name = info.get(b"name.utf-8") or info.get(b"name")
    return _decode_text(name) if name is not None else None


def _is_main_video(entry: dict) -> bool:
    """True when the entry looks like a main feature video file."""
    return classify_file(entry.get("name"), entry.get("size") or 0) == "video"


# Cluster-title normalization: strip bracketed release tags, season suffixes
# and episode ranges from a top-level directory name; what remains is compared
# across clusters.
_BRACKET_BLOCK_RE = re.compile(r"[\[【\(（][^\]】\)）]*[\]】\)）]")
_RANGE_TOKEN_RE = re.compile(r"\b\d{1,3}\s*[~\-–～〜]\s*\d{1,3}\b")
_CREDIBLE_TITLE_RE = re.compile(r"[A-Za-z一-鿿]")
# Titles that are nothing but digits / resolution-ish tokens ("1080p", "720P")
# are tech tags, not work names — they never form a franchise cluster.
_TECH_ONLY_RE = re.compile(r"^[\d\s.pP]+$")


def _cluster_title(dirname: str) -> str | None:
    """Normalized work-cluster title for a top-level directory name.

    Returns None when nothing credible remains (pure tech tags / numbers) —
    such directories never participate in franchise clustering.
    """
    t = _BRACKET_BLOCK_RE.sub(" ", dirname)
    t = _RANGE_TOKEN_RE.sub(" ", t)
    t = extract_release_title(t) or t
    t = re.sub(r"\s+", " ", t).strip(" -_.·　")
    if len(t) < 2 or not _CREDIBLE_TITLE_RE.search(t) or _TECH_ONLY_RE.match(t):
        return None
    return t


def _season_ranges_from_parsed(
    parsed: list[tuple[dict, int | None, int | None]],
) -> list[dict]:
    """Per-season episode runs from (file, season, episode) parses.

    Only explicitly-marked seasons contribute; a single-file season yields
    start == end. Sorted by season number.
    """
    by_season: dict[int, list[int]] = {}
    for _, season, episode in parsed:
        if season is None or episode is None:
            continue
        by_season.setdefault(season, []).append(episode)
    return [
        {
            "season": season,
            "episode_start": min(eps),
            "episode_end": max(eps),
        }
        for season, eps in sorted(by_season.items())
    ]


def _clusters_from_parsed(
    parsed: list[tuple[dict, int | None, int | None]],
) -> list[WorkCluster]:
    """Group main video files by their top-level directory's cluster title.

    Mirrors the franchise clustering rule: root-level files (no directory
    component) and non-credible titles (pure tech tags) never form clusters.
    """
    clusters: dict[str, WorkCluster] = {}
    for f, _, _ in parsed:
        components = [c for c in re.split(r"[/\\]+", f["name"]) if c]
        if len(components) < 2:
            continue
        title = _cluster_title(components[0])
        if title is None:
            continue
        cluster = clusters.setdefault(title.casefold(), WorkCluster(title=title))
        cluster.files.append(f["name"])
    return [clusters[k] for k in sorted(clusters)]


def analyze_torrent_files(files: list[dict]) -> TorrentReport:
    """Classify a torrent file listing into a :class:`TorrentReport`.

    Pure function. ``files`` is the ``parse_torrent_files`` output shape:
    ``[{"name": <relative path>, "size": int}, ...]``. See the dataclass
    docstring for the per-scope semantics.
    """
    unknown = TorrentReport(scope="unknown", is_batch=False)
    if not files:
        return unknown

    file_entries = [
        {
            "path": f["name"],
            "size": f.get("size"),
            "file_role": assignment_file_role(f["name"], f.get("size") or 0),
            "season": None,
            "episode": None,
        }
        for f in files
    ]
    unknown.file_entries = file_entries
    videos = [f for f in files if _is_main_video(f)]
    unknown.video_file_count = len(videos)
    if not videos:
        return unknown

    parsed: list[tuple[dict, int | None, int | None]] = []
    unparsed = 0
    for f in videos:
        season, episode = extract_season_episode_from_path(f["name"])
        parsed.append((f, season, episode))
        if episode is None:
            unparsed += 1
    unparsed_ratio = unparsed / len(videos)

    file_parses = [
        {
            "path": f["name"],
            "size": f.get("size"),
            "file_role": "episode",
            "season": season,
            "episode": episode,
        }
        for f, season, episode in parsed
    ]
    entries_by_path = {entry["path"]: entry for entry in file_entries}
    for fp in file_parses:
        entries_by_path[fp["path"]].update(
            season=fp["season"], episode=fp["episode"]
        )
    season_ranges = _season_ranges_from_parsed(parsed)
    clusters = _clusters_from_parsed(parsed)

    base = TorrentReport(
        scope="unknown",
        is_batch=False,
        video_file_count=len(videos),
        unparsed_ratio=unparsed_ratio,
        season_ranges=season_ranges,
        clusters=clusters,
        file_parses=file_parses,
        file_entries=file_entries,
    )

    if len(videos) <= 1:
        base.scope = "single"
        return base

    # Franchise clustering: group by top-level directory's normalized title.
    # Root-level files (no directory component) never form a cluster.
    if len(clusters) >= 2:
        base.scope = "franchise"
        base.is_batch = True
        base.work_titles = sorted(c.title for c in clusters)
        base.seasons = sorted({s for _, s, _ in parsed if s is not None})
        return base

    seasons = sorted({s for _, s, _ in parsed if s is not None})
    base.seasons = seasons
    # Season 0 is specials/SP, not a real season: a pack of one real season
    # plus SP extras is still a single-season pack. ``seasons`` itself keeps
    # the 0 — it reports the full covered set including specials.
    real_seasons = [s for s in seasons if s != 0]
    if len(real_seasons) >= 2:
        base.scope = "multi_season"
        base.is_batch = True
        return base

    # Single season: all parsed files share one season group (explicit season
    # number, or None for a flat unlabeled run) with >= 2 distinct episodes.
    # SP files are excluded from the episode range when a real season exists;
    # an SP-only pack keeps the season-0 files as its episode source.
    if real_seasons:
        scoped = [(f, s, e) for f, s, e in parsed if s != 0]
    else:
        scoped = parsed
    episodes = [e for _, _, e in scoped if e is not None]
    season_groups = {s for _, s, e in scoped if e is not None}
    if len(season_groups) == 1 and len(set(episodes)) >= 2:
        base.scope = "season"
        base.is_batch = True
        base.episode_start = min(episodes)
        base.episode_end = max(episodes)
        return base

    # Fallback: too many unparseable video files (or duplicate episode numbers
    # only, e.g. the same episode in two encodes) — stay conservative.
    return base


# ---------------------------------------------------------------------------
# Channel A: fetch-period inspection
# ---------------------------------------------------------------------------

def has_manual_listing_shape(resource: "FileResource") -> bool:
    """Return whether late automation must preserve the human-curated shape."""
    if getattr(resource, "episode_confidence", None) == "manual":
        return True
    return any(
        getattr(row, "source", None) == "manual"
        for attr in ("file_assignments", "work_links")
        for row in vars(resource).get(attr, []) or []
    )


async def apply_torrent_report(
    db: "AsyncSession", resource: "FileResource", report: TorrentReport,
    channel: "Channel | None" = None, *, strict: bool = False,
) -> bool:
    """Apply scope, role-aware assignments and cluster enrichment without committing."""
    manual_shape = has_manual_listing_shape(resource)
    if manual_shape:
        pass
    elif report.scope == "season":
        resource.is_batch = True
        resource.batch_scope = "season"
        resource.episode = None
        resource.episode_start = report.episode_start
        resource.episode_end = report.episode_end
        if resource.season is None and resource.series_id:
            try:
                from app.models.series import TVSeries
                from app.services.metadata_episode_reconcile import work_verified_season

                series = await db.get(TVSeries, resource.series_id)
                verified = work_verified_season(series) if series is not None else None
                if verified is not None:
                    resource.season = verified
            except Exception:  # noqa: BLE001 — best-effort unless reconciling durably
                if strict:
                    raise
                pass
    elif report.scope == "multi_season":
        resource.is_batch = True
        resource.batch_scope = "multi_season"
        resource.episode = None
        resource.season = None
        resource.episode_start = None
        resource.episode_end = None
        resource.batch_seasons = report.seasons or None
    elif report.scope == "franchise":
        resource.is_batch = True
        resource.batch_scope = "franchise"
        resource.episode = None
        resource.batch_seasons = report.seasons or None
    elif report.scope == "single" and resource.series_id and report.file_parses:
        parsed = next(
            (item for item in report.file_parses if item.get("episode") is not None),
            None,
        )
        if parsed:
            if resource.season is None and parsed.get("season") is not None:
                resource.season = parsed["season"]
            if resource.episode is None:
                resource.episode = parsed["episode"]
            if getattr(resource, "episode_confidence", None) in (None, "ambiguous"):
                resource.episode_confidence = "raw"

    if hasattr(type(resource), "file_assignments") or "file_assignments" in vars(resource):
        from app.services import batch_content_analysis as bca

        try:
            await db.refresh(resource, ["file_assignments"])
        except Exception:  # noqa: BLE001 — pending row edge
            if strict:
                raise
        bca.apply_auto_assignments(resource, report)
        resource.season_ranges = bca.compute_season_ranges(resource)
        if not manual_shape and resource.is_batch and bca.llm_refinement_needed(
            report, resource.batch_scope, resource.is_batch
        ):
            try:
                llm_bound_movies = await bca.refine_batch_content(
                    db, resource, report, channel
                )
            except Exception as e:  # noqa: BLE001 — strict mode records a durable retry
                if strict:
                    raise
                logger.warning("[torrent] LLM refinement failed for %s: %s", resource.id, e)
            else:
                if llm_bound_movies:
                    resource.season_ranges = bca.compute_season_ranges(resource)

    if not manual_shape and resource.is_batch and resource.batch_scope == "franchise":
        try:
            from app.services.franchise_service import link_franchise_pack

            await link_franchise_pack(db, resource, report, channel)
        except Exception as e:
            if strict:
                raise
            logger.warning("[torrent] franchise linking failed for %s: %s", resource.id, e)
    if not manual_shape and resource.is_batch and (
        hasattr(type(resource), "file_assignments") or "file_assignments" in vars(resource)
    ):
        try:
            from app.services.cluster_work_binding import bind_hint_clusters

            await bind_hint_clusters(db, resource, channel)
        except Exception as e:  # noqa: BLE001 — strict mode records a durable retry
            if strict:
                raise
            logger.warning("[torrent] cluster binding failed for %s: %s", resource.id, e)
    return report.is_batch


async def maybe_inspect_torrent(
    db: "AsyncSession", resource: "FileResource", channel: "Channel | None" = None
) -> bool:
    """Fetch-period torrent inspection: reclassify a resource from its .torrent.

    Preconditions (all required, otherwise a no-op returning False):

    - The resource still needs classification info: ``is_batch`` is False,
      or it is True (title regexes / LLM already judged it a batch) but the
      torrent-derived details are still missing — ``batch_scope`` unset, or
      ``batch_scope="season"`` without ``episode_start/end``. Batches with
      complete scope info are left alone.
    - ``resource.torrent_url`` is a plain http(s) direct link; magnets carry
      no file listing and are skipped.

    On a successful download the local path is cached in
    ``resource.torrent_file`` regardless of the verdict. The analysis report
    then writes back:

    - ``season``: ``is_batch=True``, ``batch_scope="season"``,
      ``episode=None``, ``episode_start/end`` from the report.
    - ``multi_season``: ``is_batch=True``, ``batch_scope="multi_season"``,
      ``episode=None``, ``season=None``, ``episode_start/end=None``,
      ``batch_seasons`` from the report. Two or more *real* seasons are
      required — season 0 (specials/SP) never counts towards multi_season,
      though it stays in the reported ``batch_seasons`` coverage set.
    - ``franchise``: ``is_batch=True``, ``batch_scope="franchise"``,
      ``episode=None``, ``batch_seasons`` from the report. When the LLM
      refinement proves a pure-movie pack (scope upgraded to ``"movies"``)
      the member movies are bound directly and NO WorkCollection is created;
      otherwise ``franchise_service.link_franchise_pack`` resolves the member
      works and links ``collection_id`` (failures are isolated — the batch
      verdict above is kept regardless).
    - ``single`` / ``unknown``: no reclassification — only the cache path
      is kept (an existing ``is_batch=True`` verdict is never downgraded).

    Every batch outcome additionally gets the enrichment pass: deterministic
    ``ResourceFileAssignment`` rows (path → cluster hint / season / episode,
    source=auto) plus recomputed ``season_ranges``, followed by the gated LLM
    refinement when the deterministic layer cannot finish the job.

    The function does NOT commit: it runs inside the caller's session
    (``fetch_service._process_resource_metadata``), whose own commit after
    metadata matching persists these changes together with the link result —
    the same short-transaction convention the rest of that flow follows.

    Returns True when the resource was (re)classified as a batch. Any
    download/parse failure is silent (debug log) and returns False.
    """
    if resource.is_batch:
        # 已判定合集且信息完整（scope 已细分；season scope 时集数范围齐全）
        # 且文件级映射已写入 → 不重跑；标题正则判出的合集（scope NULL）、
        # 缺集数范围的 season 包、或尚无 assignments 的合集仍用 torrent
        # 文件清单补齐。
        scoped = resource.batch_scope is not None
        has_range = (
            resource.episode_start is not None and resource.episode_end is not None
        )
        verdict_complete = scoped and (resource.batch_scope != "season" or has_range)
        if verdict_complete:
            try:
                await db.refresh(resource, ["file_assignments"])
            except Exception:  # noqa: BLE001 — pending row or load hiccup
                return False
            if resource.file_assignments:
                from app.services.resource_coverage import is_non_episode_video_path

                unresolved_extras = resource.batch_scope == "season" and any(
                    getattr(row, "source", "auto") == "auto"
                    and (getattr(row, "file_role", None) or "episode") == "episode"
                    and getattr(row, "season", None) != 0
                    and getattr(row, "episode_start", None) is None
                    and getattr(row, "episode_end", None) is None
                    and not is_non_episode_video_path(getattr(row, "file_path", None))
                    for row in resource.file_assignments
                )
                mixed_shape_needs_repair = resource.batch_scope == "movies" and (
                    any(getattr(row, "series_id", None) for row in resource.file_assignments)
                    and any(getattr(row, "movie_id", None) for row in resource.file_assignments)
                )
                if not unresolved_extras and not mixed_shape_needs_repair:
                    return False
    url = resource.torrent_url or ""
    cached_path = resource.torrent_file
    if (cached_path and await asyncio.to_thread(Path(cached_path).exists)
            and await asyncio.to_thread(parse_torrent_files, cached_path) is None):
        # Be defensive for callers that invoke inspection without the normal
        # ensure_torrent_cached pre-pass: a stale HTML/error page must never
        # block a later valid fetch forever.
        try:
            await asyncio.to_thread(Path(cached_path).unlink)
        except OSError:
            pass
        resource.torrent_file = None
        cached_path = None
    if not (cached_path and await asyncio.to_thread(Path(cached_path).exists)) and not url.startswith(("http://", "https://")):
        return False

    try:
        path = resource.torrent_file
        if not (path and await asyncio.to_thread(Path(path).exists)):
            if db is None or not hasattr(db, "execute"):
                path = await fetch_torrent_file(url, resource.id)
            else:
                from app.services.resource_listing import fetch_resource_torrent

                listing = await fetch_resource_torrent(db, resource)
                path = resource.torrent_file if listing.files else None
            if not path:
                return False
            resource.torrent_file = path

        files = await asyncio.to_thread(parse_torrent_files, path)
        if files is None:
            return False
        report = await asyncio.to_thread(analyze_torrent_files, files)
        return await apply_torrent_report(db, resource, report, channel)
    except Exception as e:
        logger.debug("[torrent] inspect failed for %s: %s", resource.id, e)
        return False


# ---------------------------------------------------------------------------
# Cache lifecycle: daily sweep of unreferenced .torrent files
# ---------------------------------------------------------------------------

# Cache filenames are ``<resource_id>-<suffix>.torrent``: the suffix is the
# content sha256 (``fetch_torrent_file``) or the resolution attempt id
# (magnet resolve). Only files matching this exact shape are sweep
# candidates — legacy ``<resource_id>.torrent`` paths, in-flight
# ``.<uuid>.torrent.tmp`` writes and any foreign file are never touched.
_CACHE_NAME_RE = re.compile(
    r"^(?P<rid>[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12})-[^/]+\.torrent$"
)

# Paging for the resource-reference IN lookup.
_SWEEP_ID_BATCH = 500

# In-flight grace: a cache file younger than this is never deleted. Writers
# publish the file BEFORE the referencing ``torrent_file`` commit lands
# (fetch pipeline, magnet resolution, organize manifest backfill), so age
# alone must never condemn a file — deletion always requires age PLUS a
# missing/stale DB reference.
SWEEP_MIN_AGE_SECONDS = 3600


async def _sweep_referenced_paths(
    db: "AsyncSession", resource_ids: list[str]
) -> dict[str, str | None]:
    """Map each candidate resource id to its current ``torrent_file``.

    Ids absent from ``file_resources`` (deleted resources) are simply missing
    from the result. Paged IN queries keep the lookup bounded. Raises on DB
    failure — the caller aborts the whole round rather than deleting files
    whose reference state is unknown.
    """
    from sqlalchemy import select

    from app.models.file_resource import FileResource

    referenced: dict[str, str | None] = {}
    for offset in range(0, len(resource_ids), _SWEEP_ID_BATCH):
        chunk = resource_ids[offset:offset + _SWEEP_ID_BATCH]
        rows = (await db.execute(
            select(FileResource.id, FileResource.torrent_file)
            .where(FileResource.id.in_(chunk))
        )).all()
        for rid, torrent_file in rows:
            referenced[rid] = torrent_file
    return referenced


async def sweep_torrent_cache(
    db: "AsyncSession", *, min_age_seconds: int = SWEEP_MIN_AGE_SECONDS
) -> dict:
    """Delete unreferenced .torrent cache files (daily cleanup sub-step).

    A file directly inside ``settings.torrent_cache_dir`` is deleted only
    when ALL hold:

    - its name matches ``<resource_id>-<suffix>.torrent`` (cache shape);
    - its mtime is older than ``min_age_seconds`` (in-flight write/commit
      grace — never delete on age alone);
    - its resource id is gone from ``file_resources`` (orphan), OR the
      resource's current ``torrent_file`` points at a DIFFERENT path
      (superseded version, e.g. an older magnet attempt or a re-fetch with
      new content).

    Files still referenced by the database are never deleted whatever their
    age; files outside the cache dir and non-matching names (legacy paths,
    ``.tmp``) are never touched. A DB failure aborts the whole round with
    nothing deleted; a per-file delete failure is logged and skipped. The
    session is used read-only — nothing is committed here. Returns a summary
    dict (``scanned``/``deleted``/``errors``/``aborted``).
    """
    summary = {"scanned": 0, "deleted": 0, "errors": 0, "aborted": False}
    if not settings.torrent_cache_dir:
        return summary
    cache_dir = Path(settings.torrent_cache_dir)

    def _scan() -> list[tuple[Path, str]]:
        try:
            entries = list(cache_dir.iterdir())
        except FileNotFoundError:
            return []
        except OSError as e:
            logger.warning("[torrent] cache sweep: cannot list %s: %s", cache_dir, e)
            return []
        cutoff = time.time() - min_age_seconds
        out: list[tuple[Path, str]] = []
        for path in entries:
            if not path.is_file():
                continue
            match = _CACHE_NAME_RE.match(path.name)
            if match is None:
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime >= cutoff:
                continue  # in-flight grace window
            out.append((path, match.group("rid")))
        return out

    candidates = await asyncio.to_thread(_scan)
    summary["scanned"] = len(candidates)
    if not candidates:
        return summary

    try:
        referenced = await _sweep_referenced_paths(
            db, [rid for _, rid in candidates]
        )
    except Exception as e:
        # Rather delete nothing than risk a referenced file.
        logger.warning("[torrent] cache sweep aborted, reference lookup failed: %s", e)
        summary["aborted"] = True
        return summary

    for path, rid in candidates:
        current = referenced.get(rid)
        if current is not None and os.path.abspath(current) == os.path.abspath(str(path)):
            continue  # still the authoritative DB reference
        try:
            await asyncio.to_thread(path.unlink)
            summary["deleted"] += 1
        except FileNotFoundError:
            pass  # concurrently removed — already the desired end state
        except OSError as e:
            summary["errors"] += 1
            logger.warning("[torrent] cache sweep: could not delete %s: %s", path, e)
    if summary["deleted"]:
        logger.info(
            "[torrent] cache sweep deleted %d unreferenced file(s) in %s",
            summary["deleted"], cache_dir,
        )
    return summary
