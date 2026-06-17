"""内存 RunEventStore。``run_events.backend=memory``（默认）与测试时用。

单进程 async 下线程安全（所有变更都在同一个事件循环里，无需 threading 锁）。

性能优化：除了 ``_events``（全量），还维护 ``_messages``（仅 message 的投影，
同样的 dict 对象、无拷贝，按 seq 排序），让消息分页用 bisect 做 O(log m + page)，
而不是每次请求重扫所有事件。
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
        if category == "message":
            self._messages.setdefault(thread_id, []).append(record)
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
        all_events = self._events.get(thread_id, [])
        filtered = [e for e in all_events if e["run_id"] == run_id]
        if event_types is not None:
            filtered = [e for e in filtered if e["event_type"] in event_types]
        return filtered[:limit]

    async def list_messages_by_run(self, thread_id, run_id, *, limit=50, before_seq=None, after_seq=None):
        all_events = self._events.get(thread_id, [])
        filtered = [e for e in all_events if e["run_id"] == run_id and e["category"] == "message"]
        if before_seq is not None:
            filtered = [e for e in filtered if e["seq"] < before_seq]
        if after_seq is not None:
            filtered = [e for e in filtered if e["seq"] > after_seq]
        if after_seq is not None:
            return filtered[:limit]
        else:
            return filtered[-limit:] if len(filtered) > limit else filtered

    async def count_messages(self, thread_id):
        return len(self._messages.get(thread_id, []))

    async def delete_by_thread(self, thread_id):
        events = self._events.pop(thread_id, [])
        self._messages.pop(thread_id, None)
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
        return removed
