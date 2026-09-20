"""Rebuild affected pending slots after work identity changes, without dispatch."""

import hashlib
import json
from datetime import timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload

from app.models.agent import Agent
from app.models.decision_migration import DecisionMigration
from app.models.file_resource import FileResource
from app.models.pending_decision import PendingDecision
from app.models.resource_file_assignment import ResourceFileAssignment
from app.models.resource_work_link import ResourceWorkLink
from app.services.decision_review import review_decisions
from app.services.resource_coverage import load_batch_coverage
from app.utils.time import utcnow


async def _find_work_choice_agents(db, kind, work_ids):
    column = PendingDecision.series_id if kind == "series" else PendingDecision.movie_id
    resource_column = FileResource.series_id if kind == "series" else FileResource.movie_id
    link_column = ResourceWorkLink.series_id if kind == "series" else ResourceWorkLink.movie_id
    assignment_column = ResourceFileAssignment.series_id if kind == "series" else ResourceFileAssignment.movie_id
    targets = set(work_ids)
    affected_agents = set()
    cursor = ""
    while True:
        rows = list(
            await db.execute(
                select(
                    PendingDecision.id, PendingDecision.agent_id, PendingDecision.candidates, column.label("work_id")
                )
                .where(PendingDecision.status == "pending", PendingDecision.id > cursor)
                .order_by(PendingDecision.id)
                .limit(100)
            )
        )
        if not rows:
            break
        candidates = sorted(
            {rid for row in rows if isinstance(row.candidates, list) for rid in row.candidates if isinstance(rid, str)}
        )
        affected_resources = set()
        for offset in range(0, len(candidates), 100):
            affected_resources.update(
                await db.scalars(
                    select(FileResource.id).where(
                        FileResource.id.in_(candidates[offset : offset + 100]),
                        or_(
                            resource_column.in_(targets),
                            FileResource.work_links.any(link_column.in_(targets)),
                            FileResource.file_assignments.any(assignment_column.in_(targets)),
                        ),
                    )
                )
            )
        for row in rows:
            if row.work_id in targets or (
                isinstance(row.candidates, list)
                and any(rid in affected_resources for rid in row.candidates if isinstance(rid, str))
            ):
                affected_agents.add(row.agent_id)
        cursor = rows[-1].id
    return affected_agents


async def lock_work_choice_agents(db, work_groups):
    from app.services.decision_identity_lock import lock_decision_identity

    await lock_decision_identity(db, changing=True)
    # Discover both source and target agents first, then use one global order.
    affected = set()
    for kind, work_ids in work_groups:
        affected.update(await _find_work_choice_agents(db, kind, work_ids))
    ids = sorted(affected)
    for identity in ids:
        await db.scalar(select(Agent.id).where(Agent.id == identity).with_for_update(key_share=True))
    return ids


def scope_fields(scope):
    fields = dict(series_id=None, movie_id=None, season=None, episode=None)
    if scope["kind"] == "batch":
        fields["episode"] = -1
        descriptors = scope["coverage"][1]
        if len(descriptors) == 1:
            kind, work_id, season, _ = descriptors[0]
            fields["series_id" if kind == "series" else "movie_id"] = work_id
            fields["season"] = season
    else:
        fields["series_id" if scope["kind"] == "series" else "movie_id"] = scope["work_id"]
        fields["season"], fields["episode"] = scope["season"], scope["episode"]
    return fields


async def rekey_agent_choices(db, agent_ids):
    """Caller holds parent locks and commits the surrounding work merge.

    Archive before-images; never delete user decisions or automatically pick a
    resource. Unknown/singleton coverage cannot create a pending choice.
    """
    for agent_id in sorted(set(agent_ids)):
        rows = list(
            await db.scalars(
                select(PendingDecision)
                .where(PendingDecision.agent_id == agent_id, PendingDecision.status == "pending")
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        if not rows:
            continue
        originals = json.loads(
            json.dumps(
                [
                    {column.name: getattr(row, column.name) for column in PendingDecision.__table__.columns}
                    for row in rows
                ],
                default=str,
            )
        )
        resource_ids = sorted({rid for row in rows for rid in row.candidates or []})
        resources = {}
        for offset in range(0, len(resource_ids), 100):
            batch = list(
                await db.scalars(
                    select(FileResource)
                    .where(FileResource.id.in_(resource_ids[offset : offset + 100]))
                    .options(selectinload(FileResource.series))
                    .execution_options(populate_existing=True)
                )
            )
            await load_batch_coverage(db, batch)
            resources.update((resource.id, resource) for resource in batch)
        review = review_decisions(originals, resources)
        by_id = {row.id: row for row in rows}
        stable = set()
        for group in review["proposed_groups"]:
            if len(group["source_decision_ids"]) != 1 or group["requires_review"]:
                continue
            row = by_id[group["source_decision_ids"][0]]
            if (
                row.decision_key == group["decision_key"]
                and row.decision_scope == group["decision_scope"]
                and set(row.candidates) == set(group["candidates"])
            ):
                stable.add(row.id)
        changed = set(by_id) - stable
        if not changed:
            continue
        for identity in changed:
            by_id[identity].status = "expired"
        await db.flush()  # Release pending unique slots before replacements.
        created = []
        for group in review["proposed_groups"]:
            if not changed.intersection(group["source_decision_ids"]) or group["requires_review"]:
                continue
            row = PendingDecision(
                agent_id=agent_id,
                decision_key=group["decision_key"],
                decision_scope=group["decision_scope"],
                candidates=group["candidates"],
                status="pending",
                reason="作品关联变更后的同覆盖候选，请重新选择",
                expires_at=utcnow() + timedelta(days=7),
                **scope_fields(group["decision_scope"]),
            )
            db.add(row)
            await db.flush()
            created.append(dict(id=row.id, source_decision_ids=group["source_decision_ids"]))
        review["operation"] = "work_rekey"
        fingerprint = hashlib.sha256(json.dumps(review, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        db.add(
            DecisionMigration(
                review_fingerprint=fingerprint,
                original_review=review,
                result=dict(
                    operation="work_rekey", superseded_ids=sorted(changed), created=created, blocked=review["blocked"]
                ),
            )
        )
        await db.flush()
