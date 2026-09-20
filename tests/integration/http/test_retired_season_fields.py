"""Real HTTP rejection of retired work fields, with isolated synthetic rows."""
import uuid

import pytest

from tests.integration.http._http import _api, ensure_series


@pytest.mark.parametrize('field,value', [('number_of_seasons', 2), ('number_of_seasons', None),
                                         ('seasons', [{'season_number': 1}]), ('seasons', None)])
def test_retired_fields_are_rejected_without_mutating_work(field, value):
    title = f'Synthetic retired API {uuid.uuid4().hex}'
    rejected = _api('/api/v1/series', method='post', json={'title_cn': title, field: value})
    assert rejected.status_code == 422, rejected.text
    listing = _api('/api/v1/series', params={'title': title})
    assert listing.status_code == 200
    assert not any(row['title_cn'] == title for row in listing.json()['data'])
    identity = ensure_series(title, title, single_season_entry=True)
    try:
        before = _api(f'/api/v1/series/{identity}').json()['data']
        assert before['number_of_seasons'] is None
        assert before['external_source'] == 'bangumi'
        rejected = _api(f'/api/v1/series/{identity}', method='put', json={'title_cn': 'must not persist', field: value})
        assert rejected.status_code == 422, rejected.text
        after = _api(f'/api/v1/series/{identity}').json()['data']
        for key in ['title_cn', 'number_of_seasons', 'season_number', 'external_id', 'manually_edited_fields']:
            assert after[key] == before[key]
    finally:
        deleted = _api(f'/api/v1/series/{identity}', method='delete')
        assert deleted.status_code == 200, deleted.text
