"""内存 run 注册表 + 可选持久化 RunStore 后端。

``RunManager`` 是 run 生命周期的**运行管理层**：在内存里维护活跃 run 的 ``RunRecord``，
配合可选的 ``RunStore``（SQL / 内存）做持久化，让 run 历史能跨进程重启存活。

核心职责（对齐 deer-flow，红线见 ALIGNMENT_OUTLINE Part E）：

- **所有写操作经 asyncio 锁**——状态机不会被并发请求撕裂；
- **SQLite busy 重试**（红线 #2）：``_call_store_with_retry`` 对 ``database is locked`` /
  ``SQLITE_BUSY`` / ``SQLITE_LOCKED`` 指数退避重试，transient 写压力不致终态化失败；
- **``create_or_reject``**：原子地「检查 inflight + 创建」，消除 ``has_inflight`` + ``create``
  的 TOCTOU 竞态；``multitask_strategy`` 三态：``reject``（有 inflight 抛 ``ConflictError``）/
  ``interrupt``（打断 inflight）/ ``rollback``（打断 + 回滚）；
- **幂等 cancel**：已 interrupted 的 run 再 cancel 是 no-op 成功；
- **``reconcile_orphaned_inflight_runs``**（红线 #7）：启动时把「持久化了但本 worker 无 task
  的 pending/running 行」标 error——Gateway 重启后那些 run 不可能还有本地 worker；
- **``shutdown(timeout=5)``**（红线 #6 / #3373）：关 checkpointer 前 bounded-await 在途 run，
  让能在 timeout 内 settle 的 run flush 最终 checkpoint；只有没 settle 的才标 interrupted；
- **store-only hydrate**（红线 #9）：从 RunStore 还原的 record 无 task/abort_event，cancel 返回
  False（本 worker 停不了别的 worker 的 run）；
- **rowcount 驱动 recovery**（红线 #12）：``update_status`` 返回 False → 用内存 snapshot 重建行；
- **``_persist_new_run_to_store`` 失败回滚内存记录**（红线 #13）。
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from deerflow.utils.time import now_iso as _now_iso

from .schemas import DisconnectMode, RunStatus

if TYPE_CHECKING:
    from deerflow.runtime.runs.store.base import RunStore

logger = logging.getLogger(__name__)

_RETRYABLE_SQLITE_MESSAGES = (
    "database is locked",
    "database table is locked",
    "database is busy",
)

_RETRYABLE_SQLITE_ERROR_CODES = {
    sqlite3.SQLITE_BUSY,
    sqlite3.SQLITE_LOCKED,
}


def _is_retryable_persistence_error(exc: BaseException) -> bool:
    """是否瞬时 SQLite 持久化失败（可重试）。

    SQLite 锁竞争可能经 sqlite3 异常或 SQLAlchemy 包装抛出。这里的短有界重试保护 run 状态
    终态化不被瞬时写压力卡死，同时不会把永久失败永远藏起来。
    """

    pending: list[BaseException] = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))

        message = str(current).lower()
        if any(fragment in message for fragment in _RETRYABLE_SQLITE_MESSAGES):
            return True
        if isinstance(current, (sqlite3.OperationalError, sqlite3.DatabaseError)):
            error_code = getattr(current, "sqlite_errorcode", None)
            if error_code in _RETRYABLE_SQLITE_ERROR_CODES:
                return True
        for chained in (getattr(current, "orig", None), current.__cause__, current.__context__):
            if isinstance(chained, BaseException):
                pending.append(chained)
    return False


@dataclass(frozen=True)
class PersistenceRetryPolicy:
    """短 run-store 写的有界重试策略。"""

    max_attempts: int = 5
    initial_delay: float = 0.05
    max_delay: float = 1.0
    backoff_factor: float = 2.0


@dataclass
class RunRecord:
    """单次 run 的可变记录。

    ``task`` / ``abort_event`` 是进程内（内存）状态——只有创建该 run 的 worker 才有。
    从 RunStore 还原的 record 设 ``store_only=True`` 且无 task/abort_event（红线 #9），
    cancel 这类 record 返回 False。
    """

    run_id: str
    thread_id: str
    assistant_id: str | None
    status: RunStatus
    on_disconnect: DisconnectMode
    multitask_strategy: str = "reject"
    metadata: dict = field(default_factory=dict)
    kwargs: dict = field(default_factory=dict)
    user_id: str | None = None
    created_at: str = ""
    updated_at: str = ""
    task: asyncio.Task | None = field(default=None, repr=False)
    abort_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    abort_action: str = "interrupt"
    error: str | None = None
    model_name: str | None = None
    store_only: bool = False
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_tokens: int = 0
    llm_call_count: int = 0
    lead_agent_tokens: int = 0
    subagent_tokens: int = 0
    middleware_tokens: int = 0
    # #3658：按模型归桶的 token 用量（一次 run 可能调多个模型——lead + 多个子代理）。
    token_usage_by_model: dict[str, dict[str, int]] = field(default_factory=dict)
    message_count: int = 0
    last_ai_message: str | None = None
    first_human_message: str | None = None


class RunManager:
    """内存 run 注册表 + 可选持久化 RunStore 后端。

    所有写操作经 asyncio 锁保护。给了 ``store`` 时，可序列化元数据也会持久化，让 run 历史
    跨进程重启存活。
    """

    def __init__(
        self,
        store: RunStore | None = None,
        *,
        persistence_retry_policy: PersistenceRetryPolicy | None = None,
    ) -> None:
        self._runs: dict[str, RunRecord] = {}
        # 二级索引：thread_id -> 插入序 run_id 集合（dict 当有序集合用），与 ``_runs`` 同步维护，
        # 让 per-thread 查询不必 O(全部内存 run) 全扫，同时保留 ``_runs`` 的迭代序（见
        # ``_thread_records_locked``）。
        self._runs_by_thread: dict[str, dict[str, None]] = {}
        self._lock = asyncio.Lock()
        self._store = store
        self._persistence_retry_policy = persistence_retry_policy or PersistenceRetryPolicy()

    def _index_run_locked(self, record: RunRecord) -> None:
        """把 *record* 登进 thread 索引。调用方须持 ``self._lock``。"""
        self._runs_by_thread.setdefault(record.thread_id, {})[record.run_id] = None

    def _unindex_run_locked(self, run_id: str, thread_id: str) -> None:
        """把 *run_id* 从 thread 索引移除。调用方须持 ``self._lock``。"""
        bucket = self._runs_by_thread.get(thread_id)
        if bucket is not None:
            bucket.pop(run_id, None)
            if not bucket:
                self._runs_by_thread.pop(thread_id, None)

    def _thread_records_locked(self, thread_id: str) -> list[RunRecord]:
        """返回 *thread_id* 的活跃内存 record。调用方须持 ``self._lock``。

        用 ``_runs_by_thread`` 索引做 O(thread 内 run 数) 查询而非扫全部内存 run。正确性依赖
        索引与 ``_runs`` 在 ``self._lock`` 下同步变更（两次写之间无 ``await``），所以任何持锁者
        看到的两者一致。``self._runs.get`` 过滤是纵深防御而非调和：它丢弃「还在索引里但已从
        ``_runs`` 消失」的陈旧 id，但救不回「在 ``_runs`` 里却没进索引」的 run（那种会被静默漏掉）。
        它只守这一向，以防未来重构破坏同步不变量。
        """
        run_ids = self._runs_by_thread.get(thread_id)
        if not run_ids:
            return []
        return [record for run_id in run_ids if (record := self._runs.get(run_id)) is not None]

    @staticmethod
    def _store_put_payload(record: RunRecord, *, error: str | None = None) -> dict[str, Any]:
        payload = {
            "thread_id": record.thread_id,
            "assistant_id": record.assistant_id,
            "status": record.status.value,
            "multitask_strategy": record.multitask_strategy,
            "metadata": record.metadata or {},
            "kwargs": record.kwargs or {},
            "error": error if error is not None else record.error,
            "created_at": record.created_at,
            "model_name": record.model_name,
        }
        if record.user_id is not None:
            payload["user_id"] = record.user_id
        return payload

    async def _call_store_with_retry(
        self,
        operation_name: str,
        run_id: str,
        operation: Callable[[], Awaitable[Any]],
    ) -> Any:
        """跑一个短 store 操作，对 SQLite 压力做有界重试。"""
        policy = self._persistence_retry_policy
        attempt = 1
        delay = policy.initial_delay
        while True:
            try:
                return await operation()
            except Exception as exc:
                retryable = _is_retryable_persistence_error(exc)
                if attempt >= policy.max_attempts or not retryable:
                    raise
                logger.warning(
                    "Transient persistence failure during %s for run %s (attempt %d/%d); retrying",
                    operation_name,
                    run_id,
                    attempt,
                    policy.max_attempts,
                    exc_info=True,
                )
                if delay > 0:
                    await asyncio.sleep(delay)
                delay = min(policy.max_delay, delay * policy.backoff_factor if delay else policy.initial_delay)
                attempt += 1

    async def _persist_snapshot_to_store(self, run_id: str, payload: dict[str, Any]) -> bool:
        """best-effort 持久化一个先前捕获的 run 快照。"""
        if self._store is None:
            return True
        try:
            await self._call_store_with_retry(
                "put",
                run_id,
                lambda: self._store.put(run_id, **payload),
            )
            return True
        except Exception:
            logger.warning("Failed to persist run %s to store", run_id, exc_info=True)
            return False

    async def _persist_new_run_to_store(self, record: RunRecord) -> None:
        """持久化新建的 run record 到后端 store。

        run 的初始创建是 run 可见性边界的一部分：调用方不应在内存里看到一个 store 行还没建
        的 run。与后续 status/model 更新不同，这里的失败要**上抛**让调用方把创建当失败处理。
        回滚是调用方在把 record 插进 ``_runs`` 之后的责任。
        """
        if self._store is None:
            return
        await self._call_store_with_retry(
            "put",
            record.run_id,
            lambda: self._store.put(record.run_id, **self._store_put_payload(record)),
        )

    async def _persist_to_store(self, record: RunRecord, *, error: str | None = None) -> bool:
        """best-effort 持久化 run record 到后端 store。"""
        return await self._persist_snapshot_to_store(
            record.run_id,
            self._store_put_payload(record, error=error),
        )

    async def _persist_status(self, record: RunRecord, status: RunStatus, *, error: str | None = None) -> bool:
        """best-effort 持久化一次状态迁移到后端 store。"""
        if self._store is None:
            return True
        row_recovery_payload = self._store_put_payload(record, error=error)
        try:
            updated = await self._call_store_with_retry(
                "update_status",
                record.run_id,
                lambda: self._store.update_status(record.run_id, status.value, error=error),
            )
            if updated is False:
                return await self._persist_snapshot_to_store(record.run_id, row_recovery_payload)
            return True
        except Exception:
            logger.warning("Failed to persist status update for run %s", record.run_id, exc_info=True)
            return False

    @staticmethod
    def _record_from_store(row: dict[str, Any]) -> RunRecord:
        """从序列化的 store 行建只读运行时 record。

        NULL 的 status/on_disconnect 列（例如列加之前写的旧行）分别默认 ``pending`` / ``cancel``。
        """
        return RunRecord(
            run_id=row["run_id"],
            thread_id=row["thread_id"],
            assistant_id=row.get("assistant_id"),
            status=RunStatus(row.get("status") or RunStatus.pending.value),
            on_disconnect=DisconnectMode(row.get("on_disconnect") or DisconnectMode.cancel.value),
            multitask_strategy=row.get("multitask_strategy") or "reject",
            metadata=row.get("metadata") or {},
            kwargs=row.get("kwargs") or {},
            created_at=row.get("created_at") or "",
            updated_at=row.get("updated_at") or "",
            user_id=row.get("user_id"),
            error=row.get("error"),
            model_name=row.get("model_name"),
            store_only=True,
            total_input_tokens=row.get("total_input_tokens") or 0,
            total_output_tokens=row.get("total_output_tokens") or 0,
            total_tokens=row.get("total_tokens") or 0,
            llm_call_count=row.get("llm_call_count") or 0,
            lead_agent_tokens=row.get("lead_agent_tokens") or 0,
            subagent_tokens=row.get("subagent_tokens") or 0,
            middleware_tokens=row.get("middleware_tokens") or 0,
            token_usage_by_model=row.get("token_usage_by_model") or {},
            message_count=row.get("message_count") or 0,
            last_ai_message=row.get("last_ai_message"),
            first_human_message=row.get("first_human_message"),
        )

    async def update_run_completion(self, run_id: str, **kwargs) -> None:
        """持久化 token 用量 + 完成数据到后端 store。"""
        row_recovery_payload: dict[str, Any] | None = None
        async with self._lock:
            record = self._runs.get(run_id)
            if record is not None:
                for key, value in kwargs.items():
                    if key == "status":
                        continue
                    if hasattr(record, key) and value is not None:
                        setattr(record, key, value)
                record.updated_at = _now_iso()
                row_recovery_payload = self._store_put_payload(record, error=kwargs.get("error"))
        if self._store is None:
            return
        try:
            updated = await self._call_store_with_retry(
                "update_run_completion",
                run_id,
                lambda: self._store.update_run_completion(run_id, **kwargs),
            )
            if updated is False:
                if row_recovery_payload is None:
                    logger.warning("Failed to recreate missing run %s for completion persistence", run_id)
                    return
                if not await self._persist_snapshot_to_store(run_id, row_recovery_payload):
                    return
                recovered = await self._call_store_with_retry(
                    "update_run_completion",
                    run_id,
                    lambda: self._store.update_run_completion(run_id, **kwargs),
                )
                if recovered is False:
                    logger.warning("Run completion update for %s affected no rows after row recreation", run_id)
        except Exception:
            logger.warning("Failed to persist run completion for %s", run_id, exc_info=True)

    async def update_run_progress(self, run_id: str, **kwargs) -> None:
        """持久化一个运行中的 token/消息快照，不改 run 状态。"""
        should_persist = True
        async with self._lock:
            record = self._runs.get(run_id)
            if record is not None:
                should_persist = record.status == RunStatus.running
            if record is not None and should_persist:
                for key, value in kwargs.items():
                    if hasattr(record, key) and value is not None:
                        setattr(record, key, value)
                record.updated_at = _now_iso()
        if should_persist and self._store is not None:
            try:
                await self._store.update_run_progress(run_id, **kwargs)
            except Exception:
                logger.warning("Failed to persist run progress for %s", run_id, exc_info=True)

    async def create(
        self,
        thread_id: str,
        assistant_id: str | None = None,
        *,
        on_disconnect: DisconnectMode = DisconnectMode.cancel,
        metadata: dict | None = None,
        kwargs: dict | None = None,
        multitask_strategy: str = "reject",
        user_id: str | None = None,
    ) -> RunRecord:
        """创建一个新的 pending run 并登记。"""
        run_id = str(uuid.uuid4())
        now = _now_iso()
        record = RunRecord(
            run_id=run_id,
            thread_id=thread_id,
            assistant_id=assistant_id,
            status=RunStatus.pending,
            on_disconnect=on_disconnect,
            multitask_strategy=multitask_strategy,
            metadata=metadata or {},
            kwargs=kwargs or {},
            user_id=user_id,
            created_at=now,
            updated_at=now,
        )
        async with self._lock:
            self._runs[run_id] = record
            self._index_run_locked(record)
            persisted = False
            try:
                await self._persist_new_run_to_store(record)
                persisted = True
            except Exception:
                logger.warning("Failed to persist run %s; rolled back in-memory record", run_id, exc_info=True)
                raise
            finally:
                # 也覆盖 cancellation——它绕过 ``except Exception``。
                if not persisted:
                    self._runs.pop(run_id, None)
                    self._unindex_run_locked(run_id, record.thread_id)
        logger.info("Run created: run_id=%s thread_id=%s", run_id, thread_id)
        return record

    async def get(self, run_id: str, *, user_id: str | None = None) -> RunRecord | None:
        """按 ID 返回 run record，或 ``None``。

        Args:
            run_id: 要查的 run ID。
            user_id: 可选 user ID，从 store hydrate 时做权限过滤。
        """
        async with self._lock:
            record = self._runs.get(run_id)
        if record is not None:
            return record
        if self._store is None:
            return None
        try:
            row = await self._store.get(run_id, user_id=user_id)
        except Exception:
            logger.warning("Failed to hydrate run %s from store", run_id, exc_info=True)
            return None
        # store await 后再查一次：并发的 create() 可能在 store 调用进行中插入了内存 record。
        async with self._lock:
            record = self._runs.get(run_id)
        if record is not None:
            return record
        if row is None:
            return None
        try:
            return self._record_from_store(row)
        except Exception:
            logger.warning("Failed to map store row for run %s", run_id, exc_info=True)
            return None

    async def aget(self, run_id: str, *, user_id: str | None = None) -> RunRecord | None:
        """按 ID 返回 run record，持久 store 兜底。``get`` 的向后兼容别名。"""
        return await self.get(run_id, user_id=user_id)

    async def list_by_thread(self, thread_id: str, *, user_id: str | None = None, limit: int = 100) -> list[RunRecord]:
        """返回某 thread 的 run，最新在前，至多 ``limit`` 条。

        当同一 ``run_id`` 在内存和后端 store 都存在时，内存 record 优先。合并结果按
        ``created_at`` 降序排后截到 ``limit``（默认 100）。

        Args:
            thread_id: 过滤的 thread ID。
            user_id: 可选 user ID，从 store hydrate 时做权限过滤。
            limit: 最多返回多少条。
        """
        async with self._lock:
            memory_records = self._thread_records_locked(thread_id)
        if self._store is None:
            return sorted(memory_records, key=lambda r: r.created_at, reverse=True)[:limit]
        records_by_id = {record.run_id: record for record in memory_records}
        store_limit = max(0, limit - len(memory_records))
        try:
            rows = await self._store.list_by_thread(thread_id, user_id=user_id, limit=store_limit)
        except Exception:
            logger.warning("Failed to hydrate runs for thread %s from store", thread_id, exc_info=True)
            return sorted(memory_records, key=lambda r: r.created_at, reverse=True)[:limit]
        for row in rows:
            run_id = row.get("run_id")
            if run_id and run_id not in records_by_id:
                try:
                    records_by_id[run_id] = self._record_from_store(row)
                except Exception:
                    logger.warning("Failed to map store row for run %s", run_id, exc_info=True)
        return sorted(records_by_id.values(), key=lambda record: record.created_at, reverse=True)[:limit]

    async def set_status(self, run_id: str, status: RunStatus, *, error: str | None = None) -> None:
        """把 run 迁到新状态。"""
        async with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                logger.warning("set_status called for unknown run %s", run_id)
                return
            record.status = status
            record.updated_at = _now_iso()
            if error is not None:
                record.error = error
        await self._persist_status(record, status, error=error)
        logger.info("Run %s -> %s", run_id, status.value)

    async def _persist_model_name(self, run_id: str, model_name: str | None) -> None:
        """best-effort 持久化 model_name 更新到后端 store。"""
        if self._store is None:
            return
        try:
            await self._call_store_with_retry(
                "update_model_name",
                run_id,
                lambda: self._store.update_model_name(run_id, model_name),
            )
        except Exception:
            logger.warning("Failed to persist model_name update for run %s", run_id, exc_info=True)

    async def update_model_name(self, run_id: str, model_name: str | None) -> None:
        """更新某 run 的 model name。"""
        async with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                logger.warning("update_model_name called for unknown run %s", run_id)
                return
            record.model_name = model_name
            record.updated_at = _now_iso()
        await self._persist_model_name(run_id, model_name)
        logger.info("Run %s model_name=%s", run_id, model_name)

    async def cancel(self, run_id: str, *, action: str = "interrupt") -> bool:
        """请求取消一个 run。

        Args:
            run_id: 要取消的 run ID。
            action: ``"interrupt"`` 保留 checkpoint；``"rollback"`` 回滚到 run 前状态。

        设 abort event（带 action 原因）并 cancel asyncio task。返回 ``True`` 表示取消已发起
        **或** run 已 interrupted（幂等——第二次 cancel 是 no-op 成功）。只有「本 worker 不认识
        这个 run」或「run 已到 interrupted 以外的终态（completed/failed 等）」才返回 ``False``。
        """
        async with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                return False
            if record.status == RunStatus.interrupted:
                return True  # 幂等——本 worker 已取消过
            if record.status not in (RunStatus.pending, RunStatus.running):
                return False
            record.abort_action = action
            record.abort_event.set()
            if record.task is not None and not record.task.done():
                record.task.cancel()
            record.status = RunStatus.interrupted
            record.updated_at = _now_iso()
        await self._persist_status(record, RunStatus.interrupted)
        logger.info("Run %s cancelled (action=%s)", run_id, action)
        return True

    async def create_or_reject(
        self,
        thread_id: str,
        assistant_id: str | None = None,
        *,
        on_disconnect: DisconnectMode = DisconnectMode.cancel,
        metadata: dict | None = None,
        kwargs: dict | None = None,
        multitask_strategy: str = "reject",
        model_name: str | None = None,
        user_id: str | None = None,
    ) -> RunRecord:
        """原子地检查 inflight run 并创建新的。

        ``reject`` 策略：thread 已有 pending/running run 时抛 ``ConflictError``。
        ``interrupt`` / ``rollback``：创建前先取消 inflight run。

        本方法**跨检查与插入持锁**，消除「分开 ``has_inflight`` + ``create``」的 TOCTOU 竞态。
        """
        run_id = str(uuid.uuid4())
        now = _now_iso()

        _supported_strategies = ("reject", "interrupt", "rollback")
        interrupted_records: list[RunRecord] = []

        async with self._lock:
            if multitask_strategy not in _supported_strategies:
                raise UnsupportedStrategyError(f"Multitask strategy '{multitask_strategy}' is not yet supported. Supported strategies: {', '.join(_supported_strategies)}")

            inflight = [r for r in self._thread_records_locked(thread_id) if r.status in (RunStatus.pending, RunStatus.running)]

            if multitask_strategy == "reject" and inflight:
                raise ConflictError(f"Thread {thread_id} already has an active run")

            if multitask_strategy in ("interrupt", "rollback") and inflight:
                logger.info(
                    "Preparing to cancel %d inflight run(s) on thread %s (strategy=%s)",
                    len(inflight),
                    thread_id,
                    multitask_strategy,
                )

            record = RunRecord(
                run_id=run_id,
                thread_id=thread_id,
                assistant_id=assistant_id,
                status=RunStatus.pending,
                on_disconnect=on_disconnect,
                multitask_strategy=multitask_strategy,
                metadata=metadata or {},
                kwargs=kwargs or {},
                user_id=user_id,
                created_at=now,
                updated_at=now,
                model_name=model_name,
            )
            self._runs[run_id] = record
            self._index_run_locked(record)
            persisted = False
            try:
                await self._persist_new_run_to_store(record)
                persisted = True
            except Exception:
                logger.warning("Failed to persist run %s; rolled back in-memory record", run_id, exc_info=True)
                raise
            finally:
                # 也覆盖 cancellation——它绕过 ``except Exception``。
                if not persisted:
                    self._runs.pop(run_id, None)
                    self._unindex_run_locked(run_id, record.thread_id)

            if multitask_strategy in ("interrupt", "rollback") and inflight:
                for r in inflight:
                    r.abort_action = multitask_strategy
                    r.abort_event.set()
                    if r.task is not None and not r.task.done():
                        r.task.cancel()
                    r.status = RunStatus.interrupted
                    r.updated_at = now
                    interrupted_records.append(r)

        for interrupted_record in interrupted_records:
            await self._persist_status(interrupted_record, RunStatus.interrupted)
        logger.info("Run created: run_id=%s thread_id=%s", run_id, thread_id)
        return record

    async def reconcile_orphaned_inflight_runs(
        self,
        *,
        error: str,
        before: str | None = None,
    ) -> list[RunRecord]:
        """把「持久化了但本 worker 无 task 的」活跃 run 标 error。

        Gateway run 是进程局部的：asyncio task 和 abort event 在内存里，run 行却是持久的。
        SQLite 后端的 Gateway 重启后，任何启动前创建的持久化 ``pending`` / ``running`` 行都不可能
        还有本地 worker。这一步把那个暧昧状态变成明确的 error，而不是让 UI 显示一个永久的活跃 run
        （红线 #7）。
        """
        if self._store is None:
            return []
        try:
            rows = await self._call_store_with_retry(
                "list_inflight",
                "*",
                lambda: self._store.list_inflight(before=before),
            )
        except Exception:
            logger.warning("Failed to list orphaned inflight runs for reconciliation", exc_info=True)
            return []

        recovered: list[RunRecord] = []
        now = _now_iso()
        for row in rows:
            try:
                record = self._record_from_store(row)
            except Exception:
                logger.warning("Failed to map orphaned run row during reconciliation", exc_info=True)
                continue

            async with self._lock:
                live_record = self._runs.get(record.run_id)
                if live_record is not None and live_record.status in (RunStatus.pending, RunStatus.running):
                    continue

            record.status = RunStatus.error
            record.error = error
            record.updated_at = now
            persisted = await self._persist_status(record, RunStatus.error, error=error)
            if not persisted:
                logger.warning("Skipped orphaned run %s recovery because error status was not persisted", record.run_id)
                continue
            recovered.append(record)

        if recovered:
            logger.warning("Recovered %d orphaned inflight run(s) as error", len(recovered))
        return recovered

    async def has_inflight(self, thread_id: str) -> bool:
        """``True`` 若 *thread_id* 有 pending 或 running 的 run。"""
        async with self._lock:
            return any(r.status in (RunStatus.pending, RunStatus.running) for r in self._thread_records_locked(thread_id))

    async def cleanup(self, run_id: str, *, delay: float = 300) -> None:
        """延迟后移除一个 run record。"""
        if delay > 0:
            await asyncio.sleep(delay)
        async with self._lock:
            record = self._runs.pop(run_id, None)
            if record is not None:
                self._unindex_run_locked(run_id, record.thread_id)
        logger.debug("Run record %s cleaned up", run_id)

    async def shutdown(self, *, timeout: float = 5.0) -> None:
        """进程关闭时取消并有界 await 所有在途 run（红线 #6 / #3373）。

        chat run 在 fire-and-forget 后台 ``asyncio.Task`` 里跑，经共享 checkpointer 写 checkpoint。
        关闭时 checkpointer 的资源（如 gateway ``AsyncExitStack`` 拥有的 postgres 连接池）被拆；
        若此时还有 run task 在图执行中途，langgraph 的
        ``AsyncPregelLoop._checkpointer_put_after_previous`` 会在已关的池上跑它的
        ``finally: await checkpointer.aput(...)``。因为那个 put 跑在 langgraph 内部 task 里（不在
        ``run_agent`` 调用栈上），导致的 ``psycopg_pool.PoolClosed`` worker 捕获不到，会在
        ``asyncio.run()`` 关闭时作为未处理异常冒出来（#3373）。

        **在关 checkpointer 前 drain 在途 run**，让每个能在 ``timeout`` 内 settle 的 run 趁资源还开着
        flush 最终 checkpoint。只有没自行 settle 的 run 才标 ``interrupted``——drain 期间正常完成
        （如 ``success``）的 run 保留真实终态，不被一刀覆盖。整个 drain（含尾部状态持久化）都被
        ``timeout`` 卡住，所以卡在 cleanup 的 run（或 DB 压力下慢的 store）拖不死 worker 关闭——
        那是 ``app.gateway.app._SHUTDOWN_HOOK_TIMEOUT_SECONDS`` 守的信号重入死锁的前提。超时后仍活跃
        的 run 只记日志，可能仍与 teardown 竞态。
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout

        async with self._lock:
            inflight = [record for record in self._runs.values() if record.status in (RunStatus.pending, RunStatus.running) and record.task is not None and not record.task.done()]
            for record in inflight:
                record.abort_action = "interrupt"
                record.abort_event.set()
                record.task.cancel()  # type: ignore[union-attr]  # filtered above
                # 状态在 drain 之后（见下）决定，不在这里：drain 期间自行完成的 run 必须保留真实状态。

        if not inflight:
            return

        tasks = [record.task for record in inflight]
        _, pending = await asyncio.wait(tasks, timeout=timeout)

        # 只对没自行 settle（仍 pending 或 ended cancelled）的 run 标/持久化 ``interrupted``。
        # drain 期间正常完成的 run 保留它自己设的状态。
        to_persist: list[RunRecord] = []
        async with self._lock:
            for record in inflight:
                task = record.task
                if task not in pending and not task.cancelled():
                    # 自行完成——取出冒上来的异常免得被报「never retrieved」，并保留其状态。
                    task.exception()  # type: ignore[union-attr]  # done & not cancelled
                    continue
                if record.status in (RunStatus.pending, RunStatus.running):
                    record.status = RunStatus.interrupted
                    record.updated_at = _now_iso()
                to_persist.append(record)

        # 尾部状态持久化卡在剩余预算内，防慢 store（``_call_store_with_retry`` 在 DB 压力下会退避）
        # 把 shutdown 推过 ``timeout``。
        if to_persist:
            remaining = deadline - loop.time()
            if remaining <= 0:
                logger.warning("Run drain budget exhausted before persisting %d interrupted run(s) on shutdown", len(to_persist))
            else:
                try:
                    results = await asyncio.wait_for(
                        asyncio.gather(*(self._persist_status(record, RunStatus.interrupted) for record in to_persist), return_exceptions=True),
                        timeout=remaining,
                    )
                except TimeoutError:
                    logger.warning("Run drain status persistence exceeded the %.1fs budget; %d record(s) may not be persisted", timeout, len(to_persist))
                else:
                    # ``_persist_status`` 是 best-effort：它自己捕获并记日志失败，返回 ``False``。
                    # 检查聚合结果让部分失败在 shutdown 层面（带 run_id）浮现，而非被 gather 静默吞。
                    for record, result in zip(to_persist, results):
                        if isinstance(result, Exception):
                            logger.warning("Unexpected error persisting interrupted status for run %s during shutdown: %r", record.run_id, result)
                        elif result is False:
                            logger.warning("Could not persist interrupted status for run %s during shutdown", record.run_id)

        if pending:
            logger.warning("Run drain exceeded %.1fs on shutdown; %d run task(s) still active and may race checkpointer teardown", timeout, len(pending))
        logger.info("Drained %d in-flight run(s) on shutdown (%d settled within %.1fs)", len(inflight), len(inflight) - len(pending), timeout)


class ConflictError(Exception):
    """``multitask_strategy=reject`` 且 thread 已有 inflight run 时抛。"""


class UnsupportedStrategyError(Exception):
    """``multitask_strategy`` 值尚未实现时抛。"""
