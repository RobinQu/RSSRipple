"""Revalidate terminal guards with valid identities and real HTTP/DB transitions.

Only titles are from the recorded work graph. Candidate equivalence, identities
and decision states are synthetic; downloader RPC uses the API test double.
"""
import hashlib
import json
import os
from pathlib import Path

import pytest

from app.models.pending_decision import PendingDecision
from tests.api.test_decisions import _create_resource, _make_decision
from tests.api.test_decisions import setup as setup


@pytest.mark.parametrize('terminal', ['decided', 'skipped', 'expired'])
@pytest.mark.parametrize('other_candidate', [False, True])
async def test_terminal_confirmation(
    client, setup, db_session_factory, mock_transmission, monkeypatch,
    terminal, other_candidate,
):
    fixture = Path('tests/fixtures/prod_works_v1.json')
    assert hashlib.sha256(fixture.read_bytes()).hexdigest() == (
        'd11651d2162ced23e8d919af0bff2d9f316e203234cc854909ba5f444a35ec32'
    )
    recorded = json.loads(fixture.read_text())['tables']['file_resources']
    channel, _, agent = setup
    candidates = [await _create_resource(db_session_factory, channel, row['title_raw'])
                  for row in recorded[:2]]
    identity = await _make_decision(db_session_factory, agent,
                                    candidates[0]['id'], candidates[1]['id'])
    if terminal == 'decided':
        first = await client.post(f'/api/v1/decisions/{identity}/confirm',
                                  json={'resource_id': candidates[0]['id']})
        assert first.status_code == 200, first.text
        assert mock_transmission.add_torrent.await_count == 1
    elif terminal == 'skipped':
        first = await client.post(f'/api/v1/decisions/{identity}/skip')
        assert first.status_code == 200, first.text
    else:
        async with db_session_factory() as db:
            row = await db.get(PendingDecision, identity)
            row.status = 'expired'
            await db.commit()
    async with db_session_factory() as db:
        row = await db.get(PendingDecision, identity)
        before = (row.status, row.decided_resource_id, row.decided_at)
        assert row.decision_key and row.decision_scope and len(row.candidates) == 2
        assert before[0] == terminal
    calls = mock_transmission.add_torrent.await_count
    if os.environ.get('DECISION_GUARD_MUTATION') == '1':
        from app.api.v1 import decisions
        original = decisions.current_choice_error

        async def without_status_guard(row, db, **kwargs):
            status = row.status
            row.status = 'pending'
            try:
                return await original(row, db, **kwargs)
            finally:
                row.status = status

        monkeypatch.setattr(decisions, 'current_choice_error', without_status_guard)
    response = await client.post(f'/api/v1/decisions/{identity}/confirm',
                                 json={'resource_id': candidates[int(other_candidate)]['id']})
    assert response.status_code == 409, response.text
    assert response.json()['error'] == {
        'code': 'INVALID_STATE', 'message': 'Decision is no longer pending',
    }
    assert mock_transmission.add_torrent.await_count == calls
    async with db_session_factory() as db:
        row = await db.get(PendingDecision, identity)
        assert (row.status, row.decided_resource_id, row.decided_at) == before
