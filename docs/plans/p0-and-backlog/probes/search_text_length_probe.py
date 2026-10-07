"""Actual DB write boundaries using captured titles and synthetic long aliases."""
import asyncio
import hashlib
import json
import os
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.database import Base, apply_db_pragmas
from app.models.audio_work import AudioWork
from app.models.movie import Movie
from app.models.series import TVSeries
from app.models.work_collection import WorkCollection
from app.services.text_normalizer import normalize_title
from app.services.work_search_events import build_search_text


async def main():
    fixture = Path('tests/fixtures/prod_works_v1.json')
    digest = hashlib.sha256(fixture.read_bytes()).hexdigest()
    assert digest == 'd11651d2162ced23e8d919af0bff2d9f316e203234cc854909ba5f444a35ec32'
    data = json.loads(fixture.read_text())['tables']
    url = os.environ['DATABASE_URL']
    engine = create_async_engine(url)
    apply_db_pragmas(engine)
    results = []
    observed = {}
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        for model in [Movie, TVSeries, AudioWork, WorkCollection]:
            rows = data.get(model.__tablename__, [])
            observed[model.__tablename__] = {
                'recorded_rows': len(rows),
                'max_normalized_search_length': max((len(build_search_text(model(**{
                    key: row.get(key) for key in ['title_cn', 'title_en', 'aliases']
                }, **({'original_title': row.get('original_title')}
                       if model is not WorkCollection else {})))) for row in rows), default=0),
            }
            # No captured audio rows: use a captured movie title as a label,
            # without pretending its media type or alias set is real.
            title = (rows or data['movies'])[0]['title_cn']
            for operation in ['insert', 'update']:
                for length in [4095, 4096, 4097, 8192]:
                    alias = 'a' * (length - len(normalize_title(title)) - 10) + ' tailword'
                    row = model(title_cn=title, aliases=[alias])
                    assert len(build_search_text(row)) == length
                    async with factory() as db:
                        if model is TVSeries:
                            parent = WorkCollection(title_cn=title)
                            db.add(parent)
                            await db.commit()
                            row.collection_id = parent.id
                        if operation == 'update':
                            row.aliases = ['short']
                            db.add(row)
                            await db.commit()
                            row.aliases = [alias]
                        else:
                            db.add(row)
                        result = {'table': model.__tablename__, 'operation': operation,
                                  'length': length}
                        try:
                            await db.commit()
                            identity = row.id
                            async with factory() as read:
                                stored = await read.get(model, identity)
                                assert stored.aliases == [alias]
                                assert stored.search_text == build_search_text(stored)
                                assert len(stored.search_text) == length
                                assert stored.search_text.endswith(' tailword')
                            result['ok'] = True
                        except Exception as error:
                            await db.rollback()
                            result.update(ok=False, error_type=type(error).__name__,
                                          sqlstate=getattr(getattr(error, 'orig', None), 'sqlstate', None),
                                          message=str(error).split('[SQL:', 1)[0][:400])
                        results.append(result)
    finally:
        await engine.dispose()
    output = {'fixture_sha256': digest, 'backend': engine.dialect.name,
              'recorded_inventory': observed, 'cases': results,
              'passed': sum(r['ok'] for r in results),
              'failed': sum(not r['ok'] for r in results)}
    Path(os.environ['PROBE_RESULT']).write_text(json.dumps(output, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps({k: output[k] for k in ['backend', 'passed', 'failed']}))
    assert output['failed'] == 0, 'Valid alias writes must preserve the complete search text'


if __name__ == '__main__':
    asyncio.run(main())
