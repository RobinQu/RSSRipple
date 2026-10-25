"""Integration test suite root conftest.

The shared HTTP fixtures (``test_server``, ``rssripple_url``, ``http_client``,
and the session-scoped autouse ``setup_test_environment``) live in
``tests/integration/http/conftest.py`` and apply only to the HTTP-level tests.

Direct-Python tests under ``tests/integration/external/`` and the eval app
tests under ``tests/integration/eval/`` run without the docker test-server
seed (they only need LLM_API_KEY / TMDB_API_KEY).

Cross-suite hermeticity (runtime_config override reset) is handled by the
root ``tests/conftest.py``.
"""

from pathlib import Path

import pytest

# server/ is the fake test-server's application code (built into its own Docker
# image), not test modules. Exclude it from collection so pytest doesn't try to
# collect its ``TestFile`` dataclass (PytestCollectionWarning).
collect_ignore = ["server"]


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items):
    for item in items:
        try:
            relative = item.path.relative_to(Path(__file__).parent)
        except ValueError:
            continue
        group = None
        if relative.parts[0] in {"http", "queue_recovery", "metadata_corpus"}:
            group = relative.parts[0]
        elif relative.parts[:2] == ("agent_run_lifecycle", "test_redis_disconnect.py"):
            group = "queue_recovery"
        elif relative.parts[0] == "resources":
            group = "reparse_redis"
        else:
            group = relative.as_posix()
        item.add_marker(pytest.mark.xdist_group(group))
