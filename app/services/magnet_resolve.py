"""Magnet metadata resolution — fetch torrent *metadata only* via libtorrent.

magnet: links carry no .torrent file, so the plain http(s) fetch path
(``torrent_inspect.fetch_torrent_file``) can never produce a cached .torrent
or a file listing for them. This module resolves the metadata in the
background: a process-lazy ``lt.session`` downloads just the info dictionary
(``upload_mode``, no payload), and the received metadata is rebuilt into a
standard .torrent file in the regular torrent cache (``<resource_id>.torrent``
under ``settings.torrent_cache_dir``). From then on the resource flows through
the normal Channel A inspection / file-listing paths unchanged.

libtorrent is an optional-at-runtime dependency: every import is guarded and
the feature degrades to a one-time log line plus no status transitions when
it is missing (the manual-retry API returns 422 instead).

Concurrency: queue jobs (``resolve_magnet_torrent``) only *claim* the resource
row and spawn a detached worker task — the 15-minute metadata wait never
occupies a queue slot. The DB status claim (a guarded UPDATE) is the real
cross-process dedup; the in-process ``_inflight`` set and the semaphore bound
local concurrency to ``settings.magnet_resolve_concurrency``.
"""

import asyncio
import hashlib
import logging
import re
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import bencodepy
import httpx

from app.config import settings
from app.services.torrent_inspect import (
    _MAX_TORRENT_BYTES,
    parse_torrent_files,
    parse_torrent_payload,
)
from app.utils.time import utcnow

logger = logging.getLogger(__name__)

try:  # optional-at-runtime dependency (see module docstring)
    import libtorrent as lt
except ImportError:  # pragma: no cover — exercised only without the wheel
    lt = None  # type: ignore[assignment]


class MagnetResolveError(Exception):
    """Human-readable magnet resolution failure (short message, UI-visible)."""


class MagnetUnavailableError(MagnetResolveError):
    """libtorrent is not importable in this environment."""


class InvalidMagnetError(MagnetResolveError):
    """The magnet URI cannot be parsed."""


class MagnetTimeoutError(MagnetResolveError):
    """No metadata arrived within the overall timeout."""


class MagnetMetadataError(MagnetResolveError):
    """Peers were reached but the metadata could not be retrieved/rebuilt."""


_unavailable_logged = False


def _log_unavailable_once() -> None:
    global _unavailable_logged
    if not _unavailable_logged:
        _unavailable_logged = True
        logger.warning(
            "[magnet] libtorrent is not installed; magnet metadata resolution disabled"
        )


# ---------------------------------------------------------------------------
# Tracker validation / merge
# ---------------------------------------------------------------------------

TRACKER_MAX_COUNT = 20
_TRACKER_MAX_URL_LENGTH = 200
_TRACKER_SCHEMES = {"udp", "http", "https"}


def validate_tracker_urls(trackers: list[str]) -> list[str]:
    """Validate and clean a user-supplied tracker list.

    Strips surrounding whitespace; each entry must use an allowed scheme
    (udp/http/https), carry a non-empty host, contain no whitespace/control
    characters, and stay under the per-URL length cap. Raises ValueError
    naming the first offending URL.
    """
    if len(trackers) > TRACKER_MAX_COUNT:
        raise ValueError(f"too many trackers (max {TRACKER_MAX_COUNT})")
    cleaned: list[str] = []
    for raw in trackers:
        url = raw.strip()
        if not url:
            raise ValueError("invalid tracker URL: (empty)")
        if len(url) > _TRACKER_MAX_URL_LENGTH:
            raise ValueError(f"invalid tracker URL (too long): {url[:50]}...")
        if any(c.isspace() or ord(c) < 0x20 or ord(c) == 0x7F for c in url):
            raise ValueError(f"invalid tracker URL (whitespace/control char): {url}")
        parts = urlsplit(url)
        if parts.scheme.lower() not in _TRACKER_SCHEMES:
            raise ValueError(f"invalid tracker URL (scheme): {url}")
        if not parts.netloc:
            raise ValueError(f"invalid tracker URL (no host): {url}")
        cleaned.append(url)
    return cleaned


