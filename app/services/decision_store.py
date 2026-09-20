"""Versioned pending-choice identities and transactional candidate merging."""

import hashlib
import json
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.agent import Agent
from app.models.pending_decision import PendingDecision
from app.utils.time import utcnow


def choice_identity(kind, target_id, season, episode, coverage=None):
    if coverage is None:
        if kind not in {"series", "movie"} or not target_id or episode == -1:
            raise ValueError("A choice requires a work identity or exact batch coverage")
        scope = dict(version=1, kind=kind, work_id=target_id, season=season, episode=episode)
    else:
        scope = dict(version=1, kind="batch", coverage=coverage)
    encoded = json.dumps(scope, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "v1:" + hashlib.sha256(encoded.encode()).hexdigest(), json.loads(encoded)


def stored_choice_matches(key, scope):
    """Check versioned scope shape and digest without guessing current coverage."""
    if not isinstance(scope, dict):
        return False
    try:
        if scope["kind"] == "batch":
            expected_key, expected_scope = choice_identity("series", None, None, -1, scope["coverage"])
        else:
            expected_key, expected_scope = choice_identity(
                scope["kind"], scope["work_id"], scope["season"], scope["episode"]
            )
    except (KeyError, TypeError, ValueError):
        return False
    return key == expected_key and scope == expected_scope


async def persist_choice(
    db, *, agent_id, decision_key, decision_scope, candidate_ids, proposed_ids, picked_id, suggestion, fields
):
    """No network work while holding locks. Caller owns the outer transaction.

    The parent lock orders legitimate creators of an empty slot; the unique
    index is the final guard, including writers outside this service. A
    candidate change invalidates recommendations computed on an older set.
    """
    incoming = list(dict.fromkeys(candidate_ids))
    if len(incoming) < 2:
        raise ValueError("A pending choice requires at least two distinct candidates")
    from app.services.decision_identity_lock import lock_decision_identity

    await lock_decision_identity(db)
    parent = await db.scalar(select(Agent.id).where(Agent.id == agent_id).with_for_update(key_share=True))
    if parent is None:
        raise ValueError("Agent no longer exists")
    # The model recommendation may predate a completed work merge. Reload
    # actual candidates after coordination; never persist the stale identity.
    from sqlalchemy.orm import selectinload

    from app.models.file_resource import FileResource
    from app.services.decision_review import resource_choice
    from app.services.resource_coverage import load_batch_coverage

    current = []
    for offset in range(0, len(incoming), 100):
        current.extend(await db.scalars(
            select(FileResource).where(FileResource.id.in_(incoming[offset:offset + 100]))
            .options(selectinload(FileResource.series)).execution_options(populate_existing=True)
        ))
    await load_batch_coverage(db, current)
    if len(current) != len(incoming) or any(
        resource_choice(resource) != (decision_key, decision_scope) for resource in current
    ):
        raise ValueError("Decision candidates changed identity; regroup current resources before retry")
    query = (
        select(PendingDecision)
        .where(
            PendingDecision.agent_id == agent_id,
            PendingDecision.status == "pending",
            PendingDecision.decision_key == decision_key,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )

    async def merge(row):
        if row.decision_scope != decision_scope:
            raise ValueError("Decision identity disagrees with its stored scope")
        previous_ids = set(row.candidates or [])
        merged = list(dict.fromkeys([*(row.candidates or []), *incoming]))
        row.candidates = merged
        row.reason = fields["reason"]
        row.expires_at = utcnow() + timedelta(days=7)
        if set(merged) == set(proposed_ids) and picked_id in merged:
            row.llm_picked_resource_id, row.llm_suggestion = picked_id, suggestion
        elif (
            set(merged) != previous_ids or set(merged) != set(proposed_ids) or row.llm_picked_resource_id not in merged
        ):
            row.llm_picked_resource_id, row.llm_suggestion = None, None
        await db.flush()
        return row

    existing = await db.scalar(query)
    if existing is not None:
        return await merge(existing)
    row = PendingDecision(
        agent_id=agent_id,
        decision_key=decision_key,
        decision_scope=decision_scope,
        candidates=incoming,
        status="pending",
        expires_at=utcnow() + timedelta(days=7),
        llm_picked_resource_id=picked_id if picked_id in incoming else None,
        llm_suggestion=suggestion if picked_id in incoming else None,
        **fields,
    )
    try:
        async with db.begin_nested():
            db.add(row)
            await db.flush()
        return row
    except IntegrityError as exc:
        message = str(exc.orig)
        if not any(
            marker in message
            for marker in (
                "uq_pending_decisions_agent_key",
                "UNIQUE constraint failed: pending_decisions.agent_id, pending_decisions.decision_key",
                "UNIQUE constraint failed: pending_decisions.(agent_id, decision_key)",
            )
        ):
            raise
        existing = await db.scalar(query)
        if existing is None:
            raise
        return await merge(existing)
