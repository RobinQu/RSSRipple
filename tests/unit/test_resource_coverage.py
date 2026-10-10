
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


# ---------------------------------------------------------------------------
# Extras: season-0 specials must not poison coverage
# ---------------------------------------------------------------------------


def test_specials_extras_do_not_poison_season_coverage():
    r = resource()
    r.file_assignments = [assignment(1, 12), assignment(1, 3, season=0)]
    assert batch_coverage(r) == (('series', 's1', 1, ((1, 12),)),)


def test_specials_on_special_work_are_regular_coverage():
    r = resource()
    r.series_id = 's0'
    r.series = TVSeries(id='s0', season_number=0)
    r.season = 0
    r.file_assignments = [
        ResourceFileAssignment(series_id='s0', movie_id=None, season=0,
                               episode_start=1, episode_end=3, file_path='sp1.mkv'),
    ]
    assert batch_coverage(r) == (('series', 's0', 0, ((1, 3),)),)


def test_specials_pointing_at_unknown_work_still_fail():
    r = resource()
    r.file_assignments = [assignment(1, 12), assignment(1, 1, work='sX', season=0)]
    assert batch_coverage(r) is None


def test_specials_extras_in_links_carried_multi_season_pack():
    r = resource()
    r.series_id = None
    r.batch_scope = 'multi_season'
    r.work_links = [ResourceWorkLink(series_id=s, series=TVSeries(id=s, season_number=n))
                    for s, n in [('s1', 1), ('s2', 2)]]
    r.file_assignments = [
        assignment(1, 12, work='s1', season=1),
        assignment(1, 12, work='s2', season=2),
        assignment(1, 3, work='s2', season=0),
    ]
    assert batch_coverage(r) == (
        ('series', 's1', 1, ((1, 12),)),
        ('series', 's2', 2, ((1, 12),)),
    )


def test_row_with_both_work_fks_is_invalid():
    r = resource()
    bad = ResourceFileAssignment(series_id='s1', movie_id='m1', season=1,
                                 episode_start=1, episode_end=1, file_path='x.mkv')
    r.file_assignments = [assignment(1, 12), bad]
    assert batch_coverage(r) is None


def test_linked_specials_work_needs_no_assignment_representation():
    r = resource()
    r.work_links = [ResourceWorkLink(series_id='s0', series=TVSeries(id='s0', season_number=0))]
    r.file_assignments = [assignment(1, 12)]
    assert batch_coverage(r) == (('series', 's1', 1, ((1, 12),)),)


def test_linked_specials_work_with_files_joins_the_key():
    r = resource()
    r.work_links = [ResourceWorkLink(series_id='s0', series=TVSeries(id='s0', season_number=0))]
    r.file_assignments = [
        assignment(1, 12),
        ResourceFileAssignment(series_id='s0', movie_id=None, season=0,
                               episode_start=1, episode_end=3, file_path='sp.mkv'),
    ]
    assert batch_coverage(r) == (
        ('series', 's0', 0, ((1, 3),)),
        ('series', 's1', 1, ((1, 12),)),
    )


def test_unrepresented_real_season_work_still_fails():
    r = resource()
    r.work_links = [ResourceWorkLink(series_id='s2', series=TVSeries(id='s2', season_number=2))]
    r.file_assignments = [assignment(1, 12)]
    assert batch_coverage(r) is None


def test_title_fallback_ignores_linked_specials_work():
    r = resource()
    r.work_links = [ResourceWorkLink(series_id='s0', series=TVSeries(id='s0', season_number=0))]
    assert batch_coverage(r) == (('series', 's1', 1, ((1, 12),)),)


def test_title_fallback_still_rejects_two_real_season_works():
    r = resource()
    r.work_links = [ResourceWorkLink(series_id='s2', series=TVSeries(id='s2', season_number=2))]
    assert batch_coverage(r) is None


def test_specials_only_pack_has_unknown_coverage():
    r = resource()
    r.file_assignments = [assignment(1, 3, season=0)]
    assert batch_coverage(r) is None
