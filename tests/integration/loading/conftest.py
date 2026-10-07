"""Reuse isolated database lifecycle fixtures; no shared integration database writes."""
import pytest

from tests.integration.dedup.conftest import dedup_postgres, dedup_turso  # noqa: F401


@pytest.fixture(params=["turso", "postgres"])
def loading_database(request):
    return request.getfixturevalue("dedup_" + request.param)
