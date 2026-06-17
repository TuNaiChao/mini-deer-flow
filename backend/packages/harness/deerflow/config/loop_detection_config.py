"""循环检测中间件配置。"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


class ToolFreqOverride(BaseModel):
    """单个工具的频率阈值覆盖。

    可高于或低于全局默认。常用于给 bash 这类高频工具（如 RNA-seq 流水线批量任务）
    抬高阈值，而不削弱其它工具的保护。
    """

    warn: int = Field(ge=1)
    hard_limit: int = Field(ge=1)

    @model_validator(mode="after")
    def _validate(self) -> ToolFreqOverride:
        if self.hard_limit < self.warn:
            raise ValueError("hard_limit must be >= warn")
        return self


class LoopDetectionConfig(BaseModel):
    """重复 tool-call 循环检测配置。"""

    enabled: bool = Field(
        default=True,
        description="是否启用重复 tool-call 循环检测",
    )
    warn_threshold: int = Field(
        default=3,
        ge=1,
        description="相同 tool-call 集合出现几次后注入警告",
    )
    hard_limit: int = Field(
        default=5,
        ge=1,
        description="相同 tool-call 集合出现几次后强制停止",
    )
    window_size: int = Field(
        default=20,
        ge=1,
        description="每个线程跟踪的最近 tool-call 集合数",
    )
    max_tracked_threads: int = Field(
        default=100,
        ge=1,
        description="内存中最多保留的线程历史数",
    )
    tool_freq_warn: int = Field(
        default=30,
        ge=1,
        description="同一工具类型调用几次后注入频率警告",
    )
    tool_freq_hard_limit: int = Field(
        default=50,
        ge=1,
        description="同一工具类型调用几次后强制停止",
    )
    tool_freq_overrides: dict[str, ToolFreqOverride] = Field(
        default_factory=dict,
        description="tool_freq_warn / tool_freq_hard_limit 的按工具覆盖，键为工具名。值可高可低于全局默认。常用于给 bash 抬高阈值。",
    )

    @model_validator(mode="after")
    def validate_thresholds(self) -> LoopDetectionConfig:
        """确保硬停止不会早于警告阈值发生。"""
        if self.hard_limit < self.warn_threshold:
            raise ValueError("hard_limit must be greater than or equal to warn_threshold")
        if self.tool_freq_hard_limit < self.tool_freq_warn:
            raise ValueError("tool_freq_hard_limit must be greater than or equal to tool_freq_warn")
        return self
