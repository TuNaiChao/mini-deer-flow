"""异步 checkpointer 工厂。

提供 **异步 context manager**，给需要正确资源清理的长期 async 服务用。

支持后端：memory / sqlite / postgres。

用法（如 FastAPI lifespan）::

    from deerflow.runtime.checkpointer.async_provider import make_checkpointer

    async with make_checkpointer(app_config) as checkpointer:
        app.state.checkpointer = checkpointer   # 未配 checkpointer 时是 InMemorySaver

同步用法见 :mod:`deerflow.runtime.checkpointer.provider`。

可靠性：
- sqlite 路径准备走 ``await asyncio.to_thread``（阻塞 IO 卸载，红线 #1）。
- postgres 用带 TCP keepalive 的连接池（``keepalives_idle=60`` + ``check_connection`` +
  ``prepare_threshold=0``），并经 ``psycopg_pool.AsyncConnectionPool``。
- 委托 LangGraph 内置 Saver，不自建。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

from langgraph.types import Checkpointer

from deerflow.config.app_config import AppConfig, get_app_config
from deerflow.runtime.checkpointer.provider import (
    POSTGRES_CONN_REQUIRED,
    POSTGRES_INSTALL,
    SQLITE_INSTALL,
)
from deerflow.runtime.store._sqlite_utils import ensure_sqlite_parent_dir, resolve_sqlite_conn_str

logger = logging.getLogger(__name__)


def _prepare_sqlite_checkpointer_path(raw: str) -> str:
    conn_str = resolve_sqlite_conn_str(raw)
    ensure_sqlite_parent_dir(conn_str)
    return conn_str


def _prepare_database_sqlite_checkpointer_path(db_config) -> str:
    conn_str = db_config.checkpointer_sqlite_path
    ensure_sqlite_parent_dir(conn_str)
    return conn_str


def _build_postgres_pool(conn_string: str):
    """建一个带 TCP keepalive 与连接检查的 AsyncConnectionPool。"""
    from psycopg.rows import dict_row
    from psycopg_pool import AsyncConnectionPool

    return AsyncConnectionPool(
        conn_string,
        kwargs={
            "autocommit": True,
            "prepare_threshold": 0,
            "row_factory": dict_row,
            "keepalives": 1,
            "keepalives_idle": 60,
            "keepalives_interval": 10,
            "keepalives_count": 6,
        },
        check=AsyncConnectionPool.check_connection,
    )


def _ensure_postgres_imports():
    """导入并返回 (AsyncPostgresSaver, AsyncConnectionPool)；失败抛 ImportError。"""
    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    except ImportError as exc:
        raise ImportError(POSTGRES_INSTALL) from exc

    try:
        from psycopg_pool import AsyncConnectionPool
    except ImportError as exc:
        raise ImportError(POSTGRES_INSTALL) from exc

    return AsyncPostgresSaver, AsyncConnectionPool


# ---------------------------------------------------------------------------
# 异步工厂
# ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def _async_checkpointer(config) -> AsyncIterator[Checkpointer]:
    """构造并销毁一个 checkpointer 的异步 context manager。"""
    if config.type == "memory":
        from langgraph.checkpoint.memory import InMemorySaver

        yield InMemorySaver()
        return

    if config.type == "sqlite":
        try:
            from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
        except ImportError as exc:
            raise ImportError(SQLITE_INSTALL) from exc

        conn_str = await asyncio.to_thread(_prepare_sqlite_checkpointer_path, config.connection_string or "store.db")
        async with AsyncSqliteSaver.from_conn_string(conn_str) as saver:
            await saver.setup()
            yield saver
        return

    if config.type == "postgres":
        if not config.connection_string:
            raise ValueError(POSTGRES_CONN_REQUIRED)

        AsyncPostgresSaver, _ = _ensure_postgres_imports()
        pool = _build_postgres_pool(config.connection_string)
        async with pool:
            saver = AsyncPostgresSaver(conn=pool)
            await saver.setup()
            yield saver
        return

    raise ValueError(f"Unknown checkpointer type: {config.type!r}")


# ---------------------------------------------------------------------------
# 公开异步 context manager
# ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def _async_checkpointer_from_database(db_config) -> AsyncIterator[Checkpointer]:
    """从统一 DatabaseConfig 构造 checkpointer 的异步 context manager。"""
    if db_config.backend == "memory":
        from langgraph.checkpoint.memory import InMemorySaver

        yield InMemorySaver()
        return

    if db_config.backend == "sqlite":
        try:
            from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
        except ImportError as exc:
            raise ImportError(SQLITE_INSTALL) from exc

        conn_str = await asyncio.to_thread(_prepare_database_sqlite_checkpointer_path, db_config)
        async with AsyncSqliteSaver.from_conn_string(conn_str) as saver:
            await saver.setup()
            yield saver
        return

    if db_config.backend == "postgres":
        if not db_config.postgres_url:
            raise ValueError("database.postgres_url is required for the postgres backend")

        AsyncPostgresSaver, _ = _ensure_postgres_imports()
        pool = _build_postgres_pool(db_config.postgres_url)
        async with pool:
            saver = AsyncPostgresSaver(conn=pool)
            await saver.setup()
            yield saver
        return

    raise ValueError(f"Unknown database backend: {db_config.backend!r}")


@contextlib.asynccontextmanager
async def make_checkpointer(app_config: AppConfig | None = None) -> AsyncIterator[Checkpointer]:
    """异步 context manager：在调用方生命周期内 yield 一个 checkpointer。

    资源在进入时打开、退出时关闭——**无全局状态**::

        async with make_checkpointer(app_config) as checkpointer:
            app.state.checkpointer = checkpointer

    config.yaml 里没配 checkpointer 时 yield ``InMemorySaver``。

    优先级（权威性从高到低）：
    1. legacy ``checkpointer:`` 配置段（后向兼容，显式覆盖）。
    2. 统一 ``database:`` 配置段（backend 非 memory 时）。
    3. 默认 ``InMemorySaver``。
    """
    if app_config is None:
        app_config = get_app_config()

    # legacy：独立 checkpointer 配置优先
    if app_config.checkpointer is not None:
        async with _async_checkpointer(app_config.checkpointer) as saver:
            yield saver
            return

    # 统一 database 配置
    db_config = getattr(app_config, "database", None)
    if db_config is not None and db_config.backend != "memory":
        async with _async_checkpointer_from_database(db_config) as saver:
            yield saver
            return

    # 默认：内存
    from langgraph.checkpoint.memory import InMemorySaver

    yield InMemorySaver()
