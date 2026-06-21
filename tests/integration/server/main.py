"""Integration test server — FastAPI app combining all test services.

Endpoints:
- RSS feeds: GET /rss/dmhy, /rss/mikanani, /rss/eztv, /rss/movies
- Tracker: GET /announce, /scrape
- Torrent files: GET /torrents/{hash}.torrent
- Test files: GET /files/{path}
- Torrent API: POST /api/torrents/create, /seed, /download, GET /status, POST /assert-complete
- Health: GET /health
"""

from __future__ import annotations

import logging
from urllib.parse import unquote

from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import PlainTextResponse

from .rss_server import (
    generate_dmhy_feed,
    generate_mikanani_feed,
    generate_eztv_feed,
    generate_movie_feed,
)
from .test_data import (
    generate_all_test_files,
    generate_anime_releases,
    generate_tv_releases,
    generate_movie_releases,
)
from .tracker import Tracker
from .torrent_service import TorrentService

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="RSSRipple Integration Test Server")

# ─── Global State ────────────────────────────────────────────────────

SERVER_URL = "http://test-server:8080"
TRACKER_URL = "http://test-server:8080/announce"

tracker = Tracker(interval=15)
torrent_service = TorrentService(base_dir="/tmp/torrent-test")
torrent_service.set_tracker_url(TRACKER_URL)

# Pre-generate all test files on startup
test_files = generate_all_test_files()
for name, tf in test_files.items():
    torrent_service.save_test_file(name, tf.content)


# ─── Health ──────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "test_files": len(test_files),
        "torrents": len(torrent_service.jobs),
        "tracker_swarms": len(tracker.swarms),
    }


# ─── RSS Feed Endpoints ─────────────────────────────────────────────

@app.get("/rss/dmhy")
async def rss_dmhy(series: int = 0):
    """dmhy.org-style anime RSS feed (magnet links)."""
    releases = generate_anime_releases(series_index=series, episode_count=6)
    xml = generate_dmhy_feed(
        releases=releases,
        server_url=SERVER_URL,
        tracker_url=TRACKER_URL,
    )
    return Response(content=xml, media_type="application/rss+xml; charset=utf-8")


@app.get("/rss/mikanani")
async def rss_mikanani(series: int = 1):
    """mikanani.me-style anime RSS feed (.torrent files)."""
    releases = generate_anime_releases(series_index=series, episode_count=6)
    xml = generate_mikanani_feed(
        releases=releases,
        server_url=SERVER_URL,
    )
    return Response(content=xml, media_type="application/rss+xml; charset=utf-8")


@app.get("/rss/eztv")
async def rss_eztv(show: int = 0):
    """EZTV-style TV show RSS feed (magnet + .torrent)."""
    releases = generate_tv_releases(show_index=show, episode_count=6)
    xml = generate_eztv_feed(
        releases=releases,
        server_url=SERVER_URL,
        tracker_url=TRACKER_URL,
    )
    return Response(content=xml, media_type="application/rss+xml; charset=utf-8")


@app.get("/rss/movies")
async def rss_movies():
    """Movie RSS feed with IMDB-style metadata."""
    releases = []
    for mi in range(3):
        releases.extend(generate_movie_releases(movie_index=mi))
    xml = generate_movie_feed(
        releases=releases,
        server_url=SERVER_URL,
        tracker_url=TRACKER_URL,
    )
    return Response(content=xml, media_type="application/rss+xml; charset=utf-8")


# ─── BitTorrent Tracker ─────────────────────────────────────────────

@app.get("/announce")
async def tracker_announce(request: Request):
    """BitTorrent HTTP tracker announce endpoint (BEP 3)."""
    params = request.query_params

    info_hash_raw = params.get("info_hash", "")
    # URL-decode the 20-byte binary info_hash
    info_hash = unquote(info_hash_raw).encode("latin-1").hex()

    peer_id = unquote(params.get("peer_id", "")).encode("latin-1")[:20]
    port = int(params.get("port", 6881))
    uploaded = int(params.get("uploaded", 0))
    downloaded = int(params.get("downloaded", 0))
    left = int(params.get("left", 0))
    event = params.get("event", "")
    numwant = int(params.get("numwant", 50))

    # Get peer IP — use X-Forwarded-For or client host
    ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "127.0.0.1")
    # Strip port from IP if present
    if ":" in ip:
        ip = ip.split(":")[0]

    response = tracker.announce(
        info_hash=info_hash,
        peer_id=peer_id,
        ip=ip,
        port=port,
        uploaded=uploaded,
        downloaded=downloaded,
        left=left,
        event=event,
        numwant=numwant,
    )

    return Response(content=response, media_type="text/plain")


@app.get("/scrape")
async def tracker_scrape(request: Request):
    """BitTorrent HTTP tracker scrape endpoint."""
    info_hashes = []
    for key, value in request.query_params.multi_items():
        if key == "info_hash":
            ih = unquote(value).encode("latin-1").hex()
            info_hashes.append(ih)

    response = tracker.scrape(info_hashes if info_hashes else None)
    return Response(content=response, media_type="text/plain")


