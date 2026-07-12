"""One-off migration: (1) strip season from series titles, (2) merge duplicate
show rows, (3) reset not_found resources that now match a known series so S1
re-links them. Idempotent. Dry-run by default; pass --apply to execute."""
import argparse
import asyncio
from collections import defaultdict

from sqlalchemy import func, select, update

from app.database import async_session_factory
from app.models.episode import Episode
from app.models.file_resource import FileResource
from app.models.series import TVSeries
from app.services.resource_parser import strip_season_from_title


async def main(apply: bool) -> None:
    print(f"=== migration ({'APPLY' if apply else 'DRY-RUN'}) ===")
    async with async_session_factory() as db:
        # ── Step 1: clean season-suffixed series titles ──
        series_rows = (await db.execute(select(TVSeries))).scalars().all()
        clean_count = 0
        for s in series_rows:
            changed = False
            for attr in ("title_cn", "title_en"):
                cur = getattr(s, attr, None)
                stripped = strip_season_from_title(cur)
                if cur and stripped != cur:
                    aliases = list(s.aliases or [])
                    if cur not in aliases:
                        aliases.append(cur)
                    s.aliases = aliases or None
                    setattr(s, attr, stripped)
                    changed = True
            if changed:
                clean_count += 1
                print(f"  [clean] {s.id} -> title_cn={s.title_cn!r}")
        print(f"step 1: {clean_count} series titles to clean")

        # ── Step 2: merge duplicate shows (same stripped title_cn) ──
        await db.flush()
        series_rows = (await db.execute(select(TVSeries))).scalars().all()
        groups: dict[str, list[TVSeries]] = defaultdict(list)
        for s in series_rows:
            key = (strip_season_from_title(s.title_cn) or s.title_en or "").strip().lower()
            if key:
                groups[key].append(s)
        merge_plans = []
        for key, rows in groups.items():
            if len(rows) < 2:
                continue
            counts = {}
            for r in rows:
                counts[r.id] = await db.scalar(
                    select(func.count(FileResource.id)).where(FileResource.series_id == r.id)
                ) or 0
            src_pref = {"tmdb": 0, "exa": 1, "wikipedia": 2, "jina": 3}
            # Canonical = most authoritative source first (tmdb/exa carry richer
            # metadata than wikipedia/jina), then most resources (fewer repoints).
            rows_sorted = sorted(
                rows,
                key=lambda r: (
                    src_pref.get((r.external_source or "").split(":")[0], 9),
                    -counts[r.id],
                ),
            )
            canonical = rows_sorted[0]
            dups = rows_sorted[1:]
            merge_plans.append((canonical, dups, counts))
            print(f"  [merge] canonical={canonical.id} (n={counts[canonical.id]}, {canonical.external_source}); "
                  f"dups={[ (d.id, counts[d.id]) for d in dups ]}")
        print(f"step 2: {len(merge_plans)} duplicate-show groups to merge")

        # ── Step 3: reset not_found resources that now match a known series ──
        index: dict[str, str] = {}
        ambiguous: set[str] = set()
        for s in (await db.execute(select(TVSeries))).scalars().all():
            for k in (s.title_cn, s.title_en, *(s.aliases or [])):
                nk = strip_season_from_title(k)
                if not nk:
                    continue
                nk = nk.lower()
                if nk in index and index[nk] != s.id:
                    ambiguous.add(nk)
                else:
                    index[nk] = s.id
        reset_count = 0
        nf_rows = (await db.execute(
            select(FileResource).where(FileResource.metadata_failure_type == "not_found")
        )).scalars().all()
        for r in nf_rows:
            nk = strip_season_from_title(r.title_cn or "").lower()
            if nk and nk not in ambiguous and nk in index:
                r.metadata_failure_type = None
                r.last_metadata_attempt_at = None
                r.metadata_attempts = 0
                reset_count += 1
        print(f"step 3: {reset_count} not_found resources reset for rematch")

        if not apply:
            print("\n(dry-run; no changes written. Re-run with --apply to commit.)")
            await db.rollback()
            return

        # ── Apply merges: repoint resources + episodes, delete dup series ──
        for canonical, dups, _counts in merge_plans:
            canon_aliases = list(canonical.aliases or [])
            for d in dups:
                for t in (d.title_cn, d.title_en, d.original_title, *(d.aliases or [])):
                    if t and t not in canon_aliases:
                        canon_aliases.append(t)
                # Repoint file resources.
                await db.execute(
                    update(FileResource).where(FileResource.series_id == d.id).values(series_id=canonical.id)
                )
                # Repoint episodes; drop dup episodes that conflict with
                # canonical's (season, episode) - canonical wins.
                canon_eps = {
                    (e.season, e.episode)
                    for e in (
                        await db.execute(
                            select(Episode).where(Episode.series_id == canonical.id)
                        )
                    ).scalars().all()
                }
                dup_eps = (
                    await db.execute(select(Episode).where(Episode.series_id == d.id))
                ).scalars().all()
                for e in dup_eps:
                    if (e.season, e.episode) in canon_eps:
                        await db.delete(e)
                    else:
                        e.series_id = canonical.id
                        canon_eps.add((e.season, e.episode))
                await db.delete(d)
            canonical.aliases = canon_aliases or None
        await db.commit()
        print("\nAPPLIED.")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()
    asyncio.run(main(args.apply))
