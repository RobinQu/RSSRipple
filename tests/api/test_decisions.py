"""API tests for PendingDecision endpoints."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest


def _uuid():
    return str(uuid.uuid4())


TEST_FIELD_MAPPING = {
    "list_locator": {"source": "entries"},
    "field_mappings": {"torrent_url": {"source": "link"}},
}


@pytest.fixture
async def setup(client):
    with patch("app.api.v1.channels.validate_rss_url", AsyncMock(return_value=(True, "ok", 5, 5))):
        ch = await client.post("/api/v1/channels", json={
            "name": "C", "type": "rss_feed",
            "url": "https://example.com/rss", "fetch_interval": 1800,
            "field_mapping": TEST_FIELD_MAPPING,
            "metadata_agent_enabled": False,
        })
    dl = await client.post("/api/v1/downloaders", json={
        "name": "DL", "type": "transmission",
        "url": "http://127.0.0.1:9091/transmission/rpc",
        "download_dir": "/downloads/rssripple",
    })
    a = await client.post("/api/v1/agents", json={
        "name": "A", "channel_id": ch.json()["data"]["id"],
        "downloader_id": dl.json()["data"]["id"], "scope_channel_wide": True,
    })
    return ch.json()["data"]["id"], dl.json()["data"]["id"], a.json()["data"]["id"]


async def _create_resource(db_session_factory, ch_id, title_raw, **kw):
    from app.models.file_resource import FileResource
    rid = _uuid()
    async with db_session_factory() as s:
        r = FileResource(
            id=rid, channel_id=ch_id, guid=_uuid(),
            title_raw=title_raw, search_title=kw.get("search_title", title_raw),
            torrent_url=kw.get("torrent_url", f"magnet:?xt=urn:btih:{rid}"),
            parsed_at=datetime.now(UTC),
            **{k: v for k, v in kw.items() if k not in ("search_title", "torrent_url")},
        )
        s.add(r)
        await s.commit()
    return {"id": rid, "title_raw": title_raw}


async def _make_decision(db_session_factory, agent_id, r1_id, r2_id, picked_id=None):
    from app.models.pending_decision import PendingDecision
    async with db_session_factory() as s:
        pd = PendingDecision(
            id=_uuid(), agent_id=agent_id, status="pending",
            candidates=[r1_id, r2_id],
            reason="冲突",
            llm_picked_resource_id=picked_id,
            expires_at=datetime.now(UTC) + timedelta(days=7),
        )
        s.add(pd)
        await s.commit()
        return pd.id


class TestDecisions:
    async def test_list_decisions(self, client, setup, db_session_factory):
        ch, dl, aid = setup
        r1 = await _create_resource(db_session_factory, ch, "[G] ShowA - 01")
        r2 = await _create_resource(db_session_factory, ch, "[G2] ShowA - 01")
        await _make_decision(db_session_factory, aid, r1["id"], r2["id"])
        res = await client.get(f"/api/v1/agents/{aid}/decisions")
        assert res.status_code == 200
        assert res.json()["meta"]["total"] >= 1

    async def test_list_filter_status(self, client, setup, db_session_factory):
        ch, dl, aid = setup
        r1 = await _create_resource(db_session_factory, ch, "[G] XA - 01")
        r2 = await _create_resource(db_session_factory, ch, "[G2] XA - 01")
        await _make_decision(db_session_factory, aid, r1["id"], r2["id"])
        res = await client.get(f"/api/v1/agents/{aid}/decisions?status=decided")
        assert res.status_code == 200
        assert res.json()["meta"]["total"] == 0

    async def test_confirm_invalid_id_404(self, client, setup):
        res = await client.post("/api/v1/decisions/nope/confirm", json={"resource_id": "x"})
        assert res.status_code == 404

    async def test_skip_404(self, client):
        res = await client.post("/api/v1/decisions/nope/skip")
        assert res.status_code == 404

    async def test_confirm_bad_resource_id_returns_400(self, client, setup, db_session_factory):
        ch, dl, aid = setup
        r1 = await _create_resource(db_session_factory, ch, "[G] YA - 01")
        r2 = await _create_resource(db_session_factory, ch, "[G2] YA - 01")
        did = await _make_decision(db_session_factory, aid, r1["id"], r2["id"])
        res = await client.post(f"/api/v1/decisions/{did}/confirm",
                                json={"resource_id": "not-a-candidate"})
        assert res.status_code == 400

    async def test_skip_marks_skipped(self, client, setup, db_session_factory, mock_transmission):
        ch, dl, aid = setup
        r1 = await _create_resource(db_session_factory, ch, "[G] ZA - 01")
        r2 = await _create_resource(db_session_factory, ch, "[G2] ZA - 01")
        did = await _make_decision(db_session_factory, aid, r1["id"], r2["id"])
        res = await client.post(f"/api/v1/decisions/{did}/skip")
        assert res.status_code == 200
        assert res.json()["data"]["status"] == "skipped"

    async def test_confirm_dispatches_download(self, client, setup, db_session_factory, mock_transmission):
        ch, dl, aid = setup
        r1 = await _create_resource(db_session_factory, ch, "[G] CA - 01")
        r2 = await _create_resource(db_session_factory, ch, "[G2] CA - 01")
        did = await _make_decision(db_session_factory, aid, r1["id"], r2["id"])
        res = await client.post(f"/api/v1/decisions/{did}/confirm",
                                json={"resource_id": r1["id"]})
        assert res.status_code == 200
        assert res.json()["data"]["status"] == "decided"
        # confirm should have dispatched download; add_torrent was called
        mock_transmission.add_torrent.assert_awaited()

    async def test_ai_pick_dispatches_picked(self, client, setup, db_session_factory, mock_transmission):
        """ai-pick uses the cached llm_picked_resource_id and dispatches it."""
        ch, dl, aid = setup
        r1 = await _create_resource(db_session_factory, ch, "[G] AI1 - 01")
        r2 = await _create_resource(db_session_factory, ch, "[G2] AI1 - 01")
        did = await _make_decision(db_session_factory, aid, r1["id"], r2["id"], picked_id=r1["id"])
        res = await client.post(f"/api/v1/decisions/{did}/ai-pick")
        assert res.status_code == 200
        assert res.json()["data"]["status"] == "decided"
        assert res.json()["data"]["decided_resource_id"] == r1["id"]
        mock_transmission.add_torrent.assert_awaited()

    async def test_ai_pick_no_pick_returns_400(self, client, setup, db_session_factory, mock_transmission):
        """When there's no cached pick and the LLM can't decide, returns 400."""
        ch, dl, aid = setup
        r1 = await _create_resource(db_session_factory, ch, "[G] AI2 - 01")
        r2 = await _create_resource(db_session_factory, ch, "[G2] AI2 - 01")
        did = await _make_decision(db_session_factory, aid, r1["id"], r2["id"], picked_id=None)
        # Agent created via setup has llm_enabled=False → _generate_llm_pick returns (None, None).
        res = await client.post(f"/api/v1/decisions/{did}/ai-pick")
        assert res.status_code == 400
        assert res.json()["error"]["code"] == "LLM_NO_PICK"

    async def test_batch_skip(self, client, setup, db_session_factory):
        ch, dl, aid = setup
        r1 = await _create_resource(db_session_factory, ch, "[G] BS1 - 01")
        r2 = await _create_resource(db_session_factory, ch, "[G2] BS1 - 01")
        r3 = await _create_resource(db_session_factory, ch, "[G] BS2 - 01")
        r4 = await _create_resource(db_session_factory, ch, "[G2] BS2 - 01")
        d1 = await _make_decision(db_session_factory, aid, r1["id"], r2["id"])
        d2 = await _make_decision(db_session_factory, aid, r3["id"], r4["id"])
        res = await client.post(f"/api/v1/agents/{aid}/decisions/batch", json={
            "decision_ids": [d1, d2], "action": "skip",
        })
        assert res.status_code == 200
        data = res.json()["data"]
        assert data["processed"] == 2
        assert data["skipped"] == 2

    async def test_batch_ai_dispatches(self, client, setup, db_session_factory, mock_transmission):
        ch, dl, aid = setup
        r1 = await _create_resource(db_session_factory, ch, "[G] BA1 - 01")
        r2 = await _create_resource(db_session_factory, ch, "[G2] BA1 - 01")
        r3 = await _create_resource(db_session_factory, ch, "[G] BA2 - 01")
        r4 = await _create_resource(db_session_factory, ch, "[G2] BA2 - 01")
        d1 = await _make_decision(db_session_factory, aid, r1["id"], r2["id"], picked_id=r1["id"])
        d2 = await _make_decision(db_session_factory, aid, r3["id"], r4["id"], picked_id=r3["id"])
        res = await client.post(f"/api/v1/agents/{aid}/decisions/batch", json={
            "decision_ids": [d1, d2], "action": "ai",
        })
        assert res.status_code == 200
        data = res.json()["data"]
        assert data["dispatched"] == 2
        assert data["failed"] == 0

    async def test_batch_rejects_bad_action(self, client, setup):
        ch, dl, aid = setup
        res = await client.post(f"/api/v1/agents/{aid}/decisions/batch", json={
            "decision_ids": [], "action": "bogus",
        })
        assert res.status_code == 422
