#!/usr/bin/env python3
"""Export, audit, record or replay the production-derived metadata corpus."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.metadata_corpus.dataset import (
    ROOT,
    audit,
    build_corpus,
    export_docker_graph,
    hydrate_torrents,
    index_files,
    load_corpus,
    read_json,
    validate_review,
    write_json,
)


async def execute(args):
    from tests.metadata_corpus.runner import run_scenario
    manifest, _, _ = load_corpus(args.corpus)
    scenarios = [s for s in manifest["scenarios"] if not args.scenario or s["id"] in args.scenario]
    if not scenarios:
        raise ValueError("no reviewed scenarios selected; audit/record first (an empty gate is not a pass)")
    mode = {"run": "replay", "record": "record", "record-llm": "record-llm", "llm": "llm"}[args.command]
    results = []
    with tempfile.TemporaryDirectory(prefix="metadata-corpus-db-") as temporary:
        for iteration in range(3 if mode == "llm" else 1):
            for index, scenario in enumerate(scenarios):
                url = args.test_db_url or f"sqlite+aioturso:///{temporary}/{iteration}-{index}.db"
                try:
                    result = await run_scenario(args.corpus, scenario, url, mode=mode)
                except Exception as exc:
                    result = {"scenario": scenario["id"], "passed": False,
                              "error_type": type(exc).__name__, "error": str(exc)}
                results.append({"iteration": iteration + 1, **result})
    report = {"mode": mode, "passed": all(r["passed"] for r in results), "results": results}
    write_json(args.report, report)
    print(json.dumps({"passed": report["passed"], "runs": len(results), "report": str(args.report)}))
    return 0 if report["passed"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["export", "hydrate", "index-files", "audit", "prepare", "review",
                                            "run", "record", "record-llm", "llm"])
    parser.add_argument("--corpus", type=Path, default=ROOT)
    parser.add_argument("--torrent-dir", type=Path, default=Path("data/torrents"))
    parser.add_argument("--refresh-unreviewed", action="store_true", help="Refresh only an entirely unreviewed draft")
    parser.add_argument("--test-db-url", help="Database named metadata_corpus_test; defaults to temporary Turso")
    parser.add_argument("--scenario", action="append")
    parser.add_argument("--case", action="append", help="Production resource UUID, repeat for incremental sequences")
    parser.add_argument("--expected-file", type=Path, help="Independently reviewed expected JSON (review command)")
    parser.add_argument("--note", help="Evidence supporting the reviewed answer")
    parser.add_argument("--llm-base-url", help="Recording endpoint override for a host/container address difference")
    parser.add_argument("--report", type=Path, default=Path("/tmp/metadata-corpus-report.json"))
    args = parser.parse_args()
    if args.command == "index-files":
        print(json.dumps(index_files(args.corpus)))
        return 0
    if args.command == "prepare":
        from app.config import settings
        from tests.metadata_corpus.replay import clean_url
        if not args.case or not args.scenario or len(args.scenario) != 1:
            parser.error("prepare requires --case and one --scenario")
        manifest, corpus, _ = load_corpus(args.corpus)
        if any(s["id"] == args.scenario[0] for s in manifest["scenarios"]):
            parser.error("scenario exists; use a new scenario name")
        if not set(args.case) <= {c["id"] for c in corpus["cases"]}:
            parser.error("unknown case ID")
        llm_url = clean_url(args.llm_base_url or settings.llm_base_url)
        manifest["scenarios"].append({
            "id": args.scenario[0], "case_ids": args.case,
            "cassette": f"http/{args.scenario[0]}.json.gz", "model": settings.llm_model,
            "llm_host": urlsplit(llm_url).hostname, "llm_base_url": llm_url,
            "source_hosts": ["api.bgm.tv", "api.themoviedb.org", "image.tmdb.org", "lain.bgm.tv",
                             "en.wikipedia.org", "zh.wikipedia.org", "ja.wikipedia.org",
                             urlsplit(settings.wigolo_base_url).hostname],
        })
        write_json(args.corpus / "manifest.json", manifest)
        return 0
    if args.command == "review":
        if not args.case or len(args.case) != 1 or not args.expected_file or not args.note:
            parser.error("review requires one --case, --expected-file and --note")
        manifest, corpus, reviews = load_corpus(args.corpus)
        if args.case[0] not in {c["id"] for c in corpus["cases"]}:
            parser.error("unknown case ID")
        reviews[args.case[0]] = {"status": "confirmed", "expected": read_json(args.expected_file),
                                 "evidence_note": args.note}
        validate_review(reviews[args.case[0]], args.case[0])
        write_json(args.corpus / manifest["review_file"], reviews)
        print(json.dumps(audit(args.corpus), ensure_ascii=False))
        return 0
    if args.command == "hydrate":
        print(json.dumps(hydrate_torrents(args.corpus), ensure_ascii=False))
        return 0
    if args.command == "export":
        print(json.dumps(build_corpus(export_docker_graph(), args.torrent_dir, args.corpus,
                                     refresh_unreviewed=args.refresh_unreviewed), ensure_ascii=False))
        return 0
    if args.command == "audit":
        print(json.dumps(audit(args.corpus), ensure_ascii=False))
        return 0
    return asyncio.run(execute(args))


if __name__ == "__main__":
    raise SystemExit(main())
