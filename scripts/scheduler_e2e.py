"""Deterministic scheduler HTTP regression for docker-compose.scheduler-e2e.yml.

The compose stack owns the disposable DB/Redis and three worker processes.
This driver never starts/stops Docker itself or connects to a production URL.
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

_FEED = b'''<?xml version="1.0"?><rss version="2.0"><channel>
<title>Scheduler regression</title><link>http://feed:8080/</link><description>local only</description>
<item><guid>scheduler-test-item</guid><title>Scheduler test S01E01</title>
<link>magnet:?xt=urn:btih:1111111111111111111111111111111111111111</link>
</item></channel></rss>'''


class FeedHandler(BaseHTTPRequestHandler):
    requests = 0

    def do_GET(self):  # noqa: N802 — BaseHTTPRequestHandler protocol
        if self.path == "/feed":
            FeedHandler.requests += 1
            body, content_type = _FEED, "application/rss+xml"
        else:
            body, content_type = json.dumps({"requests": FeedHandler.requests}).encode(), "application/json"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def request(path: str, body: dict | None = None, method: str = "GET") -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"http://web:9001/api/v1{path}", data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as response:
        result = json.load(response)
    if not result["success"]:
        raise AssertionError(result)
    return result


def until(predicate, *, timeout: float = 65):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.5)
    raise AssertionError("scheduler condition did not converge before deadline")


def set_channel_status(channel_id: str, status: str) -> None:
    # ChannelUpdate does not currently expose status. Exercise the existing DB
    # configuration contract without adding a production endpoint for the test.
    import asyncio
    import os

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    async def update():
        engine = create_async_engine(os.environ["DATABASE_URL"])
        try:
            async with engine.begin() as connection:
                await connection.execute(text("UPDATE channels SET status=:status WHERE id=:id"), {
                    "status": status, "id": channel_id,
                })
        finally:
            await engine.dispose()

    asyncio.run(update())


def no_fetch_for(channel_id: str, duration: int = 36) -> None:
    before = request(f"/channels/{channel_id}")["data"]["last_fetched_at"]
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        state = request(f"/channels/{channel_id}")["data"]
        assert state["last_fetched_at"] == before, "paused channel fetched again"
        assert state["status"] == "inactive", "stale automatic fetch reactivated paused channel"
        time.sleep(0.5)


def check() -> None:
    # The workers start with an empty DB. The channel is created only over web.
    created = request("/channels", {
        "name": "scheduler-e2e", "type": "rss_feed", "url": "http://feed:8080/feed",
        "fetch_interval": 10, "metadata_agent_enabled": False,
        "field_mapping": {
            "list_locator": {"source": "entries"},
            "field_mappings": {"torrent_url": {"source": "link"}},
        },
    }, "POST")
    channel_id = created["data"]["id"]
    assert created["meta"]["fetch_triggered"]
    first = until(lambda: request(f"/channels/{channel_id}")["data"]["last_fetched_at"])
    second = until(lambda: (stamp := request(f"/channels/{channel_id}")["data"]["last_fetched_at"]) != first and stamp)
    until(lambda: request(f"/channels/{channel_id}")["data"]["last_fetched_at"] != second)
    print("PASS: initial fetch and subsequent periodic fetches without worker restart", flush=True)

    # Ignore a pre-existing timer during the bounded reconciliation interval;
    # afterwards the new short interval must produce another completed fetch.
    request(f"/channels/{channel_id}", {"fetch_interval": 3}, "PUT")
    time.sleep(36)
    before = request(f"/channels/{channel_id}")["data"]["last_fetched_at"]
    until(lambda: request(f"/channels/{channel_id}")["data"]["last_fetched_at"] != before, timeout=9)
    print("PASS: interval edit applied without worker restart", flush=True)

    # Quiesce the short interval first so a different worker cannot start a
    # new fetch between the status poll and pause. In-flight cancellation is
    # outside this contract. All workers apply this long interval within 30s.
    request(f"/channels/{channel_id}", {"fetch_interval": 3600}, "PUT")
    time.sleep(36)
    until(lambda: (request(f"/channels/{channel_id}/fetch-status")["data"] or {}).get("status") == "done")
    set_channel_status(channel_id, "inactive")
    no_fetch_for(channel_id)
    print("PASS: paused channel stays paused across reconciliation", flush=True)
    before = request(f"/channels/{channel_id}")["data"]["last_fetched_at"]
    set_channel_status(channel_id, "active")
    until(lambda: request(f"/channels/{channel_id}")["data"]["last_fetched_at"] != before)
    print("PASS: re-enabled channel resumes automatically", flush=True)

    request(f"/channels/{channel_id}", method="DELETE")
    try:
        request(f"/channels/{channel_id}")
    except urllib.error.HTTPError as exc:
        assert exc.code == 404
    else:
        raise AssertionError("deleted channel still exists")
    # Let stale timers/queued jobs drain, then check the actual feed request
    # counter remains still. No metadata/external network services are used.
    time.sleep(36)
    with urllib.request.urlopen("http://feed:8080/requests", timeout=5) as response:
        count = json.load(response)["requests"]
    time.sleep(10)
    with urllib.request.urlopen("http://feed:8080/requests", timeout=5) as response:
        assert json.load(response)["requests"] == count
    print("PASS: deletion stops periodic requests", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve-feed", action="store_true")
    if parser.parse_args().serve_feed:
        HTTPServer(("0.0.0.0", 8080), FeedHandler).serve_forever()
    else:
        check()
