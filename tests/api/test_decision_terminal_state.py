"""Terminal user decisions remain immutable through action endpoints."""
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest

from app.models.pending_decision import PendingDecision
from app.utils.time import utcnow
from tests.api.test_decisions import setup as setup


@pytest.mark.parametrize('status', ['decided', 'skipped', 'expired'])
@pytest.mark.parametrize('action', ['confirm', 'skip'])
async def test_terminal_decision_is_not_rewritten(client, setup, db_session_factory, status, action):
    _, _, agent_id = setup
    timestamp = utcnow() - timedelta(days=1)
    async with db_session_factory() as db:
        row = PendingDecision(agent_id=agent_id, candidates=['old-a', 'old-b'], status=status,
                              reason='Historical user choice', decided_resource_id='old-b', decided_at=timestamp)
        db.add(row)
        await db.commit()
        identity = row.id
    with patch('app.services.agent_service.dispatch_download', AsyncMock()) as dispatch:
        response = await client.post(f'/api/v1/decisions/{identity}/{action}',
                                     json={'resource_id':'old-a'} if action == 'confirm' else None)
    assert response.status_code == 409, response.text
    assert response.json()['error']['code'] == 'INVALID_STATE'
    dispatch.assert_not_awaited()
    async with db_session_factory() as db:
        row = await db.get(PendingDecision, identity)
        assert (row.status, row.decided_resource_id, row.decided_at) == (status, 'old-b', timestamp)
