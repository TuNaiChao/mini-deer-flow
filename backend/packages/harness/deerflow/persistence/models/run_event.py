"""run 事件的 ORM 模型（RunEventRow）。

一条 run event = run 内的一个时间点记录：一条消息（message）、一段轨迹
（trace，如 LLM/tool 调用）、或一个生命周期事件（lifecycle，如 run 开始/结束）。
存储实现（memory/jsonl/db）在 M6（events/store）落地；本 Phase 1 只把表结构建好，
供 ``create_all`` 与未来的 ``DbRunEventStore`` 使用。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from deerflow.persistence.base import Base


class RunEventRow(Base):
    """run 事件表。"""

    __tablename__ = "run_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # 这条事件所属会话的拥有者。鉴权引入前的数据可为 NULL；新写入与启动期 orphan
    # 迁移会回填。
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    category: Mapped[str] = mapped_column(String(16), nullable=False)
    # "message" | "trace" | "lifecycle"
    content: Mapped[str] = mapped_column(Text, default="")
    event_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
    seq: Mapped[int] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))

    __table_args__ = (
        UniqueConstraint("thread_id", "seq", name="uq_events_thread_seq"),
        Index("ix_events_thread_cat_seq", "thread_id", "category", "seq"),
        Index("ix_events_run", "thread_id", "run_id", "seq"),
    )
