"""Keyword search and field filtering for a channel's file resources.

The Channel detail page lists resources, not works, but almost all of the
useful keyword data lives in two places:

* the resource's own release text (raw/parsed titles, subtitle groups,
  resolution/source/codec/container …), and
* the linked work's titles (TV series / movie / audio work).

For the second half we deliberately **reuse the existing work FTS** (the
Turso ngram sidecar, or ``search_text`` + ``pg_trgm`` on PostgreSQL) instead
of maintaining a second resource-level index. Keyword matching therefore
combines bounded SQL ``LIKE`` over resource columns with the work ids returned
by ``search_series_fts`` / ``search_movie_fts`` / ``search_audio_work_fts``,
including multi-work links carried by batch resources.

Field filters are evaluated against the channel's resources with the existing
Filter DSL field semantics (``filter_engine.evaluate_field_condition``) so the
list behaves exactly like Agent filtering for the fields it exposes. Only
resource-local fields are offered here — the list query never needs work
relationships, so filtering stays a single cheap SELECT per channel.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import String, cast, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.file_resource import FileResource
from app.models.resource_work_link import ResourceWorkLink
from app.services import fts
from app.services.filter_engine import NUMBER_OPS, evaluate_field_condition
from app.services.text_normalizer import normalize_title

# Field types for the list filter editor. Kept local to the channel resource
# list so the Agent Filter DSL's field catalog doesn't grow implicitly.
RESOURCE_FILTER_FIELD_TYPES: dict[str, str] = {
    "title_raw": "string",
    "title_cn": "string",
    "title_en": "string",
    "search_title": "string",
    "subtitle_group": "string",
    "subtitle_groups": "list",
    "resolution": "string",
    "source": "string",
    "video_codec": "string",
    "audio_codec": "string",
    "subtitle_type": "string",
    "subtitle_langs": "list",
    "container": "string",
    "episode_confidence": "string",
    "content_type": "string",
    "file_size": "number",
    "episode": "number",
    "season": "number",
    "title_year": "number",
    "episode_start": "number",
    "episode_end": "number",
    "absolute_episode": "number",
    "is_batch": "bool",
}

NO_VALUE_OPERATORS = {"is_empty", "is_not_empty"}
STRING_OPERATORS = {"eq", "ne", "contains", "fuzzy", "is_empty", "is_not_empty"}
LIST_OPERATORS = {"eq", "ne", "contains", "in", "is_empty", "is_not_empty"}
NUMBER_OPERATORS = NUMBER_OPS | NO_VALUE_OPERATORS

_OPERATORS_BY_TYPE = {
    "string": STRING_OPERATORS,
    "list": LIST_OPERATORS,
    "number": NUMBER_OPERATORS,
    "bool": {"eq", "ne", "is_empty", "is_not_empty"},
}

MAX_FILTER_CONDITIONS = 20

# Resource text columns searched by ``q``. Subtitle lists are JSON/text and are
# cast before LIKE so a value inside the list is still matched as a substring.
_KEYWORD_TEXT_COLUMNS = (
    FileResource.title_raw,
    FileResource.title_cn,
    FileResource.title_en,
    FileResource.search_title,
    FileResource.subtitle_group,
    FileResource.resolution,
    FileResource.source,
    FileResource.video_codec,
    FileResource.audio_codec,
    FileResource.subtitle_type,
    FileResource.container,
)
_KEYWORD_LIST_COLUMNS = (
    FileResource.subtitle_groups,
    FileResource.subtitle_langs,
)

# The work FTS call is bounded: candidates are intersected with the channel's
# resources anyway, and a handful of hundred ids keeps the IN/exists clause
# compact on both backends.
_WORK_FTS_LIMIT = 500


class ResourceFilterError(ValueError):
    """Raised when a filter JSON payload is structurally invalid."""


def _escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def parse_resource_filters(raw: str | None) -> list[dict[str, Any]]:
    """Parse and validate the ``filters`` query parameter.

    The wire format is a JSON array of ``{field, operator, value}`` objects,
    implicitly AND-ed together. Returns ``[]`` for absent/empty input.
    """
    if raw is None or not raw.strip():
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ResourceFilterError(f"filters must be valid JSON: {exc.msg}") from exc
    if not isinstance(payload, list):
        raise ResourceFilterError("filters must be a JSON array")
    if len(payload) > MAX_FILTER_CONDITIONS:
        raise ResourceFilterError(
            f"too many filter conditions (max {MAX_FILTER_CONDITIONS})"
        )

    conditions: list[dict[str, Any]] = []
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise ResourceFilterError(f"filters[{index}] must be an object")
        field = item.get("field")
        operator = item.get("operator")
        if field not in RESOURCE_FILTER_FIELD_TYPES:
            raise ResourceFilterError(f"filters[{index}].field is not supported")
        field_type = RESOURCE_FILTER_FIELD_TYPES[field]
        if operator not in _OPERATORS_BY_TYPE[field_type]:
            raise ResourceFilterError(
                f"filters[{index}].operator is not supported for {field}"
            )
        value = item.get("value")
        if operator not in NO_VALUE_OPERATORS:
            if value is None or (isinstance(value, str) and not value.strip()):
                raise ResourceFilterError(f"filters[{index}].value is required")
            if operator == "in" and not isinstance(value, list):
                raise ResourceFilterError(f"filters[{index}].value must be an array")
            if field_type == "string" and not isinstance(value, str):
                raise ResourceFilterError(f"filters[{index}].value must be a string")
            if field_type == "list" and operator != "in" and not isinstance(value, str):
                raise ResourceFilterError(f"filters[{index}].value must be a string")
            if field_type == "number":
                candidates = value if operator == "in" else [value]
                try:
                    for candidate in candidates:
                        float(str(candidate))
                except (TypeError, ValueError) as exc:
                    raise ResourceFilterError(
                        f"filters[{index}].value must be numeric"
                    ) from exc
            if field_type == "bool" and not isinstance(value, bool):
                raise ResourceFilterError(f"filters[{index}].value must be boolean")
        conditions.append({"field": field, "operator": operator, "value": value})
    return conditions


def _evaluate_title_year(raw: Any, operator: str, expected: Any) -> bool:
    """Numeric Filter-DSL semantics for ``title_year``.

    ``title_year`` is resource-local but intentionally not part of the Agent
    Filter DSL's numeric field set; evaluating it here keeps the list filter
    self-contained.
    """
    if operator in NO_VALUE_OPERATORS:
        empty = raw is None
        return empty if operator == "is_empty" else not empty
    if raw is None:
        return operator == "ne"
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return operator == "ne"
    if operator == "in":
        for candidate in expected if isinstance(expected, list) else [expected]:
            try:
                if value == float(candidate):
                    return True
            except (TypeError, ValueError):
                continue
        return False
    try:
        target = float(expected)
    except (TypeError, ValueError):
        return False
    return {
        "eq": value == target,
        "ne": value != target,
        "gt": value > target,
        "gte": value >= target,
        "lt": value < target,
        "lte": value <= target,
    }.get(operator, False)


def resource_matches_conditions(
    resource: FileResource, conditions: list[dict[str, Any]]
) -> bool:
    for condition in conditions:
        if condition["field"] == "title_year":
            if not _evaluate_title_year(
                resource.title_year, condition["operator"], condition["value"]
            ):
                return False
        elif not evaluate_field_condition(condition, resource):
            return False
    return True


def _keyword_terms(query: str) -> list[list[str]]:
    """Per-token match groups; groups are AND-ed, patterns inside OR-ed.

    Both the raw token and its normalized form (NFKC + OpenCC t2s + lowercase)
    are tried so Traditional/Simplified and full/half-width variants match.
    """
    groups: list[list[str]] = []
    for token in query.split():
        patterns: list[str] = []
        for candidate in (token, normalize_title(token)):
            candidate = candidate.strip()
            if candidate and candidate not in patterns:
                patterns.append(candidate)
        if patterns:
            groups.append(patterns)
    return groups


async def _keyword_resource_ids(
    db: AsyncSession, channel_id: str, query: str
) -> set[str]:
    groups = _keyword_terms(query)
    if not groups:
        return set()

    clause_groups = []
    for patterns in groups:
        pattern_conditions = []
        for pattern in patterns:
            like = f"%{_escape_like(pattern)}%"
            pattern_conditions.extend(
                column.ilike(like, escape="\\") for column in _KEYWORD_TEXT_COLUMNS
            )
            pattern_conditions.extend(
                cast(column, String).ilike(like, escape="\\")
                for column in _KEYWORD_LIST_COLUMNS
            )
        clause_groups.append(or_(*pattern_conditions))
    text_match = clause_groups[0]
    for group in clause_groups[1:]:
        text_match = text_match & group

    series_ids = await fts.search_series_fts(db, query, limit=_WORK_FTS_LIMIT)
    movie_ids = await fts.search_movie_fts(db, query, limit=_WORK_FTS_LIMIT)
    audio_ids = await fts.search_audio_work_fts(db, query, limit=_WORK_FTS_LIMIT)

    work_conditions = []
    if series_ids:
        work_conditions.append(FileResource.series_id.in_(series_ids))
        work_conditions.append(
            exists(
                select(ResourceWorkLink.id).where(
                    ResourceWorkLink.resource_id == FileResource.id,
                    ResourceWorkLink.series_id.in_(series_ids),
                )
            )
        )
    if movie_ids:
        work_conditions.append(FileResource.movie_id.in_(movie_ids))
        work_conditions.append(
            exists(
                select(ResourceWorkLink.id).where(
                    ResourceWorkLink.resource_id == FileResource.id,
                    ResourceWorkLink.movie_id.in_(movie_ids),
                )
            )
        )
    if audio_ids:
        work_conditions.append(FileResource.audio_work_id.in_(audio_ids))

    match_clause = text_match if not work_conditions else or_(text_match, *work_conditions)
    stmt = select(FileResource.id).where(
        FileResource.channel_id == channel_id,
        match_clause,
    )
    return set((await db.execute(stmt)).scalars().all())


async def matching_resource_ids(
    db: AsyncSession,
    channel_id: str,
    *,
    query: str | None = None,
    conditions: list[dict[str, Any]] | None = None,
) -> set[str] | None:
    """Return the ids of resources matching ``query`` AND all ``conditions``.

    ``None`` means "no search/filter was requested" (callers should skip the
    extra predicate entirely rather than building an empty set).
    """
    query = (query or "").strip()
    conditions = conditions or []
    if not query and not conditions:
        return None

    ids: set[str] | None = None
    if conditions:
        rows = (
            await db.execute(
                select(FileResource).where(FileResource.channel_id == channel_id)
            )
        ).scalars().all()
        ids = {r.id for r in rows if resource_matches_conditions(r, conditions)}

    if query:
        keyword_ids = await _keyword_resource_ids(db, channel_id, query)
        ids = keyword_ids if ids is None else ids & keyword_ids
    return ids or set()
