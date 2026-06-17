"""token 用量跟踪配置。"""

from pydantic import BaseModel, Field


class TokenUsageConfig(BaseModel):
    """token 用量跟踪配置。"""

    enabled: bool = Field(
        default=True,
        description="是否启用 token 用量跟踪中间件",
    )
