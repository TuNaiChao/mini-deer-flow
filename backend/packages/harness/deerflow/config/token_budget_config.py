"""Token 预算中间件配置（M16）。

**Token 预算**是什么
--------------------
「token」是大模型计费和上下文长度的基本单位。一次 agent 运行（一个 run）里，模型来回
「读历史消息 + 生成回复」会不断消耗 token。如果不设上限，一个跑飞的 agent（比如一直调工具、
不肯收尾）可能在单个 run 里烧掉几十万 token——既贵，又可能把上下文撑爆。

本配置给**单个 run** 设一个 token 用量上限，配合 :class:`TokenBudgetMiddleware`：
到「软阈值」时提醒模型收尾、到「硬上限」时强制停止（剥掉 tool_calls，逼模型直接给最终文本答复）。

三类上限（可分别配）
--------------------
- ``max_tokens`` —— **input + output 的总和上限**（必填，默认 20 万）；
- ``max_input_tokens`` —— 只算 input 的上限（可选，``None`` 表示不单独限 input）；
- ``max_output_tokens`` —— 只算 output 的上限（可选，``None`` 表示不单独限 output）。

中间件实际用的是「这三者里最先被突破的那个」对应的Fraction，所以三者是「或」的关系——
任何一个超比例都会触发。

两个触发阈值（都是 0~1 的比例）
--------------------------------
- ``warn_threshold`` —— 用到上限的多少比例时**注入软提醒**（默认 0.8 = 80%）；
- ``hard_stop_threshold`` —— 用到多少比例时**强制硬停**（默认 1.0 = 100%）。

校验：``hard_stop_threshold`` 不得小于 ``warn_threshold``——否则还没提醒就先硬停，提醒就失去意义。

移植自上游 deer-flow ``config/token_budget_config.py``（MIT），逻辑保持一致，注释改为面向小白的中文讲解。
"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


class TokenBudgetConfig(BaseModel):
    """单次 run 的 token 预算配置。"""

    enabled: bool = Field(
        default=False,
        description="是否启用单 run token 预算强制。关闭则 TokenBudgetMiddleware 完全不生效。",
    )
    max_tokens: int = Field(
        default=200000,
        ge=1000,
        description="单个 run 允许的 input + output token 总量上限。",
    )
    max_input_tokens: int | None = Field(
        default=None,
        ge=1,
        description="可选：只针对 input token 的单独上限。None 表示不单独限制 input。",
    )
    max_output_tokens: int | None = Field(
        default=None,
        ge=1,
        description="可选：只针对 output token 的单独上限。None 表示不单独限制 output。",
    )
    warn_threshold: float = Field(
        default=0.8,
        ge=0.0,
        le=1.0,
        description="达到上限的该比例时注入软提醒（让模型主动收尾）。0.8 = 用到 80% 时提醒。",
    )
    hard_stop_threshold: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="达到上限的该比例时强制硬停（剥 tool_calls，逼模型出最终文本答复）。1.0 = 用满时硬停。",
    )

    @model_validator(mode="after")
    def validate_thresholds(self) -> TokenBudgetConfig:
        """硬停阈值不得低于提醒阈值——否则还没提醒就直接硬停，提醒就没意义了。"""
        if self.hard_stop_threshold < self.warn_threshold:
            raise ValueError("hard_stop_threshold must be >= warn_threshold")
        return self
