"""Export input/evidence/answer separately; never seed a cold run with answers."""

from __future__ import annotations

import gzip
import hashlib
import json
import subprocess
import tarfile
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

import bencodepy

ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "metadata_corpus_v1"
WORK_FIELDS = "id title_cn title_en original_title canonical_name aliases external_source external_id collection_id season_number number_of_episodes start_date end_date release_date is_anime genre manually_edited_fields"
FIELDS = {
    "channels": "id name field_mapping metadata_agent_enabled metadata_source metadata_fallback_sources required_metadata_fields default_is_anime",
    "work_collections": "id title_cn title_en original_title aliases external_source external_id",
    "tv_series": WORK_FIELDS,
    "movies": WORK_FIELDS,
    "audio_works": "id title_cn title_en original_title external_source external_id",
    "work_external_ids": "source external_id work_type work_id",
    "episodes": "series_id season episode title air_date",
    "file_resources": "id channel_id title_raw series_id movie_id audio_work_id collection_id season episode absolute_episode episode_confidence is_batch batch_scope batch_seasons season_ranges episode_start episode_end search_title title_cn title_en resolution source subtitle_langs subtitle_groups video_codec audio_codec container subtitle_type metadata_failure_type metadata_matched_at",
    "resource_work_links": "resource_id series_id movie_id source",
    "resource_file_assignments": "resource_id file_path series_id movie_id season episode_start episode_end source",
}


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    path.write_bytes(gzip.compress(raw, mtime=0) if path.suffix == ".gz" else raw)


def read_json(path: Path):
    raw = path.read_bytes()
    return json.loads(gzip.decompress(raw) if path.suffix == ".gz" else raw)


def export_docker_graph() -> dict:
    """One consistent read-only SELECT; whitelist columns before leaving postgres."""
    expressions = []
    for table, fields in FIELDS.items():
        keys = ",".join(f"'{field}'" for field in fields.split())
        expressions.append(
            f"'{table}', (SELECT coalesce(jsonb_agg(x ORDER BY x->>'id'), '[]'::jsonb) "
            f"FROM (SELECT (SELECT jsonb_object_agg(key, value) FROM jsonb_each(to_jsonb(t)) "
            f"WHERE key = ANY(ARRAY[{keys}])) AS x FROM {table} t) s)"
        )
    query = "BEGIN TRANSACTION READ ONLY; SELECT jsonb_build_object(" + ",".join(expressions) + "); ROLLBACK;"
    result = subprocess.run(
        ["docker", "compose", "exec", "-T", "postgres", "psql", "-X", "-qAt", "-v", "ON_ERROR_STOP=1",
         "-U", "rssripple", "-d", "rssripple", "-c", query],
        check=True, capture_output=True, text=True,
    )
    return json.loads(result.stdout)


def sanitize_torrent(raw: bytes) -> bytes:
    """Keep the exact info bytes (and infohash), remove tracker/webseed metadata."""
    decoded = bencodepy.decode(raw)
    if not isinstance(decoded, dict) or not isinstance(decoded.get(b"info"), dict):
        raise ValueError("torrent lacks info dictionary")
    # A canonical re-encode must preserve the original info bytes. Reject
    # noncanonical inputs rather than silently changing a private torrent hash.
    info = bencodepy.encode(decoded[b"info"])
    if raw.count(b"4:info" + info) != 1:
        raise ValueError("noncanonical or ambiguous info encoding")
    return b"d4:info" + info + b"e"


def work_key(kind: str, row: dict) -> str:
    """Case-local semantic identity, independent of generated ORM UUIDs."""
    source, identity = row.get("external_source"), row.get("external_id")
    if source and identity:
        base = f"{kind}:{source}:{identity}"
    else:
        title = row.get("title_cn") or row.get("title_en") or row.get("original_title")
        base = f"{kind}:title:{title}"
    return f"{base}:s{row.get('season_number')}" if kind == "series" else base


