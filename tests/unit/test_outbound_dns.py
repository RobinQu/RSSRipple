"""Bounded resolver work and actual client timeout behavior, without network I/O."""

import asyncio
import socket
import threading

import httpcore
import httpx
import pytest

from app.clients import outbound_http


@pytest.mark.parametrize("asynchronous", [False, True])
def test_dns_timeout_returns_before_blocking_os_lookup_finishes(monkeypatch, asynchronous):
    release = threading.Event()
    finished = threading.Event()
    started = threading.Event()

    def blocked_lookup(*args, **kwargs):
        started.set()
        try:
            release.wait(timeout=5)
            raise socket.gaierror("synthetic late DNS failure")
        finally:
            finished.set()

    monkeypatch.setattr(socket, "getaddrinfo", blocked_lookup)

    async def get():
        async with outbound_http.async_client(timeout=.05) as client:
            await client.get("http://blocked-dns.test/")

    try:
        with pytest.raises(httpx.ConnectTimeout, match="DNS resolution timed out"):
            if asynchronous:
                asyncio.run(get())
            else:
                with outbound_http.client(timeout=.05) as client:
                    client.get("http://blocked-dns.test/")
        assert started.is_set()
        assert not finished.is_set()
    finally:
        release.set()
        assert finished.wait(timeout=2)


def test_pending_dns_work_is_bounded_until_os_lookups_finish(monkeypatch):
    release = threading.Event()

    def blocked_lookup(*args, **kwargs):
        release.wait(timeout=5)
        return []

    monkeypatch.setattr(socket, "getaddrinfo", blocked_lookup)
    futures = []
    done = []
    try:
        for _ in range(8):
            future = outbound_http._submit_resolution("blocked-dns.test", 80)
            futures.append(future)
            event = threading.Event()
            future.add_done_callback(lambda _, event=event: event.set())
            done.append(event)
        with pytest.raises(httpcore.ConnectError, match="resolver is busy"):
            outbound_http._submit_resolution("overflow.test", 80)
    finally:
        release.set()
        for event in done:
            assert event.wait(timeout=2)
    assert all(future.result() == [] for future in futures)
