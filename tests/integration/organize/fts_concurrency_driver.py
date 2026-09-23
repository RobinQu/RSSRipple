"""Bounded production FTS reads/writes on a dedicated synthetic sidecar."""

import asyncio
import json
import os
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

test_root = Path(os.environ["PROBE_ROOT"]).resolve()
assert test_root.is_relative_to(Path("/tmp"))
assert os.environ["DATABASE_URL"] == f"sqlite+aioturso:///{test_root / 'main.db'}"
import app.database  # noqa: E402,F401
from app.services import fts  # noqa: E402


async def main():
    await fts.ensure_fts_tables()
    errors = Counter()
    successful = Counter()

    async def worker(number):
        for iteration in range(100):
            try:
                if number < 4:
                    entity = SimpleNamespace(
                        id=f"synthetic-{iteration % 10}",
                        title_cn="Synthetic title",
                        title_en=None,
                        original_title=None,
                        aliases=[],
                    )
                    await fts._upsert("tv_series_fts", entity)
                    successful["write"] += 1
                else:
                    await fts._search_fts("tv_series_fts", "synthetic", 20)
                    successful["read"] += 1
            except Exception as exc:
                errors[type(exc).__name__ + ":" + str(exc).splitlines()[0]] += 1
            await asyncio.sleep(0)

    try:
        async with asyncio.timeout(60):
            await asyncio.gather(*(worker(i) for i in range(8)))
            found = await fts._search_fts("tv_series_fts", "synthetic", 20)
            assert set(found) == {f"synthetic-{i}" for i in range(10)}
            assert len(found) == 10
            assert not errors, dict(errors)
    finally:
        await fts._get_fts_engine().dispose()
    Path(os.environ["PROBE_RESULT_PATH"]).write_text(
        json.dumps(
            {
                "data": "synthetic",
                "attempts": 800,
                "successful": dict(successful),
                "errors": dict(errors),
                "final_ids": sorted(found),
            },
            indent=2,
        )
        + "\n"
    )


asyncio.run(main())
