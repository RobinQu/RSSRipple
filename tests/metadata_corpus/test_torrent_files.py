"""Every captured torrent must retain its complete original file listing."""

import pytest

from app.services.torrent_inspect import parse_torrent_payload
from tests.metadata_corpus.dataset import ROOT, asset, digest, load_corpus, read_json

MANIFEST = load_corpus(ROOT)[0]
LIST_PATH = asset(ROOT, MANIFEST["file_list_file"])
LISTINGS = read_json(LIST_PATH)


def test_file_evidence_integrity_and_coverage():
    assert digest(LIST_PATH.read_bytes()) == MANIFEST["file_list_sha256"]
    _, corpus, _ = load_corpus(ROOT)
    assert set(LISTINGS) == {c["evidence"]["torrent"] for c in corpus["cases"] if c["evidence"]["torrent"]}
    assert LISTINGS, "empty torrent evidence must never pass"


@pytest.mark.parametrize("relative", sorted(LISTINGS))
def test_original_file_listing(relative):
    raw = asset(ROOT, relative).read_bytes()
    assert digest(raw) == asset(ROOT, relative).stem
    assert parse_torrent_payload(raw) == LISTINGS[relative]
