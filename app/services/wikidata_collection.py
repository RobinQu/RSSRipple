"""Wikidata-based TV collection grouping (deterministic, out-of-band).

TV has no TMDB collection equivalent, so franchise grouping for series uses
Wikidata: resolve the work's Wikidata entity (QID), read its "part of the
series" claim (P179), and upsert a ``WorkCollection`` keyed by
``(external_source="wikidata", external_id=<franchise QID>)`` — the existing
unique constraint makes same-franchise series converge on one row.

This is a deterministic BACKFILL path used by ``scripts/tv_collection_backfill.py``
only — it is NOT a metadata-agent data source and adds no cross-source
fallback to the agent loop. Precision over recall: entity resolution prefers
the stored ``wikipedia_url``/``wikipedia_page_id`` (LLM-attached Wikipedia
page of THIS work); the ``wbsearchentities`` fallback accepts only a single
exact label/alias match, and any doubt resolves to "skip", never a guess.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import unquote

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.series import TVSeries
from app.models.work_collection import WorkCollection

logger = logging.getLogger(__name__)

WIKIDATA_API = "https://www.wikidata.org/w/api.php"
WIKIDATA_SOURCE = "wikidata"
P179 = "P179"  # "part of the series"

# Wikimedia-compliant UA (same policy as metadata_wikipedia_client).
_USER_AGENT = (
    f"{settings.app_name}/0.1.0 (https://github.com/RobinQu/RSSRipple) "
    f"collection-backfill"
)

_WIKIPEDIA_URL = re.compile(r"^https?://([a-z-]+)\.wikipedia\.org/wiki/([^?#]+)")
_CJK = re.compile(r"[぀-ヿ一-鿿가-힯]")

# Link outcome statuses returned by link_series_wikidata_collection.
STATUS_LINKED = "linked"
STATUS_NO_ENTITY = "no-entity"
STATUS_NO_P179 = "no-p179"
STATUS_AMBIGUOUS = "ambiguous"
STATUS_FAILED = "failed"
STATUS_ALREADY_LINKED = "already-linked"


def _normalize_title(title: str) -> str:
    return " ".join(title.strip().lower().split())


async def _get_json(url: str, params: dict) -> dict | None:
    """One GET against a MediaWiki/Wikidata API; None on any failure."""
    import httpx

    try:
        async with httpx.AsyncClient(
            timeout=15, headers={"User-Agent": _USER_AGENT}
        ) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.warning("[wikidata] GET %s params=%s failed: %s", url, params, e)
        return None


# ---------------------------------------------------------------------------
# Entity resolution
# ---------------------------------------------------------------------------


def _parse_wikipedia_url(url: str) -> tuple[str, str] | None:
    """``https://en.wikipedia.org/wiki/Foo_Bar`` -> (host, page title)."""
    m = _WIKIPEDIA_URL.match(url.strip())
    if not m:
        return None
    return f"{m.group(1)}.wikipedia.org", unquote(m.group(2)).replace("_", " ")


async def resolve_qid_from_wikipedia_url(wikipedia_url: str) -> str | None:
    """Map a stored Wikipedia URL to its Wikidata item via pageprops.

    Trusted without further verification: the URL was attached to THIS work
    by the metadata pipeline, and the host pins the language edition.
    """
    parsed = _parse_wikipedia_url(wikipedia_url)
    if parsed is None:
        return None
    host, title = parsed
    data = await _get_json(
        f"https://{host}/w/api.php",
        params={
            "action": "query",
            "prop": "pageprops",
            "titles": title,
            "redirects": 1,
            "format": "json",
            "formatversion": 2,
        },
    )
    for page in ((data or {}).get("query") or {}).get("pages") or []:
        qid = (page.get("pageprops") or {}).get("wikibase_item")
        if qid:
            return qid
    return None


