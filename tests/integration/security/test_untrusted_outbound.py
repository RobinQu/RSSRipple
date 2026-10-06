"""Local trap servers only; torrent payload is an unchanged recorded fixture."""

import asyncio
import base64
import hashlib
import socket
import ssl
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import bencodepy
import httpcore
import httpx
import pytest

from app.clients import outbound_http
from app.config import settings
from app.services.magnet_resolve import _try_cache_mirrors
from app.services.metadata_service import download_and_cache_poster
from app.services.torrent_inspect import fetch_torrent_file


@pytest.fixture
def private_servers(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    captured = Path(__file__).resolve().parents[2] / "fixtures/metadata_corpus_v1/torrents/987a72c09d5b0c2e934fa5016cc4dda6427a80dc5a4594e284a06ccf966acdb2.torrent"
    payloads = {
        "torrent": captured.read_bytes(),
        "poster": base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a3ioAAAAASUVORK5CYII="),
    }
    requests = []

    class Target(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            body = payloads[self.path.rsplit("/", 1)[-1]]
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    target = ThreadingHTTPServer(("127.0.0.1", 0), Target)
    target_origin = f"http://127.0.0.1:{target.server_port}"

    class Redirect(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", target_origin + self.path)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    redirect = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    servers = [target, redirect]
    threads = [threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
               for s in servers]
    for thread in threads:
        thread.start()
    try:
        yield target_origin, f"http://127.0.0.1:{redirect.server_port}", requests
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=2)
            assert not thread.is_alive()


@pytest.mark.parametrize("kind", ["torrent", "poster"])
@pytest.mark.parametrize("redirected", [False, True])
async def test_untrusted_resource_never_contacts_private_target(private_servers, tmp_path, monkeypatch, kind, redirected):
    target, redirect, requests = private_servers
    monkeypatch.setattr(settings, "torrent_cache_dir", str(tmp_path / "torrents"))
    monkeypatch.setattr(settings, "poster_cache_dir", str(tmp_path / "posters"))
    origin = redirect if redirected else target
    url = origin + "/private/" + kind
    if kind == "torrent":
        result = await fetch_torrent_file(url, "synthetic-private-target")
    else:
        result = await download_and_cache_poster(url)
    assert requests == [], {"requests": requests, "cached_result": result, "redirected": redirected}
    assert result is None


def test_explicit_private_origin_does_not_authorize_redirect(private_servers):
    target, redirect, requests = private_servers
    with outbound_http.client(timeout=2, allowed_origins=[redirect]) as client:
        with pytest.raises(outbound_http.DestinationDenied):
            client.get(redirect + "/private/torrent")
    assert requests == []
    with outbound_http.client(timeout=2, allowed_origins=[target]) as client:
        response = client.get(target + "/private/torrent")
    assert response.status_code == 200
    assert requests == ["/private/torrent"]


@pytest.mark.parametrize("trusted,hostname,success", [
    (True, "public.test", True),
    ("environment", "public.test", True),
    (False, "public.test", False),
    (True, "wrong.test", False),
])
@pytest.mark.parametrize("asynchronous", [False, True])
def test_pinned_https_preserves_sni_and_certificate_checks(tmp_path, monkeypatch, trusted, hostname, success, asynchronous):
    """Synthetic local TLS certificate, actual handshake, controlled DNS/dial."""
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", str(key), "-out", str(cert), "-days", "1",
        "-subj", "/CN=public.test", "-addext", "subjectAltName=DNS:public.test",
    ], check=True, capture_output=True, timeout=15)
    requests = []
    server_names = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.headers["Host"])
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert, key)
    server_context.set_servername_callback(lambda sock, name, context: server_names.append(name))
    server.socket = server_context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    real_resolve = socket.getaddrinfo
    real_dial = httpcore.SyncBackend.connect_tcp
    real_async_dial = httpcore.AnyIOBackend.connect_tcp
    dials = []

    def resolve(host, port, *args, **kwargs):
        if host in {"public.test", "wrong.test"}:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]
        return real_resolve(host, port, *args, **kwargs)

    def dial(backend, host, port, **kwargs):
        dials.append((host, port))
        assert host == "93.184.216.34"
        return real_dial(backend, "127.0.0.1", server.server_port, **kwargs)

    async def async_dial(backend, host, port, **kwargs):
        dials.append((host, port))
        assert host == "93.184.216.34"
        return await real_async_dial(backend, "127.0.0.1", server.server_port, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", dial)
    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", async_dial)
    if trusted == "environment":
        monkeypatch.setenv("SSL_CERT_FILE", str(cert))
        context = None
    else:
        context = ssl.create_default_context(cafile=str(cert)) if trusted else None

    async def async_get():
        async with outbound_http.async_client(timeout=2, context=context) as client:
            return await client.get(f"https://{hostname}/tls")

    def get():
        if asynchronous:
            return asyncio.run(async_get())
        with outbound_http.client(timeout=2, context=context) as client:
            return client.get(f"https://{hostname}/tls")

    try:
        if success:
            assert get().content == b"ok"
        else:
            with pytest.raises(httpx.ConnectError, match="CERTIFICATE_VERIFY_FAILED"):
                get()
        assert dials == [("93.184.216.34", 443)]
        assert server_names == [hostname]
        assert requests == ([hostname] if success else [])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


@pytest.mark.parametrize("addresses", [
    ["127.0.0.1"], ["10.0.0.1"], ["169.254.169.254"], ["::1"],
    ["::ffff:127.0.0.1"], ["64:ff9b::7f00:1"], ["2002:7f00:1::"],
    ["224.0.0.1"], ["93.184.216.34", "127.0.0.1"],
])
@pytest.mark.parametrize("asynchronous", [False, True])
def test_dns_answers_rejected_before_dial(monkeypatch, addresses, asynchronous):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 80))
        for address in addresses
    ])
    calls = []
    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", lambda *a, **kw: calls.append(a))
    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", lambda *a, **kw: calls.append(a))

    async def get():
        async with outbound_http.async_client(timeout=2) as client:
            await client.get("http://controlled-dns.test/private/torrent")

    if asynchronous:
        with pytest.raises(outbound_http.DestinationDenied):
            asyncio.run(get())
    else:
        with outbound_http.client(timeout=2) as client:
            with pytest.raises(outbound_http.DestinationDenied):
                client.get("http://controlled-dns.test/private/torrent")
    assert calls == []


