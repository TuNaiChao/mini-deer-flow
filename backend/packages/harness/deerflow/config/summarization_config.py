"""对话摘要配置。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ContextSizeType = Literal["fraction", "tokens", "messages"]


class ContextSize(BaseModel):
    """触发或保留参数的上下文尺寸规格。"""

    type: ContextSizeType = Field(description="上下文尺寸规格的类型")
    value: int | float = Field(description="上下文尺寸规格的值")

    def to_tuple(self) -> tuple[ContextSizeType, int | float]:
        """转成 SummarizationMiddleware 期望的 tuple 格式。"""
        return (self.type, self.value)


class SummarizationConfig(BaseModel):
    """自动对话摘要配置（简化版；技能保留字段在 M14 落地时补）。"""

    enabled: bool = Field(
        default=False,
        description="是否启用自动对话摘要",
    )
    model_name: str | None = Field(
        default=None,
        description="摘要用的模型名（None = 用轻量模型）",
    )
    trigger: ContextSize | list[ContextSize] | None = Field(
        default=None,
        description=(
            "触发摘要的一个或多个阈值。任一阈值满足即触发。例：{'type':'messages','value':50} 在 50 条消息时触发，{'type':'tokens','value':4000} 在 4000 token 时触发，{'type':'fraction','value':0.8} 在模型最大输入 token 的 80% 时触发。"
        ),
    )
    keep: ContextSize = Field(
        default_factory=lambda: ContextSize(type="messages", value=20),
        description="摘要后的上下文保留策略。例：{'type':'messages','value':20} 保留 20 条消息。",
    )
    trim_tokens_to_summarize: int | None = Field(
        default=4000,
        description="为摘要准备消息时最多保留的 token 数。传 null 跳过裁剪。",
    )
    summary_prompt: str | None = Field(
        default=None,
        description="生成摘要的自定义 prompt 模板。未提供则用 LangChain 默认 prompt。",
    )
