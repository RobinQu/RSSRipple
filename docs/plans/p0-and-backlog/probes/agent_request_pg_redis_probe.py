"""Disposable PostgreSQL + Redis: real processes, version races and restart."""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

os.environ.setdefault('ORGANIZE_LOCK_DIR', tempfile.mkdtemp(prefix='rssripple-replay-locks-'))
os.environ.setdefault('POSTER_CACHE_DIR', tempfile.mkdtemp(prefix='rssripple-replay-posters-'))

import redis.asyncio as redis  # noqa: E402
from sqlalchemy import MetaData, inspect, select  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

import app.database as database  # noqa: E402
import app.models  # noqa: E402,F401
from app.job_handlers import _handle_run_agent  # noqa: E402
from app.models.agent_resource_request import AgentResourceRequest  # noqa: E402
from app.models.agent_run import AgentRun  # noqa: E402
from app.services import agent_resource_requests as requests  # noqa: E402
from app.services import agent_service, task_queue  # noqa: E402
from app.services.task_queue import RedisQueue  # noqa: E402
from tests.integration.organize.test_agent_request_failures import seed  # noqa: E402


async def eventually(predicate):
    async with asyncio.timeout(30):
        while True:
            value = await predicate()
            if value:
                return value
            await asyncio.sleep(0.05)