def merge_trackers(
    magnet_trackers: list[str], custom: list[str], defaults: list[str]
) -> list[str]:
    """Merge tracker lists: magnet's own → custom → defaults.

    Exact-string dedup preserving the first occurrence.
    """
    merged: list[str] = []
    seen: set[str] = set()
    for url in (*magnet_trackers, *custom, *defaults):
        if url not in seen:
            seen.add(url)
            merged.append(url)
    return merged


# ---------------------------------------------------------------------------
# Infohash-cache mirror fast path (HTTPS, no P2P)
# ---------------------------------------------------------------------------

# This is a fast path — a long wait defeats it, so mirrors get a short timeout.
_MIRROR_TIMEOUT = 15

_BTIH_V1_RE = re.compile(r"xt=urn:btih:([0-9a-fA-F]{40})")


def _extract_v1_infohash(magnet_uri: str, params) -> str | None:
    """v1 SHA-1 infohash (lowercase 40-hex) from parsed magnet params.

    Prefers libtorrent's parsed ``info_hashes``; falls back to a regex on
    the URI when the params object lacks it (fake bindings in tests). base32
    and v2-only (``btmh``) magnets yield None — the mirror fast path simply
    does not apply to them.
    """
    info_hashes = getattr(params, "info_hashes", None)
    if info_hashes is not None:
        try:
            if info_hashes.has_v1():
                return str(info_hashes.v1)
            return None
        except Exception:  # noqa: BLE001 — fall through to the regex
            pass
    m = _BTIH_V1_RE.search(magnet_uri)
    return m.group(1).lower() if m else None


def _fetch_mirror_torrent(url: str) -> bytes | None:
    """Sync HTTPS GET of a mirror .torrent (invoked via ``asyncio.to_thread``).

    Same client pattern and UA as ``torrent_inspect.fetch_torrent_file``;
    non-200 / network error / oversized body → None (next mirror).
    """
    try:
        ua = (
            f"{settings.app_name}/0.1.0 "
            "(https://github.com/RobinQu/RSSRipple) torrent-inspect"
        )
        with httpx.Client(
            timeout=_MIRROR_TIMEOUT, follow_redirects=True, headers={"User-Agent": ua}
        ) as client, client.stream("GET", url) as resp:
            if resp.status_code != 200:
                logger.debug("[magnet] mirror %s -> HTTP %s", url[:80], resp.status_code)
                return None
            chunks: list[bytes] = []
            total = 0
            for chunk in resp.iter_bytes():
                total += len(chunk)
                if total > _MAX_TORRENT_BYTES:
                    logger.warning("[magnet] oversized mirror body (>50MB) %s", url[:80])
                    return None
                chunks.append(chunk)
            return b"".join(chunks)
    except Exception as e:
        logger.debug("[magnet] mirror fetch failed %s: %s", url[:80], e)
        return None


def _verify_mirror_payload(raw: bytes, infohash: str) -> bool:
    """Structural parse + mandatory v1 infohash re-computation.

    Mirrors (itorrents) answer HTTP 200 with an *unrelated* valid torrent for
    hashes they do not have, so re-verifying the returned torrent's infohash
    against the requested one is mandatory. ``bencodepy.encode`` emits
    canonical sorted-key bencode, so ``sha1(encode(info))`` equals the true
    infohash.
    """
    if parse_torrent_payload(raw) is None:
        return False
    try:
        decoded = bencodepy.decode(raw)
        digest = hashlib.sha1(bencodepy.encode(decoded[b"info"])).hexdigest()
    except Exception:  # noqa: BLE001 — parse_torrent_payload already validated
        return False
    return digest == infohash.lower()


