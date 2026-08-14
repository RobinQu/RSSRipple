"""Mock Bangumi API surface for the "bangumi" metadata source integration test.

Serves the minimal endpoints the Bangumi client (``app.services.bangumi_client``)
uses, under ``/bangumi/v0`` (the app's ``BANGUMI_API_BASE`` points at
``http://test-server:8080/bangumi/v0``):

- ``POST /search/subjects`` → echoes back a single anime (type 2) subject for
  keywords containing the marker ``bangumi`` (drives the deterministic
  normalized-title auto-link); empty otherwise (so other tests' post-link
  is_anime verification degrades to "no bangumi evidence").
- ``GET /subjects/{id}`` → canned subject details.
- ``GET /episodes`` → a small main-story episode page.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request

router = APIRouter(prefix="/bangumi/v0")

SUBJECT_ID = 99999
MARKER = "bangumi"


@router.post("/search/subjects")
async def bangumi_search(request: Request):
    body = await request.json()
    keyword = str((body or {}).get("keyword", "") or "")
    if MARKER not in keyword.lower():
        return {"data": []}
    return {
        "data": [
            {
                "id": SUBJECT_ID,
                "name": keyword,
                "name_cn": keyword,
                "date": "2023-09-29",
                "type": 2,
            }
        ]
    }


@router.get("/subjects/{subject_id}")
async def bangumi_subject(subject_id: int):
    return {
        "id": subject_id,
        "name": "Bangumi Mock Anime",
        "name_cn": "Bangumi Mock Anime",
        "summary": "Mock Bangumi subject summary for integration tests.",
        "date": "2023-09-29",
        "images": {},
        "rating": {"score": 9.1},
        "tags": [{"name": "奇幻"}, {"name": "冒险"}],
        "eps": 28,
        "platform": "TV",
    }


@router.get("/episodes")
async def bangumi_episodes(
    subject_id: int = Query(...),
    offset: int = 0,
    limit: int = 100,
    type: int = Query(0),
):
    all_episodes = [
        {"sort": 1, "name_cn": "episode one"},
        {"sort": 2, "name_cn": "episode two"},
        {"sort": 3, "name_cn": "episode three"},
    ]
    page = all_episodes[offset : offset + limit]
    return {"data": page, "total": len(all_episodes), "offset": offset}
