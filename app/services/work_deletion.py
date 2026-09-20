"""Reference checks shared by explicit series/movie deletion endpoints."""
from sqlalchemy import func, or_, select
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
        return target, agent_ids
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) != "55P03" and "concurrent decision identity change" not in str(exc):
            raise
        raise WorkDeletionBusyError from exc
