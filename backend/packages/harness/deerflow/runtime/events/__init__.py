"""run 事件存储运行时入口。

工厂 :func:`make_run_event_store` 按 ``run_events.backend`` 配置挑实现：

- ``memory``（默认）→ :class:`MemoryRunEventStore`。
- ``db`` → :class:`DbRunEventStore`（需 persistence engine 已初始化；若
  ``database.backend=memory`` 则 session factory 为 None，回退内存实现）。
- ``jsonl`` → :class:`JsonlRunEventStore`。
"""

from deerflow.runtime.events.store.base import RunEventStore
from deerflow.runtime.events.store.memory import MemoryRunEventStore


def make_run_event_store(config=None) -> RunEventStore:
    """按 run_events.backend 配置创建 RunEventStore。"""
    if config is None or config.backend == "memory":
        return MemoryRunEventStore()
    if config.backend == "db":
        from deerflow.persistence.engine import get_session_factory

        sf = get_session_factory()
        if sf is None:
            # database.backend=memory 但 run_events.backend=db → 回退
            return MemoryRunEventStore()
        from deerflow.runtime.events.store.db import DbRunEventStore

        return DbRunEventStore(sf, max_trace_content=config.max_trace_content)
    if config.backend == "jsonl":
        from deerflow.runtime.events.store.jsonl import JsonlRunEventStore

        return JsonlRunEventStore()
    raise ValueError(f"Unknown run_events backend: {config.backend!r}")


__all__ = ["MemoryRunEventStore", "RunEventStore", "make_run_event_store"]
