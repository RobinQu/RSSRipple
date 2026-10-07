"""Select a canonical work without discarding curated metadata."""
from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC
from typing import Any

from app.models.movie import Movie
from app.models.series import TVSeries
from app.services.metadata_service import MANUAL_EDITABLE_FIELDS

Work = TVSeries | Movie


def _populated(value: Any) -> bool:
    # False and zero are known values, unlike empty metadata containers/text.
    return value is not None and value != "" and value != []


def protected_values(rows: Sequence[Work]) -> tuple[dict[str, Any], list[str]]:
    """No per-field edit timestamps exist: conflicting manual values cannot rank."""
    values: dict[str, Any] = {}
    conflicts: set[str] = set()
    for row in rows:
        for name in row.manually_edited_fields or []:
            if name not in MANUAL_EDITABLE_FIELDS or not hasattr(row, name):
                conflicts.add(name)
                continue
            value = getattr(row, name)
            if name in values and values[name] != value:
                conflicts.add(name)
            else:
                values[name] = value
    return values, sorted(conflicts)


class DedupConflictError(ValueError):
    """A merge cannot represent all saved manual intent on its target."""

    def __init__(self, rows: Sequence[Work], fields: list[str]) -> None:
        self.fields = sorted(set(fields))
        self.work_ids = sorted(row.id for row in rows)
        super().__init__("manual-conflict fields=" + ",".join(self.fields)
                         + " ids=" + ",".join(self.work_ids))


def require_protected_values(rows: Sequence[Work], target: Work) -> dict[str, Any]:
    values, conflicts = protected_values(rows)
    conflicts.extend(name for name in values if not hasattr(target, name))
    expected_type = "movie" if isinstance(target, Movie) else "tv"
    if "content_type" in values and values["content_type"] not in {None, expected_type}:
        conflicts.append("content_type")
    if conflicts:
        raise DedupConflictError(rows, conflicts)
    primary_identity_owner(rows, target, values)
    return values


def primary_identity_owner(rows: Sequence[Work], target: Work, protected: dict[str, Any]) -> Work:
    """Never combine an id from one work with another work's source."""
    identity_fields = {"external_id", "external_source"} & protected.keys()
    ordered = [target, *sorted((row for row in rows if row is not target), key=_chronology)]
    if identity_fields:
        for row in ordered:
            if all(getattr(row, name) == protected[name] for name in identity_fields):
                return row
        raise DedupConflictError(rows, sorted(identity_fields))
    return next((row for row in ordered if row.external_id), target)


def _chronology(row: Work) -> tuple:
    created = row.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return created.astimezone(UTC), row.id


def select_survivor(rows: Sequence[Work]) -> Work:
    """Prefer curated, then complete records; stable chronology breaks ties."""
    def key(row: Work) -> tuple:
        fields = [name for name in MANUAL_EDITABLE_FIELDS if hasattr(row, name)]
        protected = set(row.manually_edited_fields or []) & set(fields)
        complete = sum(_populated(getattr(row, name)) for name in fields)
        return -len(protected), -complete, *_chronology(row)

    return min(rows, key=key)


def apply_metadata(survivor: Work, rows: Sequence[Work], protected: dict[str, Any]) -> None:
    """Transfer manual intent including null, then fill absent automatic values."""
    for name, value in protected.items():
        setattr(survivor, name, value)
    survivor.manually_edited_fields = sorted(protected) or None
    ordered = sorted(rows, key=_chronology)
    # Identity source pairs and aliases have dedicated canonicalization rules.
    fields = MANUAL_EDITABLE_FIELDS - {"external_id", "external_source", "aliases", "content_type"}
    for name in fields:
        if name in protected or not hasattr(survivor, name):
            continue
        if _populated(getattr(survivor, name)):
            continue
        for row in ordered:
            if hasattr(row, name) and _populated(getattr(row, name)):
                setattr(survivor, name, getattr(row, name))
                break

    if "content_type" not in protected:
        survivor.content_type = "movie" if isinstance(survivor, Movie) else "tv"
