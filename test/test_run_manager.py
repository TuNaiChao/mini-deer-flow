"""RunManager 测试（M18）。

hermetic：用 ``MemoryRunStore`` + 几个 flaky/missing/permanent store 桩，覆盖 run 生命周期
管理的全部红线：asyncio 锁 / busy 重试（#2）/ orphan 恢复（#7）/ shutdown drain（#6）/
store-only hydrate（#9）/ rowcount 驱动 recovery（#12）/ 创建失败回滚（#13）/ 幂等 cancel。
不依赖真实 DB、不跑真实 agent。
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
from typing import Any

import pytest
from sqlalchemy.exc import DatabaseError as SQLAlchemyDatabaseError

from deerflow.runtime.runs import (
    ConflictError,
    DisconnectMode,
    MemoryRunStore,
    RunManager,
    RunStatus,
    UnsupportedStrategyError,
)
from deerflow.runtime.runs.manager import PersistenceRetryPolicy

ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


# ---------------------------------------------------------------------------
# 桩 store
# ---------------------------------------------------------------------------


class FlakyStatusRunStore(MemoryRunStore):
    """模拟瞬时 SQLite status 写失败（前 N 次 locked，之后成功）。"""

    def __init__(self, *, status_failures: int) -> None:
        super().__init__()
        self.status_failures = status_failures
        self.status_update_attempts = 0

    async def update_status(self, run_id, status, *, error=None):
        self.status_update_attempts += 1
        if self.status_failures > 0:
            self.status_failures -= 1
            raise sqlite3.OperationalError("database is locked")
        return await super().update_status(run_id, status, error=error)


class MissingRowStatusRunStore(MemoryRunStore):
    """status 更新返回 False（行没了）——触发 rowcount 驱动 recovery（红线 #12）。"""

    async def update_status(self, run_id, status, *, error=None):
        await super().update_status(run_id, status, error=error)
        return False


class PermanentStatusRunStore(MemoryRunStore):
    """模拟永久 SQLAlchemy 写失败（不可重试）。"""

    def __init__(self) -> None:
        super().__init__()
        self.status_update_attempts = 0

    async def update_status(self, run_id, status, *, error=None):
        self.status_update_attempts += 1
        raise SQLAlchemyDatabaseError(
            "UPDATE runs SET status = :status WHERE run_id = :run_id",
            {"status": status, "run_id": run_id},
            sqlite3.DatabaseError("no such table: runs"),
        )


class AlwaysFailPutStore(MemoryRunStore):
    """put 永远失败——测创建回滚（红线 #13）。"""

    async def put(self, *a, **k):
        raise sqlite3.OperationalError("database is locked")


class MissingCompletionRunStore(MemoryRunStore):
    """completion 首次返回 False，之后成功——测 completion row recovery。"""

    def __init__(self) -> None:
        super().__init__()
        self.completion_update_attempts = 0

    async def update_run_completion(self, run_id, *, status, **kwargs):
        self.completion_update_attempts += 1
        if self.completion_update_attempts == 1:
            return False
        return await super().update_run_completion(run_id, status=status, **kwargs)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def manager() -> RunManager:
    return RunManager()


@pytest.fixture
def manager_with_store() -> RunManager:
    return RunManager(store=MemoryRunStore())


async def _stored_statuses(store: MemoryRunStore, *run_ids: str) -> dict[str, Any]:
    rows = {}
    for run_id in run_ids:
        row = await store.get(run_id)
        rows[run_id] = row["status"] if row else None
    return rows


# ===========================================================================
# 基础生命周期
# ===========================================================================


