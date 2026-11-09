"""Repair explicitly selected resources with stale title/listing metadata.

The repair is deliberately allow-listed: pass one or more ``--resource-id``
options, or explicitly select the reviewed ``--known-issues`` set. Invoking the
script without a selection prints help and refuses to open a database.

Repairs performed before listing reconciliation:

* compilation titles with an explicit whole-run season span recover
  ``is_batch=true``, ``batch_scope=multi_season`` and ``batch_seasons``;
  human-curated listing shapes are never overwritten;
* values which are wholly technical release tokens (for example ``AVC_FLAC``)
  are cleared from both subtitle-group columns, then conservatively refilled
  from the leading title bracket. Real and manual groups are preserved.

Dry-run is the default and performs no network calls, LLM calls, writes,
publication or dispatch. With ``--apply``, cache/downloader/notification
listings go through the shared ``reconcile_resource`` pipeline. An HTTP row
without any of those listings uses ``retry_resource_torrent``, including its
magnet fallback. Reconciliation rebuilds file roles, subtitle attachments,
extras and declared-season links through the normal services.

The embedded-Turso backend takes an exclusive file lock: stop the app before
running ``--apply`` against that database. PostgreSQL does not require this.

Usage inside the container:
    python -m scripts.repair_resource_listings --resource-id ID
    python -m scripts.repair_resource_listings --known-issues
    python -m scripts.repair_resource_listings --known-issues --apply
"""

from __future__ import annotations

import argparse
import asyncio
import re
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.models.download_task import DownloadTask
from app.models.file_resource import FileResource
from app.models.resource_work_link import ResourceWorkLink
from app.services.resource_parser import (
    detect_season_span,
    extract_compilation_work_title,
    normalize_parsed_fields,
)
from app.services.subtitle_groups import normalize_subtitle_groups
from app.services.torrent_inspect import has_manual_listing_shape

# Filled only with the six reviewed resource IDs for this one-off repair.
# Keeping this separate from selection prevents an accidental all-table scan.
KNOWN_ISSUE_RESOURCE_IDS: tuple[str, ...] = (
    "e649dae8-e02b-4d0d-a279-651e7f8ff8ec",
    "51919fdf-bbd6-4f3e-b2ef-b5e6fe8c63b4",
    "7954033f-e96f-42e8-9c57-dd3ddb07da85",
    "f2afe3e8-3693-4b4f-bab6-c5d9f7191d52",
    "c86e720d-2b28-4a90-a4a0-c70ebc62f9dd",
    "dfa61dfc-7f78-4c9f-bed7-0d782cc85fb5",
)
KNOWN_ISSUE_MAGNETS = {
    "e649dae8-e02b-4d0d-a279-651e7f8ff8ec": "c8dbbae1e4100ed95f3bd837d4ec9fe72cd74361",
    "51919fdf-bbd6-4f3e-b2ef-b5e6fe8c63b4": "92a01a2dccdcc8a4c7afb5b74f7f3ebf9933606c",
    "f2afe3e8-3693-4b4f-bab6-c5d9f7191d52": "36cb3acf46531dfd7d4a2b7d67f1e0f3cf9c8d57",
}

_TECHNICAL_GROUP_TOKENS = frozenset({
    "aac", "aac20", "ac3", "av1", "avc", "avcflac", "bdrip", "bluray",
    "ddp", "ddp51", "dts", "flac", "h264", "h265", "hevc", "opus",
    "remux", "truehd", "web", "webdl", "webrip", "x264", "x265",
    "x264aac", "x265opus",
})
_NON_GROUP_TITLE_TAGS = frozenset({
    "整理搬运", "合集整理", "资源整合", "全集整理", "打包整理", "整理", "搬运", "打包",
})
_LISTING_SOURCES = frozenset({"torrent_cache", "downloader", "notification"})


@dataclass(slots=True)
class RepairPlan:
    changes: dict[str, Any] = field(default_factory=dict)
    actions: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RunSummary:
    planned: int = 0
    applied: int = 0
    pending: int = 0
    skipped: int = 0
    failed: int = 0

    @property
    def exit_code(self) -> int:
        return int(bool(self.failed))


def _normalized_group_token(value: str) -> str:
    return re.sub(r"[\s._-]+", "", value).casefold()