def resource_answer(row: dict, graph: dict) -> dict:
    works = {
        kind: {w["id"]: w for w in graph[table]}
        for kind, table in (("series", "tv_series"), ("movie", "movies"), ("audio", "audio_works"))
    }
    def identity(item):
        for kind in works:
            key = "audio_work_id" if kind == "audio" else f"{kind}_id"
            if item.get(key):
                work = works[kind].get(item[key])
                return work_key(kind, work) if work else f"missing:{item[key]}"
        return None

    associated = [identity(row)] if identity(row) else []
    associated += [identity(link) for link in graph["resource_work_links"] if link["resource_id"] == row["id"]]
    fields = "season episode is_batch batch_scope batch_seasons season_ranges episode_start episode_end"
    answer = {key: row.get(key) for key in fields.split()}
    answer["works"] = sorted(set(filter(None, associated)))
    collections = {c["id"]: c for c in graph["work_collections"]}
    collection = collections.get(row.get("collection_id"))
    answer["collection"] = work_key("collection", collection) if collection else None
    answer["work_details"] = {}
    for kind, records in works.items():
        for work in records.values():
            key = work_key(kind, work)
            if key not in answer["works"]:
                continue
            parent = collections.get(work.get("collection_id"))
            details = {name: work.get(name) for name in (
                "season_number", "number_of_episodes", "start_date", "release_date", "is_anime",
            ) if name in work}
            for name, value in details.items():
                if hasattr(value, "isoformat"):
                    details[name] = value.isoformat()
            details["collection"] = work_key("collection", parent) if parent else None
            if kind == "series" and "episodes" in graph:
                episodes = [e for e in graph["episodes"] if e["series_id"] == work["id"]]
                details["episodes"] = sorted(e["episode"] for e in episodes)
                details["episode_seasons"] = sorted({e["season"] for e in episodes})
            answer["work_details"][key] = details
    answer["assignments"] = sorted([
        {"file_path": a["file_path"], "work": identity(a),
         **{k: a.get(k) for k in ("season", "episode_start", "episode_end")}}
        for a in graph["resource_file_assignments"] if a["resource_id"] == row["id"]
    ], key=lambda a: a["file_path"])
    return answer


def build_corpus(graph: dict, torrent_dir: Path, output: Path, *, refresh_unreviewed=False) -> dict:
    scenarios = []
    if (output / "manifest.json").exists():
        previous, _, reviews = load_corpus(output)
        if not refresh_unreviewed or reviews or previous["scenarios"]:
            raise ValueError("destination already contains a corpus; export a new version")
        scenarios = previous["scenarios"]
    output.mkdir(parents=True, exist_ok=True)
    cases = []
    for row in sorted(graph["file_resources"], key=lambda r: r["id"]):
        torrent = torrent_dir / f"{row['id']}.torrent"
        evidence = {"torrent": None, "http": None}
        issues = []
        if torrent.is_file():
            try:
                raw = sanitize_torrent(torrent.read_bytes())
                name = f"torrents/{digest(raw)}.torrent"
                (output / "torrents").mkdir(exist_ok=True)
                (output / name).write_bytes(raw)
                evidence["torrent"] = name
            except (ValueError, bencodepy.DecodingError):
                issues.append("torrent_invalid")
        else:
            issues.append("torrent_missing")
        answer = resource_answer(row, graph)
        if not answer["works"]:
            issues.append("work_unresolved")
        if row.get("episode_confidence") == "ambiguous":
            issues.append("numbering_ambiguous")
        if row.get("is_batch") and row.get("episode") is not None:
            issues.append("batch_has_episode")
        cases.append({
            "id": row["id"], "channel_id": row["channel_id"],
            "input": {"kind": "title_and_torrent", "title_raw": row["title_raw"], "raw_rss_available": False},
            "evidence": evidence, "candidate_expected": answer,
            "review": {"status": "pending", "issues": issues, "note": "Production state is not ground truth."},
        })
    write_json(output / "candidates.json.gz", {"graph": graph, "cases": cases})
    manifest = {
        "version": 1, "captured_at": datetime.now(UTC).isoformat(),
        "candidate_file": "candidates.json.gz", "candidate_sha256": digest((output / "candidates.json.gz").read_bytes()),
        "counts": {table: len(rows) for table, rows in graph.items()},
        "case_count": len(cases), "review_file": "reviews.json", "scenarios": scenarios,
        "input_limitation": "Historical RSS bodies were not persisted; title/torrent inputs do not claim RSS mapping coverage.",
    }
    write_json(output / "reviews.json", {})
    write_json(output / "manifest.json", manifest)
    return audit(output)


def load_corpus(root: Path = ROOT) -> tuple[dict, dict, dict]:
    manifest = read_json(root / "manifest.json")
    candidate_path = asset(root, manifest["candidate_file"])
    if digest(candidate_path.read_bytes()) != manifest["candidate_sha256"]:
        raise ValueError("candidate snapshot checksum mismatch")
    return manifest, read_json(candidate_path), read_json(asset(root, manifest["review_file"]))


def asset(root: Path, relative: str) -> Path:
    target = (root / relative).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ValueError("asset path escapes corpus")
    return target


