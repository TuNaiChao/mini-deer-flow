"""记忆机制配置。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class MemoryConfig(BaseModel):
    """全局记忆机制配置。"""

    enabled: bool = Field(
        default=True,
        description="是否启用记忆机制",
    )
    storage_path: str = Field(
        default="",
        description="记忆数据存储路径。空则默认按用户存到 `{base_dir}/users/{user_id}/memory.json`。绝对路径原样使用并退出按用户隔离（所有用户共享同一文件）；相对路径相对 base_dir 解析（非 backend 工作目录）。",
    )
    storage_class: str = Field(
        default="deerflow.agents.memory.storage.FileMemoryStorage",
        description="记忆存储 provider 的类路径",
    )
    debounce_seconds: int = Field(
        default=30,
        ge=1,
        le=300,
        description="处理排队更新前等待的秒数（去抖）",
    )
    model_name: str | None = Field(
        default=None,
        description="记忆更新用的模型名（None = 用默认模型）",
    )
    max_facts: int = Field(
        default=100,
        ge=10,
        le=500,
        description="最多存储的事实条数",
    )
    fact_confidence_threshold: float = Field(
        default=0.7,
        ge=0.0,
        le=1.0,
        description="存储事实的最低置信度阈值",
    )
    injection_enabled: bool = Field(
        default=True,
        description="是否把记忆注入系统 prompt",
    )
    max_injection_tokens: int = Field(
        default=2000,
        ge=100,
        le=8000,
        description="记忆注入最多占用的 token 数",
    )
    token_counting: Literal["tiktoken", "char"] = Field(
        default="tiktoken",
        description="记忆注入预算的 token 计数策略。'tiktoken' 精确但首次使用时可能从公共网络端点下载 BPE 数据，在网络受限环境可能长时间阻塞（见 issue #3402/#3429）；'char' 用无网络的 CJK 感知字符估算，从不触碰 tiktoken。",
    )