# ─── Torrent File Serving ────────────────────────────────────────────

@app.get("/torrents/{info_hash}.torrent")
async def serve_torrent(info_hash: str):
    """Serve a .torrent file by info hash."""
    data = torrent_service.get_torrent_bytes(info_hash.lower())
    if not data:
        return PlainTextResponse("Torrent not found", status_code=404)
    return Response(content=data, media_type="application/x-bittorrent")


# ─── Test File Serving ──────────────────────────────────────────────

@app.get("/files/{file_path:path}")
async def serve_file(file_path: str):
    """Serve a test file by path."""
    from pathlib import Path
    full_path = torrent_service.files_dir / file_path
    if not full_path.exists() or not full_path.is_file():
        return PlainTextResponse("File not found", status_code=404)
    return Response(content=full_path.read_bytes(), media_type="application/octet-stream")


# ─── Torrent Client API ─────────────────────────────────────────────

@app.post("/api/torrents/create")
async def create_torrent(file_name: str = Query(...)):
    """Create a .torrent from a test file."""
    try:
        info_hash, torrent_path = torrent_service.create_torrent(file_name)
        return {
            "success": True,
            "data": {
                "info_hash": info_hash,
                "torrent_path": torrent_path,
                "torrent_url": f"{SERVER_URL}/torrents/{info_hash}.torrent",
                "file_name": file_name,
            },
        }
    except FileNotFoundError as e:
        return {"success": False, "error": str(e)}


@app.post("/api/torrents/seed/{info_hash}")
async def seed_torrent(info_hash: str):
    """Start seeding a torrent."""
    try:
        job = await torrent_service.seed(info_hash.lower())
        return {
            "success": True,
            "data": {
                "info_hash": job.info_hash,
                "name": job.name,
                "status": job.status,
            },
        }
    except FileNotFoundError as e:
        return {"success": False, "error": str(e)}


@app.post("/api/torrents/download/{info_hash}")
async def download_torrent(info_hash: str):
    """Start downloading a torrent."""
    try:
        job = await torrent_service.download(info_hash.lower())
        return {
            "success": True,
            "data": {
                "info_hash": job.info_hash,
                "name": job.name,
                "status": job.status,
            },
        }
    except FileNotFoundError as e:
        return {"success": False, "error": str(e)}


@app.get("/api/torrents/{info_hash}/status")
async def torrent_status(info_hash: str):
    """Get torrent job status."""
    job = torrent_service.get_status(info_hash.lower())
    if not job:
        return {"success": False, "error": "Job not found"}
    return {
        "success": True,
        "data": {
            "info_hash": job.info_hash,
            "name": job.name,
            "status": job.status,
            "progress": job.progress,
            "download_rate": job.download_rate,
            "upload_rate": job.upload_rate,
            "error": job.error,
        },
    }


@app.post("/api/torrents/{info_hash}/assert-complete")
async def assert_complete(info_hash: str):
    """Assert that a torrent download is complete and verified."""
    success, message = torrent_service.assert_complete(info_hash.lower())
    return {
        "success": success,
        "data": {"info_hash": info_hash, "message": message},
    }


@app.post("/api/torrents/create-all")
async def create_all_torrents():
    """Create torrents for all pre-generated test files."""
    results = []
    for name in test_files:
        try:
            info_hash, torrent_path = torrent_service.create_torrent(name)
            results.append({"file": name, "info_hash": info_hash, "status": "created"})
        except Exception as e:
            results.append({"file": name, "error": str(e)})
    return {"success": True, "data": {"results": results, "total": len(results)}}


@app.post("/api/torrents/seed-all")
async def seed_all_torrents():
    """Seed all created torrents."""
    results = []
    for info_hash in list(torrent_service.torrents_dir.glob("*.torrent")):
        ih = info_hash.stem
        try:
            job = await torrent_service.seed(ih)
            results.append({"info_hash": ih, "name": job.name, "status": job.status})
        except Exception as e:
            results.append({"info_hash": ih, "error": str(e)})
    return {"success": True, "data": {"results": results}}


# ─── Setup endpoint for integration tests ────────────────────────────

@app.post("/api/setup/full")
async def setup_full_environment():
    """Full setup: create all torrents + seed them.

    Used by integration tests to prepare the test environment.
    """
    # Create torrents for all test files
    created = []
    for name in test_files:
        try:
            info_hash, _ = torrent_service.create_torrent(name)
            created.append(info_hash)
        except Exception:
            pass

    # Seed all created torrents
    seeded = []
    for ih in created:
        try:
            await torrent_service.seed(ih)
            seeded.append(ih)
        except Exception:
            pass

    return {
        "success": True,
        "data": {
            "test_files": len(test_files),
            "torrents_created": len(created),
            "torrents_seeded": len(seeded),
            "feeds": {
                "dmhy": f"{SERVER_URL}/rss/dmhy",
                "mikanani": f"{SERVER_URL}/rss/mikanani",
                "eztv": f"{SERVER_URL}/rss/eztv",
                "movies": f"{SERVER_URL}/rss/movies",
            },
            "tracker": TRACKER_URL,
        },
    }
