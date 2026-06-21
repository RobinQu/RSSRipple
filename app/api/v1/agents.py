"""Agent API routes."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.agent import Agent
from app.models.filter import ResourceFilter
from app.schemas.agent import AgentCreate, AgentUpdate, AgentResponse
from app.schemas.common import success_response, paginated_response
from fastapi.responses import JSONResponse

router = APIRouter()


@router.get("/agents")
async def list_agents(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    offset = (page - 1) * page_size
    total_q = await db.execute(select(func.count()).select_from(Agent))
    total = total_q.scalar_one()
    result = await db.execute(
        select(Agent).order_by(Agent.created_at.desc()).offset(offset).limit(page_size)
    )
    agents = result.scalars().all()
    return paginated_response(
        [AgentResponse.model_validate(a).model_dump() for a in agents],
        total=total, page=page, page_size=page_size,
    )


@router.post("/agents", status_code=201)
async def create_agent(
    body: AgentCreate,
    db: AsyncSession = Depends(get_db),
):
    agent_data = body.model_dump(exclude={"filters"})
    agent = Agent(**agent_data)
    db.add(agent)
    await db.flush()

    if body.filters:
        for f in body.filters:
            rf = ResourceFilter(agent_id=agent.id, **f.model_dump())
            db.add(rf)
        await db.flush()

    await db.refresh(agent)
    return success_response(AgentResponse.model_validate(agent).model_dump())


@router.get("/agents/{agent_id}")
async def get_agent(agent_id: str, db: AsyncSession = Depends(get_db)):
    agent = await db.get(Agent, agent_id)
    if not agent:
        return JSONResponse(status_code=404, content={"success": False, "data": None, "error": {"code": "NOT_FOUND", "message": "Agent not found"}})
    return success_response(AgentResponse.model_validate(agent).model_dump())


@router.put("/agents/{agent_id}")
async def update_agent(
    agent_id: str,
    body: AgentUpdate,
    db: AsyncSession = Depends(get_db),
):
    agent = await db.get(Agent, agent_id)
    if not agent:
        return JSONResponse(status_code=404, content={"success": False, "data": None, "error": {"code": "NOT_FOUND", "message": "Agent not found"}})
    update_data = body.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(agent, key, value)
    await db.flush()
    await db.refresh(agent)
    return success_response(AgentResponse.model_validate(agent).model_dump())


@router.delete("/agents/{agent_id}")
async def delete_agent(agent_id: str, db: AsyncSession = Depends(get_db)):
    agent = await db.get(Agent, agent_id)
    if not agent:
        return JSONResponse(status_code=404, content={"success": False, "data": None, "error": {"code": "NOT_FOUND", "message": "Agent not found"}})
    await db.delete(agent)
    return success_response({"deleted": True})


@router.post("/agents/{agent_id}/run")
async def run_agent(agent_id: str, db: AsyncSession = Depends(get_db)):
    agent = await db.get(Agent, agent_id)
    if not agent:
        return JSONResponse(status_code=404, content={"success": False, "data": None, "error": {"code": "NOT_FOUND", "message": "Agent not found"}})
    # TODO: Trigger agent processing
    return success_response({"message": "Agent run triggered", "agent_id": agent_id})
