"""流桥（stream bridge）配置。

流桥把 agent worker 与 SSE 端点解耦：生产者把事件投进桥，消费者（SSE 连接）
从桥读，支持有界缓冲 + 重连补播。``None`` 表示用内存默认（memory + 默认队列大小）。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

StreamBridgeType = Literal["memory", "redis"]


class StreamBridgeConfig(BaseModel):
    """连接 agent worker 与 SSE 端点的流桥配置。"""

    type: StreamBridgeType = Field(
        default="memory",
        description="流桥后端类型。'memory' 用进程内 asyncio.Queue（仅单进程）；'redis' 用 Redis Streams（规划中，尚未实现）。",
    )
    redis_url: str | None = Field(
        default=None,
        description="redis 流桥类型的 Redis URL。例：'redis://localhost:6379/0'。",
    )
    queue_maxsize: int = Field(
        default=256,
        description="memory 流桥里每个 run 缓冲的最大事件数（有界窗口，超限淘汰最旧）。",
    )