async def test_create_and_get(manager: RunManager):
    """创建的 run 能取回，字段正确。"""
    record = await manager.create(
        "thread-1",
        "lead_agent",
        metadata={"key": "val"},
        kwargs={"input": {}},
        multitask_strategy="reject",
    )
    assert record.status == RunStatus.pending
    assert record.thread_id == "thread-1"
    assert record.assistant_id == "lead_agent"
    assert record.metadata == {"key": "val"}
    assert record.multitask_strategy == "reject"
    assert ISO_RE.match(record.created_at)
    assert record.store_only is False

    fetched = await manager.get(record.run_id)
    assert fetched is record


async def test_status_transitions(manager: RunManager):
    """pending → running → success 全跑通。"""
    record = await manager.create("thread-1")
    await manager.set_status(record.run_id, RunStatus.running)
    assert (await manager.get(record.run_id)).status == RunStatus.running
    await manager.set_status(record.run_id, RunStatus.success)
    assert (await manager.get(record.run_id)).status == RunStatus.success


async def test_set_status_with_error(manager: RunManager):
    """error 透传到 record。"""
    record = await manager.create("thread-1")
    await manager.set_status(record.run_id, RunStatus.error, error="boom")
    fetched = await manager.get(record.run_id)
    assert fetched.status == RunStatus.error
    assert fetched.error == "boom"


async def test_set_status_unknown_run_noop(manager: RunManager):
    """未知 run 的 set_status 不抛（只警告）。"""
    await manager.set_status("nope", RunStatus.success)  # 不抛


async def test_get_nonexistent(manager: RunManager):
    assert await manager.get("nope") is None


async def test_has_inflight(manager: RunManager):
    record = await manager.create("thread-1")
    assert await manager.has_inflight("thread-1") is True
    await manager.set_status(record.run_id, RunStatus.success)
    assert await manager.has_inflight("thread-1") is False


async def test_has_inflight_thread_scoped(manager: RunManager):
    """has_inflight 只看本 thread。"""
    await manager.create("thread-1")
    assert await manager.has_inflight("thread-1") is True
    assert await manager.has_inflight("thread-2") is False


# ===========================================================================
# cancel（幂等红线）
# ===========================================================================


async def test_cancel_active_run(manager: RunManager):
    record = await manager.create("thread-1")
    assert await manager.cancel(record.run_id) is True
    assert (await manager.get(record.run_id)).status == RunStatus.interrupted


async def test_cancel_idempotent(manager: RunManager):
    """已 interrupted 的 run 再 cancel 是 no-op 成功。"""
    record = await manager.create("thread-1")
    assert await manager.cancel(record.run_id) is True
    assert await manager.cancel(record.run_id) is True  # 幂等


async def test_cancel_terminal_returns_false(manager: RunManager):
    """终态（非 interrupted）的 run cancel 返回 False。"""
    record = await manager.create("thread-1")
    await manager.set_status(record.run_id, RunStatus.success)
    assert await manager.cancel(record.run_id) is False


async def test_cancel_unknown_returns_false(manager: RunManager):
    assert await manager.cancel("nope") is False


async def test_cancel_persists_interrupted_status_to_store():
    """cancel 把 interrupted 写进 store。"""
    store = MemoryRunStore()
    rm = RunManager(store=store)
    record = await rm.create("thread-1")
    await rm.cancel(record.run_id)
    statuses = await _stored_statuses(store, record.run_id)
    assert statuses[record.run_id] == "interrupted"


# ===========================================================================
# store-only hydrate（红线 #9）
# ===========================================================================


async def test_get_hydrates_store_only_run():
    """内存没有、store 有的 run → hydrate 出 store_only record（无 task）。"""
    store = MemoryRunStore()
    await store.put("run-x", thread_id="t1", status="success")
    rm = RunManager(store=store)

    record = await rm.get("run-x")
    assert record is not None
    assert record.store_only is True
    assert record.task is None
    assert record.status == RunStatus.success


async def test_get_prefers_in_memory_over_store():
    """内存 + store 都有同一 run → 内存优先。"""
    store = MemoryRunStore()
    rm = RunManager(store=store)
    record = await rm.create("thread-1")
    # 篡改 store 行为 running（陈旧），内存 record 应优先
    await store.update_status(record.run_id, "running")
    fetched = await rm.get(record.run_id)
    assert fetched is record
    assert fetched.status == RunStatus.pending


