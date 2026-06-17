"""工具输出预算保护配置。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ToolOutputConfig(BaseModel):
    """工具结果输出预算执行的配置节。

    工具返回超过 ``externalize_min_chars`` 字符时，完整输出持久化到磁盘，替换为
    精简预览 + 文件引用。磁盘持久化不可用时回退为首尾截断。
    """

    enabled: bool = Field(
        default=True,
        description="启用工具输出预算中间件。",
    )
    externalize_min_chars: int = Field(
        default=12_000,
        ge=0,
        description="触发磁盘外部化的字符阈值。低于此值的输出原样放行。设 0 禁用外部化（超 fallback_max_chars 时仍会回退截断）。",
    )
    preview_head_chars: int = Field(
        default=2_000,
        ge=0,
        description="预览里从输出头部保留的字符数。",
    )
    preview_tail_chars: int = Field(
        default=1_000,
        ge=0,
        description="预览里从输出尾部保留的字符数。",
    )
    fallback_max_chars: int = Field(
        default=30_000,
        ge=0,
        description="磁盘持久化不可用时的最大字符数。0 禁用回退截断。",
    )
    fallback_head_chars: int = Field(default=8_000, ge=0, description="回退截断的头部字符数。")
    fallback_tail_chars: int = Field(default=3_000, ge=0, description="回退截断的尾部字符数。")
    storage_subdir: str = Field(
        default=".tool-results",
        description="线程输出路径下持久化工具结果的子目录。",
    )
    exempt_tools: list[str] = Field(
        default_factory=lambda: ["read_file", "read_file_tool"],
        description="免预算执行的工具名（防止 持久化→读取→持久化 循环）。",
    )
    tool_overrides: dict[str, int] = Field(
        default_factory=dict,
        description="按工具的 externalize_min_chars 覆盖。键为工具名，值为字符阈值。用 0 对特定工具禁用外部化。",
    )