def is_technical_group(value: str) -> bool:
    """Recognize only codec/source compounds, never arbitrary group names."""
    normalized = _normalized_group_token(value)
    if normalized in _TECHNICAL_GROUP_TOKENS:
        return True
    parts = [part for part in re.split(r"[\s._-]+", value.casefold()) if part]
    return len(parts) > 1 and all(part in _TECHNICAL_GROUP_TOKENS for part in parts)


def _stored_groups(resource: Any) -> list[str]:
    # Inspect both columns: a real legacy value must not be erased merely
    # because the canonical list contains a stale technical value.
    return [
        *normalize_subtitle_groups(getattr(resource, "subtitle_group", None)),
        *normalize_subtitle_groups(getattr(resource, "subtitle_groups", None), split=False),
    ]


def _plan_subtitle_groups(resource: Any, plan: RepairPlan) -> None:
    groups = _stored_groups(resource)
    if not groups:
        return
    if getattr(resource, "subtitle_groups_source", None) == "manual":
        plan.notes.append("manual subtitle groups preserved")
        return
    if not all(is_technical_group(group) for group in groups):
        plan.notes.append("real subtitle groups preserved")
        return

    parsed = normalize_parsed_fields(
        resource.title_raw, {"subtitle_group": None, "subtitle_groups": None}
    )
    replacement = normalize_subtitle_groups(parsed.get("subtitle_groups"), split=False)
    if len(replacement) == 1 and replacement[0] in _NON_GROUP_TITLE_TAGS:
        replacement = []
    if replacement and any(is_technical_group(group) for group in replacement):
        replacement = []
    source = "heuristic" if replacement else None
    desired = {
        "subtitle_group": "&".join(replacement) if replacement else None,
        "subtitle_groups": replacement or None,
        "subtitle_groups_source": source,
    }
    defaults = [
        key for key in (getattr(resource, "channel_default_fields", None) or [])
        if key != "subtitle_group"
    ]
    if defaults != (getattr(resource, "channel_default_fields", None) or []):
        desired["channel_default_fields"] = defaults
    changed = {
        key: value for key, value in desired.items()
        if getattr(resource, key, None) != value
    }
    plan.changes.update(changed)
    plan.actions.append(
        "subtitle_groups: clear technical value; "
        + (f"title backfill ({len(replacement)} group)" if replacement else "leave empty")
    )


def plan_repairs(resource: Any) -> RepairPlan:
    """Compute an idempotent, non-mutating plan from the raw title."""
    plan = RepairPlan()
    compilation = extract_compilation_work_title(resource.title_raw)
    seasons = detect_season_span(resource.title_raw)
    if compilation and seasons and len(seasons) >= 2:
        if has_manual_listing_shape(resource):
            plan.notes.append("manual listing shape preserved")
        else:
            desired = {
                "is_batch": True,
                "batch_scope": "multi_season",
                "batch_seasons": seasons,
            }
            changed = {
                key: value for key, value in desired.items()
                if getattr(resource, key, None) != value
            }
            plan.changes.update(changed)
            if changed:
                plan.actions.append(
                    f"compilation: declare multi-season coverage {seasons}"
                )
    _plan_subtitle_groups(resource, plan)
    infohash = KNOWN_ISSUE_MAGNETS.get(getattr(resource, "id", None))
    if infohash and not getattr(resource, "magnet_url", None):
        plan.changes["magnet_url"] = f"magnet:?xt=urn:btih:{infohash}"
        plan.actions.append("listing: add reviewed infohash magnet fallback")
    return plan


def apply_plan(resource: Any, plan: RepairPlan) -> None:
    for key, value in plan.changes.items():
        setattr(resource, key, value)


def _planned_snapshot(resource: Any, plan: RepairPlan) -> SimpleNamespace:
    values = {
        key: value for key, value in vars(resource).items()
        if not key.startswith("_sa_")
    }
    values.update(plan.changes)
    return SimpleNamespace(**values)


def _linked_seasons(resource: Any) -> list[int]:
    seasons = {
        work.season_number
        for work in [getattr(resource, "series", None)]
        if work is not None and isinstance(work.season_number, int)
    }
    for link in vars(resource).get("work_links", []) or []:
        work = getattr(link, "series", None)
        if work is not None and isinstance(work.season_number, int):
            seasons.add(work.season_number)
    return sorted(seasons)


