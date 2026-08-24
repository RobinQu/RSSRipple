"""RedisQueue 可靠队列语义（fakeredis 注入）与 worker 入口进程内集成测试。

单节点 compose 不配独立 Redis app 实例，``test_task_queue.py`` 的 Redis
参数化用例会跳过——本文件经 ``RedisQueue(redis_client=fakeredis)`` 直接
驱动可靠队列的 claim/lease/orphan-recovery/legacy-reconcile 路径，并把
这些语句纳入集成覆盖率（.coverage.test-runner）。worker 入口则通过
monkeypatch 拆掉进程级副作用后逐函数覆盖。
"""

from __future__ import annotations

import asyncio
import json
import uuid

import fakeredis.aioredis as fraio

from app.services.task_queue import (
    _ACTIVE_PFX,
    _CONSUMER_PFX,
    _JOB_PFX,
    _PROCESSING_PFX,
    _QUEUE_LIST,
    CONSUMER_LEASE_SECONDS,
    JobStatus,
    RedisQueue,
)


def _uuid() -> str:
    return str(uuid.uuid4())


_QUEUE_KEY = "rssripple:jobs"
_RECOVERY_LOCK = "rssripple:recovery-lock"


def _make_queue(**kw) -> RedisQueue:
    client = fraio.FakeRedis(decode_responses=True)
    return RedisQueue(redis_client=client, ttl=60, **kw)


async def _wait_for(predicate, timeout: float = 5.0, interval: float = 0.05):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if await predicate():
            return True
        await asyncio.sleep(interval)
    return False


# ---------------------------------------------------------------------------
# 基础接口：enqueue 去重 / 优先级 / status / progress / clear / throttle
# ---------------------------------------------------------------------------


async def test_enqueue_dedup_priority_and_status():
    q = _make_queue()
    q.register("fetch", _async_noop)
    q.register("sync_progress", _async_noop)
    # consume=False：只入队不消费，便于直接检查 Redis 结构
    await q.start(consume=False)
    try:
        job = await q.enqueue("fetch", "key-1", {"x": 1})
        assert job is not None and job["status"] == JobStatus.QUEUED
        # 同 key 去重
        assert await q.enqueue("fetch", "key-1", {"x": 2}) is None
        # 优先级作业 lpush 到队首
        await q.enqueue("sync_progress", "tick-1", {})
        raw = await q._redis.lrange(_QUEUE_LIST, 0, -1)
        types = [json.loads(m)["job_type"] for m in raw]
        assert types == ["sync_progress", "fetch"]
        state = await q.status("key-1")
        assert state["job_id"] == job["job_id"]
        assert await q.status("missing") is None

        await q.update_progress("key-1", {"pct": 50})
        assert (await q.status("key-1"))["result"] == {"pct": 50}
        # 不存在的 key：静默忽略
        await q.update_progress("missing", {})

        await q.clear("key-1")
        assert await q.status("key-1") is None
        # throttle：首个 tick 赢，其余被挡
        assert await q.throttle("t1", 60) is True
        assert await q.throttle("t1", 60) is False
    finally:
        await q.stop()


async def _async_noop(payload):
    return {"noop": True}


# ---------------------------------------------------------------------------
# 消费循环：完成 / 失败 / 无 handler / CancelledError 重排队
# ---------------------------------------------------------------------------


async def test_consume_completes_and_releases_active_key():
    async def work(payload):
        return {"done": payload["v"]}

    q = _make_queue()
    q.register("work", work)
    await q.start()
    try:
        await q.enqueue("work", "k-done", {"v": 42})
        assert await _wait_for(lambda: _status_is(q, "k-done", JobStatus.DONE))
        state = await q.status("k-done")
        assert state["result"] == {"done": 42}
        # 终态原子释放 dedup 键 → 同 key 可再次入队
        assert await q.enqueue("work", "k-done", {"v": 43}) is not None

        async def _second_done() -> bool:
            state = await q.status("k-done")
            return bool(state) and state.get("result") == {"done": 43}

        assert await _wait_for(_second_done)
    finally:
        await q.stop()


async def test_consume_handler_failure_marks_failed():
    async def boom(payload):
        raise RuntimeError("kaput")

    q = _make_queue()
    q.register("bad", boom)
    await q.start()
    try:
        await q.enqueue("bad", "k-bad", {})
        assert await _wait_for(lambda: _status_is(q, "k-bad", JobStatus.FAILED))
        state = await q.status("k-bad")
        assert "kaput" in state["error"]
        assert await q._redis.get(f"{_ACTIVE_PFX}k-bad") is None
    finally:
        await q.stop()


