"""Reference checks and guarded deletion shared by explicit work endpoints."""
from dataclasses import dataclass

from sqlalchemy import delete as sql_delete
from sqlalchemy import func, or_, select
from sqlalchemy import update as sql_update
from sqlalchemy.exc import DBAPIError

from app.models.channel_raw_title_mapping import ChannelRawTitleMapping
from app.models.resource_file_assignment import ResourceFileAssignment
from app.models.resource_work_link import ResourceWorkLink


async def manual_deletion_references(db, kind, work_id):
    """Count manual evidence that requires explicit reassignment before delete.

    Called inside the deletion transaction. These checks establish the policy;
    the caller must first lock the target and its current reference rows.
    """
    if kind not in {"series", "movie"}:
        raise ValueError("Unsupported deletable work type")
    counts = {}
    for name, model in (
        ("manual_work_links", ResourceWorkLink),
        ("manual_file_assignments", ResourceFileAssignment),
        ("manual_title_mappings", ChannelRawTitleMapping),
    ):
        query = select(func.count()).select_from(model).where(getattr(model, kind + "_id") == work_id)
        if model is not ChannelRawTitleMapping:
            query = query.where(model.source == "manual")
        count = await db.scalar(query)
        if count:
            counts[name] = count
    return counts


class WorkDeletionBusyError(Exception):
    """A concurrent transaction owns the deletion target; retry after rollback."""


async def lock_deletion_target(db, model, work_id):
    """Protect identity cleanup before deleting the polymorphic bag owner.

    NOWAIT prevents waiting for a producer that may already hold resource locks.
    The owner lock serializes new FK references; existing reference rows and
    resource parents are then locked without waiting before manual checks.
    """
    from app.models.movie import Movie
    from app.services.decision_rekey import lock_work_choice_agents

    kind = "movie" if model is Movie else "series"
    try:
        agent_ids = await lock_work_choice_agents(db, [(kind, [work_id])])
        target = await db.scalar(
            select(model)
            .where(model.id == work_id)
            .with_for_update(nowait=True)
            .execution_options(populate_existing=True)
        )
        if target is None:
            return None, agent_ids
        await lock_work_references(db, kind, work_id)
        return target, agent_ids
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) != "55P03" and "concurrent decision identity change" not in str(exc):
            raise
        raise WorkDeletionBusyError from exc


@dataclass(frozen=True)
class WorkDeletionOutcome:
    """Result of :func:`delete_work_with_guards` for route-level mapping."""

    status: str  # deleted | busy | not_found | references | agent_work
    references: dict | None = None
    agent_work_count: int | None = None


async def delete_work_with_guards(db, model, work_id, *, kind: str) -> WorkDeletionOutcome:
    """Lock, validate and delete a work, or report why deletion is blocked.

    The series and movie routes differ only in labels; the lock ordering,
    reference guards, FK nullification, decision rekey and identity cleanup
    stay identical here.
    """
    from app.models.agent_work import AgentWork
    from app.models.file_resource import FileResource
    from app.models.pending_decision import PendingDecision
    from app.services.decision_rekey import rekey_agent_choices
    from app.services.external_ids import delete_external_ids_for_work

    field = f"{kind}_id"
    try:
        target, choice_agent_ids = await lock_deletion_target(db, model, work_id)
    except WorkDeletionBusyError:
        await db.rollback()
        return WorkDeletionOutcome("busy")
    if not target:
        return WorkDeletionOutcome("not_found")

    references = await manual_deletion_references(db, kind, work_id)
    if references:
        return WorkDeletionOutcome("references", references=references)

    agent_work_count = (await db.execute(
        select(func.count()).select_from(AgentWork).where(getattr(AgentWork, field) == work_id)
    )).scalar_one()
    if agent_work_count > 0:
        return WorkDeletionOutcome("agent_work", agent_work_count=agent_work_count)

    await db.execute(
        sql_update(FileResource).where(getattr(FileResource, field) == work_id).values({field: None})
    )
    await db.execute(
        sql_update(ChannelRawTitleMapping)
        .where(getattr(ChannelRawTitleMapping, field) == work_id)
        .values({field: None})
    )
    # Keep the file listing evidence: losing a target makes batch coverage
    # unknown, rather than silently shrinking the package to surviving works.
    await db.execute(
        sql_update(ResourceFileAssignment)
        .where(getattr(ResourceFileAssignment, field) == work_id)
        .values({field: None})
    )
    await db.execute(
        sql_delete(ResourceWorkLink).where(getattr(ResourceWorkLink, field) == work_id)
    )
    # Review against current resource associations while original decision FKs
    # still exist, so the audit captures the complete before-image.
    await rekey_agent_choices(db, choice_agent_ids)
    await db.execute(
        sql_update(PendingDecision)
        .where(getattr(PendingDecision, field) == work_id)
        .values({field: None})
    )
    await delete_external_ids_for_work(db, kind, work_id)
    await db.delete(target)
    await db.commit()
    return WorkDeletionOutcome("deleted")


async def lock_work_references(db, kind, work_id):
    """Lock existing references before a work deletion or merge."""
    from app.models.agent_work import AgentWork
    from app.models.file_resource import FileResource
    from app.models.pending_decision import PendingDecision

    field = kind + "_id"
    # Existing source-only edits need child locks: they do not recheck the
    # work FK. Every reverse-order lock is NOWAIT to release this work lock
    # rather than wait behind a resource/decision owner awaiting the work.
    resources = select(FileResource.id).where(or_(
        getattr(FileResource, field) == work_id,
        FileResource.work_links.any(getattr(ResourceWorkLink, field) == work_id),
        FileResource.file_assignments.any(getattr(ResourceFileAssignment, field) == work_id),
    ))
    await db.execute(resources.order_by(FileResource.id).with_for_update(nowait=True))
    for child in (ResourceWorkLink, ResourceFileAssignment, ChannelRawTitleMapping, AgentWork, PendingDecision):
        await db.execute(
            select(child.id)
            .where(getattr(child, field) == work_id)
            .order_by(child.id)
            .with_for_update(nowait=True)
        )
