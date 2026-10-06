"""Real SDK requests to local protocol emulators; all account data synthetic."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.clients.transmission import TransmissionWrapper
from app.services import batch_content_analysis, feed_analyzer, metadata_agent
from app.services.torrent_inspect import parse_torrent_files


@pytest.fixture
def sdk_servers():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            requests.append((self.server.server_port, self.path, self.headers.get("Authorization")))
            if self.path == "/challenge/rpc" and self.headers.get("X-Transmission-Session-Id") != "synthetic-session":
                self.send_response(409)
                self.send_header("X-Transmission-Session-Id", "synthetic-session")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if self.path.startswith("/same/"):
                self.send_response(307)
                self.send_header("Location", self.path.removeprefix("/same"))
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if self.path.startswith("/redirect/"):
                self.send_response(307)
                self.send_header("Location", target_origin + self.path.removeprefix("/redirect"))
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if "method" in body:
                response = {"result": "success", "arguments": {"version": "4.0.6", "rpc-version": 17}, "tag": body.get("tag")}
            else:
                content = '{"works": []}' if body.get("model") == "synthetic-batch" else "synthetic-ok"
                response = {
                    "id": "synthetic-completion", "object": "chat.completion", "created": 1,
                    "model": "synthetic-model", "choices": [{"index": 0, "finish_reason": "stop",
                        "message": {"role": "assistant", "content": content}}],
                }
            content_type = "application/json"
            if body.get("stream"):
                content = '{"works": []}' if body.get("model") == "synthetic-batch" else "{}"
                response = {"id": "synthetic-stream", "object": "chat.completion.chunk", "created": 1,
                    "model": "synthetic-model", "choices": [{"index": 0, "finish_reason": "stop",
                        "delta": {"role": "assistant", "content": content}}]}
                payload = ("data: " + json.dumps(response) + "\n\ndata: [DONE]\n\n").encode()
                content_type = "text/event-stream"
            else:
                payload = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    target = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    initial = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    target_origin = f"http://127.0.0.1:{target.server_port}"
    servers = [initial, target]
    threads = [threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
               for s in servers]
    for thread in threads:
        thread.start()
    try:
        yield f"http://127.0.0.1:{initial.server_port}", target.server_port, requests
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=2)
            assert not thread.is_alive()


@pytest.mark.parametrize("kind", ["transmission", "llm", "llm_stream"])
@pytest.mark.parametrize("redirected", [False, True])
async def test_admin_sdk_origin_does_not_authorize_other_private_target(sdk_servers, monkeypatch, kind, redirected):
    initial, target_port, requests = sdk_servers
    base = initial + ("/redirect" if redirected else "")
    if kind == "transmission":
        wrapper = TransmissionWrapper(base + "/rpc", username="synthetic-user", password="synthetic-pass")
        ok, _ = await wrapper.test_connection()
    else:
        monkeypatch.setattr(feed_analyzer, "runtime_config", SimpleNamespace(
            llm_base_url=base + "/v1", llm_api_key="synthetic-test-key",
            llm_model="synthetic-model", llm_extra_body=lambda: {},
        ))
        try:
            messages = [{"role": "user", "content": "synthetic request"}]
            if kind == "llm_stream":
                events = [event async for event in feed_analyzer._stream_openai(messages)]
                ok = any(event["type"] == "done" for event in events)
            else:
                ok = await feed_analyzer._call_openai(messages) == "synthetic-ok"
        except Exception:
            ok = False
    assert requests, "The authorized initial endpoint must be exercised"
    assert not any(port == target_port for port, _, _ in requests), requests
    assert ok is (not redirected)


@pytest.mark.parametrize("kind", ["batch", "batch_stream", "metadata", "metadata_sync"])
@pytest.mark.parametrize("redirected", [False, True])
async def test_other_llm_paths_enforce_destination(sdk_servers, monkeypatch, kind, redirected):
    import asyncio

    initial, target_port, requests = sdk_servers
    base = initial + ("/redirect" if redirected else "") + "/v1"
    config = SimpleNamespace(
        llm_base_url=base, llm_api_key="synthetic-test-key",
        llm_model="synthetic-batch" if kind.startswith("batch") else "synthetic-model",
        llm_extra_body=lambda: {},
    )
    monkeypatch.setattr(batch_content_analysis, "runtime_config", config)
    monkeypatch.setattr(metadata_agent, "runtime_config", config)
    captured = Path(__file__).resolve().parents[2] / "fixtures/metadata_corpus_v1/torrents/987a72c09d5b0c2e934fa5016cc4dda6427a80dc5a4594e284a06ccf966acdb2.torrent"
    files = parse_torrent_files(str(captured))
    assert files
    failure = None
    try:
        if kind == "batch":
            ok = await batch_content_analysis.analyze_listing("Synthetic work", files, []) == {"works": []}
        elif kind == "batch_stream":
            events = [event async for event in batch_content_analysis.analyze_listing_stream("Synthetic work", files, [])]
            ok = ("result", {"works": []}) in events
        else:
            agent = metadata_agent.UnifiedMetadataAgent()
            try:
                if kind == "metadata_sync":
                    response = await asyncio.to_thread(agent._model.invoke, "synthetic request")
                else:
                    response = await agent._model.ainvoke("synthetic request")
                ok = response.content == "synthetic-ok"
            finally:
                agent._model.root_client.close()
                await agent._model.root_async_client.close()
    except Exception as exc:
        ok = False
        failure = repr(exc)
    assert requests, failure
    assert not any(port == target_port for port, _, _ in requests), requests
    assert ok is (not redirected)


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("redirected", [False, True])
async def test_openrouter_paths_enforce_destination(sdk_servers, monkeypatch, streaming, redirected):
    import openrouter

    initial, target_port, requests = sdk_servers
    base = initial + ("/redirect" if redirected else "") + "/v1"
    monkeypatch.setattr(feed_analyzer, "runtime_config", SimpleNamespace(
        llm_base_url=base, llm_api_key="synthetic-test-key", llm_model="synthetic-model",
    ))
    real_sdk = openrouter.OpenRouter

    def configured_sdk(*args, **kwargs):
        # Before the fix this service ignored its configured base URL. Use
        # the SDK's public server override, never a mocked HTTP method, so
        # the baseline cannot contact the actual OpenRouter service.
        kwargs.setdefault("server_url", base)
        return real_sdk(*args, **kwargs)

    monkeypatch.setattr(openrouter, "OpenRouter", configured_sdk)
    try:
        messages = [{"role": "user", "content": "synthetic request"}]
        if streaming:
            events = [event async for event in feed_analyzer._stream_openrouter(messages)]
            ok = any(event["type"] == "done" for event in events)
        else:
            ok = await feed_analyzer._call_openrouter(messages) == "{}"
    except Exception:
        ok = False
    assert requests
    assert not any(port == target_port for port, _, _ in requests), requests
    assert ok is (not redirected)


async def test_transmission_same_origin_redirect_remains_supported(sdk_servers):
    initial, target_port, requests = sdk_servers
    wrapper = TransmissionWrapper(initial + "/same/rpc", username="synthetic-user", password="synthetic-pass")
    ok, version = await wrapper.test_connection()
    assert ok and version == "Transmission 4.0.6"
    assert requests
    assert all(port != target_port and auth is not None for port, _, auth in requests)
    assert any(path == "/rpc" for _, path, _ in requests)


async def test_transmission_session_challenge_remains_supported(sdk_servers):
    initial, target_port, requests = sdk_servers
    wrapper = TransmissionWrapper(initial + "/challenge/rpc", username="synthetic-user", password="synthetic-pass")
    ok, version = await wrapper.test_connection()
    assert ok and version == "Transmission 4.0.6"
    assert len(requests) == 3  # constructor challenge/retry, then explicit session query
    assert all(port != target_port and auth is not None for port, _, auth in requests)
