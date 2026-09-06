"""The reviewed subset is a strict gate; pending inventory is reported separately."""

import os

import pytest

from tests.metadata_corpus.dataset import ROOT, load_corpus
from tests.metadata_corpus.runner import run_scenario

SCENARIOS = load_corpus(ROOT)[0]["scenarios"]


def test_gate_is_not_empty():
    assert SCENARIOS, "No reviewed scenarios: an empty corpus is not a passing gate"


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s["id"])
async def test_production_reconstruction(scenario, tmp_path):
    database_url = os.environ.get("CORPUS_TEST_DATABASE_URL") or f"sqlite+aioturso:///{tmp_path}/corpus.db"
    result = await run_scenario(ROOT, scenario, database_url)
    assert result["passed"], result["differences"]
