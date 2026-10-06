"""Actual loopback HTTP for administrator endpoints; protocol data synthetic.

These clients intentionally allow private services and do not follow redirects.
The notification body includes the parsed listing of an existing captured torrent.
"""

import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.models.download_notification import DownloadNotification
from app.models.movie import Movie
from app.models.webhook_delivery import WebhookDelivery
from app.services import media_server_client, notify_service, wigolo_client
from app.services.torrent_inspect import parse_torrent_files
from tests.integration.organize.test_notify_service_coverage import _seed_chain


@pytest.fixture
def admin_http(monkeypatch):
    # Real sockets, with no inherited developer proxy affecting local routing.
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(key, raising=False)
    visits = []
    state = {"redirect": False}

    class Handler(BaseHTTPRequestHandler):
        def respond(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            visits.append((self.server.server_port, self.path, dict(self.headers), body))
            if self.server is initial and state["redirect"]:
                self.send_response(307)
                self.send_header("Location", f"http://127.0.0.1:{target.server_port}/received")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            raw = json.dumps({"MediaContainer": {"version": "synthetic-1"},
                              "Version": "synthetic-1", "results": [
                                  {"title": "Synthetic hit", "url": "https://example.org", "snippet": "test"},
                              ]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            self.respond()

        def do_POST(self):
            self.respond()

        def log_message(self, *args):
            pass

    initial = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    target = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    servers = [initial, target]
    threads = [threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
               for s in servers]
    for thread in threads:
        thread.start()
    try:
        yield SimpleNamespace(url=f"http://127.0.0.1:{initial.server_port}",
                              port=initial.server_port, visits=visits, state=state)
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=2)
            assert not thread.is_alive()


@pytest.mark.parametrize("kind", ["plex", "emby", "jellyfin", "wigolo"])
@pytest.mark.parametrize("redirected", [False, True])
async def test_admin_private_services_do_not_follow_redirects(admin_http, monkeypatch, kind, redirected):
    admin_http.state["redirect"] = redirected
    if kind == "wigolo":
        monkeypatch.setattr(wigolo_client, "runtime_config", SimpleNamespace(
            wigolo_base_url=admin_http.url, wigolo_api_token="synthetic-token",
        ))
        if redirected:
            # Existing client rejects the empty redirect response at JSON decode.
            # This assertion concerns destination isolation, not error classification.
            with pytest.raises(json.JSONDecodeError):
                await wigolo_client.web_search("synthetic query")
        else:
            hits = await wigolo_client.web_search("synthetic query")
            assert hits == [{"title": "Synthetic hit", "url": "https://example.org", "text": "test"}]
    else:
        server = SimpleNamespace(type=kind, url=admin_http.url, token="synthetic-token")
        client = {"plex": media_server_client.PlexClient, "emby": media_server_client.EmbyClient,
                  "jellyfin": media_server_client.JellyfinClient}[kind](server)
        ok, detail = await client.test_connection()
        assert ok is not redirected
        if not redirected:
            assert detail == "synthetic-1"
    assert len(admin_http.visits) == 1
    port, path, headers, body = admin_http.visits[0]
    assert port == admin_http.port
    if kind == "wigolo":
        assert headers["Authorization"] == "Bearer synthetic-token"
        assert json.loads(body)["query"] == "synthetic query"
    elif kind == "emby":
        assert "api_key=synthetic-token" in path
    else:
        assert headers["X-Plex-Token" if kind == "plex" else "X-Emby-Token"] == "synthetic-token"


@pytest.mark.parametrize("redirected", [False, True])
async def test_private_webhook_delivery_does_not_forward_snapshot(db_session, admin_http, redirected):
    admin_http.state["redirect"] = redirected
    chain = await _seed_chain(db_session, work=Movie(id=str(uuid.uuid4()), title_cn="Synthetic movie"), mock=False)
    chain.webhook.url = admin_http.url + "/hook"
    torrent = (Path(__file__).resolve().parents[2] / "fixtures/metadata_corpus_v1/torrents" /
               "987a72c09d5b0c2e934fa5016cc4dda6427a80dc5a4594e284a06ccf966acdb2.torrent")
    payload = {"version": 2, "files": parse_torrent_files(str(torrent)), "test": "synthetic notification envelope"}
    assert payload["files"]
    notification = DownloadNotification(id=str(uuid.uuid4()), agent_id=chain.agent.id,
                                        download_task_id=chain.task.id, payload=payload)
    db_session.add(notification)
    await db_session.flush()
    delivery = WebhookDelivery(id=str(uuid.uuid4()), notification_id=notification.id, webhook_id=chain.webhook.id)
    db_session.add(delivery)
    await db_session.commit()
    stats = await notify_service.deliver_due_deliveries(db_session)
    await db_session.refresh(delivery)
    assert stats == {"delivered": 0 if redirected else 1, "failed": 0, "skipped": 1 if redirected else 0}
    assert delivery.status == ("pending" if redirected else "done")
    assert delivery.attempt_count == (1 if redirected else 0)
    if redirected:
        assert delivery.next_attempt_at is not None
    assert len(admin_http.visits) == 1
    port, path, _, body = admin_http.visits[0]
    assert (port, path) == (admin_http.port, "/hook")
    assert json.loads(body) == {"event": "download.completed", "notification": payload}
