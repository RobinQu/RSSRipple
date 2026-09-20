"""Read-only audit of captured assignments; does not assert torrent completeness."""

import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path


def merged_intervals(intervals):
    result = []
    for lo, hi in sorted(intervals):
        if result and lo <= result[-1][1] + 1:
            result[-1][1] = max(result[-1][1], hi)
        else:
            result.append([lo, hi])
    return result


def audit(path):
    raw = path.read_bytes()
    tables = json.loads(raw)["tables"]
    by_resource = defaultdict(list)
    for assignment in tables["resource_file_assignments"]:
        by_resource[assignment["resource_id"]].append(assignment)
    gaps, ambiguous, described = [], [], []
    for resource in tables["file_resources"]:
        if not resource["is_batch"]:
            continue
        groups = defaultdict(list)
        uncertain = []
        assignments = by_resource[resource["id"]]
        for item in assignments:
            if item["series_id"] and not item["movie_id"]:
                lo = item["episode_start"] if item["episode_start"] is not None else item["episode_end"]
                hi = item["episode_end"] if item["episode_end"] is not None else item["episode_start"]
                season = item["season"]
                if not all(type(n) is int and n >= 0 for n in (lo, hi, season)) or lo > hi:
                    uncertain.append(item["id"])
                    continue
                groups[("series", item["series_id"], season)].append((lo, hi))
            elif not item["movie_id"] or item["series_id"]:
                uncertain.append(item["id"])
        coverage = [
            dict(work_type=k[0], work_id=k[1], season=k[2], intervals=merged_intervals(v))
            for k, v in sorted(groups.items())
        ]
        if any(len(group["intervals"]) > 1 for group in coverage):
            gaps.append(dict(resource_id=resource["id"], coverage=coverage))
        if uncertain or not assignments:
            ambiguous.append(
                dict(resource_id=resource["id"], uncertain_assignment_ids=uncertain, no_assignments=not assignments)
            )
        described.append(
            dict(
                resource_id=resource["id"],
                batch_scope=resource["batch_scope"],
                assignment_count=len(assignments),
                coverage=coverage,
            )
        )
    return dict(
        fixture=str(path),
        sha256=hashlib.sha256(raw).hexdigest(),
        resource_count=len(tables["file_resources"]),
        batch_count=len(described),
        batches_with_episode_gaps=gaps,
        batches_with_missing_assignment_evidence=ambiguous,
        batch_assignment_descriptions=described,
        decision_states=[
            dict(id=row["id"], status=row["status"], candidate_count=len(row["candidates"]))
            for row in tables["pending_decisions"]
        ],
        limitation="Captured pre-split graph only; assignments do not prove complete torrent listing; no DB changes",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixture", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    report = audit(args.fixture)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(
        json.dumps(
            {
                key: len(value) if isinstance(value, list) else value
                for key, value in report.items()
                if key != "batch_assignment_descriptions"
            }
        )
    )
