"""Artifacts go to a writable report directory, never the read-only fixtures."""

import os
from pathlib import Path

import pytest

from tests.metadata_corpus.dataset import ROOT, audit


@pytest.fixture(scope="session", autouse=True)
def corpus_report_dir(tmp_path_factory):
    configured = os.environ.get("CORPUS_REPORT_DIR")
    directory = Path(configured) if configured else tmp_path_factory.mktemp("metadata-corpus-reports")
    directory.mkdir(parents=True, exist_ok=True)
    audit(ROOT, output=directory / "audit.json")
    return directory