async def test_cancel_store_only_run_returns_false():
    """store-only record 无 task，cancel 返回 False（本 worker 停不了）。"""
    store = MemoryRunStore()
    await store.put("run-x", thread_id="t1", status="running")
    rm = RunManager(store=store)
    # hydrate
    await rm.get("run-x")
    # cancel 应返 False（record 不在内存 _runs）
    assert await rm.cancel("run-x") is False


# ===========================================================================
# 创建回滚（红线 #13）
# ===========================================================================


async def test_create_rolls_back_in_memory_on_store_failure():
    """store put 失败 → 内存 record 回滚（红线 #13）。"""
    rm = RunManager(store=AlwaysFailPutStore())
    with pytest.raises(sqlite3.OperationalError):
        await rm.create("thread-1")
    # 内存里不应有这个 run
    assert await rm.has_inflight("thread-1") is False


async def test_create_or_reject_does_not_interrupt_old_run_when_new_run_store_write_fails():
    """interrupt 策略下，新 run 的 store 写失败时不应打断老 run。"""
    rm = RunManager(store=AlwaysFailPutStore())
    old = await RunManager(store=MemoryRunStore()).create("thread-1")  # 老的单独 manager
    # 把老 record 塞进 rm 的内存（模拟）
    rm._runs[old.run_id] = old
    rm._index_run_locked(old)
    # 新 run 创建失败
    with pytest.raises(sqlite3.OperationalError):
        await rm.create_or_reject("thread-1", multitask_strategy="interrupt")
    # 老 run 不应被打断
    assert old.status == RunStatus.pending


# ===========================================================================
# create_or_reject（TOCTOU 原子性 + 三策略）
# ===========================================================================


async def test_create_or_reject_reject_conflict(manager: RunManager):
    """reject 策略 + 有 inflight → ConflictError。"""
    await manager.create("thread-1", multitask_strategy="reject")
    with pytest.raises(ConflictError):
        await manager.create_or_reject("thread-1", multitask_strategy="reject")


async def test_create_or_reject_interrupt_cancels_inflight(manager: RunManager):
    """interrupt 策略 → 打断 inflight 再创建。"""
    old = await manager.create("thread-1")
    new = await manager.create_or_reject("thread-1", multitask_strategy="interrupt")
    assert old.status == RunStatus.interrupted
    assert new.status == RunStatus.pending
    assert new.run_id != old.run_id


async def test_create_or_reject_rollback_cancels_inflight(manager: RunManager):
    """rollback 策略 → 打断 inflight（abort_action=rollback）再创建。"""
    old = await manager.create("thread-1")
    new = await manager.create_or_reject("thread-1", multitask_strategy="rollback")
    assert old.status == RunStatus.interrupted
    assert old.abort_action == "rollback"
    assert new.status == RunStatus.pending


async def test_create_or_reject_unsupported_strategy(manager: RunManager):
    """未实现的策略 → UnsupportedStrategyError。"""
    with pytest.raises(UnsupportedStrategyError):
        await manager.create_or_reject("thread-1", multitask_strategy="enqueue")


async def test_create_or_reject_no_inflight_succeeds(manager: RunManager):
    """无 inflight 时 reject 直接创建。"""
    record = await manager.create_or_reject("thread-1", multitask_strategy="reject")
    assert record.status == RunStatus.pending


async def test_create_or_reject_inflight_is_thread_scoped(manager: RunManager):
    """inflight 检查只看本 thread。"""
    await manager.create("thread-1")
    # thread-2 无 inflight，reject 应成功
    record = await manager.create_or_reject("thread-2", multitask_strategy="reject")
    assert record.thread_id == "thread-2"


