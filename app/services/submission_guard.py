"""Synchronizer token pattern: form submission tokens.

Tokens are issued by GET /api/v1/channels/form-token. Browser sessions
(requests carrying the rssripple_auth cookie) must include one in
POST /channels and PUT /channels/{id} via the X-Form-Token request header;
pure API-key programmatic calls are exempt.

A token is not burned on first use. consume() rejects only a *repeat* consume
that arrives within DUPLICATE_WINDOW_SECONDS of the previous one (double-click,
HTTP retry storm) with 409 DUPLICATE_SUBMISSION. A later consume of the same
token is accepted again: a handler that fails validation after consuming the
token (422 INVALID_FEED, 404, 500) must not force the client to re-GET a fresh
form token before fixing and resubmitting.

Tokens expire after TTL_SECONDS to prevent unbounded state growth.

Two backends are provided, mirroring task_queue's MemoryQueue/RedisQueue:
- SubmissionGuard       — in-process dict, single-process deployments
- RedisSubmissionGuard  — redis.asyncio-backed, shared across workers

The module-level ``submission_guard`` facade routes to the Redis backend when
the process-level task queue is a started RedisQueue (multi-worker
deployments: default docker-compose is PostgreSQL+Redis with APP_ROLE
web/worker), and falls back to the in-process guard otherwise (Turso
single-node / MemoryQueue).
"""
import asyncio
import json
import time
import uuid


class SubmissionGuard:
    """In-process token store. State is not shared across processes."""

    # 1 hour: channel-form submission can be preceded by a long AI field-mapping
    # analysis (LLM + feed fetch), so a 5-minute token expired before the user
    # hit save, surfacing a misleading "already submitted" 409.
    TTL_SECONDS = 3600  # 1 hour

    # Window in which a repeat consume of the same token is treated as a
    # duplicate submission and rejected. Wide enough to cover double-clicks
    # and client-side retry storms (including a slow first attempt: consume
    # happens at handler entry, before feed validation), short enough that a
    # fix-and-resubmit retry after a failed validation is accepted again.
    DUPLICATE_WINDOW_SECONDS = 10

    def __init__(self):
        # token → (issued_at, consumed_at) — consumed_at is None until the
        # first consume; both are monotonic timestamps.
        self._tokens: dict[str, tuple[float, float | None]] = {}
        self._lock = asyncio.Lock()

    async def issue(self) -> str:
        """Generate and store a new token, return it."""
        token = str(uuid.uuid4())
        async with self._lock:
            self._purge_expired(time.monotonic())
            self._tokens[token] = (time.monotonic(), None)
        return token

    async def consume(self, token: str) -> bool:
        """Validate a token and record a submission attempt.

        Returns True if the token was issued, is unexpired, and was not
        already consumed within DUPLICATE_WINDOW_SECONDS. Returns False for
        unknown/expired tokens and rapid duplicate submissions. The record is
        kept (not deleted) so duplicates stay detectable; expired records are
        reclaimed by the TTL purge.
        """
        async with self._lock:
            now = time.monotonic()
            self._purge_expired(now)
            entry = self._tokens.get(token)
            if entry is None:
                return False
            issued_at, consumed_at = entry
            if (consumed_at is not None
                    and now - consumed_at <= self.DUPLICATE_WINDOW_SECONDS):
                return False
            self._tokens[token] = (issued_at, now)
            return True

    def _purge_expired(self, now: float) -> None:
        expired = [t for t, (issued_at, _) in self._tokens.items() if now - issued_at > self.TTL_SECONDS]
        for t in expired:
            del self._tokens[t]


_FORM_TOKEN_PFX = "rssripple:form-token:"


class RedisSubmissionGuard:
    """Redis-backed token store, shared by every web/worker process.

    Keys are ``rssripple:form-token:<token>`` string values holding
    ``{"consumed_at": <unix seconds>|null}``, created by issue() with
    ``SET EX TTL_SECONDS`` so Redis itself reclaims expired tokens.

    consume() runs an optimistic WATCH/MULTI transaction (the same idiom as
    RedisQueue) so the duplicate-window check is atomic across processes: a
    concurrent second consume retries after the first commits, observes the
    fresh consumed_at inside the window, and is rejected. Timestamps come
    from the Redis server's TIME command, so worker clock skew cannot widen
    or shrink the duplicate window. The token record is never deleted —
    the update preserves the remaining TTL (SET ... PX <pttl>), matching the
    in-process backend's "expire TTL_SECONDS after issue" semantics.

    Redis failures propagate (same stance as task_queue ownership checks):
    silently degrading to a per-process store would mask exactly the
    cross-worker duplicate submissions this backend exists to prevent.
    """

    TTL_SECONDS = SubmissionGuard.TTL_SECONDS
    DUPLICATE_WINDOW_SECONDS = SubmissionGuard.DUPLICATE_WINDOW_SECONDS

    def __init__(self, redis_client):
        self._redis = redis_client

    async def issue(self) -> str:
        """Generate and store a new token, return it."""
        token = str(uuid.uuid4())
        await self._redis.set(
            _FORM_TOKEN_PFX + token,
            json.dumps({"consumed_at": None}),
            ex=self.TTL_SECONDS,
        )
        return token

    async def consume(self, token: str) -> bool:
        """Validate a token and record a submission attempt.

        Same contract as SubmissionGuard.consume: True for an issued,
        unexpired token not consumed within DUPLICATE_WINDOW_SECONDS; False
        for unknown/expired tokens and rapid duplicate submissions.
        """
        from redis.exceptions import WatchError

        key = _FORM_TOKEN_PFX + token
        while True:
            async with self._redis.pipeline(transaction=True) as pipe:
                try:
                    await pipe.watch(key)
                    raw = await pipe.get(key)
                    if raw is None:
                        return False
                    data = json.loads(raw)
                    seconds, micros = await pipe.time()
                    now = seconds + micros / 1_000_000
                    consumed_at = data.get("consumed_at")
                    if (consumed_at is not None
                            and now - consumed_at <= self.DUPLICATE_WINDOW_SECONDS):
                        return False
                    pttl = await pipe.pttl(key)
                    pipe.multi()
                    data["consumed_at"] = now
                    payload = json.dumps(data)
                    if pttl > 0:
                        pipe.set(key, payload, px=pttl)
                    else:
                        pipe.set(key, payload)
                    await pipe.execute()
                    return True
                except WatchError:
                    continue


class _DispatchingSubmissionGuard:
    """Module-level facade keeping the channels.py call sites unchanged.

    Each call resolves the backend from the process-level task queue: a
    started RedisQueue exposes its connected client via ``redis_client``,
    meaning this deployment is multi-worker and token state must be shared;
    anything else (MemoryQueue, queue not started yet) uses the in-process
    guard, whose behaviour is unchanged.
    """

    def __init__(self):
        self._local = SubmissionGuard()
        self._redis_guard: RedisSubmissionGuard | None = None
        self._redis_client = None

    def _backend(self):
        from app.services.task_queue import task_queue

        client = getattr(task_queue, "redis_client", None)
        if client is None:
            return self._local
        if self._redis_guard is None or self._redis_client is not client:
            self._redis_guard = RedisSubmissionGuard(client)
            self._redis_client = client
        return self._redis_guard

    async def issue(self) -> str:
        return await self._backend().issue()

    async def consume(self, token: str) -> bool:
        return await self._backend().consume(token)


# Module-level singleton — shared across all requests in one process, and
# across processes whenever the deployment runs on the Redis queue backend.
submission_guard = _DispatchingSubmissionGuard()