def _entity_titles(entity: dict) -> set[str]:
    """Normalized en/zh labels + aliases of a Wikidata entity payload."""
    out: set[str] = set()
    for lang in ("en", "zh"):
        label = ((entity.get("labels") or {}).get(lang) or {}).get("value")
        if label:
            out.add(_normalize_title(label))
        for alias in (entity.get("aliases") or {}).get(lang) or []:
            value = alias.get("value")
            if value:
                out.add(_normalize_title(value))
    return out


def entity_label_matches(entity: dict, titles: list[str]) -> bool:
    """True when any of ``titles`` exactly matches an en/zh label or alias."""
    known = _entity_titles(entity)
    return any(_normalize_title(t) in known for t in titles if t)


async def resolve_qid_from_page_id(
    page_id: int,
    titles: list[str],
    sites: tuple[str, ...] = ("en.wikipedia.org", "zh.wikipedia.org", "ja.wikipedia.org"),
) -> str | None:
    """Resolve a stored ``wikipedia_page_id`` to a QID.

    A bare page_id does not pin the language edition (the metadata agent
    searches en/zh/ja), so each candidate edition is tried and the resolved
    entity is ACCEPTED only when its label/alias matches one of the work's
    titles — a wrong-edition page_id hit resolves to skip, not a guess.
    """
    for host in sites:
        data = await _get_json(
            f"https://{host}/w/api.php",
            params={
                "action": "query",
                "prop": "pageprops",
                "pageids": page_id,
                "format": "json",
                "formatversion": 2,
            },
        )
        qid = next(
            (
                (p.get("pageprops") or {}).get("wikibase_item")
                for p in ((data or {}).get("query") or {}).get("pages") or []
                if p.get("pageprops")
            ),
            None,
        )
        if not qid:
            continue
        entity = await fetch_entity(qid)
        if entity and entity_label_matches(entity, titles):
            return qid
    return None


async def search_entity_qid(titles: list[str]) -> str | None:
    """``wbsearchentities`` fallback — single exact label/alias match only.

    A title whose results contain zero or 2+ exact matches yields no QID
    (ambiguous titles are skipped rather than guessed).
    """
    for title in titles:
        lang = "zh" if _CJK.search(title) else "en"
        data = await _get_json(
            WIKIDATA_API,
            params={
                "action": "wbsearchentities",
                "search": title,
                "language": lang,
                "type": "item",
                "limit": 10,
                "format": "json",
            },
        )
        if not data:
            continue
        norm = _normalize_title(title)
        matches: list[str] = []
        for result in data.get("search") or []:
            candidates = [result.get("label") or "", *(result.get("aliases") or [])]
            if any(_normalize_title(c) == norm for c in candidates if c):
                matches.append(result.get("id"))
        unique = {m for m in matches if m}
        if len(unique) == 1:
            return unique.pop()
    return None


async def resolve_series_entity_qid(series: TVSeries) -> str | None:
    """Best-effort QID for a series: stored Wikipedia URL, then page_id
    (label-verified), then exact-match search. None means "skip"."""
    titles = [
        t
        for t in (
            series.title_en,
            series.original_title,
            series.title_cn,
            series.canonical_name,
        )
        if t
    ]
    if series.wikipedia_url:
        qid = await resolve_qid_from_wikipedia_url(series.wikipedia_url)
        if qid:
            return qid
    # Rows matched by the Wikipedia source store the page id in
    # ``external_id`` ("wikipedia:<pageid>") — often without filling
    # ``wikipedia_page_id``/``wikipedia_url``. Treat it as the same evidence.
    page_id = series.wikipedia_page_id
    if page_id is None and (series.external_id or "").startswith("wikipedia:"):
        raw = (series.external_id or "").split(":", 1)[1]
        page_id = int(raw) if raw.isdigit() else None
    if page_id:
        qid = await resolve_qid_from_page_id(page_id, titles)
        if qid:
            return qid
    return await search_entity_qid(titles)


# ---------------------------------------------------------------------------
# Entity claims / labels
# ---------------------------------------------------------------------------