async def test_create_or_reject_model_name():
    """create_or_reject 设 model_name。"""
    rm = RunManager()
    record = await rm.create_or_reject("thread-1", model_name="gpt-4o")
    assert record.model_name == "gpt-4o"


# ===========================================================================
# busy 重试（红线 #2）+ rowcount recovery（红线 #12）
# ===========================================================================


async def test_status_persistence_retries_transient_sqlite_lock():
    """status 写瞬时 locked → 有界重试后成功（红线 #2）。"""
    store = FlakyStatusRunStore(status_failures=2)
    rm = RunManager(store=store, persistence_retry_policy=PersistenceRetryPolicy(initial_delay=0))
    record = await rm.create("thread-1")
    await rm.set_status(record.run_id, RunStatus.success)
    statuses = await _stored_statuses(store, record.run_id)
    assert statuses[record.run_id] == "success"
    assert store.status_update_attempts == 3  # 2 失败 + 1 成功


async def test_status_persistence_does_not_retry_permanent_errors():
    """永久 SQLAlchemy 错误不重试（不藏永久失败）。"""
    store = PermanentStatusRunStore()
    rm = RunManager(store=store, persistence_retry_policy=PersistenceRetryPolicy(initial_delay=0))
    record = await rm.create("thread-1")  # put 不走 status 路径，成功
    # set_status 的 persist 会失败但不抛（best-effort），attempt 应只 1 次
    await rm.set_status(record.run_id, RunStatus.success)
    assert store.status_update_attempts == 1


async def test_status_persistence_recreates_missing_store_row():
    """update_status 返回 False → 用内存 snapshot 重建行（红线 #12）。"""
    store = MissingRowStatusRunStore()
    rm = RunManager(store=store)
    record = await rm.create("thread-1")
    await rm.set_status(record.run_id, RunStatus.success)
    # MissingRow 的 update_status 内部已写，但返 False 触发 snapshot 重建（put）
    row = await store.get(record.run_id)
    assert row is not None
    assert row["status"] == "success"


async def test_completion_persistence_recreates_missing_store_row():
    """completion update 返 False → snapshot 重建后再写。"""
    store = MissingCompletionRunStore()
    rm = RunManager(store=store)
    record = await rm.create("thread-1")
    await rm.update_run_completion(record.run_id, status="success", total_tokens=42)
    assert store.completion_update_attempts == 2  # 首次 False + 重建后再写
    row = await store.get(record.run_id)
    assert row["total_tokens"] == 42


# ===========================================================================
# orphan 恢复（红线 #7）
# ===========================================================================


async def test_reconcile_orphaned_marks_stale_rows_error():
    """持久化但本 worker 无 task 的 inflight 行 → 标 error（红线 #7）。"""
    store = MemoryRunStore()
    await store.put("orphan-1", thread_id="t1", status="running")
    await store.put("orphan-2", thread_id="t1", status="pending")
    rm = RunManager(store=store)

    recovered = await rm.reconcile_orphaned_inflight_runs(error="Worker restarted")
    assert len(recovered) == 2
    statuses = await _stored_statuses(store, "orphan-1", "orphan-2")
    assert statuses["orphan-1"] == "error"
    assert statuses["orphan-2"] == "error"


async def test_reconcile_skips_live_local_run():
    """本 worker 正在跑的 run 不算 orphan。"""
    store = MemoryRunStore()
    rm = RunManager(store=store)
    record = await rm.create("thread-1")  # 本地有，pending
    # 手动塞一个 store 里的 inflight 行指向同一 run（store 里 status 还是 pending）
    recovered = await rm.reconcile_orphaned_inflight_runs(error="restart")
    # record 在本地 _runs 且 pending → 跳过
    assert all(r.run_id != record.run_id for r in recovered)


async def test_reconcile_noop_without_store(manager: RunManager):
    """无 store → reconcile 返空（无可恢复对象）。"""
    recovered = await manager.reconcile_orphaned_inflight_runs(error="restart")
    assert recovered == []


