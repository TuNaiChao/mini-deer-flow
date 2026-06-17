"""通过 tool_search 的延迟工具加载配置。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ToolSearchConfig(BaseModel):
    """通过 tool_search 的延迟工具加载配置。

    启用时，MCP 工具不直接加载进 agent 上下文，而是在系统 prompt 里按名列出，
    运行时通过 tool_search 工具发现。
    """

    enabled: bool = Field(
        default=False,
        description="延迟工具加载并启用 tool_search",
    )