def _coverage_text(resource: Any) -> str:
    from app.services.resource_coverage import batch_coverage

    if not resource.is_batch:
        return "n/a"
    coverage = batch_coverage(resource)
    if coverage is None:
        return "unknown"
    parts = []
    for kind, _work_id, season, intervals in coverage:
        ranges = ",".join(f"{start}-{end}" for start, end in intervals) or "whole-work"
        parts.append(f"{kind}:s{season}:{ranges}" if season is not None else f"{kind}:{ranges}")
    return ";".join(parts)


def resource_stats(resource: Any) -> dict[str, Any]:
    from app.services.resource_confirmation import inspect_resource_confirmation

    assignments = vars(resource).get("file_assignments", []) or []
    roles = Counter((getattr(row, "file_role", None) or "episode") for row in assignments)
    channel = getattr(resource, "channel", None)
    confirmation = inspect_resource_confirmation(
        resource, getattr(channel, "required_metadata_fields", None)
    )
    return {
        "coverage": _coverage_text(resource),
        "missing_fields": list(confirmation.missing_fields),
        "roles": dict(sorted(roles.items())),
        "subtitle_attachments": sum(
            bool(getattr(row, "attached_to_path", None)) for row in assignments
        ),
        "linked_seasons": _linked_seasons(resource),
    }


def _print_stats(resource: Any, *, label: str) -> None:
    stats = resource_stats(resource)
    print(
        f"    {label}: coverage={stats['coverage']} "
        f"missing_fields={stats['missing_fields']} roles={stats['roles']} "
        f"subtitle_attachments={stats['subtitle_attachments']} "
        f"linked_seasons={stats['linked_seasons']}"
    )


async def _load_resource(db: Any, resource_id: str) -> FileResource | None:
    return await db.scalar(
        select(FileResource)
        .where(FileResource.id == resource_id)
        .options(
            selectinload(FileResource.channel),
            selectinload(FileResource.series),
            selectinload(FileResource.work_links).selectinload(ResourceWorkLink.series),
            selectinload(FileResource.file_assignments),
        )
        .execution_options(populate_existing=True)
    )


async def _has_downloader_listing(db: Any, resource_id: str) -> bool:
    return await db.scalar(
        select(DownloadTask.id)
        .where(
            DownloadTask.file_resource_id == resource_id,
            DownloadTask.transmission_torrent_id.isnot(None),
        )
        .limit(1)
    ) is not None


async def available_listing_source(db: Any, resource: FileResource) -> str:
    """Classify sources without downloader RPC or any other network access."""
    cached = resource.torrent_file
    if cached and Path(cached).exists():
        from app.services.torrent_inspect import parse_torrent_files

        if parse_torrent_files(cached) is not None:
            return "torrent_cache"
    if await _has_downloader_listing(db, resource.id):
        return "downloader"
    from app.services.resource_listing import _from_notification

    if await _from_notification(db, resource) is not None:
        return "notification"
    if (resource.torrent_url or "").startswith(("http://", "https://")):
        return "http"
    if resource.magnet_url:
        return "magnet"
    return "none"


def _print_plan(resource_id: str, plan: RepairPlan, source: str) -> None:
    print(f"resource={resource_id} listing_source={source}")
    if not plan.actions:
        print("    action=fields:no-change")
    for action in plan.actions:
        print(f"    action={action}")
    for key, value in plan.changes.items():
        print(f"      {key}={value!r}")
    for note in plan.notes:
        print(f"    note={note}")
    if source in _LISTING_SOURCES:
        print("    action=reconcile:roles,subtitle-attachments,extras,declared-season-links")
    elif source in ("http", "magnet"):
        print("    action=apply-only:retry-resource-torrent-with-magnet-fallback")
    else:
        print("    action=reconcile:skip-no-listing")


