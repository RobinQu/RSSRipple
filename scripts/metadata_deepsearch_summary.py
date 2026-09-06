"""Summarize incomplete experiments honestly; never infer gain from failed calls."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.metadata_corpus.dataset import read_json, write_json  # noqa: E402


def summarize(root: Path) -> dict:
    cases = read_json(root / "cases.json")["cases"]
    oracle = read_json(root / "oracles.json")["cases"]
    summaries = {}
    for directory in sorted(root.glob("runs*")):
        if not directory.is_dir():
            continue
        rows = [read_json(path) for path in sorted(directory.glob("*.json"))]
        statuses = Counter(row.get("error_type", "parsed") for row in rows)
        answers = []
        for row in rows:
            for field, claim in row.get("answer", {}).get("fields", {}).items():
                expected = oracle[row["case_id"]][field]
                known = expected["value"] is not None
                match = (claim.get("status") == "resolved" and known
                         and claim.get("value") == expected["value"])
                issues = [issue for issue in row.get("contract_issues", [])
                          if issue.startswith(field + ":") or ":" not in issue]
                answers.append({"case_id": row["case_id"], "arm": row["arm"], "iteration": row["iteration"],
                    "field": field, "status": claim.get("status"), "value": claim.get("value"),
                    "matches_provisional_value": match,
                    "auto_apply_candidate_only": match and expected["auto_apply_safe"] and not issues,
                    "note": "Value/quote checks do not replace per-answer entailment review."})
        summaries[directory.name] = {"expected_request_results": len(cases) * 2 * 3,
            "recorded_request_results": len(rows), "status_counts": dict(statuses),
            "complete_without_execution_errors": (
                len(rows) == len(cases) * 6 and not any("error_type" in r for r in rows)),
            "prompt_tokens": sum((r.get("usage") or {}).get("prompt_tokens", 0) for r in rows),
            "completion_tokens": sum((r.get("usage") or {}).get("completion_tokens", 0) for r in rows),
            "sum_request_elapsed_seconds": round(sum(r["elapsed_seconds"] for r in rows), 3), "field_results": answers}
    evidence = [read_json(p) for p in (root / "evidence").glob("*.gz")]
    baseline = [read_json(p) for p in (root / "baseline").glob("*.json")]
    return {"case_count": len(cases), "field_count": sum(len(c["fields"]) for c in cases),
        "evidence": {"cases": len(evidence), "search_requests": sum(len(e["searches"]) for e in evidence),
            "page_attempts": sum(len(e["pages"]) for e in evidence),
            "nonempty_pages": sum(bool(p.get("text")) for e in evidence for p in e["pages"])},
        "baseline": [{"case_id": b["case_id"], "found": b.get("metadata", {}).get("found"),
            "error_type": b.get("error_type"), "source_error": b.get("metadata", {}).get("search_error"),
            "elapsed_seconds": b["elapsed_seconds"]} for b in sorted(baseline, key=lambda b: b["case_id"])],
        "experiments": summaries,
        "overall_refactor_gate": (
            "NOT_EVALUATED: requires complete paired runs, independent per-answer review "
            "and holdout pipeline validation")}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("docs/plans/metadata-deepsearch-validation"))
    args = parser.parse_args()
    report = summarize(args.root)
    write_json(args.root / "summary.json", report)
    print(json.dumps({"case_count": report["case_count"], "field_count": report["field_count"],
                      "evidence": report["evidence"], "gate": report["overall_refactor_gate"]}, ensure_ascii=False))
