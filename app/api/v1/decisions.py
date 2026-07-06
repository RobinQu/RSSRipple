"""PendingDecision API routes."""


from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.file_resource import FileResource
from app.models.pending_decision import PendingDecision
from app.schemas.common import paginated_response, success_response
from app.schemas.pending_decision import (
    BatchDecisionRequest,
    BatchDecisionResponse,
    ConfirmDecisionRequest,
    DecisionActionResponse,
    PendingDecisionResponse,
)
from app.utils.time import utcnow

router = APIRouter()


async def _ai_pick_and_dispatch(
    decision: PendingDecision, db: AsyncSession
) -> tuple[bool, str | None]:
    """Resolve a pending decision by letting the LLM pick a candidate.

    Reuses the cached ``llm_picked_resource_id`` when present; otherwise asks
    the LLM now. Returns ``(ok, error)``.
    """
    from app.models.agent import Agent
    from app.services.agent_service import _generate_llm_pick, dispatch_download

    agent = await db.get(Agent, decision.agent_id)
    if not agent:
        return False, f"Agent {decision.agent_id} not found"

    picked_id = decision.llm_picked_resource_id
    if not picked_id or picked_id not in (decision.candidates or []):
        cands = (await db.execute(
            select(FileResource).where(
                FileResource.id.in_(decision.candidates or [])
            )
        )).scalars().all()
        if not cands:
            return False, "No candidates to pick from"
        picked_id, _reason = await _generate_llm_pick(
            agent, list(cands), ("series", decision.series_id, decision.episode)
        )
        decision.llm_picked_resource_id = picked_id
        if not picked_id:
            return False, "AI 未能给出选择，请手动确认"

    resource = await db.get(FileResource, picked_id)
    if not resource:
        return False, f"Picked resource {picked_id} not found"

    await dispatch_download(agent, resource, db)
    decision.status = "decided"
    decision.decided_resource_id = picked_id
    decision.decided_at = utcnow()
    return True, None


