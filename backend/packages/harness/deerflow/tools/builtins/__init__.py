"""内置工具模块——开箱即用的工具。

9 内置工具（M15 全收，对齐 deer）：
- ``present_file_tool`` / ``ask_clarification_tool`` —— 始终绑定；
- ``view_image_tool`` —— 仅 ``supports_vision`` 模型；
- ``task_tool`` —— 仅 ``subagent_enabled``；
- ``setup_agent`` —— 仅 ``is_bootstrap=True``（M17 lead_agent factory 绑定）；
- ``update_agent`` —— 仅 ``agent_name`` 且非 bootstrap（M17 绑定）；
- ``tool_search``（``build_tool_search_tool``）+ ``DeferredToolCatalog`` / ``assemble_deferred_tools`` —— 仅 ``tool_search.enabled``；
- ``invoke_acp_agent``（``build_invoke_acp_agent_tool``）—— 配置了 ACP agent 才加；
- ``skill_manage_tool``（在 ``tools/skill_manage_tool``）—— 仅 ``skill_evolution.enabled``。

绑定条件由 ``get_available_tools`` / lead_agent factory 决定，不是这里。
"""

from .clarification_tool import ask_clarification_tool
from .invoke_acp_agent_tool import build_invoke_acp_agent_tool
from .present_file_tool import present_file_tool
from .setup_agent_tool import setup_agent
from .task_tool import task_tool
from .tool_search import (
    DeferredToolCatalog,
    DeferredToolSetup,
    assemble_deferred_tools,
    build_deferred_tool_setup,
    build_tool_search_tool,
    get_deferred_tools_prompt_section,
)
from .update_agent_tool import update_agent
from .view_image_tool import view_image_tool

__all__ = [
    "setup_agent",
    "update_agent",
    "present_file_tool",
    "ask_clarification_tool",
    "view_image_tool",
    "task_tool",
    "build_tool_search_tool",
    "build_deferred_tool_setup",
    "assemble_deferred_tools",
    "DeferredToolCatalog",
    "DeferredToolSetup",
    "get_deferred_tools_prompt_section",
    "build_invoke_acp_agent_tool",
]
