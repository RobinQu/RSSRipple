"""Tests for :class:`MockDownloaderWrapper`.

Uses monkey-patched random duration so the whole suite stays fast.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.clients import mock_downloader as md
from app.clients.downloader import get_downloader_client


def _dl(dl_id: str = "dl-test", type_: str = "mock") -> SimpleNamespace:
    return SimpleNamespace(
        id=dl_id,
        name="mock",
        type=type_,
        url="mock://local",
        username=None,
        password=None,
        download_dir="/tmp/mock-downloads",
    )


@pytest.fixture(autouse=True)
def _reset_state():
    md.reset_state()
    yield
    md.reset_state()


@pytest.fixture(autouse=True)
def _fast_random(monkeypatch):
    """Keep the whole suite quick by forcing a short duration."""
    monkeypatch.setattr(md.random, "uniform", lambda a, b: 0.05)


async def test_test_connection_always_succeeds():
    w = md.MockDownloaderWrapper(downloader=_dl())
    ok, msg = await w.test_connection()
    assert ok is True
    assert msg


async def test_add_torrent_returns_immediately():
    w = md.MockDownloaderWrapper(downloader=_dl())
    result = await w.add_torrent("magnet:?xt=urn:btih:abc&dn=Sample.torrent",
                                 download_dir="/tmp/x")
    assert isinstance(result["torrent_id"], int)
    assert result["name"] == "Sample.torrent"
    assert result["hash"]


async def test_progress_reaches_completion():
    w = md.MockDownloaderWrapper(downloader=_dl())
    added = await w.add_torrent("magnet:?xt=urn:btih:done&dn=done.torrent")
    # Wait until the simulated duration has elapsed
    await asyncio.sleep(0.15)
    t = await w.get_torrent(added["torrent_id"])
    assert t["is_finished"] is True
    assert t["percent_done"] == 1.0
    assert t["left_until_done"] == 0


async def test_pause_freezes_progress():
    w = md.MockDownloaderWrapper(downloader=_dl())
    added = await w.add_torrent("magnet:?xt=urn:btih:pause&dn=pause")
    await asyncio.sleep(0.02)
    await w.pause_torrent(added["torrent_id"])
    t1 = await w.get_torrent(added["torrent_id"])
    await asyncio.sleep(0.1)
    t2 = await w.get_torrent(added["torrent_id"])
    assert t1["percent_done"] == pytest.approx(t2["percent_done"], abs=0.01)
    assert t2["status"] == "stopped"


async def test_resume_unfreezes_progress():
    w = md.MockDownloaderWrapper(downloader=_dl())
    added = await w.add_torrent("magnet:?xt=urn:btih:resume&dn=resume")
    await w.pause_torrent(added["torrent_id"])
    paused_snapshot = await w.get_torrent(added["torrent_id"])
    await asyncio.sleep(0.02)
    await w.resume_torrent(added["torrent_id"])
    await asyncio.sleep(0.08)
    t = await w.get_torrent(added["torrent_id"])
    assert t["is_finished"] is True
    assert t["percent_done"] > paused_snapshot["percent_done"]


async def test_remove_torrent():
    w = md.MockDownloaderWrapper(downloader=_dl())
    added = await w.add_torrent("magnet:?xt=urn:btih:rm&dn=rm")
    assert await w.remove_torrent(added["torrent_id"]) is True
    with pytest.raises(ValueError):
        await w.get_torrent(added["torrent_id"])


async def test_list_torrents_isolated_per_downloader():
    w1 = md.MockDownloaderWrapper(downloader=_dl(dl_id="dl-1"))
    w2 = md.MockDownloaderWrapper(downloader=_dl(dl_id="dl-2"))
    await w1.add_torrent("magnet:?dn=a")
    await w2.add_torrent("magnet:?dn=b")
    l1 = await w1.list_torrents()
    l2 = await w2.list_torrents()
    assert len(l1) == 1 and len(l2) == 1
    assert l1[0]["name"] != l2[0]["name"] or l1 is not l2


async def test_free_space_returns_positive():
    w = md.MockDownloaderWrapper(downloader=_dl())
    space = await w.free_space("/tmp")
    assert space > 0


async def test_factory_returns_mock_wrapper():
    """`get_downloader_client` must pick MockDownloaderWrapper for type='mock'."""
    dl = _dl(type_="mock")
    client = get_downloader_client(dl)
    assert isinstance(client, md.MockDownloaderWrapper)


async def test_factory_falls_back_to_transmission_for_other_types():
    from app.clients.transmission import TransmissionWrapper
    dl = _dl(type_="transmission")
    dl.url = "http://127.0.0.1:9091/transmission/rpc"
    client = get_downloader_client(dl)
    assert isinstance(client, TransmissionWrapper)
