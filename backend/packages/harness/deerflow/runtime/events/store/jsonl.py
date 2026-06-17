"""JSONL 文件后端的 RunEventStore。

每个 run 的事件存在单个文件：``.deer-flow/threads/{thread_id}/runs/{run_id}.jsonl``

所有 category（message / trace / lifecycle）在同一文件。适合轻量单节点部署。

**单进程保证**：内存 seq 计数器是进程内的。多进程共用同一目录会产生重复或非单调
seq——那种场景用 :class:`DbRunEventStore`。

文件 IO 全部经 ``asyncio.to_thread`` 卸载到线程池，不阻塞事件循环（红线 #1）。
每线程一个 ``asyncio.Lock`` 串行化单进程内的写，防 JSONL 行交错（红线 #3）。

已知权衡：``list_messages()`` 要扫一个 thread 的所有 run 文件（多个 run 的消息需
统一 seq 排序）；``list_events()`` 只读一个文件——快路径。

路径穿越防御（红线 #4）：``thread_id`` / ``run_id`` 必须匹配 ``[A-Za-z0-9_-]+``，
否则拒绝，防止 ``../`` 之类逃出 base_dir。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path

from deerflow.runtime.events.store.base import RunEventStore

logger = logging.getLogger(__name__)

_SAFE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_\-]+$")


class JsonlRunEventStore(RunEventStore):
    def __init__(self, base_dir: str | Path | None = None):
        self._base_dir = Path(base_dir) if base_dir else Path(".deer-flow")
        self._seq_counters: dict[str, int] = {}  # thread_id -> 当前最大 seq
        # 每线程一个 asyncio.Lock——串行化单进程内的并发写。
        self._write_locks: dict[str, asyncio.Lock] = {}

    def _get_write_lock(self, thread_id: str) -> asyncio.Lock:
        return self._write_locks.setdefault(thread_id, asyncio.Lock())

    @staticmethod
    def _validate_id(value: str, label: str) -> str:
        """校验一个 ID 是否可安全用于文件系统路径。"""
        if not value or not _SAFE_ID_PATTERN.match(value):
            raise ValueError(f"Invalid {label}: must be alphanumeric/dash/underscore, got {value!r}")
        return value

    def _thread_dir(self, thread_id: str) -> Path:
        self._validate_id(thread_id, "thread_id")
        return self._base_dir / "threads" / thread_id / "runs"

    def _run_file(self, thread_id: str, run_id: str) -> Path:
        self._validate_id(run_id, "run_id")
        return self._thread_dir(thread_id) / f"{run_id}.jsonl"

    def _next_seq(self, thread_id: str) -> int:
        self._seq_counters[thread_id] = self._seq_counters.get(thread_id, 0) + 1
        return self._seq_counters[thread_id]

    def _compute_max_seq(self, thread_id: str) -> int:
        """扫一个 thread 的所有 run 文件，返回当前最大 seq（阻塞 IO）。"""
        max_seq = 0
        thread_dir = self._thread_dir(thread_id)
        if thread_dir.exists():
            for f in thread_dir.glob("*.jsonl"):
                for line in f.read_text(encoding="utf-8").strip().splitlines():
                    try:
                        record = json.loads(line)
                        max_seq = max(max_seq, record.get("seq", 0))
                    except json.JSONDecodeError:
                        logger.debug("Skipping malformed JSONL line in %s", f)
        return max_seq

    async def _ensure_seq_loaded(self, thread_id: str) -> None:
        """把现有文件的最大 seq 载入内存计数器（非阻塞）。"""
        if thread_id in self._seq_counters:
            return
        max_seq = await asyncio.to_thread(self._compute_max_seq, thread_id)
        self._seq_counters[thread_id] = max_seq

    def _write_record(self, record: dict) -> None:
        path = self._run_file(record["thread_id"], record["run_id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str, ensure_ascii=False) + "\n")

    def _read_thread_events(self, thread_id: str) -> list[dict]:
        """读一个 thread 的全部事件，按 seq 排序（阻塞 IO）。"""
        events = []
        thread_dir = self._thread_dir(thread_id)
        if not thread_dir.exists():
            return events
        for f in sorted(thread_dir.glob("*.jsonl")):
            for line in f.read_text(encoding="utf-8").strip().splitlines():
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    logger.debug("Skipping malformed JSONL line in %s", f)
        events.sort(key=lambda e: e.get("seq", 0))
        return events

    def _read_run_events(self, thread_id: str, run_id: str) -> list[dict]:
        """读某个 run 文件的事件（阻塞 IO）。"""
        path = self._run_file(thread_id, run_id)
        if not path.exists():
            return []
        events = []
        for line in path.read_text(encoding="utf-8").strip().splitlines():
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                logger.debug("Skipping malformed JSONL line in %s", path)
        events.sort(key=lambda e: e.get("seq", 0))
        return events

    def _delete_thread_files(self, thread_id: str) -> None:
        thread_dir = self._thread_dir(thread_id)
        if thread_dir.exists():
            for f in thread_dir.glob("*.jsonl"):
                f.unlink()

    def _delete_run_file(self, thread_id: str, run_id: str) -> None:
        path = self._run_file(thread_id, run_id)
        if path.exists():
            path.unlink()

    async def put(self, *, thread_id, run_id, event_type, category, content="", metadata=None, created_at=None):
        async with self._get_write_lock(thread_id):
            await self._ensure_seq_loaded(thread_id)
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
            await asyncio.to_thread(self._write_record, record)
            return record

    async def put_batch(self, events):
        if not events:
            return []
        results = []
        for ev in events:
            record = await self.put(**ev)
            results.append(record)
        return results

    async def list_messages(self, thread_id, *, limit=50, before_seq=None, after_seq=None):
        all_events = await asyncio.to_thread(self._read_thread_events, thread_id)
        messages = [e for e in all_events if e.get("category") == "message"]

        if before_seq is not None:
            messages = [e for e in messages if e["seq"] < before_seq]
            return messages[-limit:]
        elif after_seq is not None:
            messages = [e for e in messages if e["seq"] > after_seq]
            return messages[:limit]
        else:
            return messages[-limit:]

    async def list_events(self, thread_id, run_id, *, event_types=None, limit=500):
        events = await asyncio.to_thread(self._read_run_events, thread_id, run_id)
        if event_types is not None:
            events = [e for e in events if e.get("event_type") in event_types]
        return events[:limit]

    async def list_messages_by_run(self, thread_id, run_id, *, limit=50, before_seq=None, after_seq=None):
        events = await asyncio.to_thread(self._read_run_events, thread_id, run_id)
        filtered = [e for e in events if e.get("category") == "message"]
        if before_seq is not None:
            filtered = [e for e in filtered if e.get("seq", 0) < before_seq]
        if after_seq is not None:
            filtered = [e for e in filtered if e.get("seq", 0) > after_seq]
        if after_seq is not None:
            return filtered[:limit]
        else:
            return filtered[-limit:] if len(filtered) > limit else filtered

    async def count_messages(self, thread_id):
        all_events = await asyncio.to_thread(self._read_thread_events, thread_id)
        return sum(1 for e in all_events if e.get("category") == "message")

    async def delete_by_thread(self, thread_id):
        async with self._get_write_lock(thread_id):
            all_events = await asyncio.to_thread(self._read_thread_events, thread_id)
            count = len(all_events)
            await asyncio.to_thread(self._delete_thread_files, thread_id)
            self._seq_counters.pop(thread_id, None)
            # 在持锁范围内 pop 锁，缩小「新调用方拿到新锁、而等待协程仍持有旧锁」的窗口。
            # 注意：delete 前已获取该锁引用的协程，在释放后仍会继续——这是一个可接受的窄竞态。
            self._write_locks.pop(thread_id, None)
            return count

    async def delete_by_run(self, thread_id, run_id):
        async with self._get_write_lock(thread_id):
            events = await asyncio.to_thread(self._read_run_events, thread_id, run_id)
            count = len(events)
            await asyncio.to_thread(self._delete_run_file, thread_id, run_id)
            return count
