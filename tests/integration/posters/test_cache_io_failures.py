"""Real HTTP failures and filesystem short writes preserve previous cache bytes."""
import hashlib

import pytest

from app.services import poster_cache
from app.services.metadata_service import download_and_cache_poster
from tests.integration.posters.test_cache_publication import BODY_SHA
from tests.integration.posters.test_cache_publication import recorded_poster as recorded_poster


@pytest.mark.parametrize("failure", ["http_error", "truncated_transport"])
async def test_legacy_cache_survives_failed_refetch(recorded_poster, failure):
    state = recorded_poster
    state.cache.mkdir()
    old = state.body[:4096]
    state.final.write_bytes(old)
    if failure == "http_error":
        state.status = 503
    else:
        state.send_size = 4096  # Content-Length still advertises the complete recorded response.
    assert await download_and_cache_poster(state.url) is None
    assert state.final.read_bytes() == old
    assert not list(state.cache.glob("*.part"))
    state.status = 200
    state.send_size = len(state.body)
    assert await download_and_cache_poster(state.url)
    assert hashlib.sha256(state.final.read_bytes()).hexdigest() == BODY_SHA
    assert state.hits == 2


@pytest.mark.parametrize("failure", ["short_write", "fsync", "permissions"])
async def test_filesystem_failure_preserves_legacy_file(recorded_poster, monkeypatch, failure):
    state = recorded_poster
    state.cache.mkdir()
    old = state.body[:4096]
    state.final.write_bytes(old)
    real_fdopen = poster_cache.os.fdopen

    class ShortWriter:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            self.handle.__enter__()
            return self

        def __exit__(self, *args):
            return self.handle.__exit__(*args)

        def write(self, content):
            return self.handle.write(content[:17])

    def short_fdopen(*args, **kwargs):
        return ShortWriter(real_fdopen(*args, **kwargs))

    def failed(*args, **kwargs):
        raise OSError("synthetic filesystem failure")

    with monkeypatch.context() as fault:
        if failure == "short_write":
            fault.setattr(poster_cache.os, "fdopen", short_fdopen)
        elif failure == "fsync":
            fault.setattr(poster_cache.os, "fsync", failed)
        else:
            fault.setattr(poster_cache, "_preserve_mode", failed)
        assert await download_and_cache_poster(state.url) is None
    assert state.final.read_bytes() == old
    assert not list(state.cache.glob("*.part"))
    assert await download_and_cache_poster(state.url)
    assert hashlib.sha256(state.final.read_bytes()).hexdigest() == BODY_SHA
