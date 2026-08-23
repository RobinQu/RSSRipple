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
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import bencodepy
import httpx

from app.config import settings
from app.services.resource_parser import extract_season_episode_from_path

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.channel import Channel
    from app.models.file_resource import FileResource

logger = logging.getLogger(__name__)

# Hard caps for the .torrent download: a real torrent is typically < 1 MB;
# 50 MB is far above anything legitimate and guards against abusive payloads.
_MAX_TORRENT_BYTES = 50 * 1024 * 1024
_DOWNLOAD_TIMEOUT = 10

# Files smaller than this are never a main feature (samples, previews, menus).
_MIN_VIDEO_SIZE = 50 * 1024 * 1024

# Common video container extensions. Disc images (iso) and BD stream files
# (m2ts) are deliberately excluded: an ISO is a whole-disc dump whose episode
# structure is invisible, and m2ts files live inside BDMV structures that the
# extras-dir filter would only half-catch.
_VIDEO_EXTS = {
    "mkv", "mp4", "avi", "ts", "wmv", "flv", "mov", "webm", "mpg", "mpeg", "rmvb", "vob",
}

# Directory names whose contents are extras rather than main episodes.
# Matched case-insensitively against whole path components only.
_EXTRAS_DIRS = {
    "sample", "samples",
    "特典", "映像特典",
    "sp", "sps", "special", "specials",
    "op", "ed", "ncop", "nced",
    "extra", "extras", "menu", "menus", "scans",
}


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
    video_file_count: int = 0
    unparsed_ratio: float = 0.0


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

async def fetch_torrent_file(url: str, resource_id: str) -> str | None:
    """Download a .torrent file into ``settings.torrent_cache_dir``.

    Only plain http(s) URLs are fetched. The file is stored as
    ``<resource_id>.torrent`` and the local path is returned (relative to the
    data root with the default config, mirroring how the poster cache hands
    back its cache-dir path). Returns None on any failure: non-http(s) URL,
    non-200 status, body over 50 MB, timeout (10 s), or write error.
    """
    if not url or not (url.startswith("http://") or url.startswith("https://")):
        return None
    if not settings.torrent_cache_dir:
        return None

    cache_dir = Path(settings.torrent_cache_dir)
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        logger.warning("[torrent] cache dir not writable %s: %s", cache_dir, e)
        return None

    def _download() -> bytes | None:
        try:
            ua = (
                f"{settings.app_name}/0.1.0 "
                f"(https://github.com/RobinQu/RSSRipple) torrent-inspect"
            )
            with httpx.Client(
                timeout=_DOWNLOAD_TIMEOUT, follow_redirects=True, headers={"User-Agent": ua}
            ) as client, client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    logger.debug("[torrent] %s -> HTTP %s", url[:80], resp.status_code)
                    return None
                chunks: list[bytes] = []
                total = 0
                for chunk in resp.iter_bytes():
                    total += len(chunk)
                    if total > _MAX_TORRENT_BYTES:
                        logger.warning("[torrent] oversized body (>50MB) %s", url[:80])
                        return None
                    chunks.append(chunk)
                return b"".join(chunks)
        except Exception as e:
            logger.warning("[torrent] download failed %s: %s", url[:80], e)
            return None

    content = await asyncio.to_thread(_download)
    if not content:
        return None

    dest = cache_dir / f"{resource_id}.torrent"
    try:
        dest.write_bytes(content)
        return str(dest)
    except Exception as e:
        logger.warning("[torrent] write failed %s: %s", dest, e)
        return None


async def ensure_torrent_cached(resource: "FileResource") -> str | None:
    """Best-effort .torrent caching for a resource during fetch.

    Downloads ``resource.torrent_url`` into the cache dir when it is a plain
    http(s) direct link and no usable cache exists yet (``torrent_file`` is
    empty or points at a file that has since been deleted). On success the
    local path is written to ``resource.torrent_file``. Magnets and any
    download/write failure are silent (returns None). Does NOT commit — the
    caller's session owns the transaction.
    """
    path = resource.torrent_file
    if path and Path(path).exists():
        return path
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
        decoded = bencodepy.decode(raw)
    except Exception as e:
        logger.debug("[torrent] bencode decode failed %s: %s", path, e)
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
        return out

    name = info.get(b"name.utf-8") or info.get(b"name")
    length = info.get(b"length")
    if name is None or not isinstance(length, int):
        return None
    return [{"name": _decode_text(name), "size": length}]


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
    name = entry.get("name") or ""
    components = [c for c in re.split(r"[/\\]+", name) if c]
    if not components:
        return False
    stem, dot, ext = components[-1].rpartition(".")
    if not dot or ext.lower() not in _VIDEO_EXTS:
        return False
    # Extras directories: any whole component matching the blocklist.
    for comp in components[:-1]:
        if comp.strip().lower() in _EXTRAS_DIRS:
            return False
    if (entry.get("size") or 0) < _MIN_VIDEO_SIZE:
        return False
    return True


