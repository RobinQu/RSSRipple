"""Repair and requeue the current Dashboard metadata-confirmation backlog.

Dry-run by default. ``--apply`` refreshes date-incomplete works, applies
 deterministic fixes, and creates durable reparse requests for the same
resource snapshot that was pending when the script started.
"""

import argparse
import asyncio
from collections import Counter

from sqlalchemy import select

from app.api.v1.dashboard import _page_pending_confirmations
from app.database import async_session_factory
from app.models.channel import Channel
from app.models.file_resource import FileResource
from app.services.resource_parser import extract_compilation_work_title, extract_release_title
from app.services.resource_reparse_requests import create_request, wake_request


async def _pending(db):
    items, total = await _page_pending_confirmations(db, 1, 100)
    if total > len(items):
        raise RuntimeError(f"refusing partial backlog: {total} pending, {len(items)} loaded")
    return items


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    async with async_session_factory() as db:
        items = await _pending(db)
        print(f"pending={len(items)}")
        print("kinds=", dict(Counter(kind for item in items for kind in item["kinds"])))
        if not args.apply:
            for item in items:
                resource = item["resource"]
                print(resource["id"], item["kinds"], item["missing_fields"], resource["title_raw"])
            return
        if not items:
            return

        ids = [item["resource"]["id"] for item in items]
        resources = list((await db.scalars(
            select(FileResource).where(FileResource.id.in_(ids))
        )).all())
        by_id = {resource.id: resource for resource in resources}

        refresh_groups = {}
        for item in items:
            if "year" not in item["missing_fields"]:
                continue
            resource = by_id[item["resource"]["id"]]
            work_id = resource.series_id or resource.movie_id
            if not work_id:
                continue
            channel = await db.get(Channel, resource.channel_id)
            if channel is None:
                continue
            channel.metadata_refresh_enabled = True
            group = refresh_groups.setdefault(channel.id, {"channel": channel, "items": [], "seen": set()})
            if work_id not in group["seen"]:
                group["seen"].add(work_id)
                group["items"].append({
                    "id": work_id,
                    "content_type": "tv" if resource.series_id else "movie",
                })
        await db.commit()

        from app.job_handlers import _refresh_works_batch
        from app.services.metadata_sources import resolve_metadata_source

        for group in refresh_groups.values():
            channel = group["channel"]
            results = await _refresh_works_batch(
                group["items"], resolve_metadata_source(channel.metadata_source),
                trusted_sites=channel.metadata_fallback_sources,
                strategy="fill_missing", time_budget=0,
            )
            print(
                "work_refresh", channel.name,
                f"{sum(bool(row.get('found')) for row in results)}/{len(results)}",
            )

        changed = 0
        from app.services.declared_season_links import link_declared_seasons
        from app.services.fetch_service import reconcile_stale_raw_episodes
        from app.services.franchise_service import enforce_franchise_resource_invariant
        from app.services.resource_publication import publish_resource

        for resource in resources:
            dirty = False
            if resource.episode is not None and (
                resource.episode == resource.title_year
                or resource.title_raw.lower().startswith(f"{resource.episode}yrs ago")
                or resource.title_raw.lower().startswith(f"{resource.episode}years ago")
                or (
                    resource.episode in (360, 480, 540, 720, 1080, 1440, 2160)
                    and f"{resource.episode}p" in resource.title_raw.lower()
                )
            ):
                resource.episode = None
                if resource.absolute_episode is not None:
                    resource.absolute_episode = None
                    resource.episode_confidence = None
                dirty = True
            if resource.search_title in (None, "", "合集", "43yrs ago -"):
                cleaned = extract_release_title(resource.title_raw)
                if cleaned and cleaned != resource.search_title:
                    resource.search_title = cleaned
                    dirty = True
            if extract_compilation_work_title(resource.title_raw):
                resource.is_batch = True
                resource.batch_scope = "franchise"
                resource.episode_start = None
                resource.episode_end = None
                dirty = True
                if enforce_franchise_resource_invariant(resource):
                    dirty = True
            if await link_declared_seasons(db, resource):
                dirty = True
            if dirty:
                await publish_resource(db, resource.id, kind="metadata")
                changed += 1

        from app.config import settings
        from app.services.app_logs import record_external_failure

        external_logs = 0
        for resource in resources:
            failures = []
            if (
                resource.metadata_failure_type == "transient"
                and resource.metadata_attempts >= settings.metadata_transient_max_attempts
            ):
                failures.append((
                    "metadata", "metadata search failed after automatic retries",
                    resource.metadata_attempts,
                ))
            if resource.torrent_fetch_status == "failed" and (
                resource.torrent_fetch_next_attempt_at is None
                or resource.torrent_fetch_attempts >= settings.torrent_fetch_max_attempts
            ):
                failures.append((
                    "torrent", resource.torrent_fetch_error or "torrent fetch failed",
                    resource.torrent_fetch_attempts,
                ))
            if resource.magnet_resolve_status == "failed":
                failures.append((
                    "magnet", resource.magnet_resolve_error or "magnet resolution failed",
                    resource.magnet_resolve_attempts,
                ))
            if resource.listing_reconcile_status == "failed" and (
                resource.listing_reconcile_next_attempt_at is None
                or resource.listing_reconcile_attempts >= settings.listing_reconcile_max_attempts
            ):
                failures.append((
                    "listing", resource.listing_reconcile_error or "listing reconcile failed",
                    resource.listing_reconcile_attempts,
                ))
            for category, message, attempts in failures:
                await record_external_failure(
                    db, category=category, message=message, resource_id=resource.id,
                    channel_id=resource.channel_id, attempts=attempts,
                )
                external_logs += 1
        print(f"external_logs={external_logs}")
        await db.commit()
        reconciled = await reconcile_stale_raw_episodes(db, return_resource_ids=True)
        await db.commit()
        print(f"deterministic_changes={changed} reconciled={len(reconciled)}")

        requests = []
        for resource_id in ids:
            resource = by_id[resource_id]
            request = await create_request(db, resource_id, resource.channel_id)
            if request is not None:
                requests.append(request)
        await db.commit()
        for request in requests:
            await wake_request(request)
        print(f"reparse_enqueued={len(requests)} already_pending={len(ids) - len(requests)}")


if __name__ == "__main__":
    asyncio.run(main())
