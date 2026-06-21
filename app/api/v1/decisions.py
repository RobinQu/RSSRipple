"""PendingDecision API routes."""

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.pending_decision import PendingDecision
from app.schemas.pending_decision import ConfirmDecisionRequest, PendingDecisionResponse
from app.schemas.common import success_response, paginated_response
from fastapi.responses import JSONResponse

router = APIRouter()


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
        base_q.order_by(PendingDecision.created_at.desc()).offset(offset).limit(page_size)
    )
    decisions = result.scalars().all()
    return paginated_response(
        [PendingDecisionResponse.model_validate(d).model_dump() for d in decisions],
        total=total, page=page, page_size=page_size,
    )


@router.post("/decisions/{decision_id}/confirm")
async def confirm_decision(
    decision_id: str,
    body: ConfirmDecisionRequest,
    db: AsyncSession = Depends(get_db),
):
    decision = await db.get(PendingDecision, decision_id)
    if not decision:
        return JSONResponse(status_code=404, content={"success": False, "data": None, "error": {"code": "NOT_FOUND", "message": "Decision not found"}})
    if body.resource_id not in decision.candidates:
        return JSONResponse(status_code=400, content={"success": False, "data": None, "error": {"code": "INVALID_RESOURCE", "message": "Resource not in candidates"}})
    decision.status = "decided"
    decision.decided_resource_id = body.resource_id
    decision.decided_at = datetime.utcnow()
    await db.flush()
    await db.refresh(decision)
    # TODO: Enqueue download task
    return success_response(PendingDecisionResponse.model_validate(decision).model_dump())


@router.post("/decisions/{decision_id}/skip")
async def skip_decision(decision_id: str, db: AsyncSession = Depends(get_db)):
    decision = await db.get(PendingDecision, decision_id)
    if not decision:
        return JSONResponse(status_code=404, content={"success": False, "data": None, "error": {"code": "NOT_FOUND", "message": "Decision not found"}})
    decision.status = "skipped"
    decision.decided_at = datetime.utcnow()
    await db.flush()
    await db.refresh(decision)
    return success_response(PendingDecisionResponse.model_validate(decision).model_dump())
