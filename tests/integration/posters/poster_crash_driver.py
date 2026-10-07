"""Terminate the actual poster pipeline at controlled filesystem boundaries."""
import argparse
import asyncio
import os
from urllib.parse import urlsplit

from app.config import settings
from app.services import poster_cache
from app.services.metadata_service import download_and_cache_poster


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=["stage", "published", "success"])
    parser.add_argument("url")
    parser.add_argument("directory")
    parser.add_argument("--umask")
    args = parser.parse_args()
    if args.umask is not None:
        os.umask(int(args.umask, 8))
    parts = urlsplit(args.url)
    assert parts.hostname == "127.0.0.1"
    settings.poster_cache_dir = args.directory
    settings.outbound_private_origins = [f"{parts.scheme}://{parts.netloc}"]
    original_write = poster_cache._write_bytes
    original_replace = poster_cache.os.replace

    def write(handle, content):
        if args.phase == "stage" and content.startswith(b"\xff\xd8\xff"):
            handle.write(content[:4096])
            handle.flush()
            os._exit(97)
        original_write(handle, content)

    def replace(source, destination):
        original_replace(source, destination)
        if args.phase == "published":
            os._exit(98)

    poster_cache._write_bytes = write
    poster_cache.os.replace = replace
    result = asyncio.run(download_and_cache_poster(args.url))
    if args.phase == "success":
        assert result is not None
        print(result)
    else:
        raise AssertionError("Crash injection was not reached")


if __name__ == "__main__":
    main()
