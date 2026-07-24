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
