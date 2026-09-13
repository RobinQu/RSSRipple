"""Three-phase driver for actual SIGKILL/restart of the isolated worker group.

Run seed; kill all test workers; run mutate; start all workers; run check.
State lives only in the disposable Redis. The driver never controls Docker.
Refresh checks schedule/handler invocation on an empty work set, not provider I/O.
"""

import argparse
import json
import time

from redis import Redis

from scripts.scheduler_e2e import request, until

_KEY = "p0:scheduler:recovery"


def refresh_job(channel_id):
    jobs = request("/queue/jobs?job_type=refresh_channel_works&page_size=100")["data"]
    return next((job for job in jobs if job["key"] == f"channel-refresh:{channel_id}"), None)


def seed():
    response = request("/channels", {
        "name": "recovery-e2e", "type": "rss_feed", "url": "http://feed:8080/feed",
        "fetch_interval": 10, "metadata_agent_enabled": False,
        "metadata_refresh_enabled": True, "metadata_refresh_interval_minutes": 5,
        "field_mapping": {"list_locator": {"source": "entries"},
                          "field_mappings": {"torrent_url": {"source": "link"}}},
    }, "POST")
    channel_id = response["data"]["id"]
    first = until(lambda: request(f"/channels/{channel_id}")["data"]["last_fetched_at"])
    until(lambda: request(f"/channels/{channel_id}")["data"]["last_fetched_at"] != first)
    until(lambda: (refresh_job(channel_id) or {}).get("status") == "done")
    with Redis(host="redis", decode_responses=True) as redis:
        redis.set(_KEY, json.dumps({"channel_id": channel_id}))
    print("PASS: fetch and automatic refresh handlers completed before worker failure", flush=True)


def mutate(state):
    channel_id = state["channel_id"]
    request(f"/channels/{channel_id}", {
        "fetch_interval": 3, "metadata_refresh_enabled": False,
    }, "PUT")
    before = request(f"/channels/{channel_id}")["data"]["last_fetched_at"]
    time.sleep(4)
    assert request(f"/channels/{channel_id}")["data"]["last_fetched_at"] == before
    state["before_restart"] = before
    with Redis(host="redis", decode_responses=True) as redis:
        redis.set(_KEY, json.dumps(state))
    print("PASS: web committed changed settings while all worker processes were stopped", flush=True)


def check(state):
    channel_id = state["channel_id"]
    first = until(lambda: (stamp := request(f"/channels/{channel_id}")["data"]["last_fetched_at"])
                  != state["before_restart"] and stamp)
    until(lambda: request(f"/channels/{channel_id}")["data"]["last_fetched_at"] != first, timeout=12)
    print("PASS: restarted workers recovered changed schedule and continued periodic fetching", flush=True)
    # Let any old queued refresh terminate under the disabled-setting guard.
    time.sleep(36)
    previous = refresh_job(channel_id)
    time.sleep(6)
    assert refresh_job(channel_id) == previous
    request(f"/channels/{channel_id}", {"metadata_refresh_enabled": True}, "PUT")
    refreshed = until(lambda: (job := refresh_job(channel_id)) and job["status"] == "done"
                      and (previous is None or job["job_id"] != previous["job_id"]) and job)
    assert refreshed["result"]["processed"] == 0  # explicitly empty work set
    print("PASS: enabling automatic refresh after recovery starts a new real worker job", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["seed", "mutate", "check"])
    phase = parser.parse_args().phase
    if phase == "seed":
        seed()
    else:
        with Redis(host="redis", decode_responses=True) as redis:
            state = json.loads(redis.get(_KEY))
        {"mutate": mutate, "check": check}[phase](state)
