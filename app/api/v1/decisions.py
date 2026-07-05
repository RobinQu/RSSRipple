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
from app.schemas.pending_decision import ConfirmDecisionRequest, PendingDecisionResponse
from app.utils.time import utcnow

router = APIRouter()


async def _load_decision_for_response(
    db: AsyncSession, decision_id: str
) -> PendingDecision | None:
    """Fetch a PendingDecision with series/movie eagerly loaded.

    Required before serializing with PendingDecisionResponse, whose ``series``
    and ``movie`` fields trigger lazy loads that fail under async
    (MissingGreenlet) unless the relationships are preloaded.
    """
    result = await db.execute(
        select(PendingDecision)
        .options(
            selectinload(PendingDecision.series),
            selectinload(PendingDecision.movie),
        )
        .where(PendingDecision.id == decision_id)
    )
    return result.scalars().first()


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
    # Re-fetch with eager-loaded relationships; db.refresh() reloads columns
    # only, leaving series/movie to lazy-load (fails under async).
    decision = await _load_decision_for_response(db, decision_id)
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
    # Re-fetch with eager-loaded relationships; db.refresh() reloads columns
    # only, leaving series/movie to lazy-load (fails under async).
    decision = await _load_decision_for_response(db, decision_id)
    return success_response(PendingDecisionResponse.model_validate(decision).model_dump())
