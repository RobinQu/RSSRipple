"""Export and apply an offline publication bootstrap; never replay excluded history."""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

import app.models  # noqa: F401
from app.config import settings
from app.database import apply_db_pragmas
from app.models.agent import Agent
from app.models.agent_publication_progress import AgentPublicationProgress
from app.models.app_setting import AppSetting
from app.models.channel import Channel
from app.models.file_resource import FileResource
from app.models.resource_publication import ChannelPublicationCounter, ResourcePublication
from app.services.publication_migration import MARKER, bootstrap_publications, lock_publication_migration


def fingerprint(report):
    body = {k: v for k, v in report.items() if k not in {"fingerprint", "approved_fingerprint"}}
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


async def export_review(db):
    channels = list(await db.scalars(select(Channel.id).order_by(Channel.id)))
    resources = [
        dict(row)
        for row in (
            await db.execute(
                select(FileResource.id, FileResource.channel_id, FileResource.created_at).order_by(FileResource.id)
            )
        ).mappings()
    ]
    agents = [
        dict(row)
        for row in (
            await db.execute(select(Agent.id, Agent.channel_id, Agent.last_consumed_at).order_by(Agent.id))
        ).mappings()
    ]
    resources_by_channel = {}
    for resource in resources:
        resources_by_channel.setdefault(resource["channel_id"], []).append(resource)
    for agent in agents:
        candidates = resources_by_channel.get(agent["channel_id"], [])
        watermark = agent["last_consumed_at"]
        agent["pending_resource_ids"] = [
            r["id"] for r in candidates if watermark is not None and r["created_at"] > watermark
        ]
        agent["excluded_or_ambiguous_resource_ids"] = [
            r["id"] for r in candidates if watermark is None or r["created_at"] <= watermark
        ]
    occupied = {}
    for model in (ChannelPublicationCounter, ResourcePublication, AgentPublicationProgress):
        occupied[model.__tablename__] = bool(await db.scalar(select(model.id).limit(1)))
    marker = await db.get(AppSetting, MARKER)
    report = json.loads(
        json.dumps(
            dict(
                version=1,
                channels=channels,
                resources=resources,
                agents=agents,
                occupied=occupied,
                marker=marker.value if marker else None,
            ),
            default=str,
        )
    )
    report["fingerprint"] = fingerprint(report)
    return report


async def apply_review(db, review):
    if review.get("version") != 1 or review.get("fingerprint") != fingerprint(review):
        raise ValueError("Invalid review fingerprint")
    if review.get("approved_fingerprint") != review["fingerprint"]:
        raise ValueError("Explicit approved_fingerprint required")
    if review["marker"] is not None or any(review["occupied"].values()):
        raise ValueError("Review must describe an uninitialized publication database")
    await lock_publication_migration(db)
    marker = await db.get(AppSetting, MARKER)
    if marker is not None:
        if marker.value == review["fingerprint"]:
            return {"status": "already_applied"}
        raise ValueError("Database was initialized by a different migration")
    current = await export_review(db)
    if current["fingerprint"] != review["fingerprint"]:
        raise ValueError("Database changed; export and review again")
    result = await bootstrap_publications(db, writers_stopped=True)
    marker = await db.get(AppSetting, MARKER)
    marker.value = review["fingerprint"]
    await db.flush()
    return result


async def run(args):
    engine = create_async_engine(settings.database_url)
    apply_db_pragmas(engine)
    try:
        if args.prepare_schema:
            async with engine.begin() as conn:
                if conn.dialect.name == "postgresql":
                    await conn.execute(text("SET LOCAL lock_timeout = '5000'"))
                else:
                    await conn.execute(text("BEGIN IMMEDIATE"))
                for model in (ChannelPublicationCounter, ResourcePublication, AgentPublicationProgress):
                    await conn.run_sync(lambda sync, table=model.__table__: table.create(sync, checkfirst=True))
            return {"status": "schema_prepared"}
        async with AsyncSession(engine) as db:
            if args.export:
                # A consistent snapshot matters even though application rechecks it.
                if db.get_bind().dialect.name == "postgresql":
                    await db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
                report = await export_review(db)
                with Path(args.export).open("x", encoding="utf-8") as output:
                    output.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
                return {
                    "agents": len(report["agents"]),
                    "resources": len(report["resources"]),
                    "fingerprint": report["fingerprint"],
                }
            review = json.loads(Path(args.apply_review).read_text())
            if db.get_bind().dialect.name == "sqlite":
                await db.execute(text("BEGIN IMMEDIATE"))
            result = await apply_review(db, review)
            await db.commit()
            return result
    finally:
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--prepare-schema", action="store_true")
    mode.add_argument("--export")
    mode.add_argument("--apply-review")
    parser.add_argument("--writers-stopped", action="store_true")
    parser.add_argument("--backup-confirmed", action="store_true")
    args = parser.parse_args()
    if (args.apply_review or args.prepare_schema) and not (args.writers_stopped and args.backup_confirmed):
        parser.error("Applying requires --writers-stopped and --backup-confirmed")
    print(json.dumps(asyncio.run(run(args)), ensure_ascii=False))


if __name__ == "__main__":
    main()
