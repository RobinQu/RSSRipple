"""Real Wikipedia image helper through synthetic DNS and local HTTP/TLS."""

import json
import socket
import ssl
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpcore
import pytest

from app.services.metadata_wikipedia_client import _fetch_wikipedia_page_image


@pytest.mark.parametrize("redirected", [False, True])
async def test_wiki_summary_redirect_cannot_reach_private_service(tmp_path, monkeypatch, redirected):
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", str(key), "-out", str(cert), "-days", "1",
        "-subj", "/CN=en.wikipedia.org", "-addext", "subjectAltName=DNS:en.wikipedia.org",
    ], check=True, capture_output=True, timeout=15)
    visits = []
    image_url = "https://images.example/synthetic.png"

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            visits.append((self.server.server_port, self.path))
            if self.server.server_port == initial.server_port and "/rest_v1/" in self.path and redirected:
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{target.server_port}/summary")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            payload = ({"query": {"pages": {}}} if "/w/api.php" in self.path
                       else {"title": "Synthetic page", "originalimage": {"source": image_url}})
            raw = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            pass

    target = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    initial = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    initial.socket = context.wrap_socket(initial.socket, server_side=True)
    servers = [initial, target]
    threads = [threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True) for s in servers]
    for thread in threads:
        thread.start()
    real_context = ssl.create_default_context
    real_resolve = socket.getaddrinfo
    real_dial = httpcore.AnyIOBackend.connect_tcp

    def trust_local_test_ca(*args, **kwargs):
        kwargs["cafile"] = str(cert)
        return real_context(*args, **kwargs)

    def resolve(host, port, *args, **kwargs):
        if host == "en.wikipedia.org":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]
        return real_resolve(host, port, *args, **kwargs)

    async def dial(backend, host, port, **kwargs):
        if host in {"en.wikipedia.org", "93.184.216.34"}:
            return await real_dial(backend, "127.0.0.1", initial.server_port, **kwargs)
        assert host == "127.0.0.1" and port == target.server_port
        return await real_dial(backend, host, port, **kwargs)

    monkeypatch.setattr(ssl, "create_default_context", trust_local_test_ca)
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", dial)
    for name in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]:
        monkeypatch.delenv(name, raising=False)
    try:
        result = await _fetch_wikipedia_page_image("Synthetic page", expected_title="Synthetic page")
        assert len([path for port, path in visits if port == initial.server_port]) == 2
        assert not any(port == target.server_port for port, _ in visits), visits
        assert result == (None if redirected else image_url)
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=2)
            assert not thread.is_alive()