async def fetch_entity(qid: str) -> dict | None:
    """Fetch one entity (claims + en/zh labels/aliases); None on failure."""
    data = await _get_json(
        WIKIDATA_API,
        params={
            "action": "wbgetentities",
            "ids": qid,
            "props": "claims|labels|aliases",
            "languages": "zh|en",
            "format": "json",
            "formatversion": 2,
        },
    )
    # wbgetentities returns ``entities`` as a dict keyed by QID — even with
    # formatversion=2 (that flag only flattens claims/labels, not the map).
    entities = (data or {}).get("entities") or {}
    if isinstance(entities, dict):
        entity = entities.get(qid) or next(iter(entities.values()), None)
    else:  # tolerate list-shaped payloads (older mocks/proxies)
        entity = entities[0] if entities else None
    if not entity or "missing" in entity:
        return None
    return entity


def extract_p179_qids(entity: dict) -> list[str]:
    """Distinct franchise QIDs from the entity's P179 ("part of the series")
    claims, in claim order. Empty when the work belongs to no series."""
    qids: list[str] = []
    for claim in (entity.get("claims") or {}).get(P179) or []:
        snak = claim.get("mainsnak") or {}
        if snak.get("snaktype") != "value":
            continue
        value = (snak.get("datavalue") or {}).get("value")
        qid = value.get("id") if isinstance(value, dict) else None
        if isinstance(qid, str) and qid.startswith("Q") and qid not in qids:
            qids.append(qid)
    return qids


def entity_labels(entity: dict) -> tuple[str | None, str | None]:
    """(zh label, en label) of an entity payload."""
    labels = entity.get("labels") or {}
    zh = (labels.get("zh") or {}).get("value")
    en = (labels.get("en") or {}).get("value")
    return zh, en


# ---------------------------------------------------------------------------
# Upsert + link
# ---------------------------------------------------------------------------


async def upsert_collection_from_wikidata(
    db: AsyncSession, qid: str, title_cn: str | None, title_en: str | None
) -> WorkCollection:
    """Idempotent upsert keyed by (external_source="wikidata", external_id=QID)."""
    existing = (
        await db.execute(
            select(WorkCollection).where(
                WorkCollection.external_source == WIKIDATA_SOURCE,
                WorkCollection.external_id == qid,
            )
        )
    ).scalars().first()
    if existing is not None:
        if title_cn and existing.title_cn != title_cn:
            existing.title_cn = title_cn
        if title_en and not existing.title_en:
            existing.title_en = title_en
        return existing
    collection = WorkCollection(
        title_cn=title_cn or title_en or f"Wikidata {qid}",
        title_en=title_en,
        external_id=qid,
        external_source=WIKIDATA_SOURCE,
    )
    db.add(collection)
    await db.flush()
    return collection


async def link_series_wikidata_collection(
    db: AsyncSession, series: TVSeries, *, apply: bool = True
) -> str:
    """Resolve the series' franchise via Wikidata P179 and link it.

    Returns one of the STATUS_* constants. With ``apply=False`` nothing is
    written (dry-run); the status is what an applied run would produce.
    """
    if series.collection_id is not None:
        return STATUS_ALREADY_LINKED
    qid = await resolve_series_entity_qid(series)
    if not qid:
        return STATUS_NO_ENTITY
    entity = await fetch_entity(qid)
    if entity is None:
        return STATUS_FAILED
    p179 = extract_p179_qids(entity)
    if not p179:
        return STATUS_NO_P179
    if len(p179) > 1:
        # Multiple "part of the series" values — never guess which franchise.
        return STATUS_AMBIGUOUS
    franchise_qid = p179[0]
    franchise = await fetch_entity(franchise_qid)
    if franchise is None:
        return STATUS_FAILED
    title_cn, title_en = entity_labels(franchise)
    if apply:
        collection = await upsert_collection_from_wikidata(
            db, franchise_qid, title_cn, title_en
        )
        series.collection_id = collection.id
        logger.info(
            "[wikidata] linked series %r -> collection %r (wikidata:%s)",
            series.id,
            collection.display_name,
            franchise_qid,
        )
    return STATUS_LINKED
