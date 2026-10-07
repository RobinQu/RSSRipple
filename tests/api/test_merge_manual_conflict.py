"""Manual merge confirmation does not authorize discarding conflicting edits."""
import pytest
from sqlalchemy import select

from app.models.movie import Movie


@pytest.mark.parametrize("same_value", [False, True])
async def test_merge_reports_conflict_without_false_success(client, db_session, same_value):
    rows = [Movie(title_en="Synthetic merge", description=value, manually_edited_fields=["description"])
            for value in ["first", "first" if same_value else "second"]]
    db_session.add_all(rows)
    await db_session.commit()
    survivor_id, duplicate_id = [row.id for row in rows]
    response = await client.post('/api/v1/works/merge', json={
        'survivor_type': 'movie', 'survivor_id': survivor_id,
        'duplicate_ids': [duplicate_id], 'confirm': True,
    })
    assert response.status_code == (200 if same_value else 409), response.text
    if not same_value:
        assert response.json()['error']['code'] == 'INVALID_STATE'
        assert response.json()['error']['details']['fields'] == ['description']
    db_session.expire_all()
    actual = (await db_session.execute(select(Movie))).scalars().all()
    assert len(actual) == (1 if same_value else 2)
    assert {row.description for row in actual} == ({'first'} if same_value else {'first', 'second'})
