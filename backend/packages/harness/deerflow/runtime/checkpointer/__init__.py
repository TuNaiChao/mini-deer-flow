"""LangGraph checkpointer 工厂。

公开两类入口：
- 异步 context manager :func:`make_checkpointer`——给长期 async 服务（FastAPI lifespan）。
- 同步 :func:`get_checkpointer` 单例 + :func:`reset_checkpointer` + :func:`checkpointer_context`——
  给图编译、CLI、嵌入式客户端。

委托 LangGraph 内置 Saver（InMemorySaver / SqliteSaver / PostgresSaver），不自建。
"""

from deerflow.runtime.checkpointer.async_provider import make_checkpointer
from deerflow.runtime.checkpointer.provider import checkpointer_context, get_checkpointer, reset_checkpointer

__all__ = [
    "checkpointer_context",
    "get_checkpointer",
    "make_checkpointer",
    "reset_checkpointer",
]
