"""Audit and backfill plural subtitle-group parsing.

Read-only by default.  The exported fixture is intentionally small and stable
so unit and integration tests exercise the same production parser and the
same real RSS strings observed in the database.

Examples::

    uv run python scripts/subtitle_groups_eval.py
    uv run python scripts/subtitle_groups_eval.py --export tests/fixtures/subtitle_groups.json
    uv run python scripts/subtitle_groups_eval.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
from pathlib import Path

from sqlalchemy import select

from app.database import async_session_factory, create_tables
from app.models.file_resource import FileResource
from app.models.subtitle_group_mapping import SubtitleGroupMapping
from app.services.subtitle_groups import (
    canonical_group_set,
    has_group_separator,
    normalize_group_key,
    normalize_subtitle_groups,
)


async def run(*, export: Path | None = None, apply: bool = False) -> int:
    if apply:
        await create_tables()
    async with async_session_factory() as db:
        rows = list((await db.execute(
            select(FileResource).where(FileResource.subtitle_group.isnot(None))
        )).scalars().all())
        fixture: dict[str, dict] = {}
        changed = unresolved = 0
        for resource in rows:
            raw = resource.subtitle_group
            groups = normalize_subtitle_groups(raw)
            if has_group_separator(raw) and len(groups) < 2:
                unresolved += 1
            if has_group_separator(raw):
                key = normalize_group_key(raw)
                fixture.setdefault(key, {
                    "title_raw": resource.title_raw,
                    "subtitle_group": raw,
                    "expected_groups": groups,
                })
            if apply and groups and resource.subtitle_groups != groups:
                resource.subtitle_groups = groups
                resource.subtitle_groups_source = "heuristic" if has_group_separator(raw) else "single"
                changed += 1
            if apply:
                mapping = (await db.execute(
                    select(SubtitleGroupMapping).where(
                        SubtitleGroupMapping.normalized_key == normalize_group_key(raw)
                    )
                )).scalar_one_or_none()
                if mapping is None:
                    mapping = SubtitleGroupMapping(
                        raw_value=raw,
                        normalized_key=normalize_group_key(raw),
                        groups=groups,
                        resolution="heuristic" if has_group_separator(raw) else "single",
                    )
                    db.add(mapping)
                elif mapping.resolution not in {"llm", "manual"}:
                    mapping.groups = groups
                    mapping.resolution = "heuristic" if has_group_separator(raw) else "single"
        if apply:
            config_changes = await migrate_persisted_configs(db)
            await db.commit()
        else:
            config_changes = 0
        if export is not None:
            export.parent.mkdir(parents=True, exist_ok=True)
            export.write_text(
                json.dumps(sorted(fixture.values(), key=lambda item: item["subtitle_group"]),
                            ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        multi = [item for item in fixture.values() if len(item["expected_groups"]) > 1]
        print(json.dumps({
            "resources_with_legacy_group": len(rows),
            "distinct_compound_values": len(fixture),
            "compound_values_with_multiple_members": len(multi),
            "unresolved_compounds": unresolved,
            "resources_backfilled": changed if apply else 0,
            "config_rows_migrated": config_changes,
            "fixture": str(export) if export else None,
        }, ensure_ascii=False, sort_keys=True))
        return 0 if unresolved == 0 else 2


def validate_fixture(path: Path) -> int:
    items = json.loads(path.read_text(encoding="utf-8"))
    errors = []
    for item in items:
        actual = normalize_subtitle_groups(item.get("subtitle_group"))
        expected = normalize_subtitle_groups(item.get("expected_groups"), split=False)
        if actual != expected and canonical_group_set(actual) != canonical_group_set(expected):
            errors.append({"subtitle_group": item.get("subtitle_group"), "actual": actual, "expected": expected})
    if errors:
        print(json.dumps({"errors": errors}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps({"fixture_rows": len(items), "status": "ok"}, ensure_ascii=False))
    return 0


def migrate_filter_node(node):
    """Translate persisted legacy field names without changing tree shape."""
    if isinstance(node, list):
        return [migrate_filter_node(item) for item in node]
    if not isinstance(node, dict):
        return node
    out = {key: migrate_filter_node(value) for key, value in node.items()}
    if out.get("field") == "subtitle_group":
        out["field"] = "subtitle_groups"
        if out.get("operator") in {"eq", "ne"}:
            out["value"] = normalize_subtitle_groups(out.get("value"), split=False)
    return out


async def migrate_persisted_configs(db) -> int:
    """Migrate agent/organize JSON configs in-place; return changed rows."""
    from app.models.agent import Agent
    from app.models.agent_work import AgentWork
    from app.models.channel import Channel
    from app.models.organize_rule import OrganizeRule

    changed = 0
    for model, attr in (
        (Agent, "filter_config"),
        (AgentWork, "filter_overrides"),
        (OrganizeRule, "filter"),
    ):
        rows = list((await db.execute(select(model))).scalars().all())
        for row in rows:
            value = getattr(row, attr, None)
            migrated = migrate_filter_node(value)
            if migrated != value:
                setattr(row, attr, migrated)
                changed += 1
    for channel in list((await db.execute(select(Channel))).scalars().all()):
        mapping = copy.deepcopy(channel.field_mapping or {})
        fields = mapping.get("field_mappings") if isinstance(mapping, dict) else None
        if isinstance(fields, dict) and "subtitle_group" in fields and "subtitle_groups" not in fields:
            fields["subtitle_groups"] = fields.pop("subtitle_group")
            channel.field_mapping = mapping
            changed += 1
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export", type=Path, help="write a JSON fixture from compound DB values")
    parser.add_argument("--validate", type=Path, help="validate an exported fixture without DB access")
    parser.add_argument("--apply", action="store_true", help="backfill subtitle_groups and mappings")
    args = parser.parse_args()
    if args.validate:
        return validate_fixture(args.validate)
    return asyncio.run(run(export=args.export, apply=args.apply))


if __name__ == "__main__":
    raise SystemExit(main())
