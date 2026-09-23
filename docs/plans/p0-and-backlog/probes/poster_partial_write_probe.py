"""Run with prototype on PYTHONPATH; writes only inside TemporaryDirectory."""

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.services import metadata_service as ms


async def main():
    content = b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"/>'
    original_write = Path.write_bytes

    def interrupted_write(path, data):
        original_write(path, data[:4])
        raise OSError("injected interrupted disk write")

    with tempfile.TemporaryDirectory(prefix="rssripple-poster-probe-") as directory:
        with patch.object(ms.settings, "poster_cache_dir", directory):
            with patch.object(ms.asyncio, "to_thread", AsyncMock(return_value=content)):
                with patch.object(Path, "write_bytes", interrupted_write):
                    first = await ms.download_and_cache_poster("https://example.invalid/recorded.svg")
                second = await ms.download_and_cache_poster("https://example.invalid/recorded.svg")
            files = list(Path(directory).iterdir())
            result = {"first_result": first, "second_result": second,
                      "cached_sizes": [p.stat().st_size for p in files],
                      "expected_size": len(content),
                      "partial_cache_accepted": second is not None and any(p.read_bytes() != content for p in files)}
            print(json.dumps(result, indent=2))
            assert result["partial_cache_accepted"], "Expected old implementation's partial-cache acceptance"


asyncio.run(main())
