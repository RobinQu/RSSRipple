"""Mock Plex API surface for media-server scan/test integration tests.

Serves the minimal endpoints the Plex adapter (``app.services.media_server_client``)
uses, so ``MediaServerInstance`` scan/connectivity can be exercised over HTTP
without a real Plex server:

- ``GET /identity`` → ``{"MediaContainer": {"version": ...}}``
- ``GET /library/sections`` → two organize targets (a bound ``show`` section and
  a bound ``movie`` section) plus one ``show`` section whose path matches no
  binding (unbound / 待绑定) and one ``artist`` section (skipped by the adapter).
- ``GET /library/sections/{key}/refresh`` → 200 (both partial ``?path=`` and full).

Mounted under ``/plex`` so a test MediaServerInstance can point its ``url`` at
``http://test-server:8080/plex``.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/plex")

MOCK_VERSION = "1.32.8-mock"


@router.get("/identity")
async def plex_identity():
    return {"MediaContainer": {"version": MOCK_VERSION}}


@router.get("/library/sections")
async def plex_sections():
    return {
        "MediaContainer": {
            "Directory": [
                {
                    "key": "1",
                    "type": "show",
                    "title": "TV Shows",
                    "Location": [{"id": 10, "path": "/data/tv"}],
                },
                {
                    "key": "2",
                    "type": "movie",
                    "title": "Movies",
                    "Location": [{"id": 11, "path": "/data/movies"}],
                },
                {
                    "key": "3",
                    "type": "show",
                    "title": "Unbound Shows",
                    "Location": [{"id": 12, "path": "/data/other"}],
                },
                {
                    "key": "4",
                    "type": "artist",
                    "title": "Music",
                    "Location": [{"id": 13, "path": "/data/music"}],
                },
            ]
        }
    }


@router.get("/library/sections/{section_key}/refresh")
async def plex_refresh(section_key: str):
    return JSONResponse(
        {"MediaContainer": {"size": 0, "section_key": section_key}},
        status_code=200,
    )
