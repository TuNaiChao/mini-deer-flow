"""同步 Store 工厂——给 CLI 工具 / 内嵌 client 用。

提供**同步单例**（``get_store``）和**同步上下文管理器**（``store_context``）。Store 的后端
**镜像 checkpointer 配置**——两者恒用同一种持久化技术（memory / sqlite / postgres），见
[checkpointer_config](../../../config/checkpointer_config.py)。

为什么 Store 和 checkpointer 共用配置？checkpointer 存「图状态快照」（按 thread + checkpoint），
Store 存「跨线程的长效记忆」（按 namespace + key）。它们是两种不同的持久化需求，但用同一个
数据库后端最省事（一份连接配置、一套运维），所以 mini 让它们读同一个 ``checkpointer`` 段。

用法::

    from deerflow.runtime.store.provider import get_store, store_context

    store = get_store()                 # 单例，进程退出才关
    with store_context() as store: ...  # 一次性，块退出即关
"""

from __future__ import annotations

import contextlib
import logging
import threading
from collections.abc import Iterator

from langgraph.store.base import BaseStore

from deerflow.config.app_config import get_app_config
from deerflow.runtime.store._sqlite_utils import ensure_sqlite_parent_dir, resolve_sqlite_conn_str

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 缺包安装提示（soft-load，红线 #24）
# ---------------------------------------------------------------------------

SQLITE_STORE_INSTALL = "langgraph-checkpoint-sqlite is required for the SQLite store. Install it with: uv add langgraph-checkpoint-sqlite"
POSTGRES_STORE_INSTALL = (
    "langgraph-checkpoint-postgres is required for the PostgreSQL store. Install the package extra with: pip install 'deerflow-harness[postgres]' (or use: uv sync --all-packages --extra postgres when developing locally)"
)
POSTGRES_CONN_REQUIRED = "checkpointer.connection_string is required for the postgres backend"


def _no_checkpointer_warning() -> None:
    logger.warning("No 'checkpointer' section in config.yaml — using InMemoryStore for the store. Thread list will be lost on server restart. Configure a sqlite or postgres backend for persistence.")


# ---------------------------------------------------------------------------
# 后端构造（同步上下文管理器）
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _sync_store_cm(config) -> Iterator[BaseStore]:
    """创建并拆除一个同步 Store 的上下文管理器。

    *config* 是 :class:`~deerflow.config.checkpointer_config.CheckpointerConfig`——和 checkpointer
    工厂用的是同一个对象。
    """
    if config.type == "memory":
        from langgraph.store.memory import InMemoryStore

        logger.info("Store: using InMemoryStore (in-process, not persistent)")
        yield InMemoryStore()
        return

    if config.type == "sqlite":
        try:
            from langgraph.store.sqlite import SqliteStore
        except ImportError as exc:
            raise ImportError(SQLITE_STORE_INSTALL) from exc

        conn_str = resolve_sqlite_conn_str(config.connection_string or "store.db")
        ensure_sqlite_parent_dir(conn_str)

        with SqliteStore.from_conn_string(conn_str) as store:
            store.setup()
            logger.info("Store: using SqliteStore (%s)", conn_str)
            yield store
        return

    if config.type == "postgres":
        try:
            from langgraph.store.postgres import PostgresStore  # type: ignore[import]
        except ImportError as exc:
            raise ImportError(POSTGRES_STORE_INSTALL) from exc

        if not config.connection_string:
            raise ValueError(POSTGRES_CONN_REQUIRED)

        with PostgresStore.from_conn_string(config.connection_string) as store:
            store.setup()
            logger.info("Store: using PostgresStore")
            yield store
        return

    raise ValueError(f"Unknown store backend type: {config.type!r}")


# ---------------------------------------------------------------------------
# 同步单例
# ---------------------------------------------------------------------------

_store: BaseStore | None = None
_store_ctx = None  # 保持连接打开的上下文管理器
_store_lock = threading.Lock()


def get_store() -> BaseStore:
    """返回全局同步 Store 单例，首次调用时创建。

    *config.yaml* 没配 ``checkpointer`` 段时返回 :class:`~langgraph.store.memory.InMemoryStore`
    （发 WARNING）。给了就按其后端建（sqlite/postgres 缺包抛 ImportError 带安装提示）。

    Raises:
        ImportError: 配置的后端缺包。
        ValueError: 后端需要 connection_string 但没给。
    """
    global _store, _store_ctx

    if _store is not None:
        return _store

    with _store_lock:
        if _store is not None:
            return _store

        app_config = get_app_config()
        ckpt_config = app_config.checkpointer

        if ckpt_config is None:
            from langgraph.store.memory import InMemoryStore

            _no_checkpointer_warning()
            _store = InMemoryStore()
            return _store

        store_ctx = _sync_store_cm(ckpt_config)
        store = store_ctx.__enter__()
        _store_ctx = store_ctx
        _store = store
    return _store


def reset_store() -> None:
    """重置同步单例，下次调用强制重建。

    关闭已打开的后端连接并清缓存。测试 / 配置变更后用。
    """
    global _store, _store_ctx
    with _store_lock:
        if _store_ctx is not None:
            try:
                _store_ctx.__exit__(None, None, None)
            except Exception:
                logger.warning("Error during store cleanup", exc_info=True)
            _store_ctx = None
        _store = None


# ---------------------------------------------------------------------------
# 同步一次性上下文管理器
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def store_context() -> Iterator[BaseStore]:
    """同步上下文管理器：yield 一个 Store，块退出清理。

    与 :func:`get_store` 不同，它**不缓存**——每个 ``with`` 块自建自毁一个连接。CLI 脚本 /
    测试想要确定性清理时用::

        with store_context() as store:
            store.put(("threads",), thread_id, {...})

    *config.yaml* 没配 ``checkpointer`` 段时 yield :class:`~langgraph.store.memory.InMemoryStore`。
    """
    app_config = get_app_config()
    ckpt_config = app_config.checkpointer

    if ckpt_config is None:
        from langgraph.store.memory import InMemoryStore

        _no_checkpointer_warning()
        yield InMemoryStore()
        return

    with _sync_store_cm(ckpt_config) as store:
        yield store
