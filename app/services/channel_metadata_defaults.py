from __future__ import annotations

from typing import Any

from app.services.subtitle_groups import (
    join_legacy_subtitle_group,
    normalize_subtitle_groups,
)

DEFAULTABLE_REQUIRED_FIELD_TYPES: dict[str, str] = {
    "subtitle_group": "string",
    "resolution": "string",
    "source": "string",
    "video_codec": "string",
    "audio_codec": "string",
    "subtitle_type": "string",
    "subtitle_langs": "string_list",
    "container": "string",
}


def normalize_required_metadata_defaults(
    value: Any, required_fields: list[str] | None = None
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("required_metadata_defaults must be an object")
    required = set(required_fields) if required_fields is not None else None
    normalized: dict[str, Any] = {}
    for key, item in value.items():
        value_type = DEFAULTABLE_REQUIRED_FIELD_TYPES.get(key)
        if value_type is None:
            raise ValueError(f"required metadata field {key!r} does not support defaults")
        if required is not None and key not in required:
            raise ValueError(f"default provided for non-required metadata field {key!r}")
        if value_type == "string_list":
            if not isinstance(item, list) or any(
                not isinstance(entry, str) or not entry.strip() for entry in item
            ):
                raise ValueError(f"default for {key!r} must be a non-empty string array")
            values = list(dict.fromkeys(entry.strip() for entry in item))
            if not values:
                raise ValueError(f"default for {key!r} must be a non-empty string array")
            normalized[key] = values
        else:
            if not isinstance(item, str) or not item.strip():
                raise ValueError(f"default for {key!r} must be a non-empty string")
            normalized[key] = item.strip()
    return normalized


def clear_channel_default(resource: Any, key: str) -> None:
    fields = [field for field in (getattr(resource, "channel_default_fields", None) or []) if field != key]
    resource.channel_default_fields = fields


def _set_default_value(resource: Any, key: str, value: Any) -> None:
    if key == "subtitle_group":
        resource.subtitle_groups = normalize_subtitle_groups(value)
        resource.subtitle_group = join_legacy_subtitle_group(resource.subtitle_groups)
        resource.subtitle_groups_source = "channel_default"
    elif key == "subtitle_langs":
        resource.subtitle_langs = list(value)
    else:
        setattr(resource, key, value)


def _clear_default_value(resource: Any, key: str) -> None:
    if key == "subtitle_group":
        resource.subtitle_group = None
        resource.subtitle_groups = None
        if resource.subtitle_groups_source == "channel_default":
            resource.subtitle_groups_source = None
    else:
        setattr(resource, key, None)


def _missing(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip()) or (
        isinstance(value, (list, tuple, set)) and not value
    )


def reconcile_channel_metadata_defaults(resource: Any, defaults: dict[str, Any] | None) -> bool:
    defaults = defaults or {}
    marked = set(getattr(resource, "channel_default_fields", None) or [])
    before = (
        {key: getattr(resource, key, None) for key in DEFAULTABLE_REQUIRED_FIELD_TYPES},
        set(marked),
        getattr(resource, "subtitle_groups", None),
        getattr(resource, "subtitle_groups_source", None),
    )
    for key in marked - set(DEFAULTABLE_REQUIRED_FIELD_TYPES):
        marked.remove(key)
    for key in list(marked):
        if key not in defaults:
            _clear_default_value(resource, key)
            marked.remove(key)
    for key, value in defaults.items():
        if key not in DEFAULTABLE_REQUIRED_FIELD_TYPES:
            continue
        current = getattr(resource, key, None)
        if key in marked or _missing(current):
            _set_default_value(resource, key, value)
            marked.add(key)
    resource.channel_default_fields = sorted(marked)
    after = (
        {key: getattr(resource, key, None) for key in DEFAULTABLE_REQUIRED_FIELD_TYPES},
        set(resource.channel_default_fields),
        getattr(resource, "subtitle_groups", None),
        getattr(resource, "subtitle_groups_source", None),
    )
    return before != after


__all__ = [
    "DEFAULTABLE_REQUIRED_FIELD_TYPES",
    "clear_channel_default",
    "normalize_required_metadata_defaults",
    "reconcile_channel_metadata_defaults",
]
