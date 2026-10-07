"""Atomic poster publication with receipts for complete downloaded bytes.

A receipt proves byte-for-byte persistence of an HTTP response, not successful
image decoding. Format recognition remains the caller's existing policy.
All functions run in the caller's filesystem/download worker thread.
"""
import hashlib
import json
import logging
import os
import stat
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO

logger = logging.getLogger(__name__)
_EXTENSIONS = ("jpg", "jpeg", "png", "webp", "gif", "svg")


def _receipt_path(path: Path) -> Path:
    return path.with_name("." + path.name + ".json")


def _cached_path(directory: Path, digest: str) -> str | None:
    for extension in _EXTENSIONS:
        path = directory / f"{digest}.{extension}"
        try:
            receipt = json.loads(_receipt_path(path).read_text())
            if not isinstance(receipt, dict) or receipt.get("version") != 1:
                continue
            with path.open("rb") as cached:
                if os.fstat(cached.fileno()).st_size != receipt.get("size"):
                    continue
                if hashlib.file_digest(cached, "sha256").hexdigest() != receipt.get("sha256"):
                    continue
            return f"/posters/{path.name}"
        except (OSError, ValueError, UnicodeError):
            # Legacy, incomplete, missing or damaged entries are cache misses.
            # Keep their bytes until a successful replacement is available.
            continue
    return None


def _write_bytes(handle: BinaryIO, content: bytes) -> None:
    if handle.write(content) != len(content):
        raise OSError("Incomplete poster staging write")
    handle.flush()
    os.fsync(handle.fileno())


def _stage(directory: Path, digest: str, content: bytes) -> Path:
    path = None
    try:
        candidate = directory / f".{digest}.{uuid.uuid4().hex}.part"
        # O_EXCL never follows an existing symlink, and 0666 respects the
        # process umask just like the historical final-file write did.
        descriptor = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        path = candidate
        with os.fdopen(descriptor, "wb") as handle:
            _write_bytes(handle, content)
        return path
    except BaseException:
        if path is not None:
            path.unlink(missing_ok=True)
        raise


def _preserve_mode(staged: Path, target: Path) -> None:
    try:
        previous = target.stat(follow_symlinks=False)
    except FileNotFoundError:
        return
    if stat.S_ISREG(previous.st_mode):
        staged.chmod(stat.S_IMODE(previous.st_mode))


def _publish(path: Path, content: bytes) -> None:
    receipt = json.dumps({
        "version": 1, "size": len(content), "sha256": hashlib.sha256(content).hexdigest(),
    }, sort_keys=True).encode()
    staged = []
    try:
        data = _stage(path.parent, path.stem, content)
        staged.append(data)
        proof = _stage(path.parent, path.stem, receipt)
        staged.append(proof)
        _preserve_mode(data, path)
        _preserve_mode(proof, _receipt_path(path))
        # Readers can see only complete response bytes. If interrupted between
        # these two renames, a missing/mismatched receipt forces a later miss.
        os.replace(data, path)
        os.replace(proof, _receipt_path(path))
    finally:
        for temporary in staged:
            temporary.unlink(missing_ok=True)


def cache_poster(
    directory: Path,
    remote_url: str,
    download: Callable[[], bytes | None],
    sniff: Callable[[bytes], str | None],
) -> str | None:
    digest = hashlib.sha256(remote_url.encode("utf-8")).hexdigest()[:16]
    try:
        directory.mkdir(parents=True, exist_ok=True)
        cached = _cached_path(directory, digest)
        if cached is not None:
            return cached
        content = download()
        if not content:
            return None
        extension = sniff(content)
        if extension not in _EXTENSIONS:
            return None
        path = directory / f"{digest}.{extension}"
        _publish(path, content)
        return _cached_path(directory, digest)
    except Exception as error:
        logger.warning("[poster] cache failed %s: %s", digest, error)
        return None
