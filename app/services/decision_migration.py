"""Offline, explicitly reviewed migration; caller stops writers and owns DDL TX."""

from datetime import timedelta

from sqlalchemy import inspect, select, text, update

from app.models.decision_migration import DecisionMigration
from app.models.pending_decision import PendingDecision
from app.services.decision_review import export_decision_review
from app.services.decision_schema import install_decision_constraints
from app.utils.time import utcnow


async def apply_decision_review(db, reviewed):
    """Archive originals and supersede pending rows only under exact approval.

    No download or enqueue occurs. Unknown/singleton groups must be resolved
    before review. Original nonpending rows are never modified. Superseded
    pending rows remain addressable as expired, with their original image in
    the transactional archive. Caller commits or rolls everything back.
    """
    fingerprint = reviewed.get("fingerprint")
    if not fingerprint or reviewed.get("approved_fingerprint") != fingerprint:
        raise ValueError("Explicit approved_fingerprint is required")
    if reviewed.get("supersede_pending") is not True:
        raise ValueError("Explicit supersede_pending approval is required")
    conn = await db.connection()
    if conn.dialect.name == "postgresql":
        # Offline migration only. Block writers of all coverage evidence while
        # comparing the approved snapshot and replacing the pending slots.
        await conn.execute(
            text(
                "LOCK TABLE agents, pending_decisions, file_resources, tv_series, movies, "
                "resource_work_links, resource_file_assignments IN ACCESS EXCLUSIVE MODE"
            )
        )
    archive_exists = await conn.run_sync(lambda sync: inspect(sync).has_table("decision_migrations"))
    if archive_exists:
        archive = await db.scalar(select(DecisionMigration).where(DecisionMigration.review_fingerprint == fingerprint))
        if archive is not None:
            return {**archive.result, "already_applied": True}
    current = await export_decision_review(db)
    if current["fingerprint"] != fingerprint:
        raise ValueError("Review is stale; export and approve current evidence")
    # Use the fresh computed groups, never user-edited proposal contents.
    if current["blocked"] or any(group["requires_review"] for group in current["proposed_groups"]):
        raise ValueError("Unresolved candidate coverage or singleton groups require review")
    columns = await conn.run_sync(lambda sync: {c["name"] for c in inspect(sync).get_columns("pending_decisions")})
    for name, ddl in [("decision_key", "VARCHAR(80)"), ("decision_scope", "JSON")]:
        if name not in columns:
            await conn.execute(text(f"ALTER TABLE pending_decisions ADD COLUMN {name} {ddl}"))
    await conn.run_sync(lambda sync: DecisionMigration.__table__.create(sync, checkfirst=True))
    old_ids = [row["id"] for row in current["original_decisions"] if row["status"] == "pending"]
    # Keep source candidates, reason and any former recommendation intact.
    for offset in range(0, len(old_ids), 100):
        await db.execute(
            update(PendingDecision)
            .where(PendingDecision.id.in_(old_ids[offset : offset + 100]))
            .values(status="expired")
            .execution_options(synchronize_session=False)
        )
    created = []
    for group in current["proposed_groups"]:
        scope = group["decision_scope"]
        fields = dict(series_id=None, movie_id=None, season=None, episode=None)
        if scope["kind"] == "batch":
            fields["episode"] = -1
            descriptors = scope["coverage"][1]
            if len(descriptors) == 1:
                kind, work_id, season, _intervals = descriptors[0]
                fields["series_id" if kind == "series" else "movie_id"] = work_id
                fields["season"] = season
        else:
            fields["series_id" if scope["kind"] == "series" else "movie_id"] = scope["work_id"]
            fields["season"], fields["episode"] = scope["season"], scope["episode"]
        row = PendingDecision(
            agent_id=group["agent_id"],
            decision_key=group["decision_key"],
            decision_scope=scope,
            candidates=group["candidates"],
            status="pending",
            reason="经审核迁移的同覆盖候选，请重新选择",
            expires_at=utcnow() + timedelta(days=7),
            **fields,
        )
        db.add(row)
        await db.flush()
        created.append(dict(id=row.id, source_decision_ids=group["source_decision_ids"]))
    await install_decision_constraints(conn)
    result = dict(superseded_ids=old_ids, created=created, already_applied=False)
    db.add(DecisionMigration(review_fingerprint=fingerprint, original_review=current, result=result))
    await db.flush()
    return result
