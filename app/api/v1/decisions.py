"""PendingDecision API routes."""


from copy import deepcopy
from dataclasses import dataclass

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy import func, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.file_resource import FileResource
from app.models.movie import Movie
from app.models.pending_decision import PendingDecision
from app.models.series import TVSeries
from app.schemas.common import paginated_response, success_response
from app.schemas.pending_decision import (
    BatchDecisionRequest,
    BatchDecisionResponse,
    ConfirmDecisionRequest,
    DecisionActionResponse,
    PendingDecisionResponse,
)
from app.services.decision_review import current_choice_error
from app.services.decision_service import maybe_reset_agent_run_status
from app.services.resource_confirmation import LEGACY_CONFIRMATION_REASON_PREFIXES
from app.utils.time import utcnow

router = APIRouter()

def _invalid_choice(message):
    return JSONResponse(status_code=409, content={
        "success": False, "data": None,
        "error": {"code": "INVALID_STATE", "message": message}, "meta": {},
    })


def _choice_decision_filter():
    """SQL predicate excluding legacy resource-metadata confirmation rows."""
    return not_(
        or_(
            *(
                PendingDecision.reason.startswith(prefix)
                for prefix in LEGACY_CONFIRMATION_REASON_PREFIXES
            )
        )
    )


def _is_choice_decision(decision: PendingDecision) -> bool:
    return (
        len(decision.candidates or []) >= 2
        and not (decision.reason or "").startswith(
            LEGACY_CONFIRMATION_REASON_PREFIXES
        )
    )


async def _lock_current_decision(decision, db):
    """Use the same parent-before-decision order as pending-choice writers."""
    from app.models.agent import Agent

    with db.no_autoflush:
        await db.scalar(select(Agent.id).where(Agent.id == decision.agent_id).with_for_update(key_share=True))
        return await db.scalar(select(PendingDecision).where(
            PendingDecision.id == decision.id
        ).with_for_update().execution_options(populate_existing=True))


@dataclass(frozen=True)
class PreparedChoice:
    candidate_ids: frozenset[str]
    scope: dict
    picked_id: str


async def _prepare_ai_choice(
    decision: PendingDecision, db: AsyncSession
) -> tuple[PreparedChoice | None, str | None]:
    """Compute a recommendation without acquiring write locks or changing rows."""
    from app.models.agent import Agent
    from app.services.agent_service import (
        _generate_llm_pick,
        pick_by_preferences,
    )

    if error := await current_choice_error(decision, db):
        return None, error
    agent = await db.get(Agent, decision.agent_id)
    if not agent:
        return None, f"Agent {decision.agent_id} not found"

    candidate_snapshot = set(decision.candidates or [])
    scope_snapshot = deepcopy(decision.decision_scope)
    picked_id = decision.llm_picked_resource_id
    if not picked_id or picked_id not in (decision.candidates or []):
        cands = (await db.execute(
            select(FileResource).where(
                FileResource.id.in_(decision.candidates or [])
            ).options(
                # The LLM pick summary reads title/year/rating via these; the
                # collection chain keeps series.collection / movie.collection
                # DSL fields correct if a decision is re-filtered here.
                selectinload(FileResource.series).selectinload(TVSeries.collection),
                selectinload(FileResource.movie).selectinload(Movie.collection),
            )
        )).scalars().all()
        if not cands:
            return None, "No candidates to pick from"
        # Deterministic preference rules first (rank-only): a unique winner
        # needs no LLM call; a remaining tie goes to the LLM on the narrowed
        # tier.
        tier, _deciding = pick_by_preferences(list(cands), agent.pick_preferences)
        if len(tier) == 1:
            picked_id = tier[0].id
        else:
            picked_id, _reason = await _generate_llm_pick(
                agent, tier,
                ("series", decision.series_id, decision.season, decision.episode)
                if decision.series_id
                else ("movie", decision.movie_id, None, None),
            )
        if not picked_id:
            return None, "AI 未能给出选择，请手动确认"

    if picked_id not in candidate_snapshot:
        return None, "AI returned a resource outside the candidate set"
    return PreparedChoice(frozenset(candidate_snapshot), scope_snapshot, picked_id), None


