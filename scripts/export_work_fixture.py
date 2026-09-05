#!/usr/bin/env python3
"""Export a production work subgraph as a JSON test fixture.

Purpose: capture real multi-season works and large batch packs (TV season /
multi_season / franchise / movies scopes, plus movie samples) together with
their full object graph, as seed data for the per-season-works migration and
its integration tests (see docs/design/per-season-works.md).

Design notes:
- Read-only. Uses SQLAlchemy reflection (no app model imports) so it works
  against the live database regardless of app-version drift.
- Original UUIDs are preserved — the fixture loads into an empty DB without
  conflicts.
- Downloader credentials are redacted (fixture is committed to the repo).

Usage:
    python scripts/export_work_fixture.py --auto --output tests/fixtures/prod_works_v1.json
    python scripts/export_work_fixture.py --auto --series-id <uuid> [...]

DATABASE_URL (or --database-url) selects the source database.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import MetaData, Table, select, text
from sqlalchemy.ext.asyncio import create_async_engine

# Load order for the fixture (respects FK dependencies).
TABLE_ORDER = [
    "channels",
    "downloader_instances",
    "work_collections",
    "tv_series",
    "movies",
    "work_external_ids",
    "episodes",
    "file_resources",
    "resource_work_links",
    "resource_file_assignments",
    "agents",
    "agent_works",
    "pending_decisions",
    "download_tasks",
    "download_notifications",
    "metadata_cache",
]

REDACTED = "redacted"


def _jsonable(v):
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    return v


async def _reflect(engine) -> dict[str, Table]:
    metadata = MetaData()

    def _load(sync_conn):
        return {
            name: Table(name, metadata, autoload_with=sync_conn)
            for name in TABLE_ORDER
        }

    async with engine.connect() as conn:
        return await conn.run_sync(_load)


async def _rows(conn, table: Table, where=None) -> list[dict]:
    stmt = select(table)
    if where is not None:
        stmt = stmt.where(where)
    result = await conn.execute(stmt)
    return [{k: _jsonable(v) for k, v in row._mapping.items()} for row in result]


async def export(args) -> dict:
    engine = create_async_engine(args.database_url)
    t = await _reflect(engine)
    async with engine.connect() as conn:
        series_ids: set[str] = set(args.series_id or [])
        movie_ids: set[str] = set(args.movie_id or [])

        # 1) Works whose resources span more than one season.
        rows = await conn.execute(text(
            "SELECT series_id FROM file_resources "
            "WHERE series_id IS NOT NULL AND season IS NOT NULL "
            "GROUP BY series_id HAVING count(DISTINCT season) > 1"
        ))
        series_ids.update(r[0] for r in rows)

        # 1b) Works declared multi-season in metadata (seasons JSON list or
        #     number_of_seasons), even if current resources sit in one season.
        rows = await conn.execute(
            select(
                t["tv_series"].c.id,
                t["tv_series"].c.seasons,
                t["tv_series"].c.number_of_seasons,
            )
        )
        for sid, seasons, n_seasons in rows:
            if isinstance(seasons, str):
                seasons = json.loads(seasons)
            if (isinstance(seasons, list) and len(seasons) > 1) or (
                isinstance(n_seasons, int) and n_seasons > 1
            ):
                series_ids.add(sid)

        # 2) Works owning multi_season / franchise / unknown-scope batch packs.
        rows = await conn.execute(text(
            "SELECT DISTINCT series_id, movie_id, collection_id FROM file_resources "
            "WHERE is_batch AND (batch_scope IN ('multi_season','franchise') OR batch_scope IS NULL)"
        ))
        collection_ids: set[str] = set()
        for sid, mid, cid in rows:
            if sid:
                series_ids.add(sid)
            if mid:
                movie_ids.add(mid)
            if cid:
                collection_ids.add(cid)

        # 3) Deterministic sample of season-pack works.
        rows = await conn.execute(text(
            "SELECT DISTINCT series_id FROM file_resources "
            "WHERE is_batch AND batch_scope='season' AND series_id IS NOT NULL "
            "ORDER BY series_id LIMIT :n"
        ), {"n": args.season_pack_sample})
        series_ids.update(r[0] for r in rows)

        # 4) Movie samples: with / without collection.
        rows = await conn.execute(text(
            "SELECT id FROM movies WHERE collection_id IS NOT NULL ORDER BY id LIMIT :n"
        ), {"n": args.movie_sample})
        movie_ids.update(r[0] for r in rows)
        rows = await conn.execute(text(
            "SELECT id FROM movies WHERE collection_id IS NULL ORDER BY id LIMIT :n"
        ), {"n": args.movie_sample})
        movie_ids.update(r[0] for r in rows)

        # 5) Closure: collections of selected works + all member works of
        #    selected collections (franchise packs hang off collections).
        for _ in range(2):  # two rounds converge the work/collection closure
            if series_ids:
                rows = await conn.execute(
                    select(t["tv_series"].c.collection_id)
                    .where(t["tv_series"].c.id.in_(series_ids))
                )
                collection_ids.update(r[0] for r in rows if r[0])
            if movie_ids:
                rows = await conn.execute(
                    select(t["movies"].c.collection_id)
                    .where(t["movies"].c.id.in_(movie_ids))
                )
                collection_ids.update(r[0] for r in rows if r[0])
            if collection_ids:
                rows = await conn.execute(
                    select(t["tv_series"].c.id)
                    .where(t["tv_series"].c.collection_id.in_(collection_ids))
                )
                series_ids.update(r[0] for r in rows)
                rows = await conn.execute(
                    select(t["movies"].c.id)
                    .where(t["movies"].c.collection_id.in_(collection_ids))
                )
                movie_ids.update(r[0] for r in rows)

        # 6) Resources of selected works/collections.
        resources = await _rows(
            conn, t["file_resources"],
            (t["file_resources"].c.series_id.in_(series_ids))
            | (t["file_resources"].c.movie_id.in_(movie_ids))
            | (t["file_resources"].c.collection_id.in_(collection_ids)),
        )
        resource_ids = {r["id"] for r in resources}

        # 7) Links/assignments of those resources, then one final work closure
        #    (a linked work may sit outside the initial selection).
        links = await _rows(
            conn, t["resource_work_links"],
            t["resource_work_links"].c.resource_id.in_(resource_ids),
        )
        assignments = await _rows(
            conn, t["resource_file_assignments"],
            t["resource_file_assignments"].c.resource_id.in_(resource_ids),
        )
        extra_series = {lk["series_id"] for lk in links if lk.get("series_id")} - series_ids
        extra_movies = {lk["movie_id"] for lk in links if lk.get("movie_id")} - movie_ids
        extra_series |= {a["series_id"] for a in assignments if a.get("series_id")} - series_ids
        extra_movies |= {a["movie_id"] for a in assignments if a.get("movie_id")} - movie_ids
        series_ids |= extra_series
        movie_ids |= extra_movies
        if extra_series:
            rows = await conn.execute(
                select(t["tv_series"].c.collection_id)
                .where(t["tv_series"].c.id.in_(extra_series))
            )
            collection_ids.update(r[0] for r in rows if r[0])

        # 8) Agents: any agent_work / pending_decision touching selected works.
        agent_works = await _rows(
            conn, t["agent_works"],
            (t["agent_works"].c.series_id.in_(series_ids))
            | (t["agent_works"].c.movie_id.in_(movie_ids)),
        )
        decisions = await _rows(
            conn, t["pending_decisions"],
            (t["pending_decisions"].c.series_id.in_(series_ids))
            | (t["pending_decisions"].c.movie_id.in_(movie_ids)),
        )
        agent_ids = {w["agent_id"] for w in agent_works}
        agent_ids |= {d["agent_id"] for d in decisions if d.get("agent_id")}
        # All works rows of those agents (subscription context), keeping only
        # rows whose target is inside the fixture to preserve FK integrity.
        all_agent_works = await _rows(
            conn, t["agent_works"], t["agent_works"].c.agent_id.in_(agent_ids),
        )
        agent_works = [
            w for w in all_agent_works
            if (w.get("series_id") in series_ids) or (w.get("movie_id") in movie_ids)
        ]

        # 9) Tasks / downloaders / notifications for exported resources.
        tasks = await _rows(
            conn, t["download_tasks"],
            t["download_tasks"].c.file_resource_id.in_(resource_ids),
        )
        task_ids = {x["id"] for x in tasks}
        dl_ids = {x["downloader_id"] for x in tasks if x.get("downloader_id")}
        downloaders = await _rows(
            conn, t["downloader_instances"],
            t["downloader_instances"].c.id.in_(dl_ids),
        ) if dl_ids else []
        for d in downloaders:
            d["username"] = REDACTED if d.get("username") else None
            d["password"] = REDACTED if d.get("password") else None
        notifications = await _rows(
            conn, t["download_notifications"],
            t["download_notifications"].c.download_task_id.in_(task_ids),
        ) if task_ids else []

        # 10) metadata_cache rows for deterministic matching replay.
        raw_titles = {r["title_raw"] for r in resources}
        cache = await _rows(
            conn, t["metadata_cache"],
            t["metadata_cache"].c.title.in_(raw_titles),
        ) if raw_titles else []

        # 11) Remaining graph tables.
        channel_ids = {r["channel_id"] for r in resources}
        channels = await _rows(
            conn, t["channels"], t["channels"].c.id.in_(channel_ids),
        )
        collections = await _rows(
            conn, t["work_collections"],
            t["work_collections"].c.id.in_(collection_ids),
        ) if collection_ids else []
        series = await _rows(
            conn, t["tv_series"], t["tv_series"].c.id.in_(series_ids),
        )
        movies = await _rows(
            conn, t["movies"], t["movies"].c.id.in_(movie_ids),
        )
        external_ids = await _rows(
            conn, t["work_external_ids"],
            (t["work_external_ids"].c.work_id.in_(series_ids | movie_ids | collection_ids)),
        )
        episodes = await _rows(
            conn, t["episodes"], t["episodes"].c.series_id.in_(series_ids),
        )
        agents = await _rows(
            conn, t["agents"], t["agents"].c.id.in_(agent_ids),
        ) if agent_ids else []

    await engine.dispose()

    tables = {
        "channels": channels,
        "downloader_instances": downloaders,
        "work_collections": collections,
        "tv_series": series,
        "movies": movies,
        "work_external_ids": external_ids,
        "episodes": episodes,
        "file_resources": resources,
        "resource_work_links": links,
        "resource_file_assignments": assignments,
        "agents": agents,
        "agent_works": agent_works,
        "pending_decisions": decisions,
        "download_tasks": tasks,
        "download_notifications": notifications,
        "metadata_cache": cache,
    }
    return {
        "meta": {
            "exported_at": datetime.now(UTC).isoformat(),
            "selection": {
                "series_ids": sorted(series_ids),
                "movie_ids": sorted(movie_ids),
                "collection_ids": sorted(collection_ids),
            },
            "counts": {k: len(v) for k, v in tables.items()},
        },
        "tables": tables,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    parser.add_argument("--auto", action="store_true", help="automatic selection (default set)")
    parser.add_argument("--series-id", action="append", default=[])
    parser.add_argument("--movie-id", action="append", default=[])
    parser.add_argument("--season-pack-sample", type=int, default=20)
    parser.add_argument("--movie-sample", type=int, default=10)
    parser.add_argument("--output", help="write fixture JSON to this path")
    args = parser.parse_args()

    if not args.database_url:
        parser.error("DATABASE_URL not set and --database-url not given")

    result = asyncio.run(export(args))
    counts = result["meta"]["counts"]
    print("export summary:")
    for name in TABLE_ORDER:
        print(f"  {name:28s} {counts[name]}")
    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, ensure_ascii=False, indent=1))
        print(f"wrote {out} ({out.stat().st_size / 1024:.0f} KiB)")
    else:
        print("(no --output given; nothing written)")


if __name__ == "__main__":
    sys.exit(main())
