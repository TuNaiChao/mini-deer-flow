"""自动线程标题生成配置。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class TitleConfig(BaseModel):
    """自动线程标题生成配置。"""

    enabled: bool = Field(
        default=True,
        description="是否启用自动标题生成",
    )
    max_words: int = Field(
        default=6,
        ge=1,
        le=20,
        description="生成标题的最大词数",
    )
    max_chars: int = Field(
        default=60,
        ge=10,
        le=200,
        description="生成标题的最大字符数",
    )
    model_name: str | None = Field(
        default=None,
        description="标题生成用的模型名（None = 用默认模型）",
    )
    prompt_template: str = Field(
        default=("Generate a concise title (max {max_words} words) for this conversation.\nUser: {user_msg}\nAssistant: {assistant_msg}\n\nReturn ONLY the title, no quotes, no explanation."),
        description="标题生成的 prompt 模板",
    )
