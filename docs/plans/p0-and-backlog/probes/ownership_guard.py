"""Prototype: persistent shared lock file plus cancellation-safe lifetime."""
import asyncio
import fcntl
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID


class OwnershipBusyError(Exception):
    pass


@contextmanager
def plan_lock(directory: Path, plan_id: str):
    # Directory provisioning/volume validation is a separate startup concern.
    name = str(UUID(plan_id)) + ".lock"
    root = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    fd = None
    try:
        fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=root)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("ownership lock must be a regular file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise OwnershipBusyError(plan_id) from exc
        yield
    finally:
        if fd is not None:
            os.close(fd)
        os.close(root)
    # Never unlink: unlink/recreate splits ownership between different inodes.


async def finish_before_cancel(operation):
    """A cancelled caller cannot release ownership while its thread still runs.

    The operation includes DB finalization and closing the lock. Inner errors
    take precedence over deferred caller cancellation so failures stay visible.
    """
    task = asyncio.create_task(operation)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result