async def _apply_listing_repair(db: Any, resource: FileResource, source: str) -> str:
    from app.services import resource_listing

    if resource.batch_scope == "multi_season" and not has_manual_listing_shape(resource):
        from app.services.declared_season_links import link_declared_seasons

        await link_declared_seasons(db, resource)
        await db.commit()
    if source in _LISTING_SOURCES:
        result = await resource_listing.reconcile_resource(resource.id, force=True)
        if result.files and not result.error:
            return "applied"
        refreshed = await _load_resource(db, resource.id)
        if (
            refreshed is not None
            and refreshed.listing_reconcile_status == "done"
            and not refreshed.listing_reconcile_error
        ):
            return "applied"
        return "failed"
    if source not in ("http", "magnet"):
        return "no-listing"

    retry = await resource_listing.retry_resource_torrent(resource.id)
    if not retry.get("fetched"):
        return "magnet-pending" if resource.magnet_url else "failed"
    refreshed = await _load_resource(db, resource.id)
    if (
        refreshed is None
        or refreshed.listing_reconcile_status != "done"
        or refreshed.listing_reconcile_error
    ):
        return "failed"
    return "applied"


async def run(resource_ids: list[str], *, apply: bool = False) -> RunSummary:
    """Process only the supplied IDs, in order, with duplicates removed."""
    from app.database import async_session_factory

    summary = RunSummary()
    selected = list(dict.fromkeys(resource_ids))
    print(f"mode={'APPLY' if apply else 'DRY-RUN'} selected_resources={len(selected)}")
    async with async_session_factory() as db:
        for resource_id in selected:
            resource = await _load_resource(db, resource_id)
            if resource is None:
                summary.failed += 1
                print(f"resource={resource_id} result=failed reason=not-found")
                continue
            plan = plan_repairs(resource)
            source = await available_listing_source(db, resource)
            _print_plan(resource_id, plan, source)
            if not apply:
                summary.planned += 1
                _print_stats(_planned_snapshot(resource, plan), label="projected")
                continue

            try:
                apply_plan(resource, plan)
                if plan.changes:
                    await db.commit()
                outcome = await _apply_listing_repair(db, resource, source)
            except Exception as error:  # noqa: BLE001 - continue the explicit ID list
                await db.rollback()
                summary.failed += 1
                print(
                    f"resource={resource_id} result=failed "
                    f"error_type={type(error).__name__} detail=redacted"
                )
                continue

            refreshed = await _load_resource(db, resource_id)
            if refreshed is not None:
                _print_stats(refreshed, label="final")
            if outcome == "applied":
                summary.applied += 1
            elif outcome == "magnet-pending":
                summary.pending += 1
            elif outcome == "no-listing" and plan.changes:
                summary.applied += 1
            elif outcome == "no-listing":
                summary.skipped += 1
            else:
                summary.failed += 1
            print(f"resource={resource_id} result={outcome}")
        if not apply:
            await db.rollback()
    print(
        f"summary planned={summary.planned} applied={summary.applied} "
        f"pending={summary.pending} skipped={summary.skipped} failed={summary.failed}"
    )
    if not apply:
        print("dry-run only; no network, LLM, writes or dispatch; rerun with --apply to write")
    return summary


def _uuid(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError as error:
        raise argparse.ArgumentTypeError("resource ID must be a UUID") from error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--resource-id", action="append", type=_uuid, default=[],
        help="resource UUID; repeat for multiple resources",
    )
    selection.add_argument(
        "--known-issues", action="store_true",
        help="select the six built-in reviewed resource UUIDs",
    )
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    return parser


async def _run_apply_with_persistent_queue(resource_ids: list[str]) -> RunSummary:
    """One-shot containers do not run the app lifespan that replaces MemoryQueue."""
    from app.config import settings
    from app.services import task_queue as queue_module

    queue = queue_module.create_queue(
        backend=settings.queue_backend,
        redis_url=settings.redis_url,
        max_concurrent=settings.queue_max_concurrent,
    )
    previous = queue_module.task_queue
    queue_module.task_queue = queue
    try:
        await queue.start(consume=False)
        return await run(resource_ids, apply=True)
    finally:
        await queue.stop()
        queue_module.task_queue = previous


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.known_issues:
        if not KNOWN_ISSUE_RESOURCE_IDS:
            parser.error("the reviewed known-issue ID set is not configured")
        resource_ids = list(KNOWN_ISSUE_RESOURCE_IDS)
    else:
        resource_ids = args.resource_id
    if not resource_ids:
        parser.print_help()
        return 2
    summary = asyncio.run(
        _run_apply_with_persistent_queue(resource_ids)
        if args.apply else run(resource_ids, apply=False)
    )
    return summary.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
