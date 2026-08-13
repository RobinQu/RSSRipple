"""Filter DSL engine: validate, evaluate, and merge BoolCondition filter trees.

Implements the DSL described in AGENTS.md — a boolean-query tree over FileResource
fields supporting combinators (and/or), negation, and per-field operators
(eq, ne, contains, fuzzy, in, regex, gt/gte/lt/lte).
"""

from __future__ import annotations

import re
from typing import Any

from app.services.text_normalizer import similarity_score

STRING_FIELDS = {
    "subtitle_group", "resolution", "source", "video_codec", "audio_codec",
    "subtitle_type", "container", "title_cn", "title_en", "search_title",
    # ``episode_confidence`` is stored as a plain string with a whitelisted
    # value set. Reusing the string-field machinery keeps the filter engine
    # small; the UI restricts the input to the enum's valid values.
    "episode_confidence",
    # Work-collection display name (WorkCollection.title_cn or title_en),
    # resolved through the resource's linked work AND that work's loaded
    # ``collection`` relation — both must be eager-loaded (selectinload) at
    # the query site or the value is None and null semantics apply.
    "series.collection", "movie.collection",
}
NUMBER_FIELDS = {"file_size", "episode", "season", "episode_start", "episode_end", "absolute_episode"}
# Namespaced fields resolved through the resource's linked work (Movie /
# TVSeries) instead of the FileResource itself. ``rating`` is the work's
# 0-10 score; ``year`` derives from Movie.release_date / TVSeries.start_date.
# When the resource has no linked work (or the relationship wasn't loaded),
# the value is None and the usual empty-value semantics apply.
WORK_NUMBER_FIELDS = {"movie.rating", "movie.year", "series.rating", "series.year"}
NUMBER_FIELDS |= WORK_NUMBER_FIELDS
BOOL_FIELDS = {"is_batch", "series.is_anime", "movie.is_anime"}
# List-of-string fields — value semantics differ from scalar strings; the
# operators below act element-wise. ``series.genre`` / ``movie.genre`` resolve
# through the resource's linked work (values are the closed TMDB genre set,
# compared case-insensitively element-wise).
LIST_STRING_FIELDS = {"subtitle_langs", "series.genre", "movie.genre"}
ALL_FIELDS = STRING_FIELDS | NUMBER_FIELDS | BOOL_FIELDS | LIST_STRING_FIELDS

STRING_OPS = {"eq", "ne", "contains", "fuzzy", "in", "regex"}
NUMBER_OPS = {"eq", "ne", "gt", "gte", "lt", "lte", "in"}
BOOL_OPS = {"eq", "ne"}
LIST_STRING_OPS = {"eq", "ne", "contains", "in"}
# Value-less operators: they test the field's own emptiness, take no ``value``
# (the key may be omitted), and are valid for every field type. Use these
# instead of ``eq ""`` — an empty-string ``eq`` never matches anything.
NO_VALUE_OPS = {"is_empty", "is_not_empty"}
ALL_OPS = STRING_OPS | NUMBER_OPS | BOOL_OPS | LIST_STRING_OPS | NO_VALUE_OPS


