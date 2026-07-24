"""Integration tests: task queue endpoints — MemoryQueue and RedisQueue.

When a distinct REDIS_APP_URL is configured (distributed/Docker-Compose with
two app instances), tests are parametrized over both backends ("memory" and
"redis").  In single-node setups REDIS_APP_URL is omitted, tests run against
the MemoryQueue instance only, and Redis-specific dedup tests are skipped.

Run single-node:
  docker compose -f docker-compose.test.yml run --rm test-runner

Run distributed:
  docker compose -f docker-compose.test-distributed.yml run --rm test-runner
"""

import os
import time
import uuid

import httpx
import pytest

from tests.integration.http._http import DEFAULT_FIELD_MAPPING

TEST_SERVER = os.environ.get("TEST_SERVER_URL", "http://test-server:8080")
APP_MEMORY = os.environ.get("RSSRIPPLE_URL", "http://app:9001")
APP_REDIS = os.environ.get("REDIS_APP_URL", "")
if not APP_REDIS:
    APP_REDIS = APP_MEMORY  # single-node fallback
_HAS_DISTINCT_REDIS = APP_REDIS != APP_MEMORY

POLL_INTERVAL = 0.5      # seconds between status polls
FETCH_TIMEOUT = 45.0     # max seconds to wait for a fetch to finish
READY_TIMEOUT = 60.0     # max seconds to wait for an app to become reachable

ALL_BACKENDS = [pytest.param(APP_MEMORY, id="memory")]
if _HAS_DISTINCT_REDIS:
    ALL_BACKENDS.append(pytest.param(APP_REDIS, id="redis"))


