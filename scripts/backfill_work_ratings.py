"""Backfill Movie.rating / Movie.release_date for resources of one channel.

Unlike the other one-off scripts this one does NOT open the database
directly: the embedded-Turso backend takes a single-process exclusive file
lock, and the dev server (uvicorn) already holds it. Instead it drives the
running server's REST API, so all metadata fetching still goes through the
project's own ``refresh_work_metadata`` service (TMDB/Exa/Jina/Wikipedia
search agents) — no direct calls to external APIs from here.

Flow:
  1. Resolve the channel (id or case-insensitive name substring).
  2. Page through ``GET /channels/{id}/resources`` and collect linked
     ``movie_id``s (also reports unlinked / series-linked counts).
  3. Page through ``GET /works?content_type=movie`` and pick the linked
     movies whose ``rating`` or ``release_date`` is empty.
  4. For each target, ``POST /works/refresh-metadata`` (fills only empty
     fields — idempotent, existing values are preserved).
  5. Re-read the works and print a before/after summary.

Dry-run by default; pass --apply to execute the refreshes.
Stdlib only, so it runs with any python3:

    python3 scripts/backfill_work_ratings.py --channel 4K-Movies
    python3 scripts/backfill_work_ratings.py --channel 4K-Movies --apply --limit 20
"""
import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE_URL = "http://localhost:9001"


def _get(base: str, path: str, **params) -> dict:
    qs = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    url = f"{base}/api/v1{path}?{qs}"
    with urllib.request.urlopen(url, timeout=30) as r:
        payload = json.load(r)
    if not payload.get("success"):
        raise RuntimeError(f"GET {path} failed: {payload.get('error')}")
    return payload


def _post(base: str, path: str, body: dict, timeout: int = 120) -> dict:
    req = urllib.request.Request(
        f"{base}/api/v1{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        payload = json.load(r)
    if not payload.get("success"):
        raise RuntimeError(f"POST {path} failed: {payload.get('error')}")
    return payload


def _paged(base: str, path: str, **params) -> list[dict]:
    items, page = [], 1
    while True:
        d = _get(base, path, page=page, page_size=100, **params)
        items.extend(d["data"])
        meta = d["meta"]
        if page * meta["page_size"] >= meta["total"]:
            return items
        page += 1


def _resolve_channel(base: str, query: str) -> dict:
    channels = _paged(base, "/channels")
    for ch in channels:
        if ch["id"] == query:
            return ch
    q = query.lower()
    hits = [ch for ch in channels if q in (ch.get("name") or "").lower()]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise SystemExit(f"no channel matching {query!r}; have: {[c['name'] for c in channels]}")
    raise SystemExit(f"ambiguous channel {query!r}: {[(c['id'], c['name']) for c in hits]}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL)
    ap.add_argument("--channel", required=True, help="channel id or name substring")
    ap.add_argument("--source", default="tmdb", help="metadata source (default: tmdb)")
    ap.add_argument("--limit", type=int, default=0, help="max works to refresh (0 = all)")
    ap.add_argument("--apply", action="store_true", help="actually refresh (default: dry-run)")
    args = ap.parse_args()
    base = args.base_url.rstrip("/")

    channel = _resolve_channel(base, args.channel)
    print(f"channel: {channel['id']}  name={channel['name']!r}")

    resources = _paged(base, f"/channels/{channel['id']}/resources")
    movie_ids = {r["movie_id"] for r in resources if r.get("movie_id")}
    n_series = sum(1 for r in resources if r.get("series_id"))
    n_unlinked = sum(
        1 for r in resources
        if not r.get("movie_id") and not r.get("series_id") and not r.get("audio_work_id")
    )
    print(
        f"resources: {len(resources)} | movie-linked: {len(movie_ids)} distinct | "
        f"series-linked: {n_series} | unlinked: {n_unlinked}"
    )

    works = {w["id"]: w for w in _paged(base, "/works", content_type="movie")}

    def missing(w: dict) -> bool:
        return w["rating"] in (None, "") or not w["release_date"]

    targets = [works[m] for m in sorted(movie_ids) if m in works and missing(works[m])]
    have_rating = sum(1 for m in movie_ids if m in works and works[m]["rating"] not in (None, ""))
    have_date = sum(1 for m in movie_ids if m in works and works[m]["release_date"])
    print(f"BEFORE: {have_rating}/{len(movie_ids)} movies have rating, {have_date}/{len(movie_ids)} have release_date")
    print(f"targets (missing rating and/or release_date): {len(targets)}")

    if not args.apply:
        for w in targets[:20]:
            print(f"  [dry-run] would refresh {w['id'][:8]}  {w['title_en'] or w['title_cn']!r}")
        print("dry-run; pass --apply to execute")
        return

    todo = targets[: args.limit] if args.limit > 0 else targets
    ok, failed = 0, []
    for i, w in enumerate(todo, 1):
        try:
            d = _post(base, "/works/refresh-metadata", {
                "id": w["id"], "content_type": "movie", "source": args.source,
            })["data"]
            filled = d.get("filled", [])
            print(f"[{i}/{len(todo)}] {w['title_en'] or w['title_cn']!r}: filled={filled} ({d.get('message')})")
            ok += 1
        except (urllib.error.URLError, RuntimeError, TimeoutError) as e:
            print(f"[{i}/{len(todo)}] {w['title_en'] or w['title_cn']!r}: FAILED {e}")
            failed.append((w["id"], str(e)))

    works_after = {w["id"]: w for w in _paged(base, "/works", content_type="movie")}
    have_rating = sum(1 for m in movie_ids if m in works_after and works_after[m]["rating"] not in (None, ""))
    have_date = sum(1 for m in movie_ids if m in works_after and works_after[m]["release_date"])
    print(f"AFTER:  {have_rating}/{len(movie_ids)} movies have rating, {have_date}/{len(movie_ids)} have release_date")
    print(f"refreshed ok: {ok}, failed: {len(failed)}")
    if failed:
        for fid, err in failed:
            print(f"  failed {fid[:8]}: {err}")
        sys.exit(1)


if __name__ == "__main__":
    main()
