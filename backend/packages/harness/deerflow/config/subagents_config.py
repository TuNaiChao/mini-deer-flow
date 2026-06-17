"""子代理系统配置（从 config.yaml 加载）。

mini 简化版：只保留主开关与运行参数。deer 的 ``custom_agents``（自定义子代理类型）
属于 M-opt-agents_config，本期不做；``agents``（按名覆盖）在 M11 落地时按需补。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class SubagentsAppConfig(BaseModel):
    """子代理系统配置。"""

    enabled: bool = Field(
        default=True,
        description="是否启用子代理委派（task 工具）",
    )
    max_concurrent: int = Field(
        default=3,
        ge=1,
        description="最大并发子代理数（与 SubagentLimitMiddleware 共同保证）",
    )
    timeout_seconds: int = Field(
        default=1800,
        ge=1,
        description="内置子代理的默认超时秒数（默认 1800 = 30 分钟）",
    )
    max_turns: int | None = Field(
        default=None,
        ge=1,
        description="所有子代理的可选默认最大轮次覆盖（None = 保持内置默认）",
    )
