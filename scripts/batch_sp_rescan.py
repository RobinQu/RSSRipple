"""One-off rescan: batch resources whose ``batch_seasons`` contains season 0.

Repairs FileResource rows mis-judged before ``analyze_torrent_files``
stopped counting season 0 (specials/SP) towards multi_season: a pack whose
real content is "one season + SP extras" was stored as
``batch_scope="multi_season"`` with ``batch_seasons=[0, 1]`` and its
``season`` / ``episode_start`` / ``episode_end`` wiped.

For every candidate (``is_batch=true`` and the ``batch_seasons`` JSON array
contains 0 — filtered Python-side so the same code serves Turso and
PostgreSQL) the cached .torrent listing is re-analyzed with the corrected
semantics and the write-back follows ``maybe_inspect_torrent``:

- ``season``: ``batch_scope="season"``, ``episode=None``,
  ``episode_start/end`` from the report, ``batch_seasons=None``;
- ``multi_season``: only ``batch_seasons`` is refreshed from the report;
- ``franchise`` / ``single`` / ``unknown``: reported, never written (an
  existing batch verdict is never downgraded).

Rewritten rows also get their deterministic assignments rebuilt
(``apply_auto_assignments``) and ``season_ranges`` recomputed
(``compute_season_ranges``). Work links and ``collection_id`` are NOT
touched and the LLM refinement is NOT invoked — work-level repair is left
to the edit wizard; this script is fully offline. Rows without a usable
``torrent_file`` cache are reported and skipped (no downloading).

NOTE on locking: the embedded-Turso backend holds a single-process exclusive
file lock — stop the app before running against a live database.

Dry-run by default; pass --apply to execute.
"""

import argparse
import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.models.file_resource import FileResource
from app.services.torrent_inspect import (
    TorrentReport,
    analyze_torrent_files,
    parse_torrent_files,
)

# Actions reported per candidate.
REWRITE = "rewrite"
SKIP = "skip"
NO_CACHE = "no-torrent-cache"


@dataclass
class Evaluation:
    """What the corrected report implies for one candidate resource."""

    action: str  # REWRITE | SKIP
    changes: dict[str, Any] = field(default_factory=dict)
    reason: str = ""


def select_candidates(resources: list[FileResource]) -> list[FileResource]:
    """Batch rows whose ``batch_seasons`` coverage includes season 0."""
    return [
        r
        for r in resources
        if r.is_batch and isinstance(r.batch_seasons, list) and 0 in r.batch_seasons
    ]


def evaluate_resource(resource: FileResource, report: TorrentReport) -> Evaluation:
    """Pure decision: write-back implied by the re-analyzed report.

    Mirrors the ``maybe_inspect_torrent`` write-back semantics; idempotent —
    a resource already matching the report evaluates to ``skip``.
    """
    if report.scope == "season":
        changes = {
            "is_batch": True,
            "batch_scope": "season",
            "episode": None,
            "episode_start": report.episode_start,
            "episode_end": report.episode_end,
            "batch_seasons": None,
        }
    elif report.scope == "multi_season":
        changes = {"batch_seasons": report.seasons or None}
    else:
        return Evaluation(action=SKIP, reason=f"scope={report.scope} — never downgraded")

    delta = {k: v for k, v in changes.items() if getattr(resource, k) != v}
    if not delta:
        return Evaluation(action=SKIP, reason="already matches report")
    return Evaluation(action=REWRITE, changes=delta)


def apply_evaluation(resource: FileResource, report: TorrentReport, evaluation: Evaluation) -> None:
    """Write the evaluated changes and rebuild deterministic mappings."""
    from app.services import batch_content_analysis as bca

    for key, value in evaluation.changes.items():
        setattr(resource, key, value)
    bca.apply_auto_assignments(resource, report)
    resource.season_ranges = bca.compute_season_ranges(resource)


def read_cached_listing(resource: FileResource) -> list[dict] | None:
    """Parse the resource's cached .torrent, or None when unavailable."""
    path = resource.torrent_file
    if not path or not Path(path).exists():
        return None
    return parse_torrent_files(path)


def _fmt(resource: FileResource) -> str:
    return (
        f"scope={resource.batch_scope} season={resource.season} "
        f"ep={resource.episode_start}-{resource.episode_end} "
        f"batch_seasons={resource.batch_seasons}"
    )


async def rescan(apply: bool, limit: int | None) -> None:
    from app.database import async_session_factory

    async with async_session_factory() as db:
        rows = (await db.execute(
            select(FileResource)
            .where(FileResource.is_batch.is_(True))
            .order_by(FileResource.created_at.desc())
        )).scalars().all()

        candidates = select_candidates(list(rows))
        if limit is not None:
            candidates = candidates[:limit]
        print(f"batch rows total={len(rows)} with season 0 in batch_seasons={len(candidates)}")

        rewritten = 0
        skipped = 0
        no_cache = 0
        parse_fail = 0
        for resource in candidates:
            path = resource.torrent_file
            if not path or not Path(path).exists():
                no_cache += 1
                print(f"  [{resource.id[:8]}] {NO_CACHE} :: {resource.title_raw[:60]}")
                continue
            files = read_cached_listing(resource)
            if files is None:
                parse_fail += 1
                print(f"  [{resource.id[:8]}] parse-failed :: {resource.title_raw[:60]}")
                continue
            report = analyze_torrent_files(files)

            evaluation = evaluate_resource(resource, report)
            old = _fmt(resource)
            if evaluation.action == REWRITE:
                if apply:
                    await db.refresh(resource, ["file_assignments"])
                    apply_evaluation(resource, report, evaluation)
                new = "; ".join(f"{k} -> {v!r}" for k, v in evaluation.changes.items())
                print(
                    f"  [{resource.id[:8]}] {REWRITE} report_scope={report.scope} "
                    f"({old}) {new} :: {resource.title_raw[:60]}"
                )
                rewritten += 1
            else:
                skipped += 1
                print(
                    f"  [{resource.id[:8]}] {SKIP} report_scope={report.scope} "
                    f"({old}) {evaluation.reason} :: {resource.title_raw[:60]}"
                )

        print(
            f"=== batch SP rescan "
            f"({'applied' if apply else 'dry-run — pass --apply to write'}) ===\n"
            f"rewrite={rewritten} skip={skipped} no_torrent_cache={no_cache} "
            f"parse_failed={parse_fail}"
        )
        if apply and rewritten:
            await db.commit()
            print("committed.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write changes")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    asyncio.run(rescan(args.apply, args.limit))


if __name__ == "__main__":
    main()
