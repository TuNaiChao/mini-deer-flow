"""MCP 工具元数据标记的单一真相源。

对齐 deer ``tools/mcp_metadata.py``。一个工具是「MCP 来源」当且仅当它带着
``deerflow_mcp`` 元数据标志。该标记在 MCP 工具加载处（``tools.tools.get_available_tools``）
**写入**，在延迟工具装配处（``tools.builtins.tool_search.assemble_deferred_tools``）和
agent 构建处（M17）**读取**。

把 key / 写入器 / 谓词集中在这里，让这个魔法字符串只活在一处，读取方 import 公开谓词
而非私有跨模块 helper。

设计为叶子模块：只依赖 ``BaseTool``，任何模块（含工具加载器）都能 import 它而不会循环。
"""

from __future__ import annotations

from langchain.tools import BaseTool

MCP_TOOL_METADATA_KEY = "deerflow_mcp"


def tag_mcp_tool(tool: BaseTool) -> BaseTool:
    """标记 ``tool`` 为 MCP 来源。就地修改并返回（便于链式调用）。"""
    tool.metadata = {**(tool.metadata or {}), MCP_TOOL_METADATA_KEY: True}
    return tool


def is_mcp_tool(tool: BaseTool) -> bool:
    """``tool`` 是否带着 :func:`tag_mcp_tool` 写入的 MCP 来源标记。"""
    return (getattr(tool, "metadata", None) or {}).get(MCP_TOOL_METADATA_KEY) is True