async def test_missing_handler_fails_job_without_raising():
    q = _make_queue()
    await q.start()
    try:
        await q.enqueue("ghost-type", "k-ghost", {})
        assert await _wait_for(
            lambda: _status_is(q, "k-ghost", JobStatus.FAILED)
        )
        assert "No handler" in (await q.status("k-ghost"))["error"]
    finally:
        await q.stop()


async def test_cancelled_handler_requeues_with_lock_retained():
    started = asyncio.Event()

    async def cancelled(payload):
        started.set()
        raise asyncio.CancelledError()

    q = _make_queue()
    q.register("slow", cancelled)
    await q.start()
    try:
        await q.enqueue("slow", "k-cancel", {})
        assert await _wait_for(started.wait)
        # 重排队：状态回 queued、描述符回主队列、active 键保留
        assert await _wait_for(
            lambda: _status_is(q, "k-cancel", JobStatus.QUEUED)
        )
        assert int(await q._redis.llen(_QUEUE_LIST)) >= 1
        assert await q._redis.get(f"{_ACTIVE_PFX}k-cancel") is not None

        async def _processing_drained() -> bool:
            return int(await q._redis.llen(q._processing_key)) == 0

        assert await _wait_for(_processing_drained)
    finally:
        await q.stop()


# ---------------------------------------------------------------------------
# 孤儿恢复：dead consumer 的 processing 列表回收到主队列
# ---------------------------------------------------------------------------


def _msg(job_id: str, key: str, job_type: str = "work") -> str:
    return json.dumps({"job_id": job_id, "job_type": job_type, "key": key,
                       "payload": {}})


async def test_orphan_recovery_requeues_dead_consumer_jobs():
    q = _make_queue()
    await q.start(consume=False)
    try:
        r = q._redis
        dead = "dead-consumer:1"
        # 死消费者的 processing 描述符 + 对应 RUNNING 状态哈希
        msg = _msg("j-orphan", "k-orphan")
        await r.lpush(f"{_PROCESSING_PFX}{dead}", msg)
        await r.hset(f"{_JOB_PFX}k-orphan", mapping={
            "job_id": "j-orphan", "job_type": "work", "key": "k-orphan",
            "status": JobStatus.RUNNING, "result": "", "error": "",
            "queued_at": "now", "started_at": "now", "finished_at": "",
            "message": msg,
        })
        # 存活消费者的列表必须不被触碰
        alive_msg = _msg("j-live", "k-live")
        await r.set(f"{_CONSUMER_PFX}alive", "1", ex=CONSUMER_LEASE_SECONDS)
        await r.lpush(f"{_PROCESSING_PFX}alive", alive_msg)
        # 状态已 DONE 的孤儿描述符只清理不回收；坏 JSON 直接丢弃
        done_msg = _msg("j-done", "k-done-stale")
        await r.hset(f"{_JOB_PFX}k-done-stale", mapping={
            "job_id": "j-done", "status": JobStatus.DONE, "message": "",
        })
        await r.rpush(f"{_PROCESSING_PFX}{dead}", done_msg)
        await r.lpush(f"{_PROCESSING_PFX}{dead}", "{not-json")

        await q._recover_orphaned_jobs()

        # 回收：状态复位 QUEUED 且描述符回主队列、processing 清空删除
        state = await q.status("k-orphan")
        assert state["status"] == JobStatus.QUEUED
        queue_msgs = [json.loads(m) for m in await r.lrange(_QUEUE_LIST, 0, -1)]
        assert any(m["job_id"] == "j-orphan" for m in queue_msgs)
        assert not await r.exists(f"{_PROCESSING_PFX}{dead}")
        # 存活消费者不动
        assert await r.lrange(f"{_PROCESSING_PFX}alive", 0, -1) == [alive_msg]
        # 恢复锁释放
        assert await r.get(_RECOVERY_LOCK) is None
    finally:
        await q.stop()


async def test_legacy_reconcile_fails_zombie_running_hashes():
    """旧版本把描述符从 Redis 移除后再执行——这类 RUNNING 哈希无恢复可能，
    启动对账直接判失败并释放 active 锁。"""
    q = _make_queue()
    await q.start(consume=False)
    try:
        r = q._redis
        await r.hset(f"{_JOB_PFX}zombie", mapping={
            "job_id": "j-z", "job_type": "work", "key": "zombie",
            "status": JobStatus.RUNNING, "message": "",
            "queued_at": "now", "started_at": "now", "finished_at": "",
        })
        await r.set(f"{_ACTIVE_PFX}zombie", "j-z", ex=60)
        # 带 message 的正常 running 哈希不受影响
        live = _msg("j-ok", "k-ok")
        await r.hset(f"{_JOB_PFX}k-ok", mapping={
            "job_id": "j-ok", "status": JobStatus.RUNNING, "message": live,
        })

        await q._recover_orphaned_jobs(reconcile_legacy=True)

        zstate = await q.status("zombie")
        assert zstate["status"] == JobStatus.FAILED
        assert "interrupted" in zstate["error"]
        assert await r.get(f"{_ACTIVE_PFX}zombie") is None
        assert (await q.status("k-ok"))["status"] == JobStatus.RUNNING
    finally:
        await q.stop()


