"""Recorded release identity with synthetic offset/DST publication times."""
import hashlib
import json
import uuid
from datetime import datetime
from pathlib import Path

import pytest
from feedparser import FeedParserDict
from sqlalchemy import insert, select, text

from app.clients.rss_parser import _extract_published_at
from app.models.channel import Channel
from app.models.file_resource import FileResource
from app.services.resource_parser import parse_entry
from tests.integration.time_contract.test_storage import channel_values

# Explicit expected instants, independent of the runtime conversion helper.
CASES = [
    ('2026-01-01T00:15:00+08:00', '2025-12-31T16:15:00'),
    ('2026-03-08T01:59:59-05:00', '2026-03-08T06:59:59'),
    ('2026-03-08T03:00:00-04:00', '2026-03-08T07:00:00'),
    ('2026-11-01T01:30:00-04:00', '2026-11-01T05:30:00'),
    ('2026-11-01T01:30:00-05:00', '2026-11-01T06:30:00'),
    ('2026-10-07T12:34:56.123456Z', '2026-10-07T12:34:56.123456'),
    ('2026-10-07T12:34:56', '2026-10-07T12:34:56'),
]


def parse_time(source, value):
    if source == 'mikan_namespace':
        return _extract_published_at(FeedParserDict(torrent_pubdate=value))
    return parse_entry(
        {'published': value},
        {'published_at': {'source': 'published', 'transform': 'iso_datetime'}},
    )['published_at']


async def check_publication(engine, source, incoming, expected):
    raw = (Path(__file__).parents[2] / 'fixtures/prod_works_v1.json').read_bytes()
    assert hashlib.sha256(raw).hexdigest() == 'd11651d2162ced23e8d919af0bff2d9f316e203234cc854909ba5f444a35ec32'
    recorded = json.loads(raw)['tables']['file_resources'][0]
    channel = channel_values()
    resource_id = str(uuid.uuid4())
    parsed = parse_time(source, incoming)
    # Exercise the actual DateTime binding before inspecting the parser value:
    # a PostgreSQL bind rejection is a real failure, not a caught expected error.
    async with engine.begin() as writer:
        if engine.dialect.name == 'postgresql':
            await writer.execute(text("SET LOCAL TIME ZONE 'America/New_York'"))
        await writer.execute(insert(Channel).values(**channel))
        await writer.execute(insert(FileResource).values(
            id=resource_id, channel_id=channel['id'], guid=recorded['guid'],
            title_raw=recorded['title_raw'], torrent_url=recorded['torrent_url'],
            published_at=parsed,
        ))
    async with engine.connect() as reader:
        row = (await reader.execute(select(
            FileResource.published_at, FileResource.title_raw,
        ).where(FileResource.id == resource_id))).one()
    assert row.title_raw == recorded['title_raw']
    assert row.published_at == datetime.fromisoformat(expected)
    assert row.published_at.tzinfo is None
    assert parsed == row.published_at
    assert parsed.tzinfo is None


@pytest.mark.parametrize('source', ['mikan_namespace', 'field_mapping'])
@pytest.mark.parametrize('incoming,expected', CASES)
async def test_postgres_publication_commits_exact_utc(dedup_postgres, source, incoming, expected):
    await check_publication(dedup_postgres[0], source, incoming, expected)


@pytest.mark.parametrize('source', ['mikan_namespace', 'field_mapping'])
@pytest.mark.parametrize('incoming,expected', CASES)
async def test_turso_publication_commits_exact_utc(dedup_turso, source, incoming, expected):
    await check_publication(dedup_turso[0], source, incoming, expected)


@pytest.mark.parametrize('source', ['mikan_namespace', 'field_mapping'])
def test_invalid_publication_stays_missing(source):
    assert parse_time(source, 'not a timestamp') is None


def test_standard_rss_timezone_is_already_normalized_by_feedparser():
    import feedparser

    feed = feedparser.parse(b'''<rss version="2.0"><channel><title>synthetic UTC boundary</title>
        <item><title>sample</title><pubDate>Sun, 01 Nov 2026 01:30:00 -0500</pubDate></item>
        </channel></rss>''')
    assert _extract_published_at(feed.entries[0]) == datetime(2026, 11, 1, 6, 30)
