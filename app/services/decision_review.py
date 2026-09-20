"""Read-only review of legacy decisions before adding strict pending keys.

Decisions are mappings so old schemas need not already contain the new columns.
No pending row is silently discarded or classified using its old nullable slot.
"""
import hashlib
import json

from app.services.decision_store import choice_identity
from app.services.metadata_episode_reconcile import is_unsplit_legacy_series
from app.services.resource_coverage import _loaded, batch_coverage


def resource_choice(resource):
    """Canonical current identity; unknown or contradictory evidence is held."""
    if resource.series_id and resource.movie_id:
        return None
    if resource.is_batch:
        coverage = batch_coverage(resource)
        if coverage is None:
            return None
        kind = 'movie' if resource.movie_id else resource.batch_scope
        return choice_identity('series', None, None, -1, (kind, coverage))
    if resource.movie_id:
        return choice_identity('movie', resource.movie_id, None, None)
    if resource.series_id:
        work = _loaded(resource, 'series')
        if (work is None or work.id != resource.series_id or is_unsplit_legacy_series(work)
                or type(work.season_number) is not int or work.season_number < 0
                or type(resource.episode) is not int or resource.episode < 0
                or resource.episode_confidence == 'ambiguous'
                or resource.season != work.season_number):
            return None
        return choice_identity('series', work.id, work.season_number, resource.episode)
    return None


