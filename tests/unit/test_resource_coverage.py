
import pytest

from app.models.file_resource import FileResource
from app.models.resource_file_assignment import ResourceFileAssignment
from app.models.resource_work_link import ResourceWorkLink
from app.models.series import TVSeries
from app.services.resource_coverage import batch_coverage, merge_intervals


def resource(start=1, end=12):
    r = FileResource(is_batch=True, batch_scope='season', series_id='s1', season=1,
                     episode_start=start, episode_end=end)
    r.series = TVSeries(id="s1", season_number=1)
    r.work_links = []
    r.file_assignments = []
    return r


def assignment(start, end, *, work='s1', season=1):
    return ResourceFileAssignment(series_id=work, season=season, episode_start=start,
                                  episode_end=end, file_path=f'{work}-{start}.mkv')


def test_half_full_and_unknown_are_distinct():
    assert batch_coverage(resource(end=6)) != batch_coverage(resource())
    assert batch_coverage(resource(end=None)) is None
    assert batch_coverage(resource(start=None)) is None


def test_authoritative_gaps_survive_minmax_title_summary():
    r = resource()
    r.file_assignments = [assignment(1, 3), assignment(5, 12)]
    assert batch_coverage(r) == (('series', 's1', 1, ((1, 3), (5, 12))),)
    assert batch_coverage(r) != batch_coverage(resource())


def test_adjacent_overlapping_intervals_are_canonical_without_expansion():
    assert merge_intervals([(5, 10**9), (3, 5), (1, 2)]) == ((1, 10**9),)


@pytest.mark.parametrize('start,end', [(None, None), (-1, 3), (4, 3), (True, 3), (1, '3')])
def test_invalid_assignment_cannot_fall_back_to_title(start, end):
    r = resource()
    r.file_assignments = [assignment(start, end)]
    assert batch_coverage(r) is None


def test_different_link_sets_and_order_independence():
    r = resource()
    r.series_id = None
    r.batch_scope = 'multi_season'
    r.work_links = [ResourceWorkLink(series_id=s, series=TVSeries(id=s, season_number=n))
                    for s,n in [('s2',2),('s1',1)]]
    r.file_assignments = [assignment(1, 12, work='s2', season=2), assignment(1, 12)]
    first = batch_coverage(r)
    r.work_links.reverse()
    r.file_assignments.reverse()
    assert batch_coverage(r) == first
    r.file_assignments[0].series_id = 's3'
    assert batch_coverage(r) is None


def test_missing_work_assignment_is_unknown():
    r = resource()
    r.work_links = [ResourceWorkLink(series_id='s2')]
    r.file_assignments = [assignment(1, 12)]
    assert batch_coverage(r) is None


def test_cross_season_legacy_cannot_be_declared_a_season_work():
    r = resource()
    r.file_assignments = [assignment(1, 12), assignment(1, 12, season=2)]
    assert batch_coverage(r) is None


def test_unloaded_relations_do_not_guess_empty_manifest():
    r = FileResource(is_batch=True, batch_scope='season', series_id='s1', season=1,
                     episode_start=1, episode_end=12)
    assert batch_coverage(r) is None


def test_wrong_single_assignment_season_is_unknown():
    r = resource()
    r.file_assignments = [assignment(1, 12, season=2)]
    assert batch_coverage(r) is None


def test_title_season_must_match_work_identity():
    r = resource()
    r.season = 2
    assert batch_coverage(r) is None


def test_legacy_work_identity_is_not_reinterpreted_as_single_season():
    r = resource()
    r.series.number_of_seasons = 2
    assert batch_coverage(r) is None


async def test_unloaded_work_never_issues_lazy_sql(db_session, db_engine, sample_channel):
    from sqlalchemy import event
    r = resource()
    r.channel_id = sample_channel.id
    r.title_raw = "Synthetic unloaded work"
    r.guid = "synthetic-unloaded-work"
    r.torrent_url = "https://example.invalid/torrent"
    r.series.title_cn = "Synthetic single season"
    db_session.add(r)
    await db_session.flush()
    db_session.expire(r, ["series"])
    statements = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(db_engine.sync_engine, "before_cursor_execute", capture)
    try:
        assert batch_coverage(r) is None
    finally:
        event.remove(db_engine.sync_engine, "before_cursor_execute", capture)
    assert statements == []
