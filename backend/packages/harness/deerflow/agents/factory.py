"""
Agent 工厂模块

提供 create_deerflow_agent() —— 纯参数化的 SDK 级入口。
不依赖 config.yaml，适合程序化使用。
"""

from typing import TYPE_CHECKING

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware

from ..models import get_default_model
from .thread_state import ThreadState

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel
    from langchain_core.tools import BaseTool
    from langgraph.graph.state import CompiledStateGraph


def create_deerflow_agent(
    model: "BaseChatModel | None" = None,
    tools: "list[BaseTool] | None" = None,
    *,
    system_prompt: str | None = None,
    middleware: list[AgentMiddleware] | None = None,
    state_schema: type | None = None,
) -> "CompiledStateGraph":
    """
    创建 DeerFlow Agent（SDK 入口）

    纯参数化创建，不读取 config.yaml。
    适合在 Python 代码中程序化创建 Agent。

    Args:
        model: 聊天模型实例。为 None 时使用默认模型
        tools: 工具列表
        system_prompt: 系统提示词。为 None 时使用默认
        middleware: 中间件列表（阶段3详解）
        state_schema: 状态类。默认为 ThreadState

    Returns:
        CompiledStateGraph 实例

    Example:
        from deerflow.agents import create_deerflow_agent
        from deerflow.models import create_chat_model

        model = create_chat_model("deepseek")
        agent = create_deerflow_agent(model=model)

        result = agent.invoke({
            "messages": [{"role": "user", "content": "你好！"}]
        })
    """
    if model is None:
        model = get_default_model()

    if tools is None:
        tools = []

    if state_schema is None:
        state_schema = ThreadState

    if system_prompt is None:
        system_prompt = "你是一个有用的 AI 助手，名叫 DeerFlow。"

    return create_agent(
        model=model,
        tools=tools,
        middleware=middleware or [],
        system_prompt=system_prompt,
        state_schema=state_schema,
    )
