"""Backfill remote poster URLs into the local ``/posters/`` cache.

Dry-run by default; pass ``--apply`` to download and write local paths.
Covers TVSeries, Movie, AudioWork, and WorkCollection rows. Existing remote
URLs are left untouched when a download fails, so the command is safe to
re-run and complements the daily scheduler sweep.
"""
import argparse
import asyncio
import sys
from pathlib import Path

# `python scripts/poster_backfill.py` runs with sys.path[0]=scripts/.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import async_session_factory
from app.services.poster_backfill import POSTER_MODELS, cache_remote_posters


def _parse_models(value: str | None) -> list[str] | None:
    if not value:
        return None
    models = [part.strip() for part in value.split(",") if part.strip()]
    unknown = [name for name in models if name not in POSTER_MODELS]
    if unknown:
        raise SystemExit(f"unknown model(s): {', '.join(unknown)}")
    return models


async def main(
    *,
    apply: bool,
    limit: int,
    models: list[str] | None,
    concurrency: int,
) -> None:
    print(f"=== poster backfill ({'APPLY' if apply else 'DRY-RUN'}) ===", flush=True)
    async with async_session_factory() as db:
        report = await cache_remote_posters(
            db,
            models=models,
            limit_per_model=limit,
            concurrency=concurrency,
            apply=apply,
        )
        if not apply:
            await db.rollback()
        else:
            await db.commit()

    for name, counters in report.items():
        print(
            f"{name}: scanned={counters['scanned']} eligible={counters['eligible']} "
            f"cached={counters['cached']} failed={counters['failed']} "
            f"skipped={counters['skipped']}",
            flush=True,
        )
    if not apply:
        print("\n(dry-run; no changes written. Re-run with --apply to commit.)", flush=True)
    else:
        print("\nAPPLIED.", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=1000, help="max rows per model")
    parser.add_argument("--models", help="comma-separated: tv,movie,audio,collection")
    parser.add_argument("--concurrency", type=int, default=4)
    args = parser.parse_args()
    asyncio.run(
        main(
            apply=args.apply,
            limit=max(0, args.limit),
            models=_parse_models(args.models),
            concurrency=max(1, args.concurrency),
        )
    )
