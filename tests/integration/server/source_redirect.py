"""Test-only HTTP routing, installed solely by the mock-LLM app entrypoint.

Keep the production source adapters, HTTP decoding and evidence generation.
Only TMDB's network destination changes; no metadata/validator is patched.
"""

import httpx


def redirect_tmdb(request: httpx.Request) -> None:
    if request.url.host == "api.themoviedb.org":
        request.url = request.url.copy_with(
            scheme="http",
            host="test-server",
            port=8080,
            path="/tmdb" + request.url.path,
        )
        request.headers["host"] = "test-server:8080"


async def redirect_tmdb_async(request: httpx.Request) -> None:
    redirect_tmdb(request)


def install() -> None:
    class SourceClient(httpx.Client):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.event_hooks["request"].append(redirect_tmdb)

    class AsyncSourceClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.event_hooks["request"].append(redirect_tmdb_async)

    httpx.Client = SourceClient
    httpx.AsyncClient = AsyncSourceClient
