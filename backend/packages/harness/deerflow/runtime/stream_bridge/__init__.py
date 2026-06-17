"""流桥 —— 把 agent worker 与 SSE 端点解耦。

``StreamBridge`` 夹在跑 agent 的后台任务（生产者）与向客户端推 Server-Sent Events
的 HTTP 端点（消费者）之间。本包提供抽象协议（:class:`StreamBridge`）+ 默认的内存实现
（:class:`MemoryStreamBridge`，基于每 run 的有界事件日志 + ``asyncio.Condition``）。
"""

from deerflow.runtime.stream_bridge.async_provider import make_stream_bridge
from deerflow.runtime.stream_bridge.base import END_SENTINEL, HEARTBEAT_SENTINEL, StreamBridge, StreamEvent
from deerflow.runtime.stream_bridge.memory import MemoryStreamBridge

__all__ = [
    "END_SENTINEL",
    "HEARTBEAT_SENTINEL",
    "MemoryStreamBridge",
    "StreamBridge",
    "StreamEvent",
    "make_stream_bridge",
]