async def main():
    url = make_url(os.environ['DATABASE_URL'])
    assert url.host == '127.0.0.1' and url.database == url.username == url.password == 'organize_test'
    redis_url = os.environ['REDIS_URL']
    assert urlparse(redis_url).hostname == '127.0.0.1'
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    database.engine, database.async_session_factory = engine, factory
    control = redis.from_url(redis_url, decode_responses=True)
    queue = None
    children = []
    mode = sys.argv[1] if len(sys.argv) > 1 else 'main'

    async def spawn(*args):
        process = await asyncio.create_subprocess_exec(sys.executable, __file__, *args)
        children.append(process)
        return process

    try:
        if mode == 'write':
            for _ in range(3):
                async with factory() as db:
                    await requests.request_resources(db, [sys.argv[2]], [sys.argv[3]])
                    await db.commit()
            return
        queue = RedisQueue(redis_url=redis_url)
        task_queue.task_queue = queue
        if mode == 'worker':
            real_process = agent_service.process_resources

            async def pause_selected(*args, **kwargs):
                await control.rpush('probe:selected', str(os.getpid()))
                assert await control.blpop('probe:finish', timeout=30)
                return await real_process(*args, **kwargs)

            agent_service.process_resources = pause_selected
            queue.register('run_agent', _handle_run_agent)
            await queue.start()
            await control.set(f'probe:ready:{os.getpid()}', '1')
            assert await control.blpop('probe:stop', timeout=60)
            return
        await queue.start(consume=False)
        if mode == 'dispatch':
            real_wake = requests.wake_agents

            async def pause_dispatch(*args, **kwargs):
                await control.set(f'probe:dispatch-ready:{os.getpid()}', '1')
                assert await control.blpop('probe:dispatch-release', timeout=30)
                return await real_wake(*args, **kwargs)

            requests.wake_agents = pause_dispatch
            await requests.dispatch_pending_requests()
            return
        assert await control.dbsize() == 0, 'Requires a fresh disposable Redis'
        async with engine.begin() as connection:
            await connection.run_sync(database.Base.metadata.drop_all)
            legacy = MetaData()
            for table in database.Base.metadata.sorted_tables:
                if table.name != 'agent_resource_requests':
                    table.to_metadata(legacy)
            await connection.run_sync(legacy.create_all)
            assert not await connection.run_sync(lambda c: inspect(c).has_table('agent_resource_requests'))
            await connection.run_sync(database.Base.metadata.create_all)
            await database._apply_light_migrations(connection)
        async with factory() as db:
            chain = await seed(db, Path(tempfile.mkdtemp(prefix='rssripple-replay-seed-')))
            agent_id, resource_id = chain.agent.id, chain.resource.id
        writers = [await spawn('write', agent_id, resource_id) for _ in range(2)]
        assert await asyncio.gather(*(p.wait() for p in writers)) == [0, 0]
        async with factory() as db:
            [original] = await requests.snapshot_requests(db, agent_id)
            assert original.revision == 6
            await requests.request_resources(db, [agent_id], [resource_id])
            await db.commit()
            await requests.acknowledge_requests(db, [original])
            await db.commit()
            [current] = await requests.snapshot_requests(db, agent_id)
            assert current.id == original.id and current.revision == 7
        async with engine.begin() as connection:
            await connection.run_sync(database.Base.metadata.create_all)
            await database._apply_light_migrations(connection)
        async with factory() as db:
            assert await requests.snapshot_requests(db, agent_id) == [current]

        workers = [await spawn('worker') for _ in range(2)]
        for p in workers:
            await eventually(lambda p=p: control.get(f'probe:ready:{p.pid}'))
        dispatchers = [await spawn('dispatch') for _ in range(2)]
        for p in dispatchers:
            await eventually(lambda p=p: control.get(f'probe:dispatch-ready:{p.pid}'))
        await control.rpush('probe:dispatch-release', '1', '1')
        assert await asyncio.gather(*(p.wait() for p in dispatchers)) == [0, 0]
        await eventually(lambda: control.llen('probe:selected'))
        assert await control.llen('probe:selected') == 1
        async with factory() as db:
            assert len((await db.scalars(select(AgentRun))).all()) == 1
            await requests.request_resources(db, [agent_id], [resource_id])
            await db.commit()
        await requests.dispatch_pending_requests()  # Busy key: new revision remains durable.
        first = await queue.status(f'agent:{agent_id}')
        await control.rpush('probe:finish', '1')

        async def done(job_id):
            state = await queue.status(f'agent:{agent_id}')
            assert not state or state['status'] != 'failed', state
            return state if state and state['job_id'] == job_id and state['status'] == 'done' else None

        await eventually(lambda: done(first['job_id']))
        async with factory() as db:
            [remaining] = await requests.snapshot_requests(db, agent_id)
            assert remaining.id == original.id and remaining.revision == 8
        await control.rpush('probe:stop', '1', '1')
        assert await asyncio.gather(*(p.wait() for p in workers)) == [0, 0]
        replacement = await spawn('worker')
        await eventually(lambda: control.get(f'probe:ready:{replacement.pid}'))
        await requests.dispatch_pending_requests()
        second = await queue.status(f'agent:{agent_id}')
        assert second['job_id'] != first['job_id']

        async def second_selected():
            return await control.llen('probe:selected') == 2

        await eventually(second_selected)
        await control.rpush('probe:finish', '1')
        await eventually(lambda: done(second['job_id']))
        async with factory() as db:
            assert await db.scalar(select(AgentResourceRequest.id)) is None
            runs = (await db.scalars(select(AgentRun))).all()
            assert len(runs) == 2 and all(run.total_resources == 1 and run.status == 'success' for run in runs)
        await control.rpush('probe:stop', '1')
        assert await replacement.wait() == 0
        print(json.dumps({'backend': 'PostgreSQL + Redis', 'writer_processes': 2, 'writes': 6,
                          'dispatch_processes': 2, 'initial_worker_processes': 2,
                          'active_jobs_during_race': 1, 'new_revision_preserved': 8,
                          'replacement_worker_completed': True, 'runs': 2, 'pending_requests': 0,
                          'old_schema_upgrade_repeated': True, 'fixture': 'synthetic unlinked resource, no RPC'}))
    finally:
        if children:
            await control.rpush('probe:finish', *(['1'] * len(children)))
            await control.rpush('probe:stop', *(['1'] * len(children)))
        for p in children:
            if p.returncode is None:
                try:
                    await asyncio.wait_for(p.wait(), 5)
                except TimeoutError:
                    p.terminate()
                    await p.wait()
        if queue is not None:
            await queue.stop()
        await control.aclose()
        await engine.dispose()


if __name__ == '__main__':
    asyncio.run(main())
