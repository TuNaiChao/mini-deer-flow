"""DeerFlow app 持久化层（SQLAlchemy 2.0 async ORM）。

本层管理 DeerFlow 自己的应用数据——run 元数据、线程归属。它与 LangGraph 的
checkpointer **完全分离**：checkpointer 管理图执行状态（节点输出 / 消息历史），
本层管理「一次 run 跑了什么 / 谁的线程」这类应用元数据。两者即使共用同一个
``.db`` 文件，表也互不重叠。

用法::

    from deerflow.persistence import init_engine, close_engine, get_session_factory

    await init_engine_from_config(cfg.database)   # memory → no-op
    sf = get_session_factory()                    # memory → None
    repo = RunRepository(sf) if sf else MemoryRunStore()
    await close_engine()
"""

from deerflow.persistence.engine import close_engine, get_engine, get_session_factory, init_engine, init_engine_from_config

__all__ = ["close_engine", "get_engine", "get_session_factory", "init_engine", "init_engine_from_config"]
