"""线程元数据持久化 —— ORM 模型、抽象存储与具体实现。

工厂 :func:`make_thread_store` 按可用后端挑实现：

- 有 session_factory（SQL）→ :class:`ThreadMetaRepository`。
- 无 session_factory（memory）但有 LangGraph Store → :class:`MemoryThreadMetaStore`。
- 两者都没有 → 抛错（调用方必须至少提供其一）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from deerflow.persistence.thread_meta.base import InvalidMetadataFilterError, ThreadMetaStore
from deerflow.persistence.thread_meta.memory import MemoryThreadMetaStore
from deerflow.persistence.thread_meta.model import ThreadMetaRow
from deerflow.persistence.thread_meta.sql import ThreadMetaRepository

if TYPE_CHECKING:
    from langgraph.store.base import BaseStore
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = [
    "InvalidMetadataFilterError",
    "MemoryThreadMetaStore",
    "ThreadMetaRepository",
    "ThreadMetaRow",
    "ThreadMetaStore",
    "make_thread_store",
]


def make_thread_store(
    session_factory: async_sessionmaker[AsyncSession] | None,
    store: BaseStore | None = None,
) -> ThreadMetaStore:
    """按可用后端创建合适的 ThreadMetaStore。

    有 session_factory 时返回 SQL 仓储，否则回退到基于内存 LangGraph Store 的实现。
    """
    if session_factory is not None:
        return ThreadMetaRepository(session_factory)
    if store is None:
        raise ValueError("make_thread_store requires either a session_factory (SQL) or a store (memory)")
    return MemoryThreadMetaStore(store)
