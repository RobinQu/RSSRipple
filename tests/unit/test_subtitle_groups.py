import json
from pathlib import Path

import pytest

from app.services.subtitle_groups import (
    canonical_group_set,
    join_legacy_subtitle_group,
    normalize_subtitle_groups,
    split_subtitle_group_candidates,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("豌豆字幕组&LoliHouse", ["豌豆字幕组", "LoliHouse"]),
        ("喵萌奶茶屋&amp;LoliHouse", ["喵萌奶茶屋", "LoliHouse"]),
        ("雪飄工作室＆WBX", ["雪飄工作室", "WBX"]),
        ("Group++Name", ["Group++Name"]),
        (["LoliHouse", "lolihouse", " ANi "], ["LoliHouse", "ANi"]),
    ],
)
def test_normalize_subtitle_groups(raw, expected):
    assert normalize_subtitle_groups(raw) == expected


def test_set_identity_ignores_order_and_case():
    assert canonical_group_set(["LoliHouse", "ANi"]) == canonical_group_set(["ani", "lolihouse"])


def test_legacy_join_is_stable():
    assert join_legacy_subtitle_group(["豌豆字幕组", "LoliHouse"]) == "豌豆字幕组&LoliHouse"


def test_fixture_matches_parser_candidates():
    fixture = json.loads(
        (Path(__file__).parents[1] / "fixtures" / "subtitle_groups.json").read_text(encoding="utf-8")
    )
    for item in fixture:
        assert split_subtitle_group_candidates(item["subtitle_group"]) == item["expected_groups"]
