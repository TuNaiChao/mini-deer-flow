"""
工具类型定义

Runtime 是所有 DeerFlow 工具的标准运行时参数类型。
它把（上下文字典 + ThreadState）打包成一个参数，由 LangGraph 在调用工具时注入。
"""

from typing import Any

from langchain.tools import ToolRuntime

from deerflow.agents.thread_state import ThreadState

# 具体的 Runtime 类型：上下文用 dict[str, Any]，状态用 ThreadState。
# 用 dict 而不是无界 ContextT，可以避免 LangChain 在 model_dump() 工具
# args_schema 时的 PydanticSerializationUnexpectedValue 警告。
Runtime = ToolRuntime[dict[str, Any], ThreadState]
