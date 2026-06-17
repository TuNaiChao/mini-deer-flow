"""SafetyFinishReasonMiddleware 配置。

中间件拦截 provider 发出安全相关终止信号（如 OpenAI ``finish_reason='content_filter'``）
却仍返回 tool_calls 的 AIMessage，抑制这些 tool_calls，避免半截参数被执行。

检测器通过 ``deerflow.reflection.resolve_variable`` 按类路径加载（与 guardrails.provider
同一加载器），用户可插入自定义 provider 检测器而无需改核心代码。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class SafetyDetectorConfig(BaseModel):
    """``safety_finish_reason.detectors`` 下的一条检测器配置。"""

    use: str = Field(
        description="SafetyTerminationDetector 实现的类路径（如 'deerflow.agents.middlewares.safety_termination_detectors:OpenAICompatibleContentFilterDetector'）。",
    )
    config: dict = Field(
        default_factory=dict,
        description="传给检测器类的构造 kwargs。",
    )


class SafetyFinishReasonConfig(BaseModel):
    """SafetyFinishReasonMiddleware 配置。"""

    enabled: bool = Field(
        default=True,
        description="SafetyFinishReasonMiddleware 主开关。",
    )
    detectors: list[SafetyDetectorConfig] | None = Field(
        default=None,
        description="自定义检测器列表。留空（None）用内置集合（覆盖 OpenAI 兼容 content_filter、Anthropic refusal、Gemini SAFETY/BLOCKLIST 等）。给出非空列表则完全覆盖。",
    )
