"""Persisted decision drift through real HTTP; only download RPC is replaced."""
from datetime import date, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from app.models.file_resource import FileResource
from app.models.movie import Movie
from app.models.pending_decision import PendingDecision
from app.services.decision_store import choice_identity
from app.utils.time import utcnow
from tests.api.test_decisions import _create_resource
from tests.api.test_decisions import setup as setup


@pytest.mark.parametrize('batch', [False, True])
@pytest.mark.parametrize('action', ['confirm','ai-pick','batch'])
@pytest.mark.parametrize('change', ['none', 'coverage', 'filter', 'scope', 'metadata'])
async def test_changed_candidate_scope_cannot_dispatch(client, setup, db_session_factory, action, change, batch):
    changed = change != 'none'
    ch, _, aid = setup
    async with db_session_factory() as db:
        first, other = Movie(title_cn='Synthetic first', release_date=date(2020,1,1), is_anime=False), Movie(
            title_cn='Synthetic other', release_date=date(2020,1,1), is_anime=False)
        db.add_all([first,other]); await db.commit()
        first_id, other_id = first.id,other.id
    a = await _create_resource(db_session_factory,ch,'Synthetic a',movie_id=first_id, is_batch=batch, batch_scope='movies' if batch else None)
    b = await _create_resource(db_session_factory,ch,'Synthetic b',movie_id=first_id, is_batch=batch, batch_scope='movies' if batch else None)
    key,scope=(choice_identity('series',None,None,-1,('movie',(('movie',first_id,None,()),)))
               if batch else choice_identity('movie',first_id,None,None))
    async with db_session_factory() as db:
        decision=PendingDecision(agent_id=aid,movie_id=first_id,candidates=[a['id'],b['id']],
            reason='Synthetic equivalent candidates',decision_key=key,decision_scope=scope,
            status='pending',llm_picked_resource_id=a['id'],expires_at=utcnow()+timedelta(days=1))
        db.add(decision);await db.commit();did=decision.id
        if change == 'coverage':
            resource=await db.get(FileResource,a['id']);resource.movie_id=other_id;await db.commit()
        if change == 'metadata':
            from app.models.channel import Channel
            channel = await db.get(Channel, ch)
            channel.required_metadata_fields = [*channel.required_metadata_fields, 'subtitle_group']
            await db.commit()
        if change in {'filter', 'scope'}:
            from app.models.agent import Agent
            agent = await db.get(Agent, aid)
            if change == 'filter':
                agent.filter_config = {'combinator':'and', 'conditions':[
                    {'field':'subtitle_group','operator':'eq','value':'RequiredNewGroup'}]}
            else:
                agent.scope_channel_wide = False
            await db.commit()
    with patch('app.services.agent_service.dispatch_download',AsyncMock()) as dispatch:
        if action=='batch':
            response=await client.post(f'/api/v1/agents/{aid}/decisions/batch',
                                       json={'action':'ai','decision_ids':[did]})
            assert response.status_code==200
            assert response.json()['data']['failed']==int(changed)
            assert response.json()['data']['dispatched']==int(not changed)
        else:
            response=await client.post(f'/api/v1/decisions/{did}/{action}',
                                       json={'resource_id':a['id']} if action=='confirm' else None)
            assert response.status_code==(409 if changed else 200), response.text
        if changed:
            dispatch.assert_not_awaited()
        else:
            dispatch.assert_awaited_once()
    async with db_session_factory() as db:
        decision=await db.get(PendingDecision,did)
        assert decision.status==('pending' if changed else 'decided')
        assert decision.decided_resource_id==(None if changed else a['id'])