def review_decisions(decisions, resources):
    """Return a deterministic proposal, never authorization to mutate data.

    Mixed historical slots split by actual candidates; identical scopes merge
    across rows of the same agent. Singleton groups and missing/unknown evidence
    require review. Every original row (including user decisions) is retained.
    """
    originals = sorted(decisions, key=lambda row: row['id'])
    groups, blocked = {}, []
    for row in originals:
        if row['status'] != 'pending':
            continue
        ids = row.get('candidates')
        if not isinstance(ids, list) or any(not isinstance(rid, str) for rid in ids):
            blocked.append(dict(decision_id=row['id'], reason='invalid_candidates'))
            continue
        if not ids:
            blocked.append(dict(decision_id=row['id'], reason='empty_candidates'))
        for rid in sorted(set(ids)):
            resource = resources.get(rid)
            identity = resource_choice(resource) if resource is not None else None
            if identity is None:
                blocked.append(dict(decision_id=row['id'], resource_id=rid,
                                    reason='missing_resource' if resource is None else 'unknown_coverage'))
                continue
            key, scope = identity
            group = groups.setdefault((row['agent_id'], key), dict(
                agent_id=row['agent_id'], decision_key=key, decision_scope=scope,
                candidates=set(), source_decision_ids=set()))
            if group['decision_scope'] != scope:
                raise ValueError('Decision digest collision or inconsistent canonical scope')
            group['candidates'].add(rid)
            group['source_decision_ids'].add(row['id'])
    proposals = []
    for _, group in sorted(groups.items()):
        group['candidates'] = sorted(group['candidates'])
        group['source_decision_ids'] = sorted(group['source_decision_ids'])
        group['requires_review'] = len(group['candidates']) < 2
        proposals.append(group)
    report = dict(version=1, original_decisions=originals, proposed_groups=proposals, blocked=blocked)
    # Fingerprint includes original decision state and derived resource identity.
    # The apply path must re-export under locks and compare before modifying.
    encoded = json.dumps(report, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    report['fingerprint'] = hashlib.sha256(encoded.encode()).hexdigest()
    return report


async def export_decision_review(db):
    """Read the pre-key schema without querying new ORM decision columns.

    Caller supplies a read transaction. No autoflush or writes are performed;
    apply must independently repeat this export in its protected transaction.
    """
    from datetime import datetime

    from sqlalchemy import select, text
    from sqlalchemy.orm import selectinload

    from app.models.file_resource import FileResource
    from app.services.resource_coverage import load_batch_coverage

    with db.no_autoflush:
        rows = [dict(row) for row in (await db.execute(text('SELECT * FROM pending_decisions ORDER BY id'))).mappings()]
        for row in rows:
            for key, value in row.items():
                if isinstance(value, datetime):
                    row[key] = value.isoformat()
            if isinstance(row.get('candidates'), str):
                try:
                    row['candidates'] = json.loads(row['candidates'])
                except ValueError:
                    pass  # Preserve malformed input in the blocked report.
        ids = sorted({rid for row in rows if row['status'] == 'pending'
                      and isinstance(row.get('candidates'), list)
                      for rid in row['candidates'] if isinstance(rid, str)})
        resources = {}
        for offset in range(0, len(ids), 100):
            batch = list(await db.scalars(select(FileResource).where(
                FileResource.id.in_(ids[offset:offset + 100])
            ).options(selectinload(FileResource.series)).execution_options(populate_existing=True)))
            await load_batch_coverage(db, batch)
            resources.update((resource.id, resource) for resource in batch)
        return review_decisions(rows, resources)


async def _lock_choice_work_metadata(db, resource_ids):
    """Caller has locked resource and association rows; references are stable."""
    from sqlalchemy import select

    from app.models.audio_work import AudioWork
    from app.models.file_resource import FileResource
    from app.models.movie import Movie
    from app.models.resource_file_assignment import ResourceFileAssignment
    from app.models.resource_work_link import ResourceWorkLink
    from app.models.series import TVSeries
    from app.models.work_collection import WorkCollection

    targets = {"series_id": set(), "movie_id": set(), "audio_work_id": set(), "collection_id": set()}
    for model in (FileResource, ResourceWorkLink, ResourceFileAssignment):
        names = [name for name in targets if hasattr(model, name)]
        condition = model.id.in_(resource_ids) if model is FileResource else model.resource_id.in_(resource_ids)
        rows = await db.execute(select(*(getattr(model, name) for name in names)).where(condition))
        for row in rows:
            for name, value in zip(names, row, strict=True):
                if value is not None:
                    targets[name].add(value)
    for model, name in ((TVSeries, "series_id"), (Movie, "movie_id"), (AudioWork, "audio_work_id")):
        columns = [model.id]
        if hasattr(model, "collection_id"):
            columns.append(model.collection_id)
        rows = await db.execute(select(*columns).where(model.id.in_(targets[name]))
                                .order_by(model.id).with_for_update(read=True))
        if len(columns) == 2:
            targets["collection_id"].update(row[1] for row in rows if row[1] is not None)
    await db.execute(select(WorkCollection.id).where(WorkCollection.id.in_(targets["collection_id"]))
                     .order_by(WorkCollection.id).with_for_update(read=True))


async def current_choice_error(decision, db, *, lock_resources=False):
    """Reject stale decisions before using any cached recommendation.

    This validates current evidence, but is not a replacement for the caller's
    transaction locks and download idempotency boundary.
    """
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.models.agent import Agent
    from app.models.file_resource import FileResource
    from app.models.movie import Movie
    from app.models.series import TVSeries
    from app.services.agent_service import _build_rule_set, _resource_matches_rules
    from app.services.resource_confirmation import inspect_resource_confirmation
    from app.services.resource_coverage import load_batch_coverage
    from app.utils.time import utcnow

    if decision.status != 'pending':
        return 'Decision is no longer pending'
    if decision.expires_at is not None and decision.expires_at <= utcnow():
        return 'Decision has expired'
    ids = decision.candidates or []
    if not isinstance(ids, list) or any(not isinstance(rid, str) for rid in ids) or len(set(ids)) < 2:
        return 'Decision requires at least two distinct candidates'
    if not decision.decision_key or not decision.decision_scope:
        return 'Decision requires identity review'
    if lock_resources:
        from app.models.agent_work import AgentWork
        from app.models.channel import Channel

        # Agent is already locked by the final-confirmation caller. API work
        # writers use that same parent lock before changing membership.
        channel_id = await db.scalar(select(Agent.channel_id).where(Agent.id == decision.agent_id))
        await db.execute(select(Channel.id).where(Channel.id == channel_id).with_for_update(read=True))
        await db.execute(select(AgentWork.id).where(AgentWork.agent_id == decision.agent_id)
                         .order_by(AgentWork.id).with_for_update(read=True))
        # Caller holds Agent then decision locks. Lock candidates in a stable
        # order before reloading evidence, and keep locks through dispatch.
        await db.execute(select(FileResource.id).where(
            FileResource.id.in_(ids)
        ).order_by(FileResource.id).with_for_update())
        from app.models.resource_file_assignment import ResourceFileAssignment
        from app.models.resource_work_link import ResourceWorkLink

        # Existing child UPDATE/DELETE does not acquire the parent's FK lock.
        # The parent FOR UPDATE protects inserts; these locks protect rows
        # already present before the evidence reload.
        for model in (ResourceWorkLink, ResourceFileAssignment):
            await db.execute(select(model.id).where(model.resource_id.in_(ids))
                             .order_by(model.id).with_for_update())
        await _lock_choice_work_metadata(db, ids)
    agent = await db.scalar(select(Agent).where(Agent.id == decision.agent_id).options(
        selectinload(Agent.works), selectinload(Agent.channel)
    ).execution_options(populate_existing=True))
    if agent is None or agent.channel is None:
        return 'Decision Agent or Channel no longer exists'
    rules = _build_rule_set(agent)
    resources = list(await db.scalars(select(FileResource).where(FileResource.id.in_(ids)).options(
        selectinload(FileResource.series).selectinload(TVSeries.collection),
        selectinload(FileResource.movie).selectinload(Movie.collection),
        selectinload(FileResource.audio_work), selectinload(FileResource.collection),
    ).execution_options(populate_existing=True)))
    if len(resources) != len(set(ids)):
        return 'Decision candidates no longer exist'
    await load_batch_coverage(db, resources)
    for resource in resources:
        if resource.channel_id != agent.channel_id or not _resource_matches_rules(resource, rules)[0]:
            return 'Decision candidate no longer matches current Agent rules'
        if inspect_resource_confirmation(resource, agent.channel.required_metadata_fields).required:
            return 'Decision candidate requires Channel metadata confirmation'
        identity = resource_choice(resource)
        if identity is None or identity != (decision.decision_key, decision.decision_scope):
            return 'Decision candidate coverage changed; regenerate the decision'
    return None
