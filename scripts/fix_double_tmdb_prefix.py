"""One-off cleanup: repair double-prefixed ``external_id`` values.

A historical bug applied ``canonicalize_external_id`` twice, leaving rows with
``external_id`` like ``tmdb:tmdb:1430077`` (the same prefix repeated). This
script scans ``Movie``, ``TVSeries`` and ``WorkCollection`` rows for that
pattern and normalizes them back to the canonical single-prefix form
(``tmdb:1430077``; raw digits for WorkCollection, whose convention is
``external_source="tmdb_collection"`` + the bare numeric id).

COLLISION HANDLING: if the normalized id is already owned by ANOTHER row of
the same model, the row is NOT rewritten — the pair is reported as a
collision (duplicate work rows for the daily dedup task or manual review).

NOTE on locking: the embedded-Turso backend holds a single-process exclusive
file lock, so STOP the app (``docker compose stop app``) before running this
against the dev database, then start it again afterwards.

Dry-run by default; pass --apply to execute.
"""

import argparse
import asyncio
import re

from sqlalchemy import select

from app.database import async_session_factory
from app.models.movie import Movie
from app.models.series import TVSeries
from app.models.work_collection import WorkCollection
from app.services.metadata_service import canonicalize_external_id

BATCH_SIZE = 20

# Same prefix twice: "tmdb:tmdb:1430077", "imdb:imdb:tt123", ...
_DOUBLE_PREFIX_RE = re.compile(r"^([a-z0-9_]+):\1:(?P<rest>.+)$", re.IGNORECASE)
_ANY_PREFIX_RE = re.compile(r"^[a-z0-9_]+:(?P<rest>.+)$", re.IGNORECASE)

OUTCOME_FIXED = "fixed"
OUTCOME_COLLISION = "collision"
OUTCOME_UNCHANGED = "unchanged"


def normalize_double_prefix(
    external_id: str | None,
    source: str | None = None,
) -> str | None:
    """Return the canonical form if ``external_id`` has a doubled prefix.

    Prefers ``canonicalize_external_id`` (it already collapses
    ``tmdb:tmdb:1430077`` -> ``tmdb:1430077``); falls back to stripping the
    duplicated prefix directly for shapes the canonicalizer doesn't know.
    Returns None when there is no double-prefix pattern.
    """
    if not external_id:
        return None
    s = external_id.strip()
    m = _DOUBLE_PREFIX_RE.match(s)
    if not m:
        return None
    canonical = canonicalize_external_id(s, source)
    if canonical and not _DOUBLE_PREFIX_RE.match(canonical):
        return canonical
    return f"{m.group(1).lower()}:{m.group('rest')}"


def normalize_collection_id(external_id: str | None) -> str | None:
    """Collapse a doubled prefix to WorkCollection's raw-digit convention.

    WorkCollection deliberately stores the bare TMDB collection numeric id
    (NOT ``tmdb:<digits>``), so all prefixes are stripped, not just one.
    Returns None when there is no double-prefix pattern.
    """
    if not external_id:
        return None
    s = external_id.strip()
    if not _DOUBLE_PREFIX_RE.match(s):
        return None
    while True:
        m = _ANY_PREFIX_RE.match(s)
        if not m:
            return s
        s = m.group("rest")


def plan_fix(
    external_id: str | None,
    source: str | None,
    taken_ids: set[str],
    *,
    keep_prefix: bool = True,
) -> tuple[str, str | None]:
    """Decide what to do with one row. Pure; mutates nothing.

    ``taken_ids`` must contain the external_ids of all OTHER rows of the same
    model (for WorkCollection: same external_source). Returns
    ``(outcome, normalized_id_or_None)``.
    """
    if keep_prefix:
        normalized = normalize_double_prefix(external_id, source)
    else:
        normalized = normalize_collection_id(external_id)
    if normalized is None or normalized == external_id:
        return OUTCOME_UNCHANGED, None
    if normalized in taken_ids:
        return OUTCOME_COLLISION, normalized
    return OUTCOME_FIXED, normalized


def _row_label(row) -> str:
    title = (
        getattr(row, "title_cn", None)
        or getattr(row, "title_en", None)
        or getattr(row, "original_title", None)
        or ""
    )
    return f"{title!r} ({row.id[:8]})"


async def fix_model(db, model, label: str, apply: bool, *, keep_prefix: bool = True) -> None:
    rows = (await db.execute(
        select(model).where(model.external_id.isnot(None))
    )).scalars().all()

    fixed = collisions = 0
    pending = 0
    for row in rows:
        taken = {
            r.external_id for r in rows
            if r.id != row.id
            and (keep_prefix or r.external_source == row.external_source)
        }
        outcome, normalized = plan_fix(
            row.external_id, row.external_source, taken, keep_prefix=keep_prefix
        )
        if outcome == OUTCOME_UNCHANGED:
            continue
        if outcome == OUTCOME_COLLISION:
            collisions += 1
            owners = [r for r in rows if r.id != row.id and r.external_id == normalized]
            owner_desc = ", ".join(_row_label(o) for o in owners)
            print(
                f"  [collision] {label} {_row_label(row)}: {row.external_id!r} -> "
                f"{normalized!r} already owned by {owner_desc}; left untouched, "
                f"needs dedup/manual merge"
            )
            continue
        print(f"  [fix] {label} {_row_label(row)}: {row.external_id!r} -> {normalized!r}")
        fixed += 1
        if apply:
            row.external_id = normalized
            pending += 1
            if pending >= BATCH_SIZE:
                await db.commit()
                pending = 0

    if apply and pending:
        await db.commit()
    verb = "fixed" if apply else "would fix"
    print(f"{label}: {verb}={fixed} collisions={collisions} scanned={len(rows)}")


async def main(apply: bool) -> None:
    print(f"=== double-prefix external_id cleanup ({'APPLY' if apply else 'DRY-RUN'}) ===")
    async with async_session_factory() as db:
        await fix_model(db, Movie, "Movie", apply)
        await fix_model(db, TVSeries, "TVSeries", apply)
        await fix_model(db, WorkCollection, "WorkCollection", apply, keep_prefix=False)
    if not apply:
        print("dry-run: re-run with --apply to execute.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.apply))