async def _try_cache_mirrors(magnet_uri: str, params, dest_path: str) -> bool:
    """HTTPS infohash-cache fast path. Returns True when *dest_path* was written.

    Never raises: any per-mirror failure degrades to the P2P path. Skipped
    silently when no mirrors are configured or the magnet has no v1 hash.
    """
    mirrors = settings.magnet_resolve_cache_mirrors
    if not mirrors:
        return False
    infohash = _extract_v1_infohash(magnet_uri, params)
    if infohash is None:
        return False
    for mirror in mirrors:
        if "{infohash}" not in mirror:
            continue
        url = mirror.replace("{infohash}", infohash)
        try:
            raw = await asyncio.to_thread(_fetch_mirror_torrent, url)
            if raw is None:
                continue
            if not _verify_mirror_payload(raw, infohash):
                logger.warning(
                    "[magnet] mirror %s returned a wrong/unusable torrent; discarding",
                    url[:80],
                )
                continue
            dest = Path(dest_path)
            dest.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(dest.write_bytes, raw)
            logger.info("[magnet] resolved via cache mirror %s", url[:80])
            return True
        except Exception:  # noqa: BLE001 — fast path must never raise
            logger.debug("[magnet] mirror attempt failed %s", url[:80], exc_info=True)
    return False


# ---------------------------------------------------------------------------
# libtorrent session (process-lazy singleton)
# ---------------------------------------------------------------------------

_session = None


def _get_session():
    """Create the shared lt.session on first use.

    Metadata-only workload: DHT/PEX/LSD on for peer discovery, no UPnP/NAT-PMP
    (we never need inbound connectivity for long), and tiny rate limits since
    no payload is ever downloaded (upload_mode).
    """
    global _session
    if lt is None:
        _log_unavailable_once()
        raise MagnetUnavailableError("libtorrent is not installed")
    if _session is None:
        _session = lt.session({
            "enable_dht": True,
            "enable_lsd": True,
            "enable_upnp": False,
            "enable_natpmp": False,
            "connections_limit": 200,
            "download_rate_limit": 64 * 1024,
            "upload_rate_limit": 64 * 1024,
        })
    return _session


async def _poll_metadata(handle) -> None:
    """Wait until the handle has the torrent metadata (or a fatal error)."""
    while True:
        status = await asyncio.to_thread(handle.status)
        if getattr(status, "has_metadata", False):
            return
        errc = getattr(status, "errc", None)
        if errc is not None:
            try:
                if errc.value() != 0:
                    raise MagnetMetadataError(
                        f"metadata fetch failed: {errc.message()}"
                    )
            except AttributeError:
                pass
        await asyncio.sleep(2)