# Cluster-title normalization: strip bracketed release tags, season tokens and
# episode ranges from a top-level directory name; what remains is compared
# across clusters.
_BRACKET_BLOCK_RE = re.compile(r"[\[【\(（][^\]】\)）]*[\]】\)）]")
_RANGE_TOKEN_RE = re.compile(r"\b\d{1,3}\s*[~\-–～〜]\s*\d{1,3}\b")
_SEASON_TOKEN_RE = re.compile(r"(?:\bS\d{1,2}\b|\bSeason\s*\d+\b|第\s*\d+\s*季)", re.IGNORECASE)
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
    t = _SEASON_TOKEN_RE.sub(" ", t)
    t = _RANGE_TOKEN_RE.sub(" ", t)
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
            "season": season,
            "episode": episode,
        }
        for f, season, episode in parsed
    ]
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
    if len(seasons) >= 2:
        base.scope = "multi_season"
        base.is_batch = True
        return base

    # Single season: all parsed files share one season group (explicit season
    # number, or None for a flat unlabeled run) with >= 2 distinct episodes.
    episodes = [e for _, _, e in parsed if e is not None]
    season_groups = {s for _, s, e in parsed if e is not None}
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
      ``batch_seasons`` from the report.
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
                return False
    url = resource.torrent_url or ""
    cached_path = resource.torrent_file
    if not (cached_path and Path(cached_path).exists()) and not url.startswith(("http://", "https://")):
        return False

    try:
        # Reuse an already-cached .torrent (``ensure_torrent_cached`` runs
        # first in the fetch pipeline); only download when there is no
        # usable cache on disk.
        path = resource.torrent_file
        if not (path and Path(path).exists()):
            path = await fetch_torrent_file(url, resource.id)
            if not path:
                return False
            resource.torrent_file = path

        files = parse_torrent_files(path)
        if files is None:
            return False
        report = analyze_torrent_files(files)

        if report.scope == "season":
            resource.is_batch = True
            resource.batch_scope = "season"
            resource.episode = None
            resource.episode_start = report.episode_start
            resource.episode_end = report.episode_end
        elif report.scope == "multi_season":
            resource.is_batch = True
            resource.batch_scope = "multi_season"
            resource.episode = None
            resource.season = None
            resource.episode_start = None
            resource.episode_end = None
            # Persist the covered seasons — the agent runner's batch dedup
            # keys on the exact coverage (see _batch_coverage_key).
            resource.batch_seasons = report.seasons or None
        elif report.scope == "franchise":
            resource.is_batch = True
            resource.batch_scope = "franchise"
            resource.episode = None
            resource.batch_seasons = report.seasons or None

        # Enrichment pass for every resource type: all main video files get a
        # durable assignment row. Batch-only LLM refinement may subsequently
        # bind ambiguous multi-work rows.
        llm_bound_movies = False
        if hasattr(resource, "file_assignments"):
            from app.services import batch_content_analysis as bca

            try:
                await db.refresh(resource, ["file_assignments"])
            except Exception:  # noqa: BLE001 — pending row edge
                pass
            bca.apply_auto_assignments(resource, report)
            resource.season_ranges = bca.compute_season_ranges(resource)

            if resource.is_batch and bca.llm_refinement_needed(report, resource.batch_scope):
                try:
                    llm_bound_movies = await bca.refine_batch_content(
                        db, resource, report, channel
                    )
                except Exception as e:  # noqa: BLE001 — refinement degrades silently
                    logger.warning(
                        "[torrent] LLM refinement failed for %s: %s", resource.id, e
                    )
                if llm_bound_movies:
                    resource.season_ranges = bca.compute_season_ranges(resource)

            # Member-work linking + collection attach — only for packs that
            # are NOT a pure-movie bundle (movie packs link per-file movie
            # rows instead of a collection). Isolated from the verdict above:
            # a linking failure must not lose the batch classification.
        if resource.is_batch and resource.batch_scope == "franchise" and not llm_bound_movies:
            try:
                from app.services.franchise_service import link_franchise_pack

                await link_franchise_pack(db, resource, report, channel)
            except Exception as e:
                logger.warning(
                    "[torrent] franchise linking failed for %s: %s", resource.id, e
                )
        # "single" / "unknown": keep the verdict, only the torrent_file cache.
        return report.is_batch
    except Exception as e:
        logger.debug("[torrent] inspect failed for %s: %s", resource.id, e)
        return False
