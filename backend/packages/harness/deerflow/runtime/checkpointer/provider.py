"""同步 checkpointer 工厂。

提供 **同步单例** 与 **同步 context manager**，给 LangGraph 图编译与 CLI 工具用。

支持后端：memory / sqlite / postgres。

用法::

    from deerflow.runtime.checkpointer.provider import get_checkpointer, checkpointer_context

    # 单例——跨调用复用，进程退出时关闭
    cp = get_checkpointer()

    # 一次性——每次 with 新建连接、块结束时关闭
    with checkpointer_context() as cp:
        graph.invoke(input, config={"configurable": {"thread_id": "1"}})

可靠性：**不自建 BaseCheckpointSaver 子类**——委托 LangGraph 内置 Saver
（InMemorySaver / SqliteSaver / PostgresSaver）。缺包时报可操作的安装命令（红线 #24）。
"""

from __future__ import annotations

import contextlib
import logging
import threading
from collections.abc import Iterator

from langgraph.types import Checkpointer

from deerflow.config.app_config import get_app_config
from deerflow.config.checkpointer_config import CheckpointerConfig
from deerflow.runtime.store._sqlite_utils import ensure_sqlite_parent_dir, resolve_sqlite_conn_str

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 错误信息常量——async_provider 也 import 它们
# ---------------------------------------------------------------------------

SQLITE_INSTALL = "langgraph-checkpoint-sqlite 未安装，SQLite checkpointer 不可用。\n安装：cd backend && uv sync --all-packages --extra sqlite"
POSTGRES_INSTALL = "langgraph-checkpoint-postgres / psycopg 未安装，PostgreSQL checkpointer 不可用。\n安装：cd backend && uv sync --all-packages --extra postgres"
POSTGRES_CONN_REQUIRED = "checkpointer.connection_string is required for the postgres backend"


# ---------------------------------------------------------------------------
# 同步工厂
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _sync_checkpointer_cm(config: CheckpointerConfig) -> Iterator[Checkpointer]:
    """创建并销毁一个同步 checkpointer 的 context manager。

    返回一个配好的 ``Checkpointer`` 实例。底层连接 / 池的资源清理由本模块更上层的
    helper（单例工厂或 context manager）处理。
    """
    if config.type == "memory":
        from langgraph.checkpoint.memory import InMemorySaver

        logger.info("Checkpointer: using InMemorySaver (in-process, not persistent)")
        yield InMemorySaver()
        return

    if config.type == "sqlite":
        try:
            from langgraph.checkpoint.sqlite import SqliteSaver
        except ImportError as exc:
            raise ImportError(SQLITE_INSTALL) from exc

        conn_str = resolve_sqlite_conn_str(config.connection_string or "store.db")
        ensure_sqlite_parent_dir(conn_str)
        with SqliteSaver.from_conn_string(conn_str) as saver:
            saver.setup()
            logger.info("Checkpointer: using SqliteSaver (%s)", conn_str)
            yield saver
        return

    if config.type == "postgres":
        try:
            from langgraph.checkpoint.postgres import PostgresSaver
        except ImportError as exc:
            raise ImportError(POSTGRES_INSTALL) from exc

        if not config.connection_string:
            raise ValueError(POSTGRES_CONN_REQUIRED)

        with PostgresSaver.from_conn_string(config.connection_string) as saver:
            saver.setup()
            logger.info("Checkpointer: using PostgresSaver")
            yield saver
        return

    raise ValueError(f"Unknown checkpointer type: {config.type!r}")


# ---------------------------------------------------------------------------
# 同步单例
# ---------------------------------------------------------------------------

_checkpointer: Checkpointer | None = None
_checkpointer_ctx = None  # 保持连接存活的、已打开的 context manager
_checkpointer_lock = threading.Lock()


def get_checkpointer() -> Checkpointer:
    """返回全局同步 checkpointer 单例，首次调用时创建。

    config.yaml 里没配 checkpointer 时返回 ``InMemorySaver``。

    配置读取放在单例锁**之外**：``get_app_config()`` 可能读盘（缓存未命中或 mtime
    变化），把它放在锁外避免「持锁读盘」拖慢其它线程的单例访问。

    Raises:
        ImportError: 配置的后端所需包未安装。
        ValueError: 需要连接串的后端没给连接串。
    """
    global _checkpointer, _checkpointer_ctx

    if _checkpointer is not None:
        return _checkpointer

    # 配置读取放在锁外（避免持锁读盘）
    config = get_app_config().checkpointer

    with _checkpointer_lock:
        if _checkpointer is not None:
            return _checkpointer

        if config is None:
            from langgraph.checkpoint.memory import InMemorySaver

            logger.info("Checkpointer: using InMemorySaver (in-process, not persistent)")
            _checkpointer = InMemorySaver()
            return _checkpointer

        checkpointer_ctx = _sync_checkpointer_cm(config)
        checkpointer = checkpointer_ctx.__enter__()
        _checkpointer_ctx = checkpointer_ctx
        _checkpointer = checkpointer

    return _checkpointer


def reset_checkpointer() -> None:
    """重置同步单例，强制下次调用重建。

    关闭已打开的后端连接并清掉缓存实例。测试或配置变更后用。
    """
    global _checkpointer, _checkpointer_ctx
    with _checkpointer_lock:
        if _checkpointer_ctx is not None:
            try:
                _checkpointer_ctx.__exit__(None, None, None)
            except Exception:
                logger.warning("Error during checkpointer cleanup", exc_info=True)
            _checkpointer_ctx = None
        _checkpointer = None


# ---------------------------------------------------------------------------
# 同步 context manager
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def checkpointer_context() -> Iterator[Checkpointer]:
    """同步 context manager：yield 一个 checkpointer，退出时清理。

    与 :func:`get_checkpointer` 不同，它**不缓存**实例——每个 ``with`` 块各自创建
    并销毁连接。CLI 脚本 / 测试里想要确定性清理时用::

        with checkpointer_context() as cp:
            graph.invoke(input, config={"configurable": {"thread_id": "1"}})

    config.yaml 里没配 checkpointer 时 yield ``InMemorySaver``。
    """
    config = get_app_config().checkpointer
    if config is None:
        from langgraph.checkpoint.memory import InMemorySaver

        yield InMemorySaver()
        return

    with _sync_checkpointer_cm(config) as saver:
        yield saver
