"""Agent API routes."""

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.agent import Agent
from app.models.agent_suggestion import AgentSuggestion
from app.models.agent_work import AgentWork
from app.models.channel import Channel
from app.models.file_resource import FileResource
from app.schemas.agent import (
    AgentCreate,
    AgentResponse,
    AgentRunResponse,
    AgentUpdate,
    AgentWorkCreate,
    AgentWorkResponse,
    AgentWorkUpdate,
    RulesPreviewRequest,
    RulesPreviewResource,
    RulesPreviewResponse,
    RunStatusResponse,
    SuggestionGroup,
    TestFilterResourceResult,
    TestFilterResult,
)
from app.schemas.common import paginated_response, success_response
from app.services.agent_service import (
    RuleSet,
    _build_rule_set,
    compute_rule_diff,
    process_resources,
)
from app.services.filter_engine import (
    evaluate_filter_config,
    validate_filter_config,
)

router = APIRouter()


def _not_found(entity: str) -> dict:
    return {"success": False, "data": None,
            "error": {"code": "NOT_FOUND", "message": f"{entity} not found"},
            "meta": {}}


def _rule_set_from_request(body) -> RuleSet:
    """Build a RuleSet from proposed (not-yet-persisted) rules.

    ``AgentWorkCreate`` objects already expose ``filter_overrides``, so they
    can stand in for ``AgentWork`` rows in :func:`_resource_matches_rules`.
    """
    by_series: dict[str, object] = {}
    by_movie: dict[str, object] = {}
    for w in body.works:
        if w.content_type == "tv" and w.series_id:
            by_series[w.series_id] = w
        elif w.content_type == "movie" and w.movie_id:
            by_movie[w.movie_id] = w
    return RuleSet(
        scope_channel_wide=body.scope_channel_wide,
        filter_config=body.filter_config,
        work_by_series_id=by_series,
        work_by_movie_id=by_movie,
    )


async def _apply_backfill(
    agent: Agent, resource_ids: list[str], db: AsyncSession
) -> None:
    """Dispatch the user-selected backfill resources (scenario ② commit) and
    advance the agent's watermark past every existing channel resource so
    subsequent delta runs only see truly new resources."""
    from app.utils.time import utcnow

    if resource_ids:
        rows = (await db.execute(
            select(FileResource).where(
                FileResource.channel_id == agent.channel_id,
                FileResource.id.in_(resource_ids),
            )
        )).scalars().all()
        if rows:
            await process_resources(agent, list(rows), db)
    # Advance watermark to the channel's current max created_at (or now if the
    # channel is empty) so the next delta run doesn't re-scan old resources.
    max_created = (await db.execute(
        select(func.max(FileResource.created_at)).where(
            FileResource.channel_id == agent.channel_id
        )
    )).scalar_one()
    agent.last_consumed_at = max_created or utcnow()


# ---------------------------------------------------------------------------
# Agent CRUD
# ---------------------------------------------------------------------------

