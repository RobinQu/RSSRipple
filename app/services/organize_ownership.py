"""Prototype: persistent shared lock file plus cancellation-safe lifetime."""
import asyncio
import contextvars
import fcntl
import functools
import hashlib
import os
import stat
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organize_configuration import CONFIGURATION_ID, OrganizeConfiguration
from app.services.organize_config_events import ensure_configuration


class OwnershipBusyError(Exception):
    pass


class OwnershipDomainError(Exception):
    pass


def _read_domain(root):
    fd = os.open(".domain", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=root)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OwnershipDomainError("整理锁目录身份文件不是普通文件")
        value = os.read(fd, 64).decode("ascii")
        if str(UUID(value, version=4)) != value:
            raise OwnershipDomainError("整理锁目录身份文件无效")
        return value
    finally:
        os.close(fd)


def _domain(directory, *, initialize):
    if initialize:
        directory.mkdir(parents=True, exist_ok=True)
    root = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            return _read_domain(root)
        except FileNotFoundError:
            if not initialize:
                raise OwnershipDomainError("整理锁目录缺少已注册身份，禁止创建另一套锁") from None
        value = str(uuid4())
        staging = f".domain-{value}.tmp"
        fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=root)
        try:
            assert os.write(fd, value.encode("ascii")) == len(value)
            os.fsync(fd)
            try:
                os.link(staging, ".domain", src_dir_fd=root, dst_dir_fd=root, follow_symlinks=False)
                os.fsync(root)
            except FileExistsError:
                pass
        finally:
            os.close(fd)
            os.unlink(staging, dir_fd=root)
        return _read_domain(root)
    finally:
        os.close(root)


async def registered_domain(db, directory):
    """Register only before first use; never recreate a missing registered domain.

    A separate transaction avoids committing unrelated caller edits. All
    workers must see the original marker and persistent lock inodes; copying
    a live lock directory to another filesystem is explicitly unsupported.
    """
    async with AsyncSession(bind=db.bind, expire_on_commit=False) as registration:
        expected = await registration.scalar(select(OrganizeConfiguration.lock_domain).where(
            OrganizeConfiguration.id == CONFIGURATION_ID,
        ))
        try:
            actual = await owned_thread(_domain, directory, initialize=expected is None)
        except (OSError, ValueError, UnicodeError) as exc:
            raise OwnershipDomainError(f"整理锁目录不可用：{exc}") from exc
        if expected is None:
            await (await registration.connection()).run_sync(ensure_configuration)
            await registration.execute(update(OrganizeConfiguration).where(
                OrganizeConfiguration.id == CONFIGURATION_ID,
                OrganizeConfiguration.lock_domain.is_(None),
            ).values(lock_domain=actual))
            await registration.commit()
            expected = await registration.scalar(select(OrganizeConfiguration.lock_domain).where(
                OrganizeConfiguration.id == CONFIGURATION_ID,
            ))
        if expected != actual:
            raise OwnershipDomainError("整理锁目录与数据库注册身份不一致，请使用所有 worker 共享的原目录")
        return expected


async def is_plan_owned(directory: Path, plan_id: str, expected_domain=None):
    """Advisory HTTP feedback; execution must independently acquire ownership."""
    async def probe():
        try:
            async with async_plan_lock(directory, plan_id, expected_domain):
                return False
        except OwnershipBusyError:
            return True
    return await finish_before_cancel(probe())


async def owned_thread(function, *args, **kwargs):
    """Wait for the real thread even if shutdown cancels the inner owner task.

    Use inside finish_before_cancel: the outer operation reports cancellation
    after finalization. A raw executor Future is not an asyncio Task, so
    asyncio's shutdown cancellation cannot independently cancel that work.
    """
    context = contextvars.copy_context()
    call = functools.partial(function, *args, **kwargs)
    future = asyncio.get_running_loop().run_in_executor(None, context.run, call)
    while not future.done():
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            continue
    return future.result()


@asynccontextmanager
async def async_plan_lock(directory: Path, plan_id: str, expected_domain=None):
    # Caller shields the whole operation, including acquisition and release.
    await owned_thread(directory.mkdir, parents=True, exist_ok=True)
    manager = plan_lock(directory, plan_id, expected_domain)
    try:
        await owned_thread(manager.__enter__)
    except (OSError, ValueError, UnicodeError) as exc:
        raise OwnershipDomainError(f"整理锁目录不可用：{exc}") from exc
    try:
        yield
    finally:
        await owned_thread(manager.__exit__, None, None, None)


@contextmanager
def plan_lock(directory: Path, plan_id: str, expected_domain=None):
    # Directory provisioning/volume validation is a separate startup concern.
    name = hashlib.sha256(plan_id.encode("utf-8")).hexdigest() + ".lock"
    root = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    fd = None
    try:
        if expected_domain is not None and _read_domain(root) != expected_domain:
            raise OwnershipDomainError("整理锁目录身份在获取锁前发生变化")
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
