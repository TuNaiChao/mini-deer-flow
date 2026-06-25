"""异步 Store 工厂——后端镜像 checkpointer 配置。

Store 和 checkpointer 共用 *config.yaml* 的 ``checkpointer`` 段，恒用同一种持久化技术：

- ``type: memory``   → :class:`langgraph.store.memory.InMemoryStore`
- ``type: sqlite``   → :class:`langgraph.store.sqlite.aio.AsyncSqliteStore`
- ``type: postgres`` → :class:`langgraph.store.postgres.aio.AsyncPostgresStore`

用法（lifespan / 长跑服务）::

    from deerflow.runtime.store import make_store

    async with make_store(app_config) as store:
        bundle.store = store
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator

from langgraph.store.base import BaseStore

from deerflow.config.app_config import AppConfig, get_app_config
from deerflow.runtime.store._sqlite_utils import ensure_sqlite_parent_dir, resolve_sqlite_conn_str
from deerflow.runtime.store.provider import POSTGRES_CONN_REQUIRED, POSTGRES_STORE_INSTALL, SQLITE_STORE_INSTALL, _no_checkpointer_warning

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 内部后端工厂
# ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def _async_store(config) -> AsyncIterator[BaseStore]:
    """构造并拆除一个异步 Store 的上下文管理器。

    *config* 是 :class:`deerflow.config.checkpointer_config.CheckpointerConfig`——和 checkpointer
    工厂用同一个对象。
    """
    if config.type == "memory":
        from langgraph.store.memory import InMemoryStore

        logger.info("Store: using InMemoryStore (in-process, not persistent)")
        yield InMemoryStore()
        return

    if config.type == "sqlite":
        try:
            from langgraph.store.sqlite.aio import AsyncSqliteStore
        except ImportError as exc:
            raise ImportError(SQLITE_STORE_INSTALL) from exc

        conn_str = resolve_sqlite_conn_str(config.connection_string or "store.db")
        ensure_sqlite_parent_dir(conn_str)

        async with AsyncSqliteStore.from_conn_string(conn_str) as store:
            await store.setup()
            logger.info("Store: using AsyncSqliteStore (%s)", conn_str)
            yield store
        return

    if config.type == "postgres":
        try:
            from langgraph.store.postgres.aio import AsyncPostgresStore  # type: ignore[import]
        except ImportError as exc:
            raise ImportError(POSTGRES_STORE_INSTALL) from exc

        if not config.connection_string:
            raise ValueError(POSTGRES_CONN_REQUIRED)

        async with AsyncPostgresStore.from_conn_string(config.connection_string) as store:
            await store.setup()
            logger.info("Store: using AsyncPostgresStore")
            yield store
        return

    raise ValueError(f"Unknown store backend type: {config.type!r}")


# ---------------------------------------------------------------------------
# 公开异步上下文管理器
# ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def make_store(app_config: AppConfig | None = None) -> AsyncIterator[BaseStore]:
    """异步上下文管理器：yield 一个后端与 checkpointer 配置一致的 Store。

    读 *config.yaml* 里和 :func:`deerflow.runtime.checkpointer.async_provider.make_checkpointer`
    同一个 ``checkpointer`` 段，让两个单件恒用同一种持久化技术::

        async with make_store(app_config) as store:
            bundle.store = store

    没配 ``checkpointer`` 段时 yield :class:`~langgraph.store.memory.InMemoryStore`（发 WARNING）。
    """
    if app_config is None:
        app_config = get_app_config()

    ckpt_config = app_config.checkpointer

    if ckpt_config is None:
        from langgraph.store.memory import InMemoryStore

        _no_checkpointer_warning()
        yield InMemoryStore()
        return

    async with _async_store(ckpt_config) as store:
        yield store
