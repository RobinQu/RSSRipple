"""Channel-scoped confirmation policy for file-resource metadata.

These issues describe incomplete or uncertain resource metadata.  They are
owned by the resource's Channel and must be resolved before any Agent may
filter or dispatch the resource; they are deliberately separate from
``PendingDecision``, whose only purpose is choosing among multiple eligible
download candidates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.services.filter_engine import loaded_relation
from app.services.required_fields import missing_required_fields

LEGACY_CONFIRMATION_REASON_PREFIXES = (
    "集号不确定",
    "季号不确定",
    "合集范围不确定",
)


@dataclass(frozen=True)
class ResourceConfirmation:
    kinds: tuple[str, ...]
    missing_fields: tuple[str, ...] = field(default_factory=tuple)

    @property
    def required(self) -> bool:
        return bool(self.kinds)


def inspect_resource_confirmation(
    resource: Any,
    required_metadata_fields: list[str] | None,
) -> ResourceConfirmation:
    """Return all Channel-level confirmation reasons for ``resource``."""
    kinds: list[str] = []

    # Terminal multi-season packs clear the flat work FKs and carry their
    # season works on the link table — links count as a work association.
    links = loaded_relation(resource, "work_links")
    has_work = bool(
        getattr(resource, "series_id", None)
        or getattr(resource, "movie_id", None)
        or getattr(resource, "audio_work_id", None)
        or getattr(resource, "collection_id", None)
        or links
    )
    if not has_work:
        kinds.append("metadata_unlinked")

    if (
        getattr(resource, "episode_confidence", None) == "ambiguous"
        and not getattr(resource, "is_batch", False)
        and not getattr(resource, "movie_id", None)
    ):
        kinds.append(
            "season_ambiguous"
            if getattr(resource, "season", None) is None
            else "episode_ambiguous"
        )

    # Batch coverage is a TV-series concept. AudioWork and Movie resources may
    # still carry ``is_batch`` for feed/torrent provenance, but never have a
    # season coverage requirement.
    if getattr(resource, "is_batch", False):
        scope = getattr(resource, "batch_scope", None)
        if getattr(resource, "series_id", None):
            if scope is None:
                kinds.append("batch_coverage_unknown")
            elif scope == "season" and getattr(resource, "season", None) is None:
                kinds.append("batch_coverage_unknown")
            elif scope == "multi_season" and not getattr(resource, "batch_seasons", None):
                kinds.append("batch_coverage_unknown")
        elif scope == "multi_season" and links is not None:
            # Links-carried multi-season pack (per-season works): the linked
            # work set must be non-empty and ``batch_seasons`` must match the
            # linked works' season_numbers (derived cache consistency).
            link_series = [link for link in links if getattr(link, "series_id", None)]
            if not link_series:
                kinds.append("batch_coverage_unknown")
            else:
                declared = sorted(getattr(resource, "batch_seasons", None) or [])
                derived = [
                    season
                    for season in (
                        getattr(loaded_relation(link, "series"), "season_number", None)
                        for link in link_series
                    )
                    if season is not None
                ]
                if not declared or (
                    len(derived) == len(link_series) and sorted(derived) != declared
                ):
                    kinds.append("batch_coverage_unknown")

    missing = (
        missing_required_fields(resource, required_metadata_fields)
        if required_metadata_fields is not None
        else []
    )
    if missing:
        kinds.append("required_fields_missing")

    return ResourceConfirmation(tuple(kinds), tuple(missing))


__all__ = [
    "LEGACY_CONFIRMATION_REASON_PREFIXES",
    "ResourceConfirmation",
    "inspect_resource_confirmation",
]
