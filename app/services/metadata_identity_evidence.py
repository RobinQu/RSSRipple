"""Validate model-selected identities against successful source evidence."""

import json
import re
from urllib.parse import urlsplit

from langchain_core.messages import ToolMessage


def _numeric_id(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    raw = str(value)
    if not re.fullmatch(r"[0-9]+", raw):
        return None
    # String normalization avoids exceptions on oversized model-generated ints.
    return raw.lstrip("0") or None


def _tool_rows(messages: list, names: set[str]):
    for message in messages:
        if not isinstance(message, ToolMessage) or message.name not in names:
            continue
        if message.status == "error" or not isinstance(message.content, str):
            continue
        try:
            payload = json.loads(message.content)
        except (ValueError, TypeError):
            continue
        if not isinstance(payload, dict) or payload.get("success") is not True:
            continue
        data = payload.get("data")
        for row in data if isinstance(data, list) else [data]:
            if isinstance(row, dict):
                yield row


def ground_tmdb_identity(finalize: dict, messages: list) -> dict:
    if not finalize.get("found"):
        return finalize
    identities: set[tuple[str, str]] = set()
    for row in _tool_rows(messages, {"search_tmdb", "get_tmdb_details"}):
        # Search returns normalized candidates (content_type); details retain
        # TMDB's media_type field. Reject contradictory evidence explicitly.
        media_type = row.get("media_type", row.get("content_type"))
        if media_type not in ("tv", "movie"):
            continue
        if "content_type" in row and row["content_type"] != media_type:
            continue
        raw_id = row.get("tmdb_id")
        if raw_id is None:
            external_id = row.get("external_id")
            if isinstance(external_id, str) and external_id.startswith("tmdb:"):
                raw_id = external_id[5:]
        identity_id = _numeric_id(raw_id)
        if identity_id is not None:
            identities.add((media_type, identity_id))
    entity = finalize.get("matched_entity")
    external_id = entity.get("external_id") if isinstance(entity, dict) else None
    match = re.fullmatch(r"tmdb:([0-9]+)", external_id) if isinstance(external_id, str) else None
    content_type = finalize.get("content_type")
    identity = (content_type, _numeric_id(match[1])) if match and isinstance(content_type, str) else None
    if identity not in identities:
        return {
            **finalize,
            "found": False,
            "matched_entity": None,
            "reason": "No matching TMDB identity and media type in tool evidence",
        }
    # These TMDB tools expose no cross-source identity evidence. Never retain
    # model-authored aliases or Wikipedia URLs alongside a grounded primary ID.
    grounded = {**entity, "external_id": f"tmdb:{identity[1]}", "external_source": "tmdb", "alt_external_ids": []}
    grounded.pop("wikipedia_url", None)
    return {**finalize, "matched_entity": grounded}


def ground_wikipedia_identity(finalize: dict, evidence: list[dict]) -> tuple[dict, dict | None]:
    """Share language-qualified identity selection between judge and ReAct."""
    from app.services.metadata_source_registry import parse_wikipedia_id

    if not finalize.get("found"):
        return finalize, None
    entity = finalize.get("matched_entity")
    external_id = entity.get("external_id") if isinstance(entity, dict) else None
    lang, pid = parse_wikipedia_id(external_id if isinstance(external_id, str) else None)
    pid = _numeric_id(pid)
    pages: dict[tuple[str, str], dict] = {}
    for entry in evidence:
        if not isinstance(entry, dict):
            continue
        edition, page_id = entry.get("lang"), _numeric_id(entry.get("page_id"))
        if not isinstance(edition, str) or not re.fullmatch(r"[a-z][a-z0-9-]*", edition):
            continue
        if page_id is None:
            continue
        key = (edition, page_id)
        # A search and a later detail response are observations of one page.
        # Empty search fields must not erase richer detail evidence.
        pages.setdefault(key, {}).update({k: v for k, v in entry.items() if v not in (None, "", [], {})})
        pages[key]["page_id"] = page_id
    matches = [
        entry for (edition, page_id), entry in pages.items() if page_id == pid and (lang is None or lang == edition)
    ]
    if len(matches) != 1:
        return {
            **finalize,
            "found": False,
            "matched_entity": None,
            "reason": "No unique matching Wikipedia identity in source evidence",
        }, None
    entry = matches[0]
    aliases = []
    language_ids = entry.get("langlink_pageids")
    for edition, raw_id in (language_ids if isinstance(language_ids, dict) else {}).items():
        alias = f"wikipedia:{edition}:{raw_id}"
        alias_lang, alias_id = parse_wikipedia_id(alias)
        alias_id = _numeric_id(alias_id)
        if alias_lang and alias_id:
            aliases.append({"source": "wikipedia", "id": f"wikipedia:{alias_lang}:{alias_id}"})
    categories = entry.get("categories")
    categories = [value for value in categories if isinstance(value, str)] if isinstance(categories, list) else []
    grounded = {
        **entity,
        "external_source": "wikipedia",
        "external_id": f"wikipedia:{entry['lang']}:{entry['page_id']}",
        "wikipedia_url": entry.get("url"),
        "alt_external_ids": aliases,
        "categories": categories[:10],
    }
    return {**finalize, "matched_entity": grounded}, entry


def ground_wikipedia_react_identity(finalize: dict, messages: list) -> dict:
    evidence = []
    for row in _tool_rows(messages, {"search_wikipedia", "get_wikipedia_page"}):
        if not isinstance(row.get("url"), str):
            continue
        try:
            host = urlsplit(row["url"]).hostname or ""
        except ValueError:
            continue
        match = re.fullmatch(r"([a-z][a-z0-9-]*)\.wikipedia\.org", host)
        if match:
            evidence.append({**row, "lang": match[1]})
    return ground_wikipedia_identity(finalize, evidence)[0]