async def _ai_pick_and_dispatch(decision, db, *, prepared=None):
    from app.models.agent import Agent
    from app.services.agent_service import dispatch_download

    choice, error = prepared if prepared is not None else await _prepare_ai_choice(decision, db)
    if choice is None:
        return False, error
    decision = await _lock_current_decision(decision, db)
    if decision is None:
        return False, "Decision no longer exists"
    if set(decision.candidates or []) != choice.candidate_ids or decision.decision_scope != choice.scope:
        return False, "Decision candidates changed while computing the recommendation"
    if error := await current_choice_error(decision, db, lock_resources=True):
        return False, error
    decision.llm_picked_resource_id = choice.picked_id
    resource = await db.get(FileResource, choice.picked_id)
    if not resource:
        return False, f"Picked resource {choice.picked_id} not found"

    agent = await db.get(Agent, decision.agent_id)
    await dispatch_download(agent, resource, db)
    decision.status = "decided"
    decision.decided_resource_id = choice.picked_id
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
    base_q = select(PendingDecision).where(
        PendingDecision.agent_id == agent_id,
        _choice_decision_filter(),
    )
    if status:
        base_q = base_q.where(PendingDecision.status == status)
    total_q = await db.execute(select(func.count()).select_from(base_q.subquery()))
    total = total_q.scalar_one()
    offset = (page - 1) * page_size
    result = await db.execute(
        base_q.options(
            selectinload(PendingDecision.series),
            selectinload(PendingDecision.movie),
        ).order_by(PendingDecision.created_at.desc()).offset(offset).limit(page_size)
    )
    decisions = [d for d in result.scalars().all() if _is_choice_decision(d)]
    out = []
    for d in decisions:
        data = PendingDecisionResponse.model_validate(d).model_dump()
        # Load candidate resources
        cands = (await db.execute(
            select(FileResource)
            .where(FileResource.id.in_(d.candidates or []))
            .options(
                selectinload(FileResource.audio_work),
                selectinload(FileResource.collection),
                selectinload(FileResource.series),
                selectinload(FileResource.movie),
            )
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
    if decision is not None:
        decision = await _lock_current_decision(decision, db)
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
    if not _is_choice_decision(decision):
        return JSONResponse(
            status_code=409,
            content={
                "success": False,
                "data": None,
                "error": {
                    "code": "INVALID_STATE",
                    "message": "Agent decisions require at least two eligible candidates",
                },
                "meta": {},
            },
        )
    if error := await current_choice_error(decision, db, lock_resources=True):
        return _invalid_choice(error)
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

    await maybe_reset_agent_run_status(db, decision.agent_id)
    await db.commit()
    # Reload with relationships eager-loaded: commit() expires the ORM object
    # and PendingDecisionResponse includes series/movie, so a plain refresh
    # would trigger implicit lazy IO (MissingGreenlet) during serialization.
    decision = (await db.execute(
        select(PendingDecision)
        .where(PendingDecision.id == decision_id)
        .options(
            selectinload(PendingDecision.series),
            selectinload(PendingDecision.movie),
        )
    )).scalar_one()
    return success_response(PendingDecisionResponse.model_validate(decision).model_dump())


@router.post("/decisions/{decision_id}/skip")
async def skip_decision(decision_id: str, db: AsyncSession = Depends(get_db)):
    decision = await db.get(PendingDecision, decision_id)
    if decision is not None:
        decision = await _lock_current_decision(decision, db)
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
    if decision.status != "pending":
        return _invalid_choice("Decision is no longer pending")
    if not _is_choice_decision(decision):
        return JSONResponse(
            status_code=409,
            content={
                "success": False,
                "data": None,
                "error": {
                    "code": "INVALID_STATE",
                    "message": "Agent decisions require at least two eligible candidates",
                },
                "meta": {},
            },
        )
    decision.status = "skipped"
    decision.decided_at = utcnow()
    await db.flush()
    await maybe_reset_agent_run_status(db, decision.agent_id)
    await db.commit()
    # See confirm_decision: reload with series/movie eager-loaded to avoid
    # implicit lazy IO during response serialization.
    decision = (await db.execute(
        select(PendingDecision)
        .where(PendingDecision.id == decision_id)
        .options(
            selectinload(PendingDecision.series),
            selectinload(PendingDecision.movie),
        )
    )).scalar_one()
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
    if not _is_choice_decision(decision):
        return JSONResponse(
            status_code=409,
            content={
                "success": False,
                "data": None,
                "error": {
                    "code": "INVALID_STATE",
                    "message": "Agent decisions require at least two eligible candidates",
                },
                "meta": {},
            },
        )
    if error := await current_choice_error(decision, db):
        return _invalid_choice(error)
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
    queried_rows = (await db.execute(
        select(PendingDecision).where(
            PendingDecision.agent_id == agent_id,
            PendingDecision.id.in_(body.decision_ids),
            PendingDecision.status == "pending",
            _choice_decision_filter(),
        )
    )).scalars().all()
    rows = [row for row in queried_rows if _is_choice_decision(row)]

    # Finish all network/model work before the first item acquires write locks.
    recommendations = {}
    if body.action == "ai":
        for dec in rows:
            try:
                recommendations[dec.id] = await _prepare_ai_choice(dec, db)
            except Exception as exc:
                from app.database import _is_retryable_lock_error

                if (_is_retryable_lock_error(exc) or not db.is_active
                        or getattr(exc, "connection_invalidated", False)):
                    raise
                recommendations[dec.id] = (None, str(exc))

    class RejectedChoiceError(Exception):
        """Roll back the item even when its action returns a policy failure."""

    resp = BatchDecisionResponse()
    for dec in rows:
        # Save scalars before a failed flush expires the ORM object's state.
        decision_id = dec.id
        resp.processed += 1
        try:
            async with db.begin_nested():
                if body.action == "skip":
                    dec = await _lock_current_decision(dec, db)
                    if dec is None or dec.status != "pending" or not _is_choice_decision(dec):
                        raise RejectedChoiceError("Decision is no longer a pending choice")
                    dec.status = "skipped"
                    dec.decided_at = utcnow()
                else:
                    ok, err = await _ai_pick_and_dispatch(dec, db, prepared=recommendations[decision_id])
                    if not ok:
                        raise RejectedChoiceError(err or "Decision rejected")
                await db.flush()
            if body.action == "skip":
                resp.skipped += 1
            else:
                resp.dispatched += 1
        except Exception as e:  # noqa: BLE001
            from app.database import _is_retryable_lock_error

            # A SAVEPOINT cannot recover a lost outer transaction. Let the
            # request boundary roll back/retry it rather than report success.
            if (_is_retryable_lock_error(e) or not db.is_active
                    or getattr(e, "connection_invalidated", False)):
                raise
            resp.failed += 1
            resp.errors.append(f"{decision_id}: {e}")
    await maybe_reset_agent_run_status(db, agent_id)
    await db.commit()
    return success_response(resp.model_dump())