@router.get("/agents")
async def list_agents(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    from app.models.download_task import DownloadTask
    offset = (page - 1) * page_size
    total_q = await db.execute(select(func.count()).select_from(Agent))
    total = total_q.scalar_one()
    result = await db.execute(
        select(Agent)
        .options(
            selectinload(Agent.channel),
            selectinload(Agent.downloader),
            selectinload(Agent.works).selectinload(AgentWork.series),
            selectinload(Agent.works).selectinload(AgentWork.movie),
        )
        .order_by(Agent.created_at.desc())
        .offset(offset).limit(page_size)
    )
    agents = result.scalars().all()
    items = []
    for a in agents:
        d = AgentResponse.model_validate(a).model_dump()
        d["channel_name"] = a.channel.name if a.channel else None
        d["downloader_name"] = a.downloader.name if a.downloader else None
        cnt_q = await db.execute(
            select(func.count()).select_from(DownloadTask).where(
                DownloadTask.agent_id == a.id,
                DownloadTask.status.in_(["pending", "queued", "downloading"]),
            )
        )
        d["active_task_count"] = cnt_q.scalar_one() or 0
        items.append(d)
    return paginated_response(items, total=total, page=page, page_size=page_size)


@router.post("/agents", status_code=201)
async def create_agent(body: AgentCreate, db: AsyncSession = Depends(get_db)):
    # Validate channel
    ch = await db.get(Channel, body.channel_id)
    if not ch:
        return JSONResponse(status_code=422, content={
            "success": False, "data": None,
            "error": {"code": "VALIDATION_ERROR", "message": "channel_id does not exist"},
            "meta": {},
        })
    from app.models.downloader import DownloaderInstance
    dl = await db.get(DownloaderInstance, body.downloader_id)
    if not dl:
        return JSONResponse(status_code=422, content={
            "success": False, "data": None,
            "error": {"code": "VALIDATION_ERROR", "message": "downloader_id does not exist"},
            "meta": {},
        })
    # Validate filter_config
    if body.filter_config is not None:
        errs = validate_filter_config(body.filter_config)
        if errs:
            return JSONResponse(status_code=422, content={
                "success": False, "data": None,
                "error": {"code": "VALIDATION_ERROR", "message": "; ".join(errs)},
                "meta": {},
            })
    works_data = body.works or []
    if not body.scope_channel_wide and len(works_data) > 10:
        return JSONResponse(status_code=422, content={
            "success": False, "data": None,
            "error": {"code": "VALIDATION_ERROR", "message": "Maximum 10 works"},
            "meta": {},
        })
    payload = body.model_dump(exclude={"works", "dispatch_resource_ids"})
    agent = Agent(**payload)
    db.add(agent)
    await db.flush()

    for w in works_data:
        w_data = w.model_dump()
        # validate single-target
        has_series = bool(w_data.get("series_id"))
        has_movie = bool(w_data.get("movie_id"))
        if w_data.get("content_type") == "tv" and not has_series:
            continue
        if w_data.get("content_type") == "movie" and not has_movie:
            continue
        if has_series == has_movie:
            continue
        db.add(AgentWork(agent_id=agent.id, **w_data))
    await db.flush()
    # Ensure agent.works is populated for _apply_backfill (see update_agent).
    await db.refresh(agent, ["works"])

    # If the save went through the rules-preview flow (dispatch_resource_ids
    # is not None), dispatch the user-selected backfill resources and advance
    # the watermark so future delta runs only see truly new resources.
    if body.dispatch_resource_ids is not None:
        await _apply_backfill(agent, body.dispatch_resource_ids, db)

    await db.commit()
    # Refetch with relationships eager-loaded
    cur = await db.execute(
        select(Agent).where(Agent.id == agent.id).options(
            selectinload(Agent.channel), selectinload(Agent.downloader),
            selectinload(Agent.works).selectinload(AgentWork.series),
            selectinload(Agent.works).selectinload(AgentWork.movie),
        )
    )
    agent = cur.scalar_one()
    return success_response(AgentResponse.model_validate(agent).model_dump())


@router.get("/agents/{agent_id}")
async def get_agent(agent_id: str, db: AsyncSession = Depends(get_db)):
    agent = (await db.execute(
        select(Agent).where(Agent.id == agent_id).options(
            selectinload(Agent.channel), selectinload(Agent.downloader),
            selectinload(Agent.works).selectinload(AgentWork.series),
            selectinload(Agent.works).selectinload(AgentWork.movie),
        )
    )).scalar_one_or_none()
    if not agent:
        return JSONResponse(status_code=404, content=_not_found("Agent"))
    return success_response(AgentResponse.model_validate(agent).model_dump())


@router.put("/agents/{agent_id}")
async def update_agent(agent_id: str, body: AgentUpdate, db: AsyncSession = Depends(get_db)):
    agent = (await db.execute(
        select(Agent).where(Agent.id == agent_id).options(
            selectinload(Agent.works),
        )
    )).scalar_one_or_none()
    if not agent:
        return JSONResponse(status_code=404, content=_not_found("Agent"))
    data = body.model_dump(exclude_unset=True)
    new_works = data.pop("works", None)
    dispatch_resource_ids = data.pop("dispatch_resource_ids", None)
    if data.get("status") == "active" and data.get("downloader_id") is None and not agent.downloader_id:
        return JSONResponse(status_code=422, content={
            "success": False, "data": None,
            "error": {"code": "VALIDATION_ERROR", "message": "downloader_id is required for active agents"},
            "meta": {},
        })
    if data.get("downloader_id") is not None:
        from app.models.downloader import DownloaderInstance
        dl = await db.get(DownloaderInstance, data["downloader_id"])
        if not dl:
            return JSONResponse(status_code=422, content={
                "success": False, "data": None,
                "error": {"code": "VALIDATION_ERROR", "message": "downloader_id does not exist"},
                "meta": {},
            })
    for key, value in data.items():
        setattr(agent, key, value)
    if new_works is not None:
        # Replace works
        for w in list(agent.works):
            await db.delete(w)
        await db.flush()
        agent.works = []
        for w in new_works:
            w_data = w if isinstance(w, dict) else w.model_dump()
            has_series = bool(w_data.get("series_id"))
            has_movie = bool(w_data.get("movie_id"))
            if has_series == has_movie:
                continue
            db.add(AgentWork(agent_id=agent.id, **w_data))
        await db.flush()
        # ``agent.works`` was set to [] above and the new rows added via
        # db.add don't flow back into the in-memory collection. Refresh the
        # relationship so _apply_backfill → process_resources → _build_rule_set
        # sees the new works; otherwise every backfill resource fails the
        # work-scope check and nothing dispatches.
        await db.refresh(agent, ["works"])

    # If the save went through the rules-preview flow, dispatch the user-
    # selected backfill resources and advance the watermark.
    if dispatch_resource_ids is not None:
        await _apply_backfill(agent, dispatch_resource_ids, db)

    await db.commit()
    cur = await db.execute(
        select(Agent).where(Agent.id == agent_id).options(
            selectinload(Agent.channel), selectinload(Agent.downloader),
            selectinload(Agent.works).selectinload(AgentWork.series),
            selectinload(Agent.works).selectinload(AgentWork.movie),
        )
    )
    agent = cur.scalar_one()
    return success_response(AgentResponse.model_validate(agent).model_dump())


@router.delete("/agents/{agent_id}")
async def delete_agent(agent_id: str, db: AsyncSession = Depends(get_db)):
    agent = await db.get(Agent, agent_id)
    if not agent:
        return JSONResponse(status_code=404, content=_not_found("Agent"))
    # Cancel tasks
    from sqlalchemy import update as sql_update

    from app.models.download_task import DownloadTask
    await db.execute(
        sql_update(DownloadTask)
        .where(DownloadTask.agent_id == agent_id)
        .values(status="cancelled")
    )
    await db.delete(agent)
    return success_response({"deleted": True})


# ---------------------------------------------------------------------------
# Agent run / status
# ---------------------------------------------------------------------------

@router.post("/agents/{agent_id}/run")
async def run_agent(agent_id: str, db: AsyncSession = Depends(get_db)):
    agent = await db.get(Agent, agent_id)
    if not agent:
        return JSONResponse(status_code=404, content=_not_found("Agent"))
    from app.services.task_queue import task_queue
    job = await task_queue.enqueue("run_agent", f"agent:{agent_id}", {"agent_id": agent_id})
    if job is None:
        current = await task_queue.status(f"agent:{agent_id}")
        return JSONResponse(status_code=409, content={
            "success": False, "data": current,
            "error": {"code": "ALREADY_RUNNING", "message": "Agent is already running"},
            "meta": {},
        })
    return success_response(job)


@router.get("/agents/{agent_id}/run-status")
async def get_run_status(agent_id: str, db: AsyncSession = Depends(get_db)):
    agent = await db.get(Agent, agent_id)
    if not agent:
        return JSONResponse(status_code=404, content=_not_found("Agent"))
    from app.services.task_queue import task_queue
    st = await task_queue.status(f"agent:{agent_id}")
    return success_response(RunStatusResponse(
        job_id=st.get("job_id") if st else None,
        status=st.get("status") if st else None,
        result=st.get("result") if st else None,
        error=st.get("error") if st else None,
        queued_at=st.get("queued_at") if st else None,
        started_at=st.get("started_at") if st else None,
        finished_at=st.get("finished_at") if st else None,
    ).model_dump())


@router.get("/agents/{agent_id}/runs")
async def list_agent_runs(
    agent_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """Run history: one row per agent execution with counts, status, and the
    list of resources that matched (passed work-scope + filter) that run."""
    from app.models.agent_run import AgentRun

    agent = await db.get(Agent, agent_id)
    if not agent:
        return JSONResponse(status_code=404, content=_not_found("Agent"))
    base_q = select(AgentRun).where(AgentRun.agent_id == agent_id)
    total_q = await db.execute(select(func.count()).select_from(base_q.subquery()))
    total = total_q.scalar_one()
    rows = (await db.execute(
        base_q.order_by(AgentRun.started_at.desc())
        .offset((page - 1) * page_size).limit(page_size)
    )).scalars().all()

    # Eager-load the matched resource summaries (dedup ids across the page).
    res_ids: set[str] = set()
    for r in rows:
        res_ids.update(r.matched_resource_ids or [])
    res_by_id: dict[str, FileResource] = {}
    if res_ids:
        res_rows = (await db.execute(
            select(FileResource).where(FileResource.id.in_(res_ids))
        )).scalars().all()
        res_by_id = {r.id: r for r in res_rows}

    items = []
    for r in rows:
        data = AgentRunResponse.model_validate(r).model_dump()
        data["matched_resources"] = [
            RulesPreviewResource.model_validate(res_by_id[rid]).model_dump()
            for rid in (r.matched_resource_ids or [])
            if rid in res_by_id
        ]
        items.append(data)
    return paginated_response(items, total=total, page=page, page_size=page_size)


# ---------------------------------------------------------------------------
# Test filters
# ---------------------------------------------------------------------------

def _condition_results_for_resource(resource, filter_cfg):
    """Return a list of per-condition results for debugging.

    Walks the filter tree, recording each FieldCondition's outcome.
    """
    results: list[dict] = []
    if not filter_cfg:
        return results
    _walk_conditions(filter_cfg, resource, [], results)
    return results


def _walk_conditions(node, resource, path, results):
    if not isinstance(node, dict):
        return
    if "combinator" in node and "conditions" in node:
        for i, c in enumerate(node.get("conditions", [])):
            _walk_conditions(c, resource, path + [i], results)
        return
    if "field" in node and "operator" in node:
        from app.services.filter_engine import evaluate_field_condition
        passed = evaluate_field_condition(node, resource)
        results.append({
            "path": ".".join(str(p) for p in path) if path else "0",
            "field": node.get("field"),
            "operator": node.get("operator"),
            "value": node.get("value"),
            "actual": getattr(resource, node.get("field", ""), None),
            "passed": passed,
        })


@router.post("/agents/{agent_id}/test-filters")
async def test_filters(
    agent_id: str,
    body: dict | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Test agent's filter_config against its channel's FileResources."""
    agent = await db.get(Agent, agent_id)
    if not agent:
        return JSONResponse(status_code=404, content=_not_found("Agent"))

    resource_ids = (body or {}).get("resource_ids") if body else None

    base_q = select(FileResource).where(FileResource.channel_id == agent.channel_id)
    if resource_ids:
        base_q = base_q.where(FileResource.id.in_(resource_ids))
    else:
        base_q = base_q.order_by(FileResource.published_at.desc()).limit(50)
    result = await db.execute(base_q)
    resources = result.scalars().all()

    items: list[TestFilterResourceResult] = []
    passed_count = 0
    for res in resources:
        # Build a "global" effective filter (we don't have per-work here, use agent filter)
        eff = agent.filter_config
        ok = evaluate_filter_config(eff, res) if eff else True
        conds = _condition_results_for_resource(res, eff)
        items.append(TestFilterResourceResult(
            resource_id=res.id, title_raw=res.title_raw, passed=ok, condition_results=conds,
        ))
        if ok:
            passed_count += 1

    return success_response(TestFilterResult(
        resources=items, total=len(items), passed=passed_count,
    ).model_dump())


@router.post("/agents/rules-preview")
async def rules_preview(body: RulesPreviewRequest, db: AsyncSession = Depends(get_db)):
    """Preview the match diff before committing a subscription rule change.

    Scenario ②: when an agent's rules (scope/filter/works) change, compute
    which channel resources are newly-matching (backfill candidates, excluding
    those already tasked) and which are no-longer-matching (informational;
    in-queue tasks are never revoked). The frontend shows the newly-matching
    list for the user to select; the selection is then sent via
    ``dispatch_resource_ids`` on the create/update call.
    """
    if body.filter_config is not None:
        errs = validate_filter_config(body.filter_config)
        if errs:
            return JSONResponse(status_code=422, content={
                "success": False, "data": None,
                "error": {"code": "VALIDATION_ERROR", "message": "; ".join(errs)},
                "meta": {},
            })

    channel_id: str | None = body.channel_id
    old = RuleSet(scope_channel_wide=False, filter_config=None)
    if body.agent_id:
        agent = (await db.execute(
            select(Agent).where(Agent.id == body.agent_id).options(
                selectinload(Agent.works),
            )
        )).scalar_one_or_none()
        if not agent:
            return JSONResponse(status_code=404, content=_not_found("Agent"))
        old = _build_rule_set(agent)
        channel_id = agent.channel_id
    if not channel_id:
        return JSONResponse(status_code=422, content={
            "success": False, "data": None,
            "error": {"code": "VALIDATION_ERROR",
                      "message": "channel_id is required when agent_id is absent"},
            "meta": {},
        })

    new = _rule_set_from_request(body)
    resources = (await db.execute(
        select(FileResource).where(FileResource.channel_id == channel_id)
    )).scalars().all()
    diff = await compute_rule_diff(old, new, list(resources), db)
    return success_response(RulesPreviewResponse(
        newly_matching=[
            RulesPreviewResource.model_validate(r) for r in diff["newly_matching"]
        ],
        no_longer_matching=[
            RulesPreviewResource.model_validate(r) for r in diff["no_longer_matching"]
        ],
        in_queue_skipped=diff["in_queue_skipped"],
    ).model_dump())


# ---------------------------------------------------------------------------
# AgentWork CRUD
# ---------------------------------------------------------------------------

async def _get_work(agent_id: str, work_id: str, db: AsyncSession) -> AgentWork | None:
    res = await db.execute(
        select(AgentWork).where(AgentWork.id == work_id, AgentWork.agent_id == agent_id)
    )
    return res.scalar_one_or_none()


@router.get("/agents/{agent_id}/works")
async def list_works(agent_id: str, db: AsyncSession = Depends(get_db)):
    agent = await db.get(Agent, agent_id)
    if not agent:
        return JSONResponse(status_code=404, content=_not_found("Agent"))
    # Ensure works are loaded with series/movie
    res = await db.execute(
        select(AgentWork)
        .where(AgentWork.agent_id == agent_id)
        .options(selectinload(AgentWork.series), selectinload(AgentWork.movie))
    )
    works = res.scalars().all()
    return success_response([
        AgentWorkResponse.model_validate(w).model_dump() for w in works
    ])


@router.post("/agents/{agent_id}/works", status_code=201)
async def create_work(agent_id: str, body: AgentWorkCreate, db: AsyncSession = Depends(get_db)):
    agent = await db.get(Agent, agent_id)
    if not agent:
        return JSONResponse(status_code=404, content=_not_found("Agent"))
    if not agent.scope_channel_wide and len(agent.works) >= 10:
        return JSONResponse(status_code=400, content={
            "success": False, "data": None,
            "error": {"code": "VALIDATION_ERROR", "message": "Maximum 10 works"},
            "meta": {},
        })
    has_s = bool(body.series_id)
    has_m = bool(body.movie_id)
    if body.content_type == "tv" and not has_s:
        return JSONResponse(status_code=422, content={
            "success": False, "data": None,
            "error": {"code": "VALIDATION_ERROR", "message": "series_id is required for tv works"},
            "meta": {},
        })
    if body.content_type == "movie" and not has_m:
        return JSONResponse(status_code=422, content={
            "success": False, "data": None,
            "error": {"code": "VALIDATION_ERROR", "message": "movie_id is required for movie works"},
            "meta": {},
        })
    if has_s == has_m:
        return JSONResponse(status_code=422, content={
            "success": False, "data": None,
            "error": {"code": "VALIDATION_ERROR", "message": "Exactly one of series_id/movie_id must be set"},
            "meta": {},
        })
    work = AgentWork(agent_id=agent_id, **body.model_dump())
    db.add(work)
    await db.flush()
    await db.commit()
    cur = await db.execute(
        select(AgentWork).where(AgentWork.id == work.id).options(
            selectinload(AgentWork.series), selectinload(AgentWork.movie),
        )
    )
    work = cur.scalar_one()
    return success_response(AgentWorkResponse.model_validate(work).model_dump())


@router.put("/agents/{agent_id}/works/{work_id}")
async def update_work(
    agent_id: str, work_id: str, body: AgentWorkUpdate, db: AsyncSession = Depends(get_db)
):
    work = await _get_work(agent_id, work_id, db)
    if work is None:
        return JSONResponse(status_code=404, content=_not_found("AgentWork"))
    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(work, key, value)
    await db.flush()
    await db.commit()
    cur = await db.execute(
        select(AgentWork).where(AgentWork.id == work_id).options(
            selectinload(AgentWork.series), selectinload(AgentWork.movie),
        )
    )
    work = cur.scalar_one()
    return success_response(AgentWorkResponse.model_validate(work).model_dump())


@router.delete("/agents/{agent_id}/works/{work_id}")
async def delete_work(agent_id: str, work_id: str, db: AsyncSession = Depends(get_db)):
    work = await _get_work(agent_id, work_id, db)
    if work is None:
        return JSONResponse(status_code=404, content=_not_found("AgentWork"))
    await db.delete(work)
    return success_response({"deleted": True})


# ---------------------------------------------------------------------------
# Suggestions — resources that aren't subscribed / matched to any work
# ---------------------------------------------------------------------------

@router.get("/agents/{agent_id}/suggestions")
async def get_suggestions(
    agent_id: str,
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    agent = await db.get(Agent, agent_id)
    if not agent:
        return JSONResponse(status_code=404, content=_not_found("Agent"))

    result = await db.execute(
        select(AgentSuggestion)
        .where(AgentSuggestion.agent_id == agent_id, AgentSuggestion.status == "active")
        .order_by(AgentSuggestion.updated_at.desc())
        .limit(limit)
    )
    suggestions = result.scalars().all()
    return success_response({
        "scope_channel_wide": agent.scope_channel_wide,
        "suggestions": [
            SuggestionGroup(
                id=s.id,
                sample_title=s.sample_title,
                resources=s.resources,
                status=s.status,
                created_at=s.created_at,
                updated_at=s.updated_at,
            ).model_dump()
            for s in suggestions
        ],
    })