async def resolve_magnet_to_cache(
    magnet_uri: str,
    dest_path: str,
    timeout_seconds: int,
    *,
    extra_trackers: list[str] | None = None,
) -> None:
    """Fetch torrent metadata for *magnet_uri* and write a .torrent to *dest_path*.

    ``extra_trackers`` (validated custom trackers from the manual retry) are
    merged with the magnet's own trackers and the configured default public
    trackers — harvested magnets (e.g. ThePirateBay) carry no ``tr=`` params,
    leaving DHT as the only peer source otherwise.

    Step 0 (before any P2P work): the infohash-cache mirror fast path —
    a verified HTTPS hit writes *dest_path* and returns immediately.

    Raises :class:`MagnetResolveError` (or a subclass) on any failure; raw
    libtorrent errors never escape. On success *dest_path* holds a valid
    .torrent that ``parse_torrent_payload`` accepts.
    """
    if lt is None:
        _log_unavailable_once()
        raise MagnetUnavailableError("libtorrent is not installed")

    try:
        params = await asyncio.to_thread(lt.parse_magnet_uri, magnet_uri)
    except Exception as e:
        raise InvalidMagnetError(f"invalid magnet link: {e}") from e

    # Step 0: HTTPS infohash-cache mirror fast path. A verified hit writes
    # dest_path and short-circuits the entire libtorrent/P2P phase below.
    if await _try_cache_mirrors(magnet_uri, params, dest_path):
        return

    merged = merge_trackers(
        list(getattr(params, "trackers", None) or []),
        extra_trackers or [],
        settings.magnet_resolve_default_trackers,
    )
    # libtorrent 2.x keeps tracker_tiers parallel to trackers — assign both
    # explicitly so the lists never go out of sync.
    params.trackers = merged
    params.tracker_tiers = list(range(len(merged)))

    session = _get_session()
    # Metadata only: upload_mode tells libtorrent never to request payload
    # pieces — we leave as soon as the info dictionary arrives.
    params.flags |= lt.torrent_flags.upload_mode
    tmp_dir = tempfile.mkdtemp(prefix="rssripple-magnet-")
    params.save_path = tmp_dir
    handle = None
    try:
        handle = await asyncio.to_thread(session.add_torrent, params)
        try:
            await asyncio.wait_for(_poll_metadata(handle), timeout=timeout_seconds)
        except TimeoutError as e:
            raise MagnetTimeoutError(
                f"no metadata received within {timeout_seconds}s"
            ) from e

        def _rebuild() -> bytes:
            ti = handle.torrent_file()
            ct = lt.create_torrent(ti)
            # torrent_info from magnet metadata does not carry the trackers
            # the magnet itself listed — re-add them so the rebuilt .torrent
            # stays usable on its own.
            for tracker in getattr(params, "trackers", None) or []:
                ct.add_tracker(tracker)
            return lt.bencode(ct.generate())

        raw = await asyncio.to_thread(_rebuild)
        if parse_torrent_payload(raw) is None:
            raise MagnetMetadataError("rebuilt torrent metadata is unusable")
        dest = Path(dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(dest.write_bytes, raw)
    except MagnetResolveError:
        raise
    except Exception as e:
        raise MagnetMetadataError(f"metadata resolution failed: {e}") from e
    finally:
        if handle is not None:
            try:
                await asyncio.to_thread(session.remove_torrent, handle)
            except Exception:  # noqa: BLE001 — cleanup must never raise
                pass
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Worker pool / launcher
# ---------------------------------------------------------------------------

_semaphore: asyncio.Semaphore | None = None
_inflight: set[str] = set()
_background_tasks: set[asyncio.Task] = set()


def _get_semaphore() -> asyncio.Semaphore:
    """Lazily create the concurrency semaphore inside the running event loop."""
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(settings.magnet_resolve_concurrency)
    return _semaphore


def _has_usable_cache(resource) -> bool:
    """Same validity rule as ``ensure_torrent_cached``: path exists and parses."""
    path = resource.torrent_file
    return bool(path and Path(path).exists() and parse_torrent_files(path) is not None)


async def launch_resolution(resource_id: str) -> bool:
    """Claim *resource_id* for resolution and spawn the detached worker task.

    The claim is an atomic guarded UPDATE, so it is safe across processes
    (3-worker deployment): only the first caller transitions the row out of a
    non-active state. Returns False when the row is already pending/running
    (or the feature is off / libtorrent is missing).
    """
    if lt is None:
        _log_unavailable_once()
        return False
    if not settings.magnet_resolve_enabled:
        return False
    if resource_id in _inflight:
        return False

    from sqlalchemy import or_, update

    from app.database import committed_session
    from app.models.file_resource import FileResource

    async with committed_session() as db:
        result = await db.execute(
            update(FileResource)
            .where(
                FileResource.id == resource_id,
                or_(
                    FileResource.magnet_resolve_status.is_(None),
                    FileResource.magnet_resolve_status.notin_(("pending", "running")),
                ),
            )
            .values(
                magnet_resolve_status="pending",
                magnet_resolve_updated_at=utcnow(),
            )
        )
        if result.rowcount == 0:
            return False

    _inflight.add(resource_id)
    task = asyncio.create_task(_run_resolution(resource_id))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return True


async def _run_resolution(resource_id: str) -> None:
    try:
        async with _get_semaphore():
            await _attempt_loop(resource_id)
    except Exception:  # noqa: BLE001 — last-resort guard for the detached task
        logger.warning(
            "[magnet] resolution task crashed for %s", resource_id, exc_info=True
        )
    finally:
        _inflight.discard(resource_id)


async def _set_status(resource_id: str, **values) -> bool:
    """Write status fields in their own short transaction.

    Returns False when the row disappeared (resource deleted mid-flight).
    """
    from sqlalchemy import update

    from app.database import committed_session
    from app.models.file_resource import FileResource

    async with committed_session() as db:
        result = await db.execute(
            update(FileResource)
            .where(FileResource.id == resource_id)
            .values(magnet_resolve_updated_at=utcnow(), **values)
        )
        return result.rowcount > 0


async def _attempt_loop(resource_id: str) -> None:
    """Run resolve attempts for a claimed resource until done/failed."""
    from app.database import committed_session
    from app.models.channel import Channel
    from app.models.file_resource import FileResource
    from app.services.torrent_inspect import maybe_inspect_torrent

    max_attempts = 1 + settings.magnet_resolve_max_attempts
    while True:
        if not await _set_status(resource_id, magnet_resolve_status="running"):
            return
        async with committed_session() as db:
            resource = await db.get(FileResource, resource_id)
            magnet_uri = (resource.torrent_url or "") if resource is not None else ""
            # Custom trackers from the manual-retry endpoint (NULL = defaults).
            extra_trackers = (
                list(resource.magnet_resolve_trackers)
                if resource is not None and resource.magnet_resolve_trackers
                else None
            )
        if not magnet_uri.startswith("magnet:"):
            return
        dest = str(Path(settings.torrent_cache_dir) / f"{resource_id}.torrent")
        try:
            await resolve_magnet_to_cache(
                magnet_uri, dest, settings.magnet_resolve_timeout_seconds,
                extra_trackers=extra_trackers,
            )
        except MagnetResolveError as e:
            async with committed_session() as db:
                resource = await db.get(FileResource, resource_id)
                if resource is None:
                    return
                attempts = (resource.magnet_resolve_attempts or 0) + 1
                exhausted = attempts >= max_attempts
                resource.magnet_resolve_attempts = attempts
                resource.magnet_resolve_error = str(e)
                resource.magnet_resolve_status = "failed" if exhausted else "pending"
                resource.magnet_resolve_updated_at = utcnow()
            logger.info(
                "[magnet] attempt %d/%d failed for %s: %s",
                attempts, max_attempts, resource_id, e,
            )
            if exhausted:
                return
            await asyncio.sleep(60)
            continue
        except Exception as e:  # noqa: BLE001 — never leak a raw crash
            logger.warning(
                "[magnet] unexpected failure for %s", resource_id, exc_info=True
            )
            await _set_status(
                resource_id,
                magnet_resolve_status="failed",
                magnet_resolve_error=f"unexpected error: {e}",
            )
            return

        # Success: cache path + terminal status first, then the same Channel A
        # inspection call site as the fetch pipeline (fresh session, committed
        # on exit). Custom trackers are cleared on done; on failure they are
        # kept so the UI can show what was tried and prefill the next retry.
        await _set_status(
            resource_id,
            torrent_file=dest,
            magnet_resolve_status="done",
            magnet_resolve_error=None,
            magnet_resolve_trackers=None,
        )
        logger.info("[magnet] resolved metadata for %s -> %s", resource_id, dest)
        try:
            async with committed_session() as db:
                resource = await db.get(FileResource, resource_id)
                if resource is None:
                    return
                channel = await db.get(Channel, resource.channel_id)
                await maybe_inspect_torrent(db, resource, channel)
        except Exception:  # noqa: BLE001 — inspection failure must not flip done
            logger.warning(
                "[magnet] post-resolve inspection failed for %s",
                resource_id, exc_info=True,
            )
        return


async def enqueue_resolution(resource_id: str) -> None:
    """Enqueue a ``resolve_magnet_torrent`` queue job for *resource_id*.

    Used by the fetch path, the hourly sweep and the manual-retry endpoint.
    Skips silently when the feature is disabled, libtorrent is missing, the
    resource is not a magnet link, or a usable .torrent is already cached.
    A ``None`` return from ``enqueue`` (same key already active) is fine —
    the DB status claim in ``launch_resolution`` is the real dedup.
    """
    if not settings.magnet_resolve_enabled:
        return
    if lt is None:
        _log_unavailable_once()
        return

    from app.database import async_session_factory
    from app.models.file_resource import FileResource

    async with async_session_factory() as db:
        resource = await db.get(FileResource, resource_id)
        if resource is None:
            return
        if not (resource.torrent_url or "").startswith("magnet:"):
            return
        if _has_usable_cache(resource):
            return

    from app.services.task_queue import task_queue

    await task_queue.enqueue(
        "resolve_magnet_torrent",
        f"magnet:{resource_id}",
        {"resource_id": resource_id},
    )