def _coerce_bool(value: Any) -> bool:
    """Interpret user-supplied filter values as bool.

    Accepts native ``True/False``, numeric ``1/0``, and the string forms
    ``"true"``/``"false"``/``"1"``/``"0"``/``"yes"``/``"no"`` (case-insensitive).
    Anything else falls back to Python truthiness.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("true", "1", "yes", "y", "on"):
            return True
        if s in ("false", "0", "no", "n", "off", ""):
            return False
    return bool(value)


def _is_bool_condition(node: Any) -> bool:
    return isinstance(node, dict) and "combinator" in node and "conditions" in node


def _is_field_condition(node: Any) -> bool:
    if not (isinstance(node, dict) and "field" in node and "operator" in node):
        return False
    # Value-less operators may omit the ``value`` key entirely.
    return "value" in node or node.get("operator") in NO_VALUE_OPS


def validate_filter_config(config: Any) -> list[str]:
    """Return list of error messages; empty list means valid.

    Accepts ``None`` (meaning "no filter" / pass-all) as valid.
    """
    errors: list[str] = []
    if config is None:
        return errors
    _validate_node(config, errors, path="$")
    return errors


def _validate_node(node: Any, errors: list[str], path: str) -> None:
    if not isinstance(node, dict):
        errors.append(f"{path}: filter node must be a dict")
        return
    if _is_bool_condition(node):
        combinator = node.get("combinator")
        if combinator not in ("and", "or"):
            errors.append(f"{path}.combinator: must be 'and' or 'or'")
        conditions = node.get("conditions")
        if not isinstance(conditions, list) or not conditions:
            errors.append(f"{path}.conditions: must be a non-empty list")
        else:
            for i, child in enumerate(conditions):
                _validate_node(child, errors, f"{path}.conditions[{i}]")
        if "is_not" in node and not isinstance(node.get("is_not"), bool):
            errors.append(f"{path}.is_not: must be a bool")
    elif _is_field_condition(node):
        field = node.get("field")
        op = node.get("operator")
        value = node.get("value", None)
        if field not in ALL_FIELDS:
            errors.append(f"{path}.field: unknown field {field!r}")
            return
        if op not in ALL_OPS:
            errors.append(f"{path}.operator: unknown operator {op!r}")
            return
        if op in NO_VALUE_OPS:
            return  # no value required; valid for every field type
        if field in STRING_FIELDS and op not in STRING_OPS:
            errors.append(f"{path}.operator: operator {op!r} not supported for string field {field!r}")
            return
        if field in NUMBER_FIELDS and op not in NUMBER_OPS:
            errors.append(f"{path}.operator: operator {op!r} not supported for number field {field!r}")
            return
        if field in BOOL_FIELDS and op not in BOOL_OPS:
            errors.append(f"{path}.operator: operator {op!r} not supported for bool field {field!r}")
            return
        if field in LIST_STRING_FIELDS and op not in LIST_STRING_OPS:
            errors.append(f"{path}.operator: operator {op!r} not supported for list field {field!r}")
            return
        # A missing/blank value is always a mistake for value-taking
        # operators: ``eq ""` matches nothing (positive ops fail on empty
        # resource values), so reject it at save time instead of silently
        # filtering out every resource.
        if value is None or (isinstance(value, str) and not value.strip()):
            errors.append(
                f"{path}.value: operator {op!r} requires a non-empty value "
                "(use is_empty/is_not_empty to test emptiness)"
            )
            return
        # Validate value types
        if op == "in":
            if isinstance(value, str):
                items = [v.strip() for v in value.split(",") if v.strip()]
                if not items:
                    errors.append(f"{path}.value: 'in' requires a non-empty list")
            elif isinstance(value, list):
                if not value:
                    errors.append(f"{path}.value: 'in' requires a non-empty list")
            else:
                errors.append(f"{path}.value: 'in' requires a list or comma-separated string")
        elif op == "regex":
            if not isinstance(value, str) or not value:
                errors.append(f"{path}.value: 'regex' requires a non-empty string")
            else:
                try:
                    re.compile(value)
                except re.error as e:
                    errors.append(f"{path}.value: invalid regex: {e}")
        elif field in NUMBER_FIELDS and op in ("eq", "ne", "gt", "gte", "lt", "lte"):
            if not isinstance(value, (int, float)):
                try:
                    float(value)
                except (TypeError, ValueError):
                    errors.append(f"{path}.value: numeric field requires a numeric value")
    else:
        errors.append(f"{path}: node must be a BoolCondition or FieldCondition")


def evaluate_filter_config(config: dict | None, resource: Any) -> bool:
    """Evaluate a BoolCondition tree against a FileResource-like object.

    ``None``/empty config means pass-all.
    """
    if config is None:
        return True
    if not config:
        return True
    return _eval_node(config, resource)


def _eval_node(node: dict, resource: Any) -> bool:
    if _is_bool_condition(node):
        combinator = node.get("combinator", "and")
        conditions = node.get("conditions", [])
        if combinator == "and":
            result = all(_eval_node(c, resource) for c in conditions)
        else:  # or
            result = any(_eval_node(c, resource) for c in conditions)
        if node.get("is_not"):
            result = not result
        return result
    if _is_field_condition(node):
        return evaluate_field_condition(node, resource)
    return False


def evaluate_field_condition(cond: dict, resource: Any) -> bool:
    field = cond["field"]
    op = cond["operator"]
    expected = cond.get("value")

    raw = get_field_value(resource, field)

    # Value-less emptiness operators — evaluated before the type-specific
    # branches so they work uniformly for string/number/bool/list fields.
    if op in NO_VALUE_OPS:
        if raw is None:
            empty = True
        elif isinstance(raw, str):
            empty = not raw.strip()
        elif isinstance(raw, (list, tuple, set)):
            empty = len(raw) == 0
        else:
            empty = False
        return empty if op == "is_empty" else not empty

    # List-of-strings field short-circuit — element-wise semantics.
    if field in LIST_STRING_FIELDS:
        items = [str(x).strip().lower() for x in (raw or []) if str(x).strip()]
        item_set = set(items)
        if op == "eq":
            if isinstance(expected, list):
                exp_set = {str(v).strip().lower() for v in expected if str(v).strip()}
            else:
                exp_set = {str(expected).strip().lower()} if str(expected).strip() else set()
            return item_set == exp_set
        if op == "ne":
            if isinstance(expected, list):
                exp_set = {str(v).strip().lower() for v in expected if str(v).strip()}
            else:
                exp_set = {str(expected).strip().lower()} if str(expected).strip() else set()
            return item_set != exp_set
        if op == "contains":
            return str(expected).strip().lower() in item_set
        if op == "in":
            values = [str(v).strip().lower() for v in _coerce_in_list(expected) if str(v).strip()]
            return any(v in item_set for v in values)
        return False

    # Bool field short-circuit — null semantics match the documented scalar
    # rule (None fails positive ops, passes ne). ``is_batch`` is NOT NULL so
    # this only affects tri-state fields like ``series.is_anime``.
    if field in BOOL_FIELDS:
        expected_bool = _coerce_bool(expected)
        if raw is None:
            return op == "ne"
        actual_bool = bool(raw)
        if op == "eq":
            return actual_bool == expected_bool
        if op == "ne":
            return actual_bool != expected_bool
        return False

    # Empty handling
    is_empty = raw is None or (isinstance(raw, str) and raw.strip() == "")

    if field in NUMBER_FIELDS:
        # Numeric comparison
        if is_empty:
            # None values: positive ops fail; ne passes
            return op == "ne"
        try:
            num_val = float(raw)
        except (TypeError, ValueError):
            return op == "ne"

        if op == "in":
            values = _coerce_in_list(expected)
            nums: list[float] = []
            for v in values:
                try:
                    nums.append(float(v))
                except (TypeError, ValueError):
                    continue
            return any(num_val == n for n in nums)
        try:
            cmp_val = float(expected)
        except (TypeError, ValueError):
            return False
        if op == "eq":
            return num_val == cmp_val
        if op == "ne":
            return num_val != cmp_val
        if op == "gt":
            return num_val > cmp_val
        if op == "gte":
            return num_val >= cmp_val
        if op == "lt":
            return num_val < cmp_val
        if op == "lte":
            return num_val <= cmp_val
        return False

    # String comparison
    val = ("" if raw is None else str(raw)).strip()
    val_l = val.lower()

    if is_empty:
        # positive ops fail; ne passes
        if op == "ne":
            return True
        return False

    if op == "eq":
        if isinstance(expected, str):
            return val_l == expected.strip().lower()
        return val_l == str(expected).strip().lower()
    if op == "ne":
        if isinstance(expected, str):
            return val_l != expected.strip().lower()
        return val_l != str(expected).strip().lower()
    if op == "contains":
        return str(expected).strip().lower() in val_l
    if op == "fuzzy":
        target = str(expected).strip().lower()
        return similarity_score(val_l, target) >= 70
    if op == "in":
        values = _coerce_in_list(expected)
        return any(str(v).strip().lower() in val_l for v in values)
    if op == "regex":
        try:
            return bool(re.search(str(expected), val, re.IGNORECASE))
        except re.error:
            return False
    return False


def _coerce_in_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return [value]


def loaded_relation(resource: Any, rel_name: str) -> Any:
    """Return an already-loaded relationship value without triggering a lazy
    load.

    Async SQLAlchemy raises ``MissingGreenlet`` when an unloaded relationship
    is touched, so query sites feeding the filter engine / LLM pick summary
    must eager-load ``series``/``movie`` via ``selectinload``. A value that
    isn't loaded is treated as absent (None) — empty-value semantics then
    apply instead of crashing the run.
    """
    inst_dict = getattr(resource, "__dict__", None)
    if inst_dict is not None and rel_name in inst_dict:
        return inst_dict[rel_name]
    try:
        return getattr(resource, rel_name, None)
    except Exception:  # e.g. MissingGreenlet on an unloaded async relationship
        return None


def get_field_value(resource: Any, field: str) -> Any:
    """Get attribute from resource, supporting common ORM object access.

    Namespaced work fields (``movie.rating``, ``series.year`` …) resolve
    through the linked Movie/TVSeries; ``year`` derives from the work's
    date field (Movie.release_date / TVSeries.start_date).
    """
    if "." in field:
        rel_name, attr = field.split(".", 1)
        related = loaded_relation(resource, rel_name)
        if related is None:
            return None
        if attr == "year":
            date_attr = "release_date" if rel_name == "movie" else "start_date"
            d = getattr(related, date_attr, None)
            return d.year if d else None
        if attr == "collection":
            # The collection is itself a relationship on the work — defensive
            # access again (unloaded → None → standard null semantics), then
            # resolve to the display name.
            collection = loaded_relation(related, "collection")
            if collection is None:
                return None
            return collection.title_cn or collection.title_en
        return getattr(related, attr, None)
    return getattr(resource, field, None)


def merge_filters(global_cfg: dict | None, override_cfg: dict | None) -> dict | None:
    """AND-wrap global and override if both exist; else return the non-null one.

    Returns ``None`` when both are null/empty.
    """
    g = global_cfg if (isinstance(global_cfg, dict) and global_cfg) else None
    o = override_cfg if (isinstance(override_cfg, dict) and override_cfg) else None
    if g and o:
        return {"combinator": "and", "conditions": [g, o]}
    return g or o
