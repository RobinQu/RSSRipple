"""Use real elapsed time even if another suite imports unit-test patches."""
import asyncio

import pytest


@pytest.fixture(autouse=True)
def actual_sleep(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", asyncio.tasks.sleep)
