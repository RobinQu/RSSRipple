"""Agent API routes."""

import re

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from thefuzz import fuzz

from app.database import get_db
from app.models.agent import Agent
from app.models.filter import ResourceFilter
from app.models.file_resource import FileResource
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


@router.post("/agents/{agent_id}/test-filters")
async def test_filters(
    agent_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Test agent's filters against its channel's FileResources.

    Returns per-resource match results showing which filters pass/fail.
    """
    agent = await db.get(Agent, agent_id)
    if not agent:
        return JSONResponse(status_code=404, content={"success": False, "data": None, "error": {"code": "NOT_FOUND", "message": "Agent not found"}})

    # Load filters (already eager-loaded but ensure fresh)
    filters = agent.filters
    if not filters:
        return success_response({
            "total_resources": 0,
            "matched": 0,
            "failed": 0,
            "results": [],
            "message": "No filters configured",
        })

    # Load channel resources
    result = await db.execute(
        select(FileResource)
        .where(FileResource.channel_id == agent.channel_id)
        .order_by(FileResource.published_at.desc())
        .limit(100)
    )
    resources = result.scalars().all()

    results = []
    matched_count = 0

    for res in resources:
        resource_result = {
            "resource_id": res.id,
            "title_raw": res.title_raw,
            "filters": [],
            "all_required_passed": True,
        }

        for f in filters:
            field_value = getattr(res, f.field, None)
            field_str = str(field_value) if field_value is not None else ""
            passed = _evaluate_filter(field_str, f.operator, f.value)

            resource_result["filters"].append({
                "field": f.field,
                "operator": f.operator,
                "filter_value": f.value,
                "resource_value": field_str,
                "passed": passed,
                "is_required": f.is_required,
            })

            if f.is_required and not passed:
                resource_result["all_required_passed"] = False

        if resource_result["all_required_passed"]:
            matched_count += 1

        results.append(resource_result)

    return success_response({
        "total_resources": len(resources),
        "matched": matched_count,
        "failed": len(resources) - matched_count,
        "results": results,
    })


def _evaluate_filter(field_value: str, operator: str, filter_value: str) -> bool:
    """Evaluate a single filter operator against a field value."""
    if not field_value:
        return False

    if operator == "eq":
        return field_value.lower() == filter_value.lower()
    elif operator == "contains":
        return filter_value.lower() in field_value.lower()
    elif operator == "fuzzy":
        return fuzz.ratio(field_value.lower(), filter_value.lower()) >= 70
    elif operator == "in":
        values = [v.strip().lower() for v in filter_value.split(",")]
        return field_value.lower() in values
    elif operator == "regex":
        try:
            return bool(re.search(filter_value, field_value, re.IGNORECASE))
        except re.error:
            return False
    return False
