"""内存 RunEventStore。``run_events.backend=memory``（默认）与测试时用。

单进程 async 下线程安全（所有变更都在同一个事件循环里，无需 threading 锁）。

性能优化：除了 ``_events``（全量），还维护 ``_messages``（仅 message 的投影，
同样的 dict 对象、无拷贝，按 seq 排序），让消息分页用 bisect 做 O(log m + page)，
而不是每次请求重扫所有事件。

进一步优化（#3686）：再维护按 ``run_id`` 分桶的两组投影 ``_events_by_run`` /
``_messages_by_run``（同样是原始 dict 对象、无拷贝，按 seq 排序）。这样单次 run
维度的读（``list_events`` / ``list_messages_by_run``）只触碰该 run 的事件
（O(该 run 的事件数)），而不是每次都重扫整个 thread 的事件日志（O(该 thread 的
事件数)）——一个 thread 里可能累积成百上千个 run 的事件，但单次请求往往只关心
其中一个 run。这是 thread 级 ``_messages`` 投影在 run 维度的对应物。
"""

from __future__ import annotations

import bisect
from datetime import UTC, datetime

from deerflow.runtime.events.store.base import RunEventStore


class MemoryRunEventStore(RunEventStore):
    def __init__(self) -> None:
        self._events: dict[str, list[dict]] = {}  # thread_id -> 按 seq 排序的事件列表
        # ``_events`` 的 message-only 投影（同一个 dict 对象，无拷贝），按 seq 排序，
        # 让消息分页用 bisect 做 O(log m + page)，而非每次重扫所有事件。
        self._messages: dict[str, list[dict]] = {}  # thread_id -> 按 seq 排序的消息列表
        # 上面两个列表的 run 分桶投影（同一个 dict 对象、无拷贝），按 seq 排序。
        # 单次 run 维度的读（``list_events`` / ``list_messages_by_run``）因此只花
        # O(该 run 的事件数)，而非 O(该 thread 的事件数)：没有这两组投影的话，这两
        # 个读即使一个 run 只握着寥寥几条事件，也会在每次请求时重扫整个 thread 的
        # 事件日志。这是 thread 级 ``_messages`` 投影在 run 维度的对应物（#3686）。
        self._events_by_run: dict[str, dict[str, list[dict]]] = {}  # thread_id -> run_id -> 按 seq 排序的事件
        self._messages_by_run: dict[str, dict[str, list[dict]]] = {}  # thread_id -> run_id -> 按 seq 排序的消息
        self._seq_counters: dict[str, int] = {}  # thread_id -> 上次分配的 seq

    def _next_seq(self, thread_id: str) -> int:
        current = self._seq_counters.get(thread_id, 0)
        next_val = current + 1
        self._seq_counters[thread_id] = next_val
        return next_val

    def _put_one(
        self,
        *,
        thread_id: str,
        run_id: str,
        event_type: str,
        category: str,
        content: str | dict = "",
        metadata: dict | None = None,
        created_at: str | None = None,
    ) -> dict:
        seq = self._next_seq(thread_id)
        record = {
            "thread_id": thread_id,
            "run_id": run_id,
            "event_type": event_type,
            "category": category,
            "content": content,
            "metadata": metadata or {},
            "seq": seq,
            "created_at": created_at or datetime.now(UTC).isoformat(),
        }
        self._events.setdefault(thread_id, []).append(record)
        self._events_by_run.setdefault(thread_id, {}).setdefault(run_id, []).append(record)
        if category == "message":
            self._messages.setdefault(thread_id, []).append(record)
            self._messages_by_run.setdefault(thread_id, {}).setdefault(run_id, []).append(record)
        return record

    async def put(
        self,
        *,
        thread_id,
        run_id,
        event_type,
        category,
        content="",
        metadata=None,
        created_at=None,
    ):
        return self._put_one(
            thread_id=thread_id,
            run_id=run_id,
            event_type=event_type,
            category=category,
            content=content,
            metadata=metadata,
            created_at=created_at,
        )

    async def put_batch(self, events):
        results = []
        for ev in events:
            record = self._put_one(**ev)
            results.append(record)
        return results

    async def list_messages(self, thread_id, *, limit=50, before_seq=None, after_seq=None):
        # ``messages`` 是 message-only 且按 seq 排序的，所以 seq 窗口是一段连续切片，
        # 用 bisect 定位（O(log m)），而非全扫。
        messages = self._messages.get(thread_id, [])

        if before_seq is not None:
            # seq < before_seq 的记录，取其中最后 ``limit`` 条。
            hi = bisect.bisect_left(messages, before_seq, key=lambda e: e["seq"])
            return messages[max(0, hi - limit) : hi]
        elif after_seq is not None:
            # seq > after_seq 的记录，取其中前 ``limit`` 条。
            lo = bisect.bisect_right(messages, after_seq, key=lambda e: e["seq"])
            return messages[lo : lo + limit]
        else:
            # 返回最近 ``limit`` 条，升序。
            return messages[-limit:]

    async def list_events(self, thread_id, run_id, *, event_types=None, limit=500):
        # ``_events_by_run`` 已经按 run 分桶且按 seq 排序，所以只触碰该 run 的事件，
        # 而不是扫整个 thread。
        run_events = self._events_by_run.get(thread_id, {}).get(run_id, [])
        if event_types is not None:
            run_events = [e for e in run_events if e["event_type"] in event_types]
        return run_events[:limit]

    async def list_messages_by_run(self, thread_id, run_id, *, limit=50, before_seq=None, after_seq=None):
        # 单 run、仅 message、按 seq 排序：seq 窗口是一段连续切片，用 bisect
        # （O(log m_run)）只在该 run 的消息上定位，而非重扫整个 thread 的事件日志。
        messages = self._messages_by_run.get(thread_id, {}).get(run_id, [])
        lo = 0 if after_seq is None else bisect.bisect_right(messages, after_seq, key=lambda e: e["seq"])
        hi = len(messages) if before_seq is None else bisect.bisect_left(messages, before_seq, key=lambda e: e["seq"])
        window = messages[lo:hi]
        # ``after_seq`` 游标向前翻页（取前 ``limit`` 条）；否则取最后 ``limit`` 条
        # （最新一页，或恰好结束在 ``before_seq`` 之前的那一页）。与旧的过滤式语义一致。
        if after_seq is not None:
            return window[:limit]
        return window[-limit:]

    async def count_messages(self, thread_id):
        return len(self._messages.get(thread_id, []))

    async def delete_by_thread(self, thread_id):
        events = self._events.pop(thread_id, [])
        self._messages.pop(thread_id, None)
        self._events_by_run.pop(thread_id, None)
        self._messages_by_run.pop(thread_id, None)
        self._seq_counters.pop(thread_id, None)
        return len(events)

    async def delete_by_run(self, thread_id, run_id):
        all_events = self._events.get(thread_id, [])
        if not all_events:
            return 0
        remaining = [e for e in all_events if e["run_id"] != run_id]
        removed = len(all_events) - len(remaining)
        self._events[thread_id] = remaining
        # message 投影与存活对象保持同步（同一个存活的 dict 对象）。
        self._messages[thread_id] = [e for e in remaining if e["category"] == "message"]
        # 从 run 分桶投影里删掉被删除的 run（#3686）。
        self._events_by_run.get(thread_id, {}).pop(run_id, None)
        self._messages_by_run.get(thread_id, {}).pop(run_id, None)
        return removed