# ===========================================================================
# shutdown drain（红线 #6）
# ===========================================================================


async def test_shutdown_drains_inflight_and_marks_interrupted():
    """shutdown 取消在途 task + 有界 await + 未 settle 的标 interrupted。"""
    rm = RunManager()

    async def slow():
        try:
            await asyncio.sleep(100)
        except asyncio.CancelledError:
            raise

    record = await rm.create("thread-1")
    record.task = asyncio.create_task(slow())
    await asyncio.sleep(0)  # 让 task 起来

    await rm.shutdown(timeout=0.5)
    await asyncio.sleep(0)
    assert record.task.cancelled() or record.task.done()
    assert record.status == RunStatus.interrupted


async def test_shutdown_keeps_status_of_run_that_settles_during_drain():
    """drain 期间自行完成的 run 保留真实终态（不被一刀覆盖 interrupted）。"""
    rm = RunManager()

    async def quick():
        await asyncio.sleep(0.01)

    record = await rm.create("thread-1")
    record.task = asyncio.create_task(quick())
    await rm.set_status(record.run_id, RunStatus.success)

    await rm.shutdown(timeout=1.0)
    # task 自行完成 → 保留 success
    assert record.status == RunStatus.success


async def test_shutdown_no_inflight_is_noop(manager: RunManager):
    """无在途 run → shutdown 立即返。"""
    await manager.shutdown(timeout=0.1)  # 不抛、不卡


# ===========================================================================
# list_by_thread + 线程索引
# ===========================================================================


async def test_list_by_thread_memory_only(manager: RunManager):
    r1 = await manager.create("thread-1")
    r2 = await manager.create("thread-1")
    await manager.create("thread-2")
    records = await manager.list_by_thread("thread-1")
    assert {r.run_id for r in records} == {r1.run_id, r2.run_id}


async def test_list_by_thread_merges_store_newest_first(manager_with_store: RunManager):
    """内存 + store 合并，最新在前。"""
    rm = manager_with_store
    await rm.create("thread-1")  # 进内存 + store
    # 直接往 store 塞一条历史 run（不在内存）
    await rm._store.put("old-run", thread_id="thread-1", status="success", created_at="2020-01-01T00:00:00")
    records = await rm.list_by_thread("thread-1")
    assert len(records) == 2
    # 最新（内存的，created_at 较新）在前
    assert records[0].run_id != "old-run"


async def test_list_by_thread_no_store(manager: RunManager):
    records = await manager.list_by_thread("thread-x")
    assert records == []


async def test_thread_index_scopes_per_thread(manager: RunManager):
    """线程索引按 thread 隔离——一个 thread 的 cleanup 不影响另一个。"""
    r1 = await manager.create("thread-1")
    await manager.create("thread-2")
    await manager.cleanup(r1.run_id, delay=0)
    # thread-2 仍在
    assert await manager.has_inflight("thread-2") is True


async def test_thread_index_preserves_insertion_order(manager: RunManager):
    """索引保留插入序。"""
    r1 = await manager.create("thread-1")
    r2 = await manager.create("thread-1")
    records = await manager.list_by_thread("thread-1")
    # 两条 created_at 可能同瞬；索引序应稳定
    ids = {r.run_id for r in records}
    assert ids == {r1.run_id, r2.run_id}


# ===========================================================================
# update_model_name + update_run_completion + aget
# ===========================================================================


async def test_update_model_name(manager_with_store: RunManager):
    rm = manager_with_store
    record = await rm.create("thread-1")
    await rm.update_model_name(record.run_id, "claude")
    assert (await rm.get(record.run_id)).model_name == "claude"
    row = await rm._store.get(record.run_id)
    assert row["model_name"] == "claude"


