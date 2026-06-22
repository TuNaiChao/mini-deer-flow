"""LLM 错误处理中间件的熔断配置（M16）。

``LLMErrorHandlingMiddleware`` 在连续失败后「熔断」——短路返回兜底回复而不是再
打 provider，给上游恢复时间。熔断是保护系统的最后一道闸：重试退避已经在做，
但如果 provider 持续挂（限流窗口 / 区域故障），逐请求退避仍会放大噪声，熔断让
本进程在一段时间内直接 fail-fast，直到半开探测成功才恢复。

mini 的轻量化约定：与 deer 对齐两个字段（``failure_threshold`` / ``recovery_timeout_sec``），
不引入完整的 half-open / probe 计数 schema——中间件内部用线程锁 + 时间戳维护状态。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class CircuitBreakerConfig(BaseModel):
    """LLM 调用熔断器配置。"""

    failure_threshold: int = Field(
        default=5,
        ge=1,
        description="连续失败多少次后熔断（短路返回兜底回复）。默认 5。",
    )
    recovery_timeout_sec: int = Field(
        default=30,
        ge=1,
        description="熔断后多久进入半开探测一次（秒）。默认 30。",
    )