async def test_startup_recovery_runs_before_worker_loop():
    """start(consume=True) 即刻做一次 startup recovery（含 legacy 对账）。"""
    q = _make_queue()
    dead = "dead:start"
    msg = _msg("j-start", "k-start")
    r = q._redis
    await r.lpush(f"{_PROCESSING_PFX}{dead}", msg)
    await r.hset(f"{_JOB_PFX}k-start", mapping={
        "job_id": "j-start", "job_type": "work", "key": "k-start",
        "status": JobStatus.RUNNING, "message": msg,
        "queued_at": "n", "started_at": "n", "finished_at": "", "result": "",
        "error": "",
    })
    await q.start(consume=True)
    try:
        assert await _wait_for(lambda: _status_is(q, "k-start", JobStatus.QUEUED))
    finally:
        await q.stop()


async def test_stop_cleans_consumer_key_and_loops():
    q = _make_queue()
    q.register("work", _async_noop)
    await q.start()
    consumer_key = f"{_CONSUMER_PFX}{q._consumer_id.split(':', 1)[0]}" \
        if False else q._consumer_key
    assert await q._redis.exists(consumer_key)
    await q.enqueue("work", "k-stop", {})
    await q.stop()
    assert not await q._redis.exists(consumer_key)


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


async def _status_is(q: RedisQueue, key: str, status: str) -> bool:
    state = await q.status(key)
    return bool(state) and state["status"] == status


async def await_status(q: RedisQueue, key: str):
    return await q.status(key)


# ---------------------------------------------------------------------------
# 补充分支：未启动 throttle / 工厂 / 坏 JSON / 心跳 / worker 异常 / 优先级重排队
# ---------------------------------------------------------------------------


async def test_throttle_before_start_returns_true():
    q = _make_queue()
    assert await q.throttle("never-started", 30) is True


def test_create_queue_factory():
    from app.services.task_queue import MemoryQueue, create_queue

    assert isinstance(create_queue("memory"), MemoryQueue)
    fq = create_queue("redis", redis_client=fraio.FakeRedis(decode_responses=True))
    assert isinstance(fq, RedisQueue)
    # 未知后端回退 memory；redis 不认识的 kwargs 被静默忽略
    assert isinstance(create_queue("bogus"), MemoryQueue)
    assert isinstance(
        create_queue("redis", bogus_kw=1,
                     redis_client=fraio.FakeRedis(decode_responses=True)),
        RedisQueue,
    )


async def test_status_deserialize_corrupt_result_json():
    q = _make_queue()
    await q.start(consume=False)
    try:
        await q._redis.hset(f"{_JOB_PFX}k-corrupt", mapping={
            "job_id": "j1", "job_type": "work", "key": "k-corrupt",
            "status": JobStatus.DONE, "result": "{not-json",
        })
        state = await q.status("k-corrupt")
        assert state["result"] == "{not-json"
    finally:
        await q.stop()


