"""A real closed TCP route denies Agent progress and stops lease renewal."""

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import database
from app.models.agent_run_lease import AgentRunLease
from app.services import agent_run_execution as execution
from app.services.agent_run_lifecycle import renew_lease


class DatabaseRoute:
    """Forward real protocol bytes, then disconnect every established socket."""

    def __init__(self, host, port):
        self.host, self.port = host, port
        self.connections = set()
        self.tasks = set()
        self.server = None

    async def start(self):
        self.server = await asyncio.start_server(self.forward, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    async def forward(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        peer_writer = None
        pumps = []
        self.connections.add(writer)
        try:
            peer_reader, peer_writer = await asyncio.open_connection(self.host, self.port)
            self.connections.add(peer_writer)

            async def copy(source, destination):
                while data := await source.read(65536):
                    destination.write(data)
                    await destination.drain()

            pumps = [asyncio.create_task(copy(reader, peer_writer)),
                     asyncio.create_task(copy(peer_reader, writer))]
            await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for pump in pumps:
                pump.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)
            for connection in [writer, peer_writer]:
                if connection is not None:
                    connection.close()
                    await asyncio.gather(connection.wait_closed(), return_exceptions=True)
                    self.connections.discard(connection)
            self.tasks.discard(task)

    async def close(self):
        if self.server is not None:
            self.server.close()
        # Python 3.13 Server.wait_closed also waits for accepted transports.
        # Close those first, and bound cleanup independently of test assertions.
        for connection in list(self.connections):
            connection.close()
        async with asyncio.timeout(5):
            if self.tasks:
                await asyncio.gather(*list(self.tasks), return_exceptions=True)
            if self.server is not None:
                await self.server.wait_closed()


@pytest.mark.parametrize("lifecycle_backend", ["work_fk_postgres"], indirect=True)
async def test_database_route_loss_denies_checkpoint_and_renewal(lifecycle_db, monkeypatch):
    engine, factory, owner, _, _ = lifecycle_db
    route = DatabaseRoute(engine.url.host, engine.url.port)
    port = await route.start()
    proxied = create_async_engine(engine.url.set(host="127.0.0.1", port=port))
    proxied_factory = async_sessionmaker(proxied, expire_on_commit=False)
    stopped, begin_heartbeat = asyncio.Event(), asyncio.Event()
    heartbeat = execution._heartbeat

    async def observe_heartbeat(*args):
        try:
            await begin_heartbeat.wait()
            return await heartbeat(*args)
        finally:
            stopped.set()

    monkeypatch.setattr(execution, "_heartbeat", observe_heartbeat)
    monkeypatch.setattr(execution, "HEARTBEAT_SECONDS", 0.05)
    monkeypatch.setattr(database, "async_session_factory", proxied_factory)
    try:
        async with factory() as db:
            assert await renew_lease(db, owner, seconds=5)
            await db.commit()
            deadline = await db.scalar(select(AgentRunLease.expires_at_epoch).where(
                AgentRunLease.run_id == owner.run_id,
            ))
        async with execution.maintain_run_lease(owner):
            try:
                # Initial ownership check traversed the live TCP proxy successfully.
                assert route.connections
                await route.close()
                await proxied.dispose()
                begin_heartbeat.set()
                with pytest.raises((SQLAlchemyError, OSError)):
                    await execution.require_agent_execution_ownership()
                await asyncio.wait_for(stopped.wait(), 5)
                async with factory() as observer:
                    lease = await observer.scalar(select(AgentRunLease).where(AgentRunLease.run_id == owner.run_id))
                    assert lease.token == owner.token and lease.expires_at_epoch == deadline
                # The underlying database is healthy; only this execution's route
                # failed. It must not reinterpret unknown ownership as permission.
                with pytest.raises((SQLAlchemyError, OSError)):
                    await execution.require_agent_execution_ownership()
            finally:
                begin_heartbeat.set()
    finally:
        await route.close()
        await proxied.dispose()
