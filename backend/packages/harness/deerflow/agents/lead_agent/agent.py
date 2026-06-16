"""
Lead Agent 实现

这是 LangGraph 的图入口点（langgraph.json 中注册的函数）。
make_lead_agent(config) → CompiledStateGraph
"""
from typing import Any

from langchain.agents import create_agent
from langchain_core.runnables import RunnableConfig

from ...config import get_app_config
from ...models import create_chat_model
from ..thread_state import ThreadState
from .prompt import apply_prompt_template


def make_lead_agent(config: RunnableConfig) -> Any:
    """
    LangGraph 图工厂函数

    这是 langgraph.json 中注册的入口点。
    langgraph dev 启动时会调用此函数来构建 Agent 图。

    Args:
        config: LangGraph 运行时配置（包含 thread_id, model_name 等）

    Returns:
        CompiledStateGraph — 编译后的 LangGraph 状态图
    """
    # 获取应用配置
    app_config = get_app_config()

    # 从运行时配置中解析参数
    configurable = config.get("configurable", {})

    # 模型名称（可从运行时配置中动态切换）
    model_name = configurable.get("model_name")
    thinking_enabled = configurable.get("thinking_enabled", False)
    plan_mode = configurable.get("is_plan_mode", False)

    # 创建模型
    model = create_chat_model(
        name=model_name,
        thinking_enabled=thinking_enabled,
    )
    print(f"使用模型: {model_name or app_config.models[0].name}")

    # 获取工具列表（暂时为空，阶段2添加）
    tools = []

    # 构建系统提示词
    system_prompt = apply_prompt_template()

    # --- 使用 langchain.agents.create_agent 创建 Agent ---
    # 这是 LangChain 的现代 API，返回 CompiledStateGraph
    # 支持：中间件、状态持久化、流式输出
    agent = create_agent(
        model=model,
        tools=tools,
        system_prompt=system_prompt,
        state_schema=ThreadState,
    )

    return agent