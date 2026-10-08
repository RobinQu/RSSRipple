"""Recorded channel text; synthetic writes through actual UTC defaults."""
import hashlib
import json
import uuid
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import insert, select, text, update

from app.models.channel import Channel
from app.utils.time import utcnow


def channel_values():
    raw = (Path(__file__).parents[2] / 'fixtures/prod_works_v1.json').read_bytes()
    assert hashlib.sha256(raw).hexdigest() == 'd11651d2162ced23e8d919af0bff2d9f316e203234cc854909ba5f444a35ec32'
    recorded = json.loads(raw)['tables']['channels'][0]
    return {'id': str(uuid.uuid4()), 'name': recorded['name'], 'url': recorded['url'],
            'field_mapping': recorded['field_mapping'], 'last_fetched_at': utcnow()}


async def check_storage(engine, zone):
    values = channel_values()
    async with engine.begin() as conn:
        if engine.dialect.name == 'postgresql':
            await conn.execute(text("SELECT set_config('TimeZone', :zone, true)"), {'zone': zone})
        await conn.execute(insert(Channel).values(**values))
        await conn.execute(update(Channel).where(Channel.id == values['id']).values(name=values['name']+' [synthetic]'))
        reference = await conn.scalar(text("SELECT CURRENT_TIMESTAMP AT TIME ZONE 'UTC'"
                                           if engine.dialect.name == 'postgresql' else 'SELECT CURRENT_TIMESTAMP'))
    if isinstance(reference, str):
        reference = datetime.fromisoformat(reference)
    async with engine.connect() as reader:
        row = (await reader.execute(select(Channel.__table__).where(Channel.id == values['id']))).mappings().one()
    for field in ['created_at', 'updated_at', 'last_fetched_at']:
        assert row[field].tzinfo is None
        assert abs((row[field]-reference).total_seconds()) < 5, (zone, field, row[field], reference)


@pytest.mark.parametrize('zone', ['UTC', 'Asia/Shanghai', 'America/New_York'])
async def test_postgres_defaults_and_updates_are_utc(dedup_postgres, zone):
    await check_storage(dedup_postgres[0], zone)


async def test_turso_defaults_and_updates_remain_utc(dedup_turso):
    await check_storage(dedup_turso[0], 'UTC')