# ---------------------------------------------------------------------------
# Session-scoped readiness fixture
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session", autouse=True)
def wait_for_all_apps():
    """Block until all configured app instances answer 200 on GET /api/v1/channels."""
    urls = {APP_MEMORY}
    if _HAS_DISTINCT_REDIS:
        urls.add(APP_REDIS)
    for url in urls:
        deadline = time.monotonic() + READY_TIMEOUT
        while True:
            try:
                resp = httpx.get(f"{url}/api/v1/channels", timeout=3)
                if resp.status_code == 200:
                    break
            except Exception:
                pass
            if time.monotonic() > deadline:
                pytest.fail(f"App at {url} did not become ready within {READY_TIMEOUT}s")
            time.sleep(1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def poll_job_status(app_url: str, endpoint: str, timeout: float = FETCH_TIMEOUT) -> dict:
    """Poll *endpoint* (GET) until a terminal job state is returned."""
    deadline = time.monotonic() + timeout
    while True:
        resp = httpx.get(f"{app_url}{endpoint}", timeout=10)
        assert resp.status_code == 200, f"Unexpected {resp.status_code}: {resp.text}"
        state = resp.json()["data"]
        if state and state["status"] in ("done", "failed"):
            return state
        if time.monotonic() > deadline:
            raise TimeoutError(
                f"Job at {endpoint!r} did not finish in {timeout}s; last state={state}"
            )
        time.sleep(POLL_INTERVAL)




def make_channel(app_url: str, name_suffix: str = "") -> str:
    """Create a test channel pointing at the mikanani feed and return its ID."""
    resp = httpx.post(
        f"{app_url}/api/v1/channels",
        json={
            "name": f"QueueTest-{uuid.uuid4().hex[:6]}{name_suffix}",
            "url": f"{TEST_SERVER}/rss/mikanani",
            "metadata_agent_enabled": False,
            "field_mapping": DEFAULT_FIELD_MAPPING,
        },
        timeout=45,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]["id"]


def make_agent(app_url: str) -> tuple[str, str]:
    """Create a test channel + agent; return (channel_id, agent_id)."""
    channel_id = make_channel(app_url)

    dl_resp = httpx.post(
        f"{app_url}/api/v1/downloaders",
        json={
            "name": f"QueueTest-DL-{uuid.uuid4().hex[:6]}",
            "type": "transmission",
            "url": "http://transmission:9092/transmission/rpc",
            "download_dir": "/downloads",
        },
        timeout=10,
    )
    assert dl_resp.status_code == 201, dl_resp.text

    ag_resp = httpx.post(
        f"{app_url}/api/v1/agents",
        json={
            "name": f"QueueTest-Agent-{uuid.uuid4().hex[:6]}",
            "channel_id": channel_id,
            "downloader_id": dl_resp.json()["data"]["id"],
            "scope_channel_wide": True,
            "conflict_resolution": "auto",
        },
        timeout=10,
    )
    assert ag_resp.status_code == 201, ag_resp.text
    return channel_id, ag_resp.json()["data"]["id"]


# ---------------------------------------------------------------------------
# Channel fetch job
# ---------------------------------------------------------------------------

class TestChannelFetchJob:
    """POST /channels/{id}/fetch → background job lifecycle."""

    @pytest.mark.parametrize("app_url", ALL_BACKENDS)
    def test_enqueue_returns_job_state(self, app_url):
        """POST /fetch returns a job state dict with expected fields."""
        channel_id = make_channel(app_url)
        resp = httpx.post(f"{app_url}/api/v1/channels/{channel_id}/fetch", timeout=10)
        assert resp.status_code == 200, resp.text
        job = resp.json()["data"]
        assert job["job_id"]
        assert job["job_type"] == "fetch_channel"
        assert job["key"] == f"channel:{channel_id}"
        assert job["status"] in ("queued", "running")
        assert job["queued_at"]
        assert job["started_at"] is None or isinstance(job["started_at"], str)
        # Drain the background job so its DB writes don't block subsequent tests.
        poll_job_status(app_url, f"/api/v1/channels/{channel_id}/fetch-status")

    @pytest.mark.parametrize("app_url", ALL_BACKENDS)
    def test_fetch_status_none_before_any_fetch(self, app_url):
        """GET /fetch-status on a channel never fetched returns null data."""
        channel_id = make_channel(app_url)
        resp = httpx.get(f"{app_url}/api/v1/channels/{channel_id}/fetch-status", timeout=10)
        assert resp.status_code == 200
        assert resp.json()["data"] is None

    @pytest.mark.parametrize("app_url", ALL_BACKENDS)
    def test_fetch_completes_and_creates_resources(self, app_url):
        """Job reaches 'done' and FileResources appear in the channel."""
        channel_id = make_channel(app_url)
        httpx.post(f"{app_url}/api/v1/channels/{channel_id}/fetch", timeout=10)

        state = poll_job_status(app_url, f"/api/v1/channels/{channel_id}/fetch-status")
        assert state["status"] == "done", f"Expected done, got: {state}"
        assert state["started_at"] is not None
        assert state["finished_at"] is not None

        # FileResources should have been created by the fetch handler
        resp = httpx.get(
            f"{app_url}/api/v1/channels/{channel_id}/resources",
            timeout=10,
        )
        assert resp.status_code == 200
        assert resp.json()["meta"]["total"] > 0, "No resources created after fetch"

    @pytest.mark.parametrize("app_url", ALL_BACKENDS)
    def test_dedup_returns_409_while_active(self, app_url):
        """Second POST /fetch while first is active returns 409 ALREADY_RUNNING."""
        channel_id = make_channel(app_url)

        resp1 = httpx.post(f"{app_url}/api/v1/channels/{channel_id}/fetch", timeout=10)
        assert resp1.status_code == 200
        job1_id = resp1.json()["data"]["job_id"]

        resp2 = httpx.post(f"{app_url}/api/v1/channels/{channel_id}/fetch", timeout=10)
        # Job may have completed in the time between the two requests (fast feeds).
        # Accept either 409 (still in flight) or 200 (already done).
        if resp2.status_code == 409:
            body = resp2.json()
            assert body["error"]["code"] == "ALREADY_RUNNING"
            assert body["data"]["job_id"] == job1_id
        else:
            assert resp2.status_code == 200, resp2.text

    @pytest.mark.parametrize("app_url", ALL_BACKENDS)
    def test_reenqueue_after_completion(self, app_url):
        """After the first fetch completes, a new fetch for the same channel succeeds."""
        channel_id = make_channel(app_url)

        # First run — wait for it to finish
        httpx.post(f"{app_url}/api/v1/channels/{channel_id}/fetch", timeout=10)
        first_state = poll_job_status(app_url, f"/api/v1/channels/{channel_id}/fetch-status")
        first_job_id = first_state["job_id"]

        # Second run — must be accepted (new job)
        resp = httpx.post(f"{app_url}/api/v1/channels/{channel_id}/fetch", timeout=10)
        assert resp.status_code == 200, resp.text
        second_job = resp.json()["data"]
        assert second_job["job_id"] != first_job_id, "Same job_id returned — dedup key not cleared"

        # Let it finish so we don't leave dangling tasks
        poll_job_status(app_url, f"/api/v1/channels/{channel_id}/fetch-status")

    @pytest.mark.parametrize("app_url", ALL_BACKENDS)
    def test_status_reaches_terminal_state(self, app_url):
        """Observed statuses during a fetch cycle include terminal state."""
        channel_id = make_channel(app_url)
        httpx.post(f"{app_url}/api/v1/channels/{channel_id}/fetch", timeout=10)

        seen: set[str] = set()
        deadline = time.monotonic() + FETCH_TIMEOUT
        while time.monotonic() < deadline:
            resp = httpx.get(
                f"{app_url}/api/v1/channels/{channel_id}/fetch-status", timeout=10
            )
            assert resp.status_code == 200
            state = resp.json()["data"]
            if state:
                seen.add(state["status"])
            if state and state["status"] in ("done", "failed"):
                break
            time.sleep(POLL_INTERVAL)

        assert seen & {"queued", "running", "done", "failed"}, \
            f"No recognisable status seen; observed: {seen}"
        assert seen & {"done", "failed"}, \
            f"Job never reached terminal state; observed: {seen}"

    @pytest.mark.parametrize("app_url", ALL_BACKENDS)
    def test_fetch_nonexistent_channel_404(self, app_url):
        """POST /fetch on a nonexistent channel returns 404."""
        resp = httpx.post(
            f"{app_url}/api/v1/channels/{uuid.uuid4()}/fetch",
            timeout=10,
        )
        assert resp.status_code == 404

    @pytest.mark.parametrize("app_url", ALL_BACKENDS)
    def test_fetch_status_nonexistent_channel_404(self, app_url):
        """GET /fetch-status on a nonexistent channel returns 404."""
        resp = httpx.get(
            f"{app_url}/api/v1/channels/{uuid.uuid4()}/fetch-status",
            timeout=10,
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Agent run job
# ---------------------------------------------------------------------------

class TestAgentRunJob:
    """POST /agents/{id}/run → background job lifecycle."""

    @pytest.mark.parametrize("app_url", ALL_BACKENDS)
    def test_run_returns_job_state(self, app_url):
        """POST /agents/{id}/run returns a queued job immediately."""
        _, agent_id = make_agent(app_url)
        resp = httpx.post(f"{app_url}/api/v1/agents/{agent_id}/run", timeout=10)
        assert resp.status_code == 200, resp.text
        job = resp.json()["data"]
        assert job["job_type"] == "run_agent"
        assert job["key"] == f"agent:{agent_id}"
        assert job["status"] in ("queued", "running", "done")

    @pytest.mark.parametrize("app_url", ALL_BACKENDS)
    def test_run_status_none_before_any_run(self, app_url):
        """GET /run-status on an agent that was never run returns null status."""
        _, agent_id = make_agent(app_url)
        resp = httpx.get(f"{app_url}/api/v1/agents/{agent_id}/run-status", timeout=10)
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["status"] is None

    @pytest.mark.parametrize("app_url", ALL_BACKENDS)
    def test_run_reaches_terminal_state(self, app_url):
        """Agent run job reaches done or failed and exposes timestamps."""
        _, agent_id = make_agent(app_url)
        httpx.post(f"{app_url}/api/v1/agents/{agent_id}/run", timeout=10)

        state = poll_job_status(app_url, f"/api/v1/agents/{agent_id}/run-status")
        assert state["status"] in ("done", "failed")
        assert state["finished_at"] is not None

    @pytest.mark.parametrize("app_url", ALL_BACKENDS)
    def test_run_dedup_returns_409(self, app_url):
        """Second POST /run while first is active returns 409 ALREADY_RUNNING."""
        _, agent_id = make_agent(app_url)

        resp1 = httpx.post(f"{app_url}/api/v1/agents/{agent_id}/run", timeout=10)
        assert resp1.status_code == 200
        job1_id = resp1.json()["data"]["job_id"]

        resp2 = httpx.post(f"{app_url}/api/v1/agents/{agent_id}/run", timeout=10)
        # Stub handler is very fast, so job may already be done
        if resp2.status_code == 409:
            assert resp2.json()["error"]["code"] == "ALREADY_RUNNING"
            assert resp2.json()["data"]["job_id"] == job1_id
        else:
            assert resp2.status_code == 200

    @pytest.mark.parametrize("app_url", ALL_BACKENDS)
    def test_run_reenqueue_after_completion(self, app_url):
        """After an agent run completes, a new run is accepted (new job_id)."""
        _, agent_id = make_agent(app_url)

        httpx.post(f"{app_url}/api/v1/agents/{agent_id}/run", timeout=10)
        first = poll_job_status(app_url, f"/api/v1/agents/{agent_id}/run-status")

        resp = httpx.post(f"{app_url}/api/v1/agents/{agent_id}/run", timeout=10)
        assert resp.status_code == 200
        second = resp.json()["data"]
        assert second["job_id"] != first["job_id"], \
            "Same job_id returned — dedup key not cleared after completion"

    @pytest.mark.parametrize("app_url", ALL_BACKENDS)
    def test_run_nonexistent_agent_404(self, app_url):
        """POST /run on a nonexistent agent returns 404."""
        resp = httpx.post(
            f"{app_url}/api/v1/agents/{uuid.uuid4()}/run",
            timeout=10,
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Cross-instance dedup (Redis only)
# ---------------------------------------------------------------------------

class TestRedisDistributedDedup:
    """Verify that Redis-backed dedup works across independent HTTP clients
    by checking the shared job state is consistent after enqueue.

    In production, 'multiple clients' would be multiple app instances all
    sharing the same Redis.  Here we use a single app-redis instance but
    validate that the shared Redis state returned via /fetch-status matches
    the job initiated via /fetch, demonstrating the shared-state contract.

    Skipped automatically when no distinct Redis-backed app instance is
    available (single-node docker-compose.test.yml).
    """

    @pytest.fixture(autouse=True)
    def _require_redis(self):
        if not _HAS_DISTINCT_REDIS:
            pytest.skip("No distinct Redis-backed app instance configured")

    def test_status_key_shared_after_enqueue(self):
        """State stored by /fetch is immediately visible via /fetch-status."""
        channel_id = make_channel(APP_REDIS)

        post_resp = httpx.post(f"{APP_REDIS}/api/v1/channels/{channel_id}/fetch", timeout=10)
        assert post_resp.status_code == 200
        enqueued = post_resp.json()["data"]

        # Poll until at least one status update is visible
        get_resp = httpx.get(
            f"{APP_REDIS}/api/v1/channels/{channel_id}/fetch-status", timeout=10
        )
        assert get_resp.status_code == 200
        polled = get_resp.json()["data"]
        assert polled is not None
        assert polled["job_id"] == enqueued["job_id"]
        assert polled["key"] == f"channel:{channel_id}"

    def test_redis_job_state_persists_after_done(self):
        """After a job finishes, /fetch-status still returns the final state."""
        channel_id = make_channel(APP_REDIS)
        httpx.post(f"{APP_REDIS}/api/v1/channels/{channel_id}/fetch", timeout=10)

        done = poll_job_status(APP_REDIS, f"/api/v1/channels/{channel_id}/fetch-status")
        assert done["status"] == "done"

        # Retrieve status again — should still be there (Redis TTL is 24 h)
        resp = httpx.get(
            f"{APP_REDIS}/api/v1/channels/{channel_id}/fetch-status", timeout=10
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["status"] == "done"
        assert resp.json()["data"]["job_id"] == done["job_id"]
