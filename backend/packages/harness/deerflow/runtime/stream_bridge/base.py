"""流桥抽象协议。

StreamBridge 把 agent worker（生产者）与 SSE 端点（消费者）解耦，对齐 LangGraph
Platform 的 Queue + StreamManager 架构。

为什么要解耦：worker 在后台跑一个长任务（可能几分钟），SSE 连接是前端的一条
HTTP 流。如果两者直连，前端一断连，worker 就得跟着取消；多个客户端（或同一客户端
重连）想看同一次 run 的事件，直连做不到。流桥做「中转 + 缓冲 + 重连补播」。
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class StreamEvent:
    """单条流事件。

    Attributes:
        id: 单调递增的事件 id（用作 SSE ``id:`` 字段，支持 ``Last-Event-ID`` 重连）。
        event: SSE 事件名，如 ``"metadata"`` / ``"updates"`` / ``"events"`` /
            ``"error"`` / ``"end"``。
        data: JSON 可序列化的 payload。
    """

    id: str
    event: str
    data: Any


HEARTBEAT_SENTINEL = StreamEvent(id="", event="__heartbeat__", data=None)
END_SENTINEL = StreamEvent(id="", event="__end__", data=None)


class StreamBridge(abc.ABC):
    """流桥抽象基类。"""

    @abc.abstractmethod
    async def publish(self, run_id: str, event: str, data: Any) -> None:
        """为 *run_id* 入队一条事件（生产者侧）。"""

    @abc.abstractmethod
    async def publish_end(self, run_id: str) -> None:
        """示意 *run_id* 不会再产生新事件。"""

    @abc.abstractmethod
    def subscribe(
        self,
        run_id: str,
        *,
        last_event_id: str | None = None,
        heartbeat_interval: float = 15.0,
    ) -> AsyncIterator[StreamEvent]:
        """yield *run_id* 事件的异步迭代器（消费者侧）。

        *heartbeat_interval* 秒内没有事件时 yield :data:`HEARTBEAT_SENTINEL`。
        生产者调用 :meth:`publish_end` 后 yield 一次 :data:`END_SENTINEL`。
        """

    @abc.abstractmethod
    async def cleanup(self, run_id: str, *, delay: float = 0) -> None:
        """释放 *run_id* 关联的资源。

        *delay* > 0 时实现应先等再释放，给迟到的订阅者一个排空剩余事件的机会。
        """

    async def close(self) -> None:
        """释放后端资源。默认 no-op。"""
