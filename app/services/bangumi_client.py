"""Bangumi API client (https://bangumi.github.io/api/).

Used by two consumers:
  * the post-link ``is_anime`` verification (layer-1 anime detection —
    :func:`search_subjects` without a type filter, so 三次元 (type 6) hits
    can serve as live-action evidence);
  * the "bangumi" channel metadata source (Phase 1) — subject search limited
    to the anime category (type 2), subject details, and the full episode
    list.

Auth: ``Authorization: Bearer <token>`` from
``runtime_config.bangumi_api_key`` (env ``BANGUMI_API_KEY`` or the settings
UI override). No token → the source/verification stay disabled
(:func:`bangumi_configured`). The User-Agent is fixed per the operator's
API-registration requirement.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.services.runtime_config import runtime_config

logger = logging.getLogger(__name__)

BANGUMI_API_BASE = "https://api.bgm.tv/v0"
BANGUMI_USER_AGENT = "robinqu/RSSRipple"

# Bangumi subject types (only the two we act on).
SUBJECT_TYPE_ANIME = 2
SUBJECT_TYPE_REAL = 6  # 三次元 (live-action)

_EPISODES_PAGE_SIZE = 100
_EPISODES_MAX_PAGES = 20  # 2000 main-story episodes cap — far beyond any TV work


def bangumi_configured() -> bool:
    """True when a Bangumi token is configured (token == the enable switch)."""
    return bool(runtime_config.bangumi_api_key)


def _headers() -> dict[str, str]:
    return {
        "User-Agent": BANGUMI_USER_AGENT,
        "Authorization": f"Bearer {runtime_config.bangumi_api_key}",
    }


async def search_subjects(
    client: httpx.AsyncClient,
    keyword: str,
    limit: int = 5,
    anime_only: bool = False,
) -> list[dict[str, Any]]:
    """Search subjects by keyword. ``anime_only`` restricts to the anime
    category (type 2); the unfiltered form also surfaces 三次元 (type 6)
    hits, which the is_anime verification uses as live-action evidence."""
    body: dict[str, Any] = {"keyword": keyword, "limit": limit}
    if anime_only:
        body["filter"] = {"type": [SUBJECT_TYPE_ANIME]}
    resp = await client.post(
        f"{BANGUMI_API_BASE}/search/subjects", json=body, headers=_headers()
    )
    resp.raise_for_status()
    return (resp.json() or {}).get("data") or []


async def get_subject(
    client: httpx.AsyncClient, subject_id: int | str
) -> dict[str, Any]:
    """Full subject details (name, name_cn, summary, date, images, rating,
    tags, eps count, platform, infobox)."""
    resp = await client.get(
        f"{BANGUMI_API_BASE}/subjects/{subject_id}", headers=_headers()
    )
    resp.raise_for_status()
    return resp.json() or {}


async def get_subject_episodes(
    client: httpx.AsyncClient, subject_id: int | str
) -> list[dict[str, Any]]:
    """Full main-story (type 0) episode list, paginated.

    Returns the raw Bangumi episode dicts (``sort`` is the global episode
    number and may be fractional for in-between specials — callers decide).
    """
    episodes: list[dict[str, Any]] = []
    offset = 0
    for _ in range(_EPISODES_MAX_PAGES):
        resp = await client.get(
            f"{BANGUMI_API_BASE}/episodes",
            params={
                "subject_id": subject_id,
                "type": 0,
                "limit": _EPISODES_PAGE_SIZE,
                "offset": offset,
            },
            headers=_headers(),
        )
        resp.raise_for_status()
        payload = resp.json() or {}
        page = payload.get("data") or []
        episodes.extend(page)
        total = int(payload.get("total") or 0)
        offset += len(page)
        if not page or offset >= total:
            break
    return episodes