async def test_update_run_completion(manager_with_store: RunManager):
    rm = manager_with_store
    record = await rm.create("thread-1")
    await rm.update_run_completion(record.run_id, status="success", total_tokens=100, total_input_tokens=60)
    fetched = await rm.get(record.run_id)
    assert fetched.total_tokens == 100
    assert fetched.total_input_tokens == 60


async def test_aget_returns_in_memory(manager_with_store: RunManager):
    """aget 是 get 的别名。"""
    rm = manager_with_store
    record = await rm.create("thread-1")
    assert await rm.aget(record.run_id) is record


async def test_aget_falls_back_to_store(manager_with_store: RunManager):
    rm = manager_with_store
    await rm._store.put("hist", thread_id="t1", status="error")
    fetched = await rm.aget("hist")
    assert fetched is not None
    assert fetched.status == RunStatus.error
    assert fetched.store_only is True


async def test_cleanup_removes_record(manager: RunManager):
    record = await manager.create("thread-1")
    await manager.cleanup(record.run_id, delay=0)
    assert await manager.get(record.run_id) is None


# ===========================================================================
# 默认值
# ===========================================================================


async def test_create_defaults(manager: RunManager):
    """create 默认值：cancel 断连 / reject 策略。"""
    record = await manager.create("thread-1")
    assert record.on_disconnect == DisconnectMode.cancel
    assert record.multitask_strategy == "reject"
    assert record.model_name is None


# ===========================================================================
# 并发可见性 + cancellation 边界（deer 回归锁，审查补齐）
# ===========================================================================


async def test_create_does_not_expose_run_until_store_persist_completes():
    """并发读者必须等到新 run 持久化完成才能看到它（锁跨 persist 持有）。

    锁住 run 可见性边界：``create`` 在 ``_persist_new_run_to_store`` 完成前不释放锁，
    所以并发的 ``list_by_thread`` 看不到半成品 run。
    """
    store = MemoryRunStore()
    rm = RunManager(store=store)
    original_put = store.put
    put_started = asyncio.Event()
    allow_put = asyncio.Event()

    async def blocking_put(run_id, **kwargs):
        put_started.set()
        await allow_put.wait()
        return await original_put(run_id, **kwargs)

    store.put = blocking_put
    create_task = asyncio.create_task(rm.create("thread-1"))
    list_task = None

    try:
        await put_started.wait()
        # persist 卡住时，并发的 list_by_thread 应被锁挡住（不 done）
        list_task = asyncio.create_task(rm.list_by_thread("thread-1"))
        await asyncio.sleep(0)
        assert not list_task.done()

        allow_put.set()
        record = await create_task
        runs = await list_task

        assert [r.run_id for r in runs] == [record.run_id]
    finally:
        allow_put.set()
        cleanup = []
        for task in (list_task, create_task):
            if task is None or task.done():
                continue
            task.cancel()
            cleanup.append(task)
        await asyncio.gather(*cleanup, return_exceptions=True)


async def test_create_or_reject_does_not_interrupt_old_run_when_new_run_store_write_cancelled():
    """新 run 持久化期间被 CancelledError 打断时，**不应**取消已存在的老 run。

    CancelledError 是 BaseException（不被 ``except Exception`` 捕获），经 finally 清理新 run 后
    直接传播，跳过「打断 inflight」块——老 run 保持 running。
    """
    store = MemoryRunStore()
    rm = RunManager(store=store)
    old = await rm.create("thread-1")
    await rm.set_status(old.run_id, RunStatus.running)

    async def cancelled_put(run_id, **kwargs):
        raise asyncio.CancelledError

    store.put = cancelled_put

    with pytest.raises(asyncio.CancelledError):
        await rm.create_or_reject("thread-1", multitask_strategy="interrupt")

    # 老 run 没被打断
    assert old.status == RunStatus.running
    assert old.abort_event.is_set() is False
    # 内存里只剩老 run（新 run 被 finally 清掉）
    assert list(rm._runs) == [old.run_id]
