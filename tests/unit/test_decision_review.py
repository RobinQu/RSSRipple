"""Synthetic legacy mappings: no new model constraints mask upgrade cases."""
from copy import deepcopy

from app.services.decision_review import review_decisions
from tests.unit.test_agent_service import _make_resource


def row(identity, candidates, **overrides):
    return dict(id=identity, agent_id='agent', candidates=candidates, status='pending', **overrides)


def movie(identity, work):
    return _make_resource('channel', id=identity, movie_id=work, season=None, episode=None)


def test_mixed_slots_split_and_duplicate_slots_merge_without_losing_sources():
    resources = {r.id: r for r in [movie('a','film1'), movie('b','film1'), movie('c','film2'), movie('d','film2')]}
    original = [row('old1',['a','c']), row('old2',['b','d','a'])]
    before = deepcopy(original)
    report = review_decisions(original, resources)
    assert original == before
    assert not report['blocked']
    assert {tuple(g['candidates']) for g in report['proposed_groups']} == {('a','b'),('c','d')}
    assert all(g['source_decision_ids'] == ['old1','old2'] for g in report['proposed_groups'])
    assert not any(g['requires_review'] for g in report['proposed_groups'])
    assert report == review_decisions(list(reversed(original)), dict(reversed(list(resources.items()))))


def test_unknown_missing_and_singleton_are_not_silently_dropped():
    resources = {'a':movie('a','film'), 'unknown':_make_resource('channel',id='unknown',series_id=None,movie_id=None)}
    report = review_decisions([row('old',['a','a','unknown','deleted'])], resources)
    assert {b['reason'] for b in report['blocked']} == {'unknown_coverage','missing_resource'}
    assert report['proposed_groups'][0]['requires_review']
    assert report['proposed_groups'][0]['candidates'] == ['a']
    assert report['original_decisions'][0]['candidates'] == ['a','a','unknown','deleted']


def test_decided_history_is_preserved_even_if_resource_no_longer_exists():
    historical = dict(id='history', agent_id='agent', candidates=['gone'], status='decided', decided_resource_id='gone')
    report = review_decisions([historical], {})
    assert report['original_decisions'] == [historical]
    assert not report['proposed_groups'] and not report['blocked']


def test_changed_candidate_work_invalidates_review_fingerprint():
    resource = movie('a','film1')
    first = review_decisions([row('old',['a'])], {'a':resource})
    resource.movie_id = 'film2'
    second = review_decisions([row('old',['a'])], {'a':resource})
    assert first['fingerprint'] != second['fingerprint']


def test_different_agents_do_not_merge():
    a,b=movie('a','film'),movie('b','film')
    first=row('one',['a','b']); second=row('two',['a','b']);second['agent_id']='another'
    report=review_decisions([first,second],{'a':a,'b':b})
    assert len(report['proposed_groups']) == 2


async def test_export_reads_actual_legacy_table_without_new_key_columns(db_session):
    import json

    from sqlalchemy import event, text

    from app.services.decision_review import export_decision_review

    # Deliberately pre-key table. No ORM PendingDecision SELECT may be used.
    await db_session.execute(text('DROP TABLE pending_decisions'))
    await db_session.execute(text('CREATE TABLE pending_decisions '
                                  '(id TEXT PRIMARY KEY, agent_id TEXT, status TEXT, '
                                  'candidates TEXT, decided_resource_id TEXT)'))
    await db_session.execute(text('INSERT INTO pending_decisions VALUES '
                                  "('old', 'agent', 'pending', :candidates, NULL)"),
                             {'candidates': json.dumps(['deleted'])})
    await db_session.execute(text('INSERT INTO pending_decisions VALUES '
                                  "('history', 'agent', 'decided', :candidates, 'gone')"),
                             {'candidates': json.dumps(['gone'])})
    statements = []
    conn = await db_session.connection()

    def observe(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(conn.sync_connection, 'before_cursor_execute', observe)
    try:
        report = await export_decision_review(db_session)
    finally:
        event.remove(conn.sync_connection, 'before_cursor_execute', observe)
    assert len(report['original_decisions']) == 2
    assert report['blocked'] == [dict(decision_id='old', resource_id='deleted', reason='missing_resource')]
    assert all(statement.lstrip().upper().startswith('SELECT') for statement in statements), statements
    assert (await db_session.execute(text('SELECT count(*) FROM pending_decisions'))).scalar_one() == 2
