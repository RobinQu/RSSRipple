"""Genre unification backfill: bring stored work genres onto the closed TMDB set.

Mode A (default): scan TVSeries / Movie / AudioWork rows and rewrite each
``genre`` array through ``genre_registry.normalize_genres`` — drops legacy
non-canonical values, maps TMDB ids / aliases / case variants onto the
canonical English names. Idempotent.

Mode B (``--refresh-empty``): works whose genre is still empty after
mode A get a full metadata refresh via ``refresh_work_metadata`` (the cache
generation bump to 3 forces a re-run, and the updated judge/ReAct prompts
now emit genre). The refresh source is the work's own identity source when
it is wikipedia/tmdb, otherwise wikipedia (works identified only via
``exa_web``/manual rows are refreshed by title). AudioWork is not covered
by ``refresh_work_metadata``. This mode makes network + LLM calls per
work — use --limit/--delay to pace it.

NOTE on locking: the embedded-Turso backend holds a single-process exclusive
file lock, so STOP the app (``docker compose stop app``) before running this
against the dev database, then start it again afterwards.

Dry-run by default; pass --apply to execute.

Usage:
    uv run python scripts/genre_backfill.py [--apply] [--limit N]
    uv run python scripts/genre_backfill.py --apply --refresh-empty [--limit N] [--delay S]
"""

import argparse
import asyncio

from sqlalchemy import select

from app.database import async_session_factory
from app.models.audio_work import AudioWork
from app.models.movie import Movie
from app.models.series import TVSeries
from app.services.genre_registry import normalize_genres
from app.services.metadata_service import refresh_work_metadata

APPLY_BATCH_SIZE = 20
_REFRESH_SOURCES = {"wikipedia", "tmdb"}


async def _select_works(db) -> list[tuple[str, object]]:
    """All works as (kind, row) pairs: series / movie / audio."""
    rows: list[tuple[str, object]] = []
    for kind, model in (("series", TVSeries), ("movie", Movie), ("audio", AudioWork)):
        result = await db.execute(select(model).order_by(model.created_at))
        rows.extend((kind, w) for w in result.scalars().all())
    return rows


def _title_of(work) -> str:
    return work.title_cn or work.title_en or work.original_title or work.id


async def main(apply: bool, limit: int | None, delay: float, refresh_empty: bool) -> None:
    print(f"=== genre backfill ({'APPLY' if apply else 'DRY-RUN'}) ===")
    async with async_session_factory() as db:
        rows = await _select_works(db)
    print(f"{len(rows)} works (series/movie/audio)")
    if limit:
        rows = rows[:limit]

    # ── Mode A: in-place normalization ──────────────────────────────────
    changed = unchanged = emptied = 0
    session = async_session_factory() if apply else None
    try:
        for kind, w in rows:
            norm = normalize_genres(w.genre)
            current = list(w.genre or [])
            if norm == current:
                unchanged += 1
                continue
            changed += 1
            if not norm:
                emptied += 1
            print(f"[{kind}] {_title_of(w)!r}: {current} -> {norm}")
            if apply:
                model = {"series": TVSeries, "movie": Movie, "audio": AudioWork}[kind]
                row = await session.get(model, w.id)
                row.genre = norm
                if changed % APPLY_BATCH_SIZE == 0:
                    await session.commit()
                    print(f"    ... committed batch ({changed} works)")
        if apply and changed:
            await session.commit()
            print(f"applied: normalized {changed} works; committed.")
        elif not apply:
            print("dry-run: nothing written; re-run with --apply to execute.")
        print(f"mode A: changed={changed} unchanged={unchanged} (became empty: {emptied})")

        # ── Mode B: refresh works still without genre ───────────────────
        if not refresh_empty:
            return
        empty_rows = [
            (kind, w) for kind, w in rows
            if kind in ("series", "movie") and not normalize_genres(w.genre)
        ]
        print(f"\nmode B: {len(empty_rows)} empty-genre series/movie works to refresh")
        ok = failed = 0
        for i, (kind, w) in enumerate(empty_rows, 1):
            if i > 1:
                await asyncio.sleep(delay)
            title = _title_of(w)
            # Refresh via the work's own identity source when supported,
            # else fall back to wikipedia (title-based judge path).
            src = (w.external_source or "").lower()
            if src not in _REFRESH_SOURCES:
                src = "wikipedia"
            if not apply:
                print(f"[{i}/{len(empty_rows)}] -- [{kind}] {title!r} ({w.external_source} -> {src})")
                continue
            try:
                result = await refresh_work_metadata(
                    session, w.id, "movie" if kind == "movie" else "tv", src,
                )
            except Exception as exc:  # keep going; one bad work must not kill the run
                failed += 1
                print(f"[{i}/{len(empty_rows)}] FAIL [{kind}] {title!r}: {exc}")
                await session.rollback()
                continue
            if "genre" in (result.get("filled") or []):
                ok += 1
                print(f"[{i}/{len(empty_rows)}] OK [{kind}] {title!r}: genre filled")
            else:
                failed += 1
                print(
                    f"[{i}/{len(empty_rows)}] -- [{kind}] {title!r}: "
                    f"no genre from refresh ({result.get('message')})"
                )
        if apply:
            print(f"mode B applied: filled={ok} no-genre={failed} of {len(empty_rows)}")
        else:
            print("mode B dry-run: nothing written; re-run with --apply to execute.")
    finally:
        if session is not None:
            await session.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--delay", type=float, default=0.5, help="seconds between refreshes (mode B)")
    parser.add_argument("--refresh-empty", action="store_true",
                        help="mode B: re-run metadata for works still without genre")
    args = parser.parse_args()
    asyncio.run(main(args.apply, args.limit, args.delay, args.refresh_empty))