def test_public_dns_is_pinned_and_proxy_cannot_bypass(private_servers, monkeypatch):
    """Only DNS/dial are substituted; payload travels through real local HTTP."""
    target, redirect, requests = private_servers
    real_resolve = socket.getaddrinfo
    real_dial = httpcore.SyncBackend.connect_tcp
    lookups = []
    dials = []

    def resolve(host, port, *args, **kwargs):
        if host == "public.test":
            lookups.append(host)
            assert len(lookups) == 1, "Hostname must not be resolved again during dial"
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]
        return real_resolve(host, port, *args, **kwargs)

    def dial(backend, host, port, **kwargs):
        dials.append((host, port))
        assert host == "93.184.216.34"
        return real_dial(backend, "127.0.0.1", httpx.URL(target).port, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", dial)
    monkeypatch.setenv("HTTP_PROXY", redirect)
    monkeypatch.setenv("ALL_PROXY", redirect)
    with outbound_http.client(timeout=2) as client:
        response = client.get("http://public.test/private/torrent")
    assert response.status_code == 200
    assert dials == [("93.184.216.34", 80)]
    assert lookups == ["public.test"]
    assert requests == ["/private/torrent"]


@pytest.mark.parametrize("kind", ["torrent", "poster"])
async def test_real_fetch_path_keeps_valid_payload(private_servers, tmp_path, monkeypatch, kind):
    target, _, requests = private_servers
    real_resolve = socket.getaddrinfo
    real_dial = httpcore.SyncBackend.connect_tcp
    monkeypatch.setattr(settings, "torrent_cache_dir", str(tmp_path / "torrents"))
    monkeypatch.setattr(settings, "poster_cache_dir", str(tmp_path / "posters"))

    def resolve(host, port, *args, **kwargs):
        if host == "public.test":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]
        return real_resolve(host, port, *args, **kwargs)

    def dial(backend, host, port, **kwargs):
        assert host == "93.184.216.34"
        return real_dial(backend, "127.0.0.1", httpx.URL(target).port, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", dial)
    url = "http://public.test/private/" + kind
    if kind == "torrent":
        result = await fetch_torrent_file(url, "captured-valid")
        assert result is not None
        assert hashlib.sha256(Path(result).read_bytes()).hexdigest() == (
            "987a72c09d5b0c2e934fa5016cc4dda6427a80dc5a4594e284a06ccf966acdb2"
        )
    else:
        result = await download_and_cache_poster(url)
        assert result is not None
        assert (tmp_path / "posters" / Path(result).name).read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert requests == ["/private/" + kind]


@pytest.mark.parametrize("kind", ["torrent", "poster"])
@pytest.mark.parametrize("exact_origin", [False, True])
async def test_configured_private_resource_exception_is_exact(private_servers, tmp_path, monkeypatch, kind, exact_origin):
    target, different_port, requests = private_servers
    monkeypatch.setattr(settings, "outbound_private_origins", [target if exact_origin else different_port])
    monkeypatch.setattr(settings, "torrent_cache_dir", str(tmp_path / "torrents"))
    monkeypatch.setattr(settings, "poster_cache_dir", str(tmp_path / "posters"))
    if kind == "torrent":
        result = await fetch_torrent_file(target + "/private/torrent", "explicit-private")
    else:
        result = await download_and_cache_poster(target + "/private/poster")
    assert bool(result) is exact_origin
    assert requests == (["/private/" + kind] if exact_origin else [])


@pytest.mark.parametrize("host", ["127.1", "2130706433", "0x7f000001", "0177.0.0.1"])
def test_noncanonical_loopback_never_reaches_socket(monkeypatch, host):
    calls = []
    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", lambda *args, **kwargs: calls.append(args))
    with outbound_http.client(timeout=2) as client:
        with pytest.raises((outbound_http.DestinationDenied, httpx.InvalidURL)):
            client.get(f"http://{host}/private/torrent")
    assert calls == []


@pytest.mark.parametrize("redirected", [False, True])
@pytest.mark.parametrize("matching_hash", [False, True])
async def test_admin_mirror_redirect_and_infohash_boundary(private_servers, tmp_path, monkeypatch, redirected, matching_hash):
    target, redirect, requests = private_servers
    captured = Path(__file__).resolve().parents[2] / "fixtures/metadata_corpus_v1/torrents/987a72c09d5b0c2e934fa5016cc4dda6427a80dc5a4594e284a06ccf966acdb2.torrent"
    payload = captured.read_bytes()
    infohash = hashlib.sha1(bencodepy.encode(bencodepy.decode(payload)[b"info"])).hexdigest()
    requested = infohash if matching_hash else "0" * 40
    monkeypatch.setattr(settings, "magnet_resolve_cache_mirrors", [
        (redirect if redirected else target) + "/{infohash}/torrent"
    ])
    dest = tmp_path / "mirror.torrent"
    result = await _try_cache_mirrors("magnet:?xt=urn:btih:" + requested, object(), str(dest))
    assert result is (matching_hash and not redirected)
    if redirected:
        assert requests == [], "Mirror authorization must not extend to redirect origin"
    else:
        assert requests == ["/" + requested + "/torrent"]
    if result:
        assert dest.read_bytes() == payload
    else:
        assert not dest.exists()
