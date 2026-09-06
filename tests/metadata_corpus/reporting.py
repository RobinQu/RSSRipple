"""Persist semantic failures as CI artifacts, including early replay errors."""

from pathlib import Path

from .dataset import digest, write_json
from .runner import run_scenario


async def run_reported_scenario(root: Path, scenario: dict, database_url: str, report_dir: Path) -> dict:
    # Manifest IDs are labels, not trusted filesystem paths.
    path = report_dir / f"scenario-{digest(scenario['id'].encode())[:16]}.json"
    try:
        result = await run_scenario(root, scenario, database_url)
    except Exception as exc:
        write_json(path, {"scenario": scenario["id"], "mode": "replay", "passed": False,
                          "error_type": type(exc).__name__, "error": str(exc)})
        raise
    write_json(path, result)
    return result
