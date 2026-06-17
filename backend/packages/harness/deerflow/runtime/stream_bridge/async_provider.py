"""异步流桥工厂。

提供 **异步 context manager**，对齐
:func:`deerflow.runtime.checkpointer.async_provider.make_checkpointer`。

用法（如 FastAPI lifespan）::

    from deerflow.runtime.stream_bridge import make_stream_bridge

    async with make_stream_bridge() as bridge:
        app.state.stream_bridge = bridge
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator

from deerflow.config.app_config import AppConfig, get_app_config
from deerflow.runtime.stream_bridge.base import StreamBridge

logger = logging.getLogger(__name__)


@contextlib.asynccontextmanager
async def make_stream_bridge(app_config: AppConfig | None = None) -> AsyncIterator[StreamBridge]:
    """yield 一个 :class:`StreamBridge` 的异步 context manager。

    无配置（``stream_bridge`` 段为 None）或 type=memory 时回退
    :class:`MemoryStreamBridge`。
    """
    if app_config is None:
        config = get_app_config().stream_bridge
    else:
        config = app_config.stream_bridge

    if config is None or config.type == "memory":
        from deerflow.runtime.stream_bridge.memory import MemoryStreamBridge

        maxsize = config.queue_maxsize if config is not None else 256
        bridge = MemoryStreamBridge(queue_maxsize=maxsize)
        logger.info("Stream bridge initialised: memory (queue_maxsize=%d)", maxsize)
        try:
            yield bridge
        finally:
            await bridge.close()
        return

    if config.type == "redis":
        raise NotImplementedError("Redis stream bridge planned for a later phase")

    raise ValueError(f"Unknown stream bridge type: {config.type!r}")
