"""Synchronizer token pattern: single-use form submission tokens.

Tokens are issued by GET /api/v1/channels/form-token and must be included
in POST /channels and PUT /channels/{id} via the X-Form-Token request header.
A token can only be consumed once; a second request carrying the same token
is rejected with 409 DUPLICATE_SUBMISSION.

Tokens expire after TTL_SECONDS to prevent unbounded memory growth.
"""
import asyncio
import time
import uuid


class SubmissionGuard:
    TTL_SECONDS = 300  # 5 minutes

    def __init__(self):
        self._tokens: dict[str, float] = {}  # token → issued_at (monotonic)
        self._lock = asyncio.Lock()

    async def issue(self) -> str:
        """Generate and store a new single-use token, return it."""
        token = str(uuid.uuid4())
        async with self._lock:
            self._purge_expired(time.monotonic())
            self._tokens[token] = time.monotonic()
        return token

    async def consume(self, token: str) -> bool:
        """Atomically validate and delete a token.

        Returns True if the token was valid and is now consumed.
        Returns False if the token was already used, never issued, or expired.
        """
        async with self._lock:
            self._purge_expired(time.monotonic())
            if token not in self._tokens:
                return False
            del self._tokens[token]
            return True

    def _purge_expired(self, now: float) -> None:
        expired = [t for t, ts in self._tokens.items() if now - ts > self.TTL_SECONDS]
        for t in expired:
            del self._tokens[t]


# Module-level singleton — shared across all requests in one process
submission_guard = SubmissionGuard()