async def test_heartbeat_loop_refreshes_lease_and_recovers(monkeypatch):
    import app.services.task_queue as tq_mod

    monkeypatch.setattr(tq_mod, "CONSUMER_HEARTBEAT_SECONDS", 0.01)
    q = _make_queue()
    calls = {"n": 0}

    original_set = q._redis.set

    async def flaky_set(key, value, ex=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("redis hiccup")
        return await original_set(key, value, ex=ex)

    monkeypatch.setattr(q._redis, "set", flaky_set)
    recover_calls = []

    async def fake_recover(**kw):
        recover_calls.append(kw)

    monkeypatch.setattr(q, "_recover_orphaned_jobs", fake_recover)
    task = asyncio.create_task(q._heartbeat_loop())
    await asyncio.sleep(0.08)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert calls["n"] >= 2  # 第一次异常被吞，循环继续
    assert recover_calls  # 每次 lease 刷新都伴随一次孤儿恢复


async def test_worker_loop_survives_transient_redis_error():
    q = _make_queue()
    q.register("work", _async_noop)
    original_lmove = q._redis.lmove
    state = {"failed_once": False}

    async def flaky_lmove(*a, **kw):
        if not state["failed_once"]:
            state["failed_once"] = True
            raise RuntimeError("lmove hiccup")
        return await original_lmove(*a, **kw)

    q._redis.lmove = flaky_lmove
    await q.start()
    try:
        await q.enqueue("work", "k-after-error", {})
        assert await _wait_for(lambda: _status_is(q, "k-after-error", JobStatus.DONE))
    finally:
        await q.stop()


async def test_priority_job_cancel_requeues_at_front():
    started = asyncio.Event()

    async def cancelled(payload):
        started.set()
        raise asyncio.CancelledError()

    q = _make_queue()
    q.register("sync_progress", cancelled)
    await q.start()
    try:
        await q.enqueue("sync_progress", "k-prio-cancel", {})
        assert await _wait_for(started.wait)
        assert await _wait_for(
            lambda: _status_is(q, "k-prio-cancel", JobStatus.QUEUED)
        )
        msgs = [json.loads(m) for m in await q._redis.lrange(_QUEUE_LIST, 0, -1)]
        # lpush → 重排队描述符位于队首
        assert msgs[0]["key"] == "k-prio-cancel"
    finally:
        await q.stop()


async def test_orphan_recovery_requeues_priority_job_at_front():
    q = _make_queue()
    await q.start(consume=False)
    try:
        r = q._redis
        dead = "dead:prio"
        msg = _msg("j-prio", "k-prio-orphan", job_type="download_notifications")
        await r.lpush(f"{_PROCESSING_PFX}{dead}", msg)
        await r.hset(f"{_JOB_PFX}k-prio-orphan", mapping={
            "job_id": "j-prio", "job_type": "download_notifications",
            "key": "k-prio-orphan", "status": JobStatus.RUNNING,
            "message": msg, "queued_at": "n", "started_at": "n",
            "finished_at": "", "result": "", "error": "",
        })
        await q._recover_orphaned_jobs()
        raw_head = await r.lindex(_QUEUE_LIST, 0)
        assert json.loads(raw_head)["job_id"] == "j-prio"
    finally:
        await q.stop()


async def test_stop_cancels_inflight_run_task_and_heartbeat():
    """stop() 对在途任务与心跳循环的取消清理路径。"""
    release = asyncio.Event()

    async def slow(payload):
        try:
            await release.wait()
        except asyncio.CancelledError:
            raise
        return {}

    q = _make_queue()
    q.register("slow", slow)
    await q.start(consume=False)
    # 手工装配心跳任务与在途任务，走 stop() 的完整清理分支
    q._heartbeat = asyncio.create_task(asyncio.sleep(3600))
    q._sem = asyncio.Semaphore(2)
    await q._sem.acquire()
    msg = _msg("j-slow", "k-slow-inflight")
    task = asyncio.create_task(q._run(json.loads(msg), msg))
    q._run_tasks.add(task)
    await asyncio.sleep(0.05)  # 让 _run 进入 handler 等待
    await q.stop()
    assert task.cancelled() or task.done()


# ---------------------------------------------------------------------------
# MemoryQueue 接口面（集成进程内直跑，纳入覆盖率）
# ---------------------------------------------------------------------------


async def test_memory_queue_full_surface():
    from app.services.task_queue import MemoryQueue

    ran = []

    async def ok(payload):
        ran.append(payload["v"])
        return {"echo": payload["v"]}

    async def boom(payload):
        raise ValueError("mem-fail")

    q = MemoryQueue(max_concurrent=2)
    q.register("ok", ok)
    q.register("boom", boom)
    await q.start()
    try:
        first = await q.enqueue("ok", "m1", {"v": 1})
        assert first["status"] == JobStatus.QUEUED
        assert await q.enqueue("ok", "m1", {}) is None  # 活跃去重
        await q.enqueue("boom", "m2", {})
        await q.enqueue("ghost", "m3", {})
        # 进度更新：排队中可见
        await q.update_progress("m1", {"step": 1})
        deadline = asyncio.get_running_loop().time() + 5
        while asyncio.get_running_loop().time() < deadline:
            s1 = await q.status("m1")
            s2 = await q.status("m2")
            s3 = await q.status("m3")
            if (s1["status"] == JobStatus.DONE and s2["status"] == JobStatus.FAILED
                    and s3["status"] == JobStatus.FAILED):
                break
            await asyncio.sleep(0.02)
        s1, s2, s3 = await q.status("m1"), await q.status("m2"), await q.status("m3")
        assert s1["result"] == {"echo": 1}
        assert "mem-fail" in s2["error"]
        assert "No handler" in s3["error"]
        # 终态后 progress 更新被忽略
        await q.update_progress("m1", {"late": True})
        assert (await q.status("m1"))["result"] == {"echo": 1}
        await q.clear("m1")
        assert await q.status("m1") is None
        assert await q.enqueue("ok", "m1", {"v": 2}) is not None
        await asyncio.sleep(0.1)
    finally:
        await q.stop()
    assert sorted(ran) == [1, 2]
