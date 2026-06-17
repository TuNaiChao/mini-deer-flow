"""LangGraph checkpointer 配置。

checkpointer 负责 LangGraph 状态持久化（对话状态快照，跨轮次恢复）。
与 :class:`DatabaseConfig` 是**独立**的两个配置：``database`` 统一管 app 数据，
``checkpointer`` 单独管 LangGraph 状态（可覆盖默认后端，对齐 langgraph.json 的
``checkpointer:`` 段）。``None`` 表示用 ``database`` 派生的默认 checkpointer。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

CheckpointerType = Literal["memory", "sqlite", "postgres"]


class CheckpointerConfig(BaseModel):
    """LangGraph 状态持久化 checkpointer 配置。"""

    type: CheckpointerType = Field(
        description="checkpointer 后端类型。'memory' 进程内（重启丢失）。'sqlite' 持久化到本地文件（需 langgraph-checkpoint-sqlite）。'postgres' 持久化到 PostgreSQL（装 deerflow-harness[postgres]）。",
    )
    connection_string: str | None = Field(
        default=None,
        description="sqlite（文件路径）或 postgres（DSN）的连接串。sqlite 可省略，默认 'store.db'；postgres 必填。sqlite 例：'.deer-flow/checkpoints.db' 或 ':memory:'；postgres 例：'postgresql://user:pass@localhost:5432/db'。",
    )
