"""One-off backfill: fetch and cache lead images for TVSeries / Movie /
AudioWork rows sourced from Wikipedia that never got a poster (the pageimages
fetch landed after these rows were created).

Iterates rows where ``external_source = 'wikipedia'`` and ``poster_url`` is
empty or not yet locally cached, calls the MediaWiki image fetcher with the
stored ``wikipedia_page_id``, downloads the image via
``download_and_cache_poster``, and stores the local ``/posters/...`` URL.

Runs fetches concurrently (capped). Idempotent. Dry-run by default; pass
--apply to execute (dry-run skips the image download, only resolves URLs)."""
import argparse
import asyncio
import re

from sqlalchemy import select

from app.database import async_session_factory
from app.models.audio_work import AudioWork
from app.models.movie import Movie
from app.models.series import TVSeries
from app.services.metadata_agent import _fetch_wikipedia_page_image
from app.services.metadata_service import download_and_cache_poster

_WIKI_LANG_RE = re.compile(r"://(zh|en|ja)\.wikipedia\.")
_KANA_RE = re.compile(r"[぀-ヿ]")
_CJK_RE = re.compile(r"[一-鿿]")


def _infer_lang(row) -> str:
    """Pageids are per-wiki, so we must query the right language edition.
    Prefer the stored wikipedia_url; fall back to title script (CJK->zh,
    kana->ja) since many auto-linked rows carry no wikipedia_url."""
    if row.wikipedia_url:
        m = _WIKI_LANG_RE.search(row.wikipedia_url)
        if m:
            return m.group(1)
    # title_cn carries the matched (often CJK) title; kana => ja, CJK => zh.
    probe = row.title_cn or row.original_title or row.title_en or ""
    if _KANA_RE.search(probe):
        return "ja"
    if _CJK_RE.search(probe):
        return "zh"
    return "en"


def _title_for(row, lang: str) -> str | None:
    if lang == "zh":
        return row.title_cn or row.original_title or row.title_en
    if lang == "ja":
        return row.original_title or row.title_cn or row.title_en
    return row.title_en or row.original_title or row.title_cn


_CONCURRENCY = 8


async def _process(row, model_name, apply: bool, sem: asyncio.Semaphore) -> tuple[str, str | None]:
    page_id = row.wikipedia_page_id
    if not page_id and row.external_id and row.external_id.startswith("wikipedia:"):
        try:
            page_id = int(row.external_id.split(":", 1)[1])
        except ValueError:
            page_id = None
    lang = _infer_lang(row)
    title = _title_for(row, lang)
    if not title and not page_id:
        return (f"{model_name} {row.id} ({title!r}): no title/pageid", None)
    async with sem:
        # ``expected_title`` guards the REST-summary fallback: when the stored
        # title is not the canonical article title, REST can resolve to a
        # different page and return an unrelated image. Reject those.
        remote = await _fetch_wikipedia_page_image(
            title or "", lang, page_id, expected_title=title or None
        )
    if not remote:
        return (f"{model_name} {row.id} ({title!r}): no image", None)
    poster: str | None
    if apply:
        local = await download_and_cache_poster(remote)
        poster = local or remote
        row.poster_url = poster
    else:
        poster = remote
    return (f"{model_name} {row.id} ({title!r}) -> {poster}", poster)


async def _backfill_model(model, db, apply: bool, sem: asyncio.Semaphore) -> int:
    rows = (
        await db.execute(
            select(model).where(
                model.external_source == "wikipedia",
                (model.poster_url.is_(None))
                | (~model.poster_url.like("/posters/%")),
            )
        )
    ).scalars().all()
    if not rows:
        return 0
    print(f"-- {model.__name__}: {len(rows)} rows --", flush=True)
    tasks = [_process(r, model.__name__, apply, sem) for r in rows]
    filled = 0
    for coro in asyncio.as_completed(tasks):
        msg, poster = await coro
        if poster:
            filled += 1
            print(f"  [fill] {msg}", flush=True)
        else:
            print(f"  [skip] {msg}", flush=True)
    return filled


async def main(apply: bool) -> None:
    print(f"=== wikipedia poster backfill ({'APPLY' if apply else 'DRY-RUN'}) ===", flush=True)
    sem = asyncio.Semaphore(_CONCURRENCY)
    async with async_session_factory() as db:
        s = await _backfill_model(TVSeries, db, apply, sem)
        m = await _backfill_model(Movie, db, apply, sem)
        a = await _backfill_model(AudioWork, db, apply, sem)
        print(f"\nseries={s} movie={m} audio={a} posters filled", flush=True)
        if not apply:
            print("\n(dry-run; no changes written. Re-run with --apply to commit.)", flush=True)
            await db.rollback()
            return
        await db.commit()
        print("\nAPPLIED.", flush=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()
    asyncio.run(main(args.apply))