@router.get("/agents/{agent_id}/decisions")
async def list_decisions(
    agent_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    offset = (page - 1) * page_size
    base_q = select(PendingDecision).where(PendingDecision.agent_id == agent_id)
    if status:
        base_q = base_q.where(PendingDecision.status == status)
    total_q = await db.execute(select(func.count()).select_from(base_q.subquery()))
    total = total_q.scalar_one()
    result = await db.execute(
        base_q.options(
            selectinload(PendingDecision.series),
            selectinload(PendingDecision.movie),
        ).order_by(PendingDecision.created_at.desc()).offset(offset).limit(page_size)
    )
    decisions = result.scalars().all()
    out = []
    for d in decisions:
        data = PendingDecisionResponse.model_validate(d).model_dump()
        # Load candidate resources
        cands = (await db.execute(
            select(FileResource).where(FileResource.id.in_(d.candidates or []))
        )).scalars().all()
        from app.schemas.file_resource import FileResourceResponse
        data["candidate_resources"] = [
            FileResourceResponse.model_validate(c).model_dump() for c in cands
        ]
        out.append(data)
    return paginated_response(out, total=total, page=page, page_size=page_size)


@router.post("/decisions/{decision_id}/confirm")
async def confirm_decision(
    decision_id: str,
    body: ConfirmDecisionRequest,
    db: AsyncSession = Depends(get_db),
):
    from app.models.agent import Agent
    from app.services.agent_service import dispatch_download
    decision = await db.get(PendingDecision, decision_id)
    if not decision:
        return JSONResponse(
            status_code=404,
            content={
                "success": False,
                "data": None,
                "error": {"code": "NOT_FOUND", "message": "Decision not found"},
                "meta": {},
            },
        )
    if body.resource_id not in decision.candidates:
        return JSONResponse(
            status_code=400,
            content={
                "success": False,
                "data": None,
                "error": {"code": "VALIDATION_ERROR", "message": "Resource not in candidates"},
                "meta": {},
            },
        )
    decision.status = "decided"
    decision.decided_resource_id = body.resource_id
    decision.decided_at = utcnow()
    await db.flush()

    # Dispatch the chosen resource
    agent = await db.get(Agent, decision.agent_id)
    resource = await db.get(FileResource, body.resource_id)
    if agent and resource:
        await dispatch_download(agent, resource, db)

    await db.commit()
    await db.refresh(decision)
    return success_response(PendingDecisionResponse.model_validate(decision).model_dump())


@router.post("/decisions/{decision_id}/skip")
async def skip_decision(decision_id: str, db: AsyncSession = Depends(get_db)):
    decision = await db.get(PendingDecision, decision_id)
    if not decision:
        return JSONResponse(
            status_code=404,
            content={
                "success": False,
                "data": None,
                "error": {"code": "NOT_FOUND", "message": "Decision not found"},
                "meta": {},
            },
        )
    decision.status = "skipped"
    decision.decided_at = utcnow()
    await db.flush()
    await db.commit()
    await db.refresh(decision)
    return success_response(PendingDecisionResponse.model_validate(decision).model_dump())


@router.post("/decisions/{decision_id}/ai-pick")
async def ai_pick_decision(decision_id: str, db: AsyncSession = Depends(get_db)):
    """Let the LLM pick the best candidate and dispatch it (AI auto-handle)."""
    decision = await db.get(PendingDecision, decision_id)
    if not decision:
        return JSONResponse(
            status_code=404,
            content={
                "success": False, "data": None,
                "error": {"code": "NOT_FOUND", "message": "Decision not found"},
                "meta": {},
            },
        )
    if decision.status != "pending":
        return JSONResponse(
            status_code=400,
            content={
                "success": False, "data": None,
                "error": {"code": "NOT_PENDING", "message": f"Decision is {decision.status}"},
                "meta": {},
            },
        )
    ok, err = await _ai_pick_and_dispatch(decision, db)
    if not ok:
        await db.rollback()
        return JSONResponse(
            status_code=400,
            content={
                "success": False, "data": None,
                "error": {"code": "LLM_NO_PICK", "message": err or "AI 未能决策"},
                "meta": {},
            },
        )
    await db.commit()
    await db.refresh(decision)
    return success_response(DecisionActionResponse(
        id=decision.id, status=decision.status,
        decided_resource_id=decision.decided_resource_id,
        decided_at=decision.decided_at,
    ).model_dump())


@router.post("/agents/{agent_id}/decisions/batch")
async def batch_decisions(
    agent_id: str,
    body: BatchDecisionRequest,
    db: AsyncSession = Depends(get_db),
):
    """Apply skip or AI auto-handle to many decisions at once."""
    if body.action not in ("skip", "ai"):
        return JSONResponse(
            status_code=422,
            content={
                "success": False, "data": None,
                "error": {"code": "VALIDATION_ERROR", "message": "action must be 'skip' or 'ai'"},
                "meta": {},
            },
        )
    rows = (await db.execute(
        select(PendingDecision).where(
            PendingDecision.agent_id == agent_id,
            PendingDecision.id.in_(body.decision_ids),
            PendingDecision.status == "pending",
        )
    )).scalars().all()

    resp = BatchDecisionResponse()
    for dec in rows:
        resp.processed += 1
        try:
            if body.action == "skip":
                dec.status = "skipped"
                dec.decided_at = utcnow()
                resp.skipped += 1
            else:
                ok, err = await _ai_pick_and_dispatch(dec, db)
                if ok:
                    resp.dispatched += 1
                else:
                    resp.failed += 1
                    resp.errors.append(f"{dec.id}: {err}")
        except Exception as e:  # noqa: BLE001
            resp.failed += 1
            resp.errors.append(f"{dec.id}: {e}")
    await db.commit()
    return success_response(resp.model_dump())
