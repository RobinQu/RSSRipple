"""Batch request with an actual failed SQL flush and a following good item."""
from unittest.mock import patch

import pytest

from app.models.pending_decision import PendingDecision
from app.services.decision_store import choice_identity
from tests.api.test_decisions import setup as setup


@pytest.mark.parametrize('failure', ['integrity', 'rejected'])
async def test_failed_item_rolls_back_and_next_item_commits(client, setup, db_session_factory, failure):
    _, _, agent_id = setup
    async with db_session_factory() as db:
        for index in range(2):
            key, scope = choice_identity('movie', f'synthetic-{index}', None, None)
            db.add(PendingDecision(agent_id=agent_id, candidates=['synthetic-a','synthetic-b'],
                decision_key=key, decision_scope=scope, status='pending', reason='Original reason'))
        await db.commit()
    called = []

    async def action(decision, db, **kwargs):
        called.append(decision.id)
        decision.status = 'decided'
        if len(called) == 1:
            if failure == 'integrity':
                decision.reason = None
                await db.flush()  # Actual NOT NULL violation, not a mocked exception.
            decision.reason = 'Uncommitted rejected mutation'
            await db.flush()
            return False, 'Synthetic policy rejection'
        decision.reason = 'Accepted second item'
        await db.flush()
        return True, None

    from sqlalchemy import select
    async with db_session_factory() as db:
        ids = list(await db.scalars(select(PendingDecision.id).where(PendingDecision.agent_id == agent_id)))
    with patch('app.api.v1.decisions._ai_pick_and_dispatch', action), patch(
        'app.api.v1.decisions._prepare_ai_choice', return_value=(None, 'Fault-injected action')):
        response = await client.post(f'/api/v1/agents/{agent_id}/decisions/batch',
                                     json={'action':'ai', 'decision_ids':ids})
    assert response.status_code == 200, response.text
    data = response.json()['data']
    assert (data['processed'], data['failed'], data['dispatched']) == (2,1,1)
    assert len(called) == 2
    async with db_session_factory() as db:
        failed = await db.get(PendingDecision, called[0])
        good = await db.get(PendingDecision, called[1])
        assert failed.status == 'pending' and failed.reason == 'Original reason'
        assert good.status == 'decided' and good.reason == 'Accepted second item'
