"""
工具加载与组装

get_available_tools() 是所有工具的统一入口，被 make_lead_agent 调用。
工具来源：
  1. 内置工具（present_files, ask_clarification）—— 始终可用
  2. 配置定义的工具 —— 从 config.yaml 的 tools[] 用 resolve_variable 动态加载
  3. MCP 工具 —— 从 MCP 服务器加载（阶段5详解）
  4. 子代理 task 工具 —— 仅 subagent_enabled 时（阶段5详解）
"""

import logging

from langchain.tools import BaseTool

from deerflow.config import get_app_config
from deerflow.config.app_config import AppConfig
from deerflow.reflection import resolve_variable
from deerflow.tools.builtins import ask_clarification_tool, present_file_tool

logger = logging.getLogger(__name__)

# 始终可用的内置工具
BUILTIN_TOOLS = [
    present_file_tool,
    ask_clarification_tool,
]


def get_available_tools(
    groups: list[str] | None = None,
    include_mcp: bool = True,
    model_name: str | None = None,
    subagent_enabled: bool = False,
    *,
    app_config: AppConfig | None = None,
) -> list[BaseTool]:
    """
    获取所有可用工具。

    Args:
        groups: 可选的工具分组过滤（对应 config.yaml 的 tool_groups）
        include_mcp: 是否包含 MCP 工具（默认 True）
        model_name: 模型名，用于决定是否加 view_image 工具
        subagent_enabled: 是否包含子代理 task 工具
        app_config: 显式配置，为 None 时用全局 get_app_config()

    Returns:
        工具列表
    """
    config = app_config or get_app_config()

    # 1. 内置工具
    tools: list[BaseTool] = list(BUILTIN_TOOLS)

    # 2. 配置定义的工具：通过 resolve_variable(cfg.use, BaseTool) 动态加载
    #    config.yaml 里写 use: "deerflow.sandbox.tools:bash_tool"，
    #    resolve_variable 就 import deerflow.sandbox.tools 并取出 bash_tool。
    for cfg in config.tools:
        if groups is not None and cfg["group"] not in groups:
            continue
        tool_obj = resolve_variable(cfg["use"], BaseTool)
        tools.append(tool_obj)

    # 3. MCP 工具（阶段5详解，这里留接口）
    # if include_mcp:
    #     try:
    #         from deerflow.config.extensions_config import ExtensionsConfig
    #         from deerflow.mcp.cache import get_cached_mcp_tools

    #         extensions_config = ExtensionsConfig.from_file()
    #         if extensions_config.get_enabled_mcp_servers():
    #             mcp_tools = get_cached_mcp_tools()
    #             tools.extend(mcp_tools)
    #     except ImportError:
    #         logger.warning("MCP 模块不可用。安装 langchain-mcp-adapters 以启用 MCP 工具。")

    return tools
