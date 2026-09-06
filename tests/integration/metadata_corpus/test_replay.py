"""Integration replay failures must remain failures even if production catches them."""

import asyncio
import base64

import bencodepy
import httpx
import pytest

from tests.metadata_corpus.dataset import asset, sanitize_torrent, write_json
from tests.metadata_corpus.replay import Cassette, ReplayError, request_key


def recording(path, requests):
    rows = {}
    for request, body in requests:
        key, safe = request_key(request)
        rows[key] = {"request": safe, "status": 200, "headers": {"content-type": "application/json"},
                     "body": base64.b64encode(body).decode()}
    write_json(path, {"version": 1, "requests": rows})


def test_credentials_do_not_change_fingerprint_or_enter_recording():
    first = httpx.Request("GET", "https://example.com/search?q=show&api_key=secret", headers={"Authorization": "secret"})
    second = httpx.Request("GET", "https://example.com/search?api_key=other&q=show")
    assert request_key(first) == request_key(second)
    assert "secret" not in str(request_key(first))


def test_prompt_and_tool_contract_change_fingerprint():
    first = httpx.Request("POST", "https://llm.test/chat", json={"model": "m", "messages": ["old"], "tools": []})
    changed = httpx.Request("POST", "https://llm.test/chat", json={"model": "m", "messages": ["new"], "tools": []})
    assert request_key(first)[0] != request_key(changed)[0]


async def test_sync_and_async_replay_independent_of_request_order(tmp_path):
    path = tmp_path / "http.json.gz"
    requests = [(httpx.Request("GET", f"https://source.test/{i}"), f'{{"id":{i}}}'.encode()) for i in range(3)]
    recording(path, requests)
    from tests.metadata_corpus.dataset import read_json
    data = read_json(path)
    data["requests"][request_key(requests[2][0])[0]]["count"] = 2
    write_json(path, data)
    with Cassette(path) as cassette:
        with httpx.Client() as client:
            assert client.get("https://source.test/2").json() == {"id": 2}
        async with httpx.AsyncClient() as client:
            responses = await asyncio.gather(*(client.get(str(req.url)) for req, _ in reversed(requests)))
        assert [r.json()["id"] for r in responses] == [2, 1, 0]
        cassette.assert_complete()


def test_swallowed_unrecorded_request_still_fails_final_verification(tmp_path):
    with Cassette(tmp_path / "absent.json.gz") as cassette:
        with httpx.Client() as client:
            try:
                client.get("https://source.test/missing")
            except Exception:
                pass
        with pytest.raises(ReplayError, match="unrecorded request"):
            cassette.assert_complete()


def test_record_never_overwrites_evidence(tmp_path):
    path = tmp_path / "http.json.gz"
    write_json(path, {"requests": {}})
    with pytest.raises(ValueError, match="overwrite"):
        Cassette(path, mode="record")


def test_asset_path_cannot_escape_corpus(tmp_path):
    with pytest.raises(ValueError, match="escapes"):
        asset(tmp_path, "../secret")


def test_sanitized_torrent_preserves_info_and_removes_tracker():
    info = {b"name": b"Show S01E01.mkv", b"length": 100_000_000, b"piece length": 16384, b"pieces": b"x" * 20}
    raw = bencodepy.encode({b"announce": b"https://tracker.test/secret", b"info": info})
    cleaned = sanitize_torrent(raw)
    assert bencodepy.decode(cleaned) == {b"info": info}
    assert bencodepy.encode(info) in cleaned


def test_invalid_torrent_is_not_silently_replaced():
    with pytest.raises(ValueError, match="info"):
        sanitize_torrent(b"de")


def test_nested_secrets_and_response_urls_are_redacted():
    from tests.metadata_corpus.replay import redact
    value = {"nested": [{"token": "secret", "url": "https://host/path?passkey=secret"}]}
    assert "secret" not in str(redact(value))


def test_llm_mode_blocks_non_httpx_sockets(tmp_path):
    import socket
    with Cassette(tmp_path / "absent.json", mode="llm", llm_host="llm.test") as cassette:
        with socket.socket() as sock, pytest.raises(ReplayError, match="non-HTTP"):
            sock.connect(("127.0.0.1", 8000))
        with pytest.raises(ReplayError):
            cassette.assert_complete()


def test_unused_recording_and_zero_llm_calls_are_not_passes(tmp_path):
    path = tmp_path / "http.json"
    recording(path, [(httpx.Request("GET", "https://source.test/a"), b"{}")])
    with Cassette(path, mode="llm", llm_host="llm.test") as cassette:
        with pytest.raises(ReplayError, match="not fully consumed"):
            cassette.assert_complete()
        assert any("did not invoke" in error for error in cassette.errors)


def test_extra_replay_call_is_detected(tmp_path):
    path = tmp_path / "http.json"
    recording(path, [(httpx.Request("GET", "https://source.test/a"), b"{}")])
    with Cassette(path):
        with httpx.Client() as client:
            client.get("https://source.test/a")
            with pytest.raises(ReplayError, match="overused"):
                client.get("https://source.test/a")


def test_non_json_credentials_cannot_be_recorded():
    request = httpx.Request("POST", "https://source.test/token", data={"api_key": "secret"})
    with pytest.raises(ReplayError, match="credential-safe"):
        request_key(request)


def test_semantic_headers_change_fingerprint():
    first = httpx.Request("GET", "https://source.test/a", headers={"Accept-Language": "zh"})
    second = httpx.Request("GET", "https://source.test/a", headers={"Accept-Language": "en"})
    assert request_key(first)[0] != request_key(second)[0]


def test_live_llm_http_failure_is_not_quality_success(tmp_path):
    cassette = Cassette(tmp_path / "unused.json", mode="llm", llm_host="llm.test")
    request = httpx.Request("POST", "https://llm.test/chat", json={})
    cassette._record(request, httpx.Response(500, request=request))
    with pytest.raises(ReplayError, match="HTTP failure"):
        cassette.assert_complete()
