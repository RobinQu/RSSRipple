"""Package the reviewed experiment for offline integration tests (no network).

Recorded model answers are observations, NOT semantic gold. Preserve known
contract failures as negative regressions; never silently overwrite a release.
"""
import argparse
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.metadata_deepsearch_eval import evidence_input  # noqa: E402
from tests.metadata_corpus.dataset import read_json, write_json  # noqa: E402


def freeze(source: Path, output: Path):
    if output.exists():
        raise ValueError("fixture exists; choose a new version")
    inputs = {}

    def read(relative):
        path = source / relative
        inputs[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        return read_json(path)

    cases = read("cases.json")["cases"]
    packaged = []
    for case in cases:
        evidence = read(f"evidence/{case['id']}.json.gz")
        runs = []
        for arm in ("snippets", "pages"):
            for iteration in range(1, 4):
                run = read(f"runs-recovery-v1/{case['id']}-{arm}-{iteration}.json")
                if run.get("error_type"):
                    raise ValueError("cannot freeze incomplete execution")
                runs.append({"arm": arm, "iteration": iteration, "answer": run["answer"],
                             "expected_contract_issues": run["contract_issues"]})
        packaged.append({"id": case["id"], "resource_id": case["resource_id"],
                         "raw_title": case["raw_title"],
                         "payloads": {arm: evidence_input(case, evidence, arm) for arm in ("snippets", "pages")},
                         "runs": runs})
    baseline = read("recovery-baseline/baseline/kozame-season2.json")["metadata"]
    write_json(output, {"version": 1, "semantic_gold": False, "source_sha256": inputs,
                        "cases": packaged, "legitimate_fallback": baseline})
    print(f"Frozen {len(packaged)} cases, {sum(len(c['runs']) for c in packaged)} observations to {output}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("docs/plans/metadata-deepsearch-validation"))
    parser.add_argument("--output", type=Path, default=Path("tests/fixtures/deepsearch_corpus_v1.json.gz"))
    args = parser.parse_args()
    freeze(args.source, args.output)
