"""基于进程内事件日志的内存流桥。

每个 run 保留一个**有界**时间窗口的事件，让迟到的订阅者与重连的客户端能从
``Last-Event-ID`` 回放缓冲里的事件。

红线 #11：``queue_maxsize``（默认 256）的有界窗口 + 淘汰最旧（eviction）+ ``start_offset``，
保证长 run 不爆内存。

心跳：订阅者在 ``heartbeat_interval`` 秒内没收到事件时收到 :data:`HEARTBEAT_SENTINEL`，
防止反向代理（nginx 等）因「长时间无数据」掐断空闲 SSE 连接。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from deerflow.runtime.stream_bridge.base import END_SENTINEL, HEARTBEAT_SENTINEL, StreamBridge, StreamEvent

logger = logging.getLogger(__name__)


@dataclass
class _RunStream:
    events: list[StreamEvent] = field(default_factory=list)
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    ended: bool = False
    start_offset: int = 0


class MemoryStreamBridge(StreamBridge):
    """每 run 一个的内存事件日志实现。

    事件按 run 保留一个有界时间窗口，让迟到订阅者与重连客户端能从 ``Last-Event-ID``
    回放缓冲里的事件。
    """

    def __init__(self, *, queue_maxsize: int = 256) -> None:
        self._maxsize = queue_maxsize
        self._streams: dict[str, _RunStream] = {}
        self._counters: dict[str, int] = {}

    # -- helpers ---------------------------------------------------------------

    def _get_or_create_stream(self, run_id: str) -> _RunStream:
        if run_id not in self._streams:
            self._streams[run_id] = _RunStream()
            self._counters[run_id] = 0
        return self._streams[run_id]

    def _next_id(self, run_id: str) -> str:
        self._counters[run_id] = self._counters.get(run_id, 0) + 1
        ts = int(time.time() * 1000)
        seq = self._counters[run_id] - 1
        return f"{ts}-{seq}"

    @staticmethod
    def _parse_event_seq(event_id: str) -> int | None:
        """从 ``{ts}-{seq}`` 格式的事件 id 解析 per-run 序号。

        ``seq``（由 :meth:`_next_id` 分配）每发一个事件 +1，等于该事件在 run 内的绝对 offset。
        格式不符返回 ``None``。
        """
        _, sep, seq_text = event_id.rpartition("-")
        if not sep:
            return None
        try:
            return int(seq_text)
        except ValueError:
            return None

    def _resolve_start_offset(self, stream: _RunStream, last_event_id: str | None) -> int:
        if last_event_id is None:
            return stream.start_offset

        # #3700：事件 id 内嵌 per-run 单调递增的 ``seq``，等于该事件的绝对 offset，所以用算术
        # O(1) 定位事件，而非线性扫保留缓冲。仍在算出的 index 处核验 id，所以过期/被淘汰/外来/
        # 畸形 id 仍回退到「从最早保留事件回放」——与旧的线性扫行为完全一致。
        seq = self._parse_event_seq(last_event_id)
        if seq is not None:
            local_index = seq - stream.start_offset
            if 0 <= local_index < len(stream.events) and stream.events[local_index].id == last_event_id:
                return stream.start_offset + local_index + 1

        if stream.events:
            logger.warning(
                "last_event_id=%s not found in retained buffer; replaying from earliest retained event",
                last_event_id,
            )
        return stream.start_offset

    # -- StreamBridge API ------------------------------------------------------

    async def publish(self, run_id: str, event: str, data: Any) -> None:
        stream = self._get_or_create_stream(run_id)
        entry = StreamEvent(id=self._next_id(run_id), event=event, data=data)
        async with stream.condition:
            stream.events.append(entry)
            if len(stream.events) > self._maxsize:
                overflow = len(stream.events) - self._maxsize
                del stream.events[:overflow]
                stream.start_offset += overflow
            stream.condition.notify_all()

    async def publish_end(self, run_id: str) -> None:
        stream = self._get_or_create_stream(run_id)
        async with stream.condition:
            stream.ended = True
            stream.condition.notify_all()

    async def subscribe(
        self,
        run_id: str,
        *,
        last_event_id: str | None = None,
        heartbeat_interval: float = 15.0,
    ) -> AsyncIterator[StreamEvent]:
        stream = self._get_or_create_stream(run_id)
        async with stream.condition:
            next_offset = self._resolve_start_offset(stream, last_event_id)

        while True:
            async with stream.condition:
                if next_offset < stream.start_offset:
                    logger.warning(
                        "subscriber for run %s fell behind retained buffer; resuming from offset %s",
                        run_id,
                        stream.start_offset,
                    )
                    next_offset = stream.start_offset

                local_index = next_offset - stream.start_offset
                if 0 <= local_index < len(stream.events):
                    entry = stream.events[local_index]
                    next_offset += 1
                elif stream.ended:
                    entry = END_SENTINEL
                else:
                    try:
                        await asyncio.wait_for(stream.condition.wait(), timeout=heartbeat_interval)
                    except TimeoutError:
                        entry = HEARTBEAT_SENTINEL
                    else:
                        continue

            if entry is END_SENTINEL:
                yield END_SENTINEL
                return
            yield entry

    async def cleanup(self, run_id: str, *, delay: float = 0) -> None:
        if delay > 0:
            await asyncio.sleep(delay)
        self._streams.pop(run_id, None)
        self._counters.pop(run_id, None)

    async def close(self) -> None:
        self._streams.clear()
        self._counters.clear()
