"""运行事件存储配置。

控制运行事件（消息 + 执行轨迹）持久化到哪。后端：
- **memory**：内存存储，重启丢失。适合开发 / 测试。
- **db**：经 SQLAlchemy ORM 的 SQL 数据库。提供完整查询能力。适合生产部署。
- **jsonl**：追加写 JSONL 文件。单节点需要持久化但不想上数据库时的轻量方案。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class RunEventsConfig(BaseModel):
    """运行事件存储配置。"""

    backend: Literal["memory", "db", "jsonl"] = Field(
        default="memory",
        description="运行事件存储后端。'memory' 开发用（不持久化），'db' 生产用（SQL 查询），'jsonl' 轻量单节点持久化。",
    )
    max_trace_content: int = Field(
        default=10240,
        description="trace 内容截断前的最大字节数（仅 db 后端）。",
    )
    track_token_usage: bool = Field(
        default=True,
        description="是否在事件里记录 token 用量。",
    )
