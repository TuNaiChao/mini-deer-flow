"""LangGraph Store 工厂——与 checkpointer 平行的跨线程记忆。

Store 和 checkpointer 共用 *config.yaml* 的 ``checkpointer`` 段，恒用同一种持久化技术
（memory / sqlite / postgres）。两者区别：

- **checkpointer**：存图**状态快照**（按 thread + checkpoint_id）——run 间恢复对话；
- **Store**：存**跨线程长效记忆**（按 namespace + key）——thread 列表、长期数据。

公开 API（M19 落地）：

- :func:`make_store`（异步 CM）：长跑服务 / lifespan 用；
- :func:`get_store`（同步单例）+ :func:`store_context`（同步 CM）：CLI / 内嵌 client 用；
- :func:`reset_store`：重置单例（测试 / 配置变更）。

SQLite 工具（``_sqlite_utils``）由 checkpointer 与 store provider 共用，这里一并转发。
"""

from deerflow.runtime.store._sqlite_utils import ensure_sqlite_parent_dir, resolve_sqlite_conn_str
from deerflow.runtime.store.async_provider import make_store
from deerflow.runtime.store.provider import get_store, reset_store, store_context

__all__ = [
    # 异步
    "make_store",
    # 同步
    "get_store",
    "reset_store",
    "store_context",
    # SQLite 工具
    "ensure_sqlite_parent_dir",
    "resolve_sqlite_conn_str",
]
