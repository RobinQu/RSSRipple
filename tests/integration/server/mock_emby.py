"""Mock Emby/Jellyfin API surface for media-server scan/test integration tests.

Serves the minimal endpoints the Emby/Jellyfin adapter
(``app.services.media_server_client._EmbyLikeClient``) uses:

- ``GET /System/Info`` → ``{"Version": ...}``
- ``GET /Library/VirtualFolders`` → movie/tvshows virtual folders
- ``POST /Library/Refresh`` → 204

Mounted under ``/emby``; the adapter ignores auth on the mock side (Emby uses an
``api_key`` query param, Jellyfin an ``X-Emby-Token`` header), so a single
surface serves both types.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import Response

router = APIRouter(prefix="/emby")

MOCK_VERSION = "4.8.11-mock"


@router.get("/System/Info")
async def emby_system_info():
    return {"Version": MOCK_VERSION, "ServerName": "mock-emby"}


@router.get("/Library/VirtualFolders")
async def emby_virtual_folders():
    return [
        {
            "Name": "TV",
            "CollectionType": "tvshows",
            "Locations": ["/data/tv"],
            "ItemId": "vf-tv",
        },
        {
            "Name": "Movies",
            "CollectionType": "movies",
            "Locations": ["/data/movies"],
            "ItemId": "vf-movies",
        },
    ]


@router.post("/Library/Refresh")
async def emby_refresh():
    return Response(status_code=204)
