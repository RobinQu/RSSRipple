"""RSS transport boundaries using unchanged captured magnet rows in feed XML."""

import base64
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from app.clients import outbound_http, rss_parser


@pytest.fixture
def feed_servers():
    payload = (Path(__file__).resolve().parents[2] / "fixtures/magnet_feed.xml").read_bytes()
    requests = []

    class Feed(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.server.server_port, self.path, self.headers.get("Authorization")))
            if self.path == "/same":
                self.send_response(302)
                self.send_header("Location", "/feed")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if self.path == "/cross":
                self.send_response(302)
                self.send_header("Location", target_origin + "/feed")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if self.path == "/failure":
                self.send_response(503)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = (b'<rss version="2.0"><channel><title>Synthetic empty feed</title></channel></rss>'
                    if self.path == "/empty" else payload)
            self.send_response(200)
            self.send_header("Content-Type", "application/rss+xml; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    target = ThreadingHTTPServer(("127.0.0.1", 0), Feed)
    initial = ThreadingHTTPServer(("127.0.0.1", 0), Feed)
    target_origin = f"http://127.0.0.1:{target.server_port}"
    servers = [initial, target]
    threads = [threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
               for s in servers]
    for thread in threads:
        thread.start()
    try:
        yield f"http://127.0.0.1:{initial.server_port}", target.server_port, requests, payload
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=2)
            assert not thread.is_alive()


@pytest.mark.parametrize("path", ["/feed", "/same"])
async def test_admin_private_feed_preserves_captured_entries(feed_servers, path):
    initial, _, _, _ = feed_servers
    items = await rss_parser.parse_rss_feed(initial + path)
    assert len(items) == 3
    assert items[0].title == "Mufasa The Lion King 2024 2160p DSNP WEB-DL MULTi Atmos DV HDR10+ H265-SomniWare"
    assert items[0].magnet_url.startswith("magnet:?xt=urn:btih:6D8205C10E4E907A80E6B1DA7754BCEEF2B769FC")


async def test_feed_redirect_cannot_authorize_another_private_origin(feed_servers):
    initial, target_port, requests, _ = feed_servers
    with pytest.raises(outbound_http.DestinationDenied):
        await rss_parser.parse_rss_feed(initial + "/cross")
    assert not any(port == target_port for port, _, _ in requests), requests
    assert len(requests) == 1


@pytest.mark.parametrize("as_uri", [False, True])
async def test_feed_url_cannot_read_local_files(feed_servers, tmp_path, as_uri):
    _, _, _, payload = feed_servers
    path = tmp_path / "private-feed.xml"
    path.write_bytes(payload)
    with pytest.raises(outbound_http.DestinationDenied):
        await rss_parser.parse_rss_feed(path.as_uri() if as_uri else str(path))


async def test_admin_basic_auth_survives_same_origin_redirect(feed_servers):
    initial, _, requests, _ = feed_servers
    url = initial.replace("http://", "http://synthetic-user:synthetic-pass@") + "/same"
    items = await rss_parser.parse_rss_feed(url)
    assert len(items) == 3
    expected = "Basic " + base64.b64encode(b"synthetic-user:synthetic-pass").decode()
    assert len(requests) == 2
    assert all(auth == expected for _, _, auth in requests)


async def test_admin_basic_auth_not_forwarded_to_other_public_origin(feed_servers, monkeypatch):
    """Actual cross-origin redirect with a controlled DNS/dial mapping."""
    import socket

    import httpcore

    initial, target_port, requests, _ = feed_servers
    # Reuse the local redirect but use a synthetic public host for its target.
    from http.server import BaseHTTPRequestHandler

    real_send_header = BaseHTTPRequestHandler.send_header
    real_resolve = socket.getaddrinfo
    real_dial = httpcore.SyncBackend.connect_tcp

    def send_header(handler, keyword, value):
        if keyword.lower() == "location" and str(target_port) in value:
            value = "http://public-feed.test/feed"
        return real_send_header(handler, keyword, value)

    def resolve(host, port, *args, **kwargs):
        if host == "public-feed.test":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]
        return real_resolve(host, port, *args, **kwargs)

    def dial(backend, host, port, **kwargs):
        if host == "93.184.216.34":
            return real_dial(backend, "127.0.0.1", target_port, **kwargs)
        return real_dial(backend, host, port, **kwargs)

    monkeypatch.setattr(BaseHTTPRequestHandler, "send_header", send_header)
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", dial)
    url = initial.replace("http://", "http://synthetic-user:synthetic-pass@") + "/cross"
    items = await rss_parser.parse_rss_feed(url)
    assert len(items) == 3
    assert len(requests) == 2
    assert requests[0][2] is not None
    assert requests[1] == (target_port, "/feed", None)


@pytest.mark.parametrize("path,status,count", [("/feed", 200, 3), ("/empty", 200, 0),
                                              ("/failure", 400, None), ("/cross", 400, None)])
async def test_preview_distinguishes_empty_feed_from_fetch_failure(feed_servers, path, status, count):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.api.v1.channels import router

    initial, target_port, requests, _ = feed_servers
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/v1/channels/preview-feed", json={"url": initial + path})
    assert response.status_code == status
    body = response.json()
    if count is not None:
        assert body["success"] and len(body["data"]["entries"]) == count
    else:
        assert body["success"] is False and body["data"] is None
        assert body["error"]["code"] == "FETCH_ERROR" and body["error"]["message"]
    assert len(requests) == 1
    assert all(port != target_port for port, _, _ in requests)