def hydrate_torrents(root: Path) -> dict:
    """Stream the container archive; never extract archive paths to disk."""
    manifest, corpus, _ = load_corpus(root)
    process = subprocess.Popen(
        ["docker", "compose", "exec", "-T", "app", "tar", "-C", "data/torrents", "-cf", "-", "."],
        stdout=subprocess.PIPE,
    )
    by_id = {c["id"]: c for c in corpus["cases"]}
    with process, tarfile.open(fileobj=process.stdout, mode="r|") as archive:
        for member in archive:
            if not member.isfile() or member.size > 50 * 1024 * 1024:
                continue
            case = by_id.get(Path(member.name).stem)
            if case is None or case["evidence"]["torrent"]:
                continue
            try:
                raw = sanitize_torrent(archive.extractfile(member).read())
            except (ValueError, bencodepy.DecodingError):
                case["review"]["issues"].append("torrent_invalid")
                continue
            name = f"torrents/{digest(raw)}.torrent"
            (root / "torrents").mkdir(exist_ok=True)
            (root / name).write_bytes(raw)
            case["evidence"]["torrent"] = name
            case["review"]["issues"] = [i for i in case["review"]["issues"] if i != "torrent_missing"]
        # Drain tar padding before waiting, otherwise a full stdout pipe can
        # leave the docker process blocked while we wait for its exit.
        while process.stdout.read(65536):
            pass
        if process.wait() != 0:
            raise RuntimeError("container torrent export failed")
    candidate_path = asset(root, manifest["candidate_file"])
    write_json(candidate_path, corpus)
    manifest["candidate_sha256"] = digest(candidate_path.read_bytes())
    write_json(root / "manifest.json", manifest)
    return audit(root)


def index_files(root: Path) -> dict:
    """Freeze file names/lengths directly from bencode, without application parsing."""
    manifest, corpus, _ = load_corpus(root)
    listings = {}
    for case in corpus["cases"]:
        relative = case["evidence"]["torrent"]
        if not relative or relative in listings:
            continue
        info = bencodepy.decode(asset(root, relative).read_bytes())[b"info"]
        def decode(value):
            return value.decode("utf-8", errors="replace")
        if b"files" in info:
            files = [{"name": "/".join(decode(p) for p in f.get(b"path.utf-8", f[b"path"])),
                      "size": f[b"length"]} for f in info[b"files"]]
        else:
            files = [{"name": decode(info.get(b"name.utf-8", info[b"name"])), "size": info[b"length"]}]
        listings[relative] = files
    write_json(root / "file_lists.json.gz", listings)
    manifest["file_list_file"] = "file_lists.json.gz"
    manifest["file_list_sha256"] = digest((root / "file_lists.json.gz").read_bytes())
    write_json(root / "manifest.json", manifest)
    return {"unique_torrents": len(listings), "files": sum(len(files) for files in listings.values())}


def audit(root: Path = ROOT) -> dict:
    manifest, corpus, reviews = load_corpus(root)
    issues = Counter()
    statuses = Counter()
    details = []
    recorded = {case_id for scenario in manifest["scenarios"]
                if asset(root, scenario["cassette"]).is_file() for case_id in scenario["case_ids"]}
    seasons = defaultdict(list)
    for work in corpus["graph"]["tv_series"]:
        seasons[(work.get("collection_id"), work.get("season_number"))].append(work["id"])
    for case in corpus["cases"]:
        review = reviews.get(case["id"], case["review"])
        status = review["status"]
        if status not in {"pending", "confirmed", "insufficient"}:
            raise ValueError(f"invalid review status for {case['id']}")
        if status == "confirmed":
            validate_review(review, case["id"])
        statuses[status] += 1
        reasons = list(case["review"]["issues"])
        if case["id"] not in recorded:
            reasons.append("http_evidence_missing")
        if reasons:
            details.append({"id": case["id"], "issues": reasons})
        issues.update(reasons)
        if case["evidence"]["torrent"]:
            path = asset(root, case["evidence"]["torrent"])
            if digest(path.read_bytes()) != path.stem:
                raise ValueError(f"torrent checksum mismatch: {case['id']}")
    report = {
        "case_count": len(corpus["cases"]), "counts": manifest["counts"],
        "review_status": dict(statuses), "issues": dict(issues), "case_issues": details,
        "duplicate_collection_seasons": [ids for (collection, _), ids in seasons.items() if collection and len(ids) > 1],
        "scenario_count": len(manifest["scenarios"]),
    }
    write_json(root / "audit.json", report)
    return {key: value for key, value in report.items() if key != "case_issues"}


def validate_review(review: dict, case_id: str) -> None:
    expected = review.get("expected") or {}
    required = {"works", "work_details", "collection", "assignments", "season", "episode", "is_batch",
                "batch_scope", "batch_seasons", "season_ranges", "episode_start", "episode_end", "confirmation"}
    if not required <= expected.keys() or not review.get("evidence_note"):
        raise ValueError(f"confirmed case lacks independent complete answer/evidence: {case_id}")
    if set(expected["works"]) != set(expected["work_details"]):
        raise ValueError(f"work details do not cover identities: {case_id}")
