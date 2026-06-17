"""SQLAlchemy 后端的 RunEventStore。

把事件持久化到 ``run_events`` 表。trace 内容按 ``max_trace_content`` 字节截断，
避免撑爆数据库。

seq 单调性（红线 #3）：写时用 ``SELECT max(seq) ... FOR UPDATE``（sqlite）或
``pg_advisory_xact_lock``（postgres，因为 postgres 不允许对聚合结果加 FOR UPDATE）
串行化同 thread 的写者。
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deerflow.persistence.models.run_event import RunEventRow
from deerflow.runtime.events.store.base import RunEventStore
from deerflow.runtime.user_context import AUTO, _AutoSentinel, get_current_user, resolve_user_id
from deerflow.utils.time import coerce_iso

logger = logging.getLogger(__name__)


class DbRunEventStore(RunEventStore):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], *, max_trace_content: int = 10240):
        self._sf = session_factory
        self._max_trace_content = max_trace_content

    @staticmethod
    def _row_to_dict(row: RunEventRow) -> dict:
        d = row.to_dict()
        d["metadata"] = d.pop("event_metadata", {})
        val = d.get("created_at")
        if isinstance(val, datetime):
            # SQLite 读回时丢 tzinfo（尽管声明了 ``DateTime(timezone=True)``）；
            # ``coerce_iso`` 把 naive datetime 归一成 UTC。
            d["created_at"] = coerce_iso(val)
        d.pop("id", None)
        # 还原写时被 JSON 序列化的结构化 content。
        raw = d.get("content", "")
        metadata = d.get("metadata", {})
        if isinstance(raw, str) and (metadata.get("content_is_json") or metadata.get("content_is_dict")):
            try:
                d["content"] = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                # content 看着像 JSON 但解析失败；保留原始字符串。
                logger.debug("Failed to deserialize content as JSON for event seq=%s", d.get("seq"))
        return d

    def _truncate_trace(self, category: str, content: Any, metadata: dict | None) -> tuple[Any, dict]:
        if category == "trace":
            text_content = content if isinstance(content, str) else json.dumps(content, default=str, ensure_ascii=False)
            encoded = text_content.encode("utf-8")
            if len(encoded) > self._max_trace_content:
                # 按字节截断，再 decode 回来（可能切断多字节字符，故用 errors="ignore"）。
                content = encoded[: self._max_trace_content].decode("utf-8", errors="ignore")
                metadata = {**(metadata or {}), "content_truncated": True, "original_byte_length": len(encoded)}
        return content, metadata or {}

    @staticmethod
    def _content_to_db(content: Any, metadata: dict | None) -> tuple[str, dict]:
        metadata = metadata or {}
        if isinstance(content, str):
            return content, metadata

        db_content = json.dumps(content, default=str, ensure_ascii=False)
        metadata = {**metadata, "content_is_json": True}
        if isinstance(content, dict):
            metadata["content_is_dict"] = True
        return db_content, metadata

    @staticmethod
    def _user_id_from_context() -> str | None:
        """从 contextvar 软读 user_id（写路径用）。

        contextvar 未设时返回 ``None``（不加过滤 / 不 stamp），这是后台 worker 写的
        预期情况。HTTP 请求写会有鉴权中间件设 contextvar，user_id 自动 stamp。

        在边界处把 ``user.id`` 强转 ``str``（红线 #10）：auth 层把 ``User.id`` 类型化
        为 ``UUID``，但 ``run_events.user_id`` 是 ``VARCHAR(64)``，aiosqlite 无法把
        原生 UUID 绑定到 VARCHAR 列（"type 'UUID' is not supported"）——INSERT 会
        静默回滚，worker 会挂。
        """
        user = get_current_user()
        return str(user.id) if user is not None else None

    @staticmethod
    async def _max_seq_for_thread(session: AsyncSession, thread_id: str) -> int | None:
        """返回当前最大 seq，同时串行化同 thread 的写者。

        PostgreSQL 拒绝 ``SELECT max(...) FOR UPDATE``（聚合结果不是可锁行）。
        作为 release-safe 的变通，在读聚合前按 thread_id 取一个事务级 advisory lock。
        其它方言保留既有的行锁语句。
        """
        stmt = select(func.max(RunEventRow.seq)).where(RunEventRow.thread_id == thread_id)
        bind = session.get_bind()
        dialect_name = bind.dialect.name if bind is not None else ""

        if dialect_name == "postgresql":
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(CAST(:thread_id AS text))::bigint)"),
                {"thread_id": thread_id},
            )
            return await session.scalar(stmt)

        return await session.scalar(stmt.with_for_update())

    async def put(self, *, thread_id, run_id, event_type, category, content="", metadata=None, created_at=None):  # noqa: D401
        """写单条事件——仅低频路径。

        本方法开一个带 FOR UPDATE 锁的专用事务来分配单调 seq。高频写用
        :meth:`put_batch`（整批只取一次锁）。当前唯一调用方是 ``worker.run_agent``
        写初始 ``human_message`` 事件（每 run 一次）。
        """
        content, metadata = self._truncate_trace(category, content, metadata)
        db_content, metadata = self._content_to_db(content, metadata)
        user_id = self._user_id_from_context()
        async with self._sf() as session:
            async with session.begin():
                max_seq = await self._max_seq_for_thread(session, thread_id)
                seq = (max_seq or 0) + 1
                row = RunEventRow(
                    thread_id=thread_id,
                    run_id=run_id,
                    user_id=user_id,
                    event_type=event_type,
                    category=category,
                    content=db_content,
                    event_metadata=metadata,
                    seq=seq,
                    created_at=datetime.fromisoformat(created_at) if created_at else datetime.now(UTC),
                )
                session.add(row)
            return self._row_to_dict(row)

    async def put_batch(self, events):
        if not events:
            return []
        thread_ids = {e["thread_id"] for e in events}
        if len(thread_ids) > 1:
            raise ValueError(f"put_batch requires all events to belong to the same thread; got {thread_ids!r}")
        user_id = self._user_id_from_context()
        async with self._sf() as session:
            async with session.begin():
                # 所有事件属同一 thread（上面已校验）。
                thread_id = events[0]["thread_id"]
                max_seq = await self._max_seq_for_thread(session, thread_id)
                seq = max_seq or 0
                rows = []
                for e in events:
                    seq += 1
                    content = e.get("content", "")
                    category = e.get("category", "trace")
                    metadata = e.get("metadata")
                    content, metadata = self._truncate_trace(category, content, metadata)
                    db_content, metadata = self._content_to_db(content, metadata)
                    row = RunEventRow(
                        thread_id=e["thread_id"],
                        run_id=e["run_id"],
                        user_id=e.get("user_id", user_id),
                        event_type=e["event_type"],
                        category=category,
                        content=db_content,
                        event_metadata=metadata,
                        seq=seq,
                        created_at=datetime.fromisoformat(e["created_at"]) if e.get("created_at") else datetime.now(UTC),
                    )
                    session.add(row)
                    rows.append(row)
            return [self._row_to_dict(r) for r in rows]

    async def list_messages(
        self,
        thread_id,
        *,
        limit=50,
        before_seq=None,
        after_seq=None,
        user_id: str | None | _AutoSentinel = AUTO,
    ):
        resolved_user_id = resolve_user_id(user_id, method_name="DbRunEventStore.list_messages")
        stmt = select(RunEventRow).where(RunEventRow.thread_id == thread_id, RunEventRow.category == "message")
        if resolved_user_id is not None:
            stmt = stmt.where(RunEventRow.user_id == resolved_user_id)
        if before_seq is not None:
            stmt = stmt.where(RunEventRow.seq < before_seq)
        if after_seq is not None:
            stmt = stmt.where(RunEventRow.seq > after_seq)

        if after_seq is not None:
            # 正向分页：游标之后的前 ``limit`` 条
            stmt = stmt.order_by(RunEventRow.seq.asc()).limit(limit)
            async with self._sf() as session:
                result = await session.execute(stmt)
                return [self._row_to_dict(r) for r in result.scalars()]
        else:
            # before_seq 或默认（最近）：取最后 ``limit`` 条，返回时升序
            stmt = stmt.order_by(RunEventRow.seq.desc()).limit(limit)
            async with self._sf() as session:
                result = await session.execute(stmt)
                rows = list(result.scalars())
                return [self._row_to_dict(r) for r in reversed(rows)]

    async def list_events(
        self,
        thread_id,
        run_id,
        *,
        event_types=None,
        limit=500,
        user_id: str | None | _AutoSentinel = AUTO,
    ):
        resolved_user_id = resolve_user_id(user_id, method_name="DbRunEventStore.list_events")
        stmt = select(RunEventRow).where(RunEventRow.thread_id == thread_id, RunEventRow.run_id == run_id)
        if resolved_user_id is not None:
            stmt = stmt.where(RunEventRow.user_id == resolved_user_id)
        if event_types:
            stmt = stmt.where(RunEventRow.event_type.in_(event_types))
        stmt = stmt.order_by(RunEventRow.seq.asc()).limit(limit)
        async with self._sf() as session:
            result = await session.execute(stmt)
            return [self._row_to_dict(r) for r in result.scalars()]

    async def list_messages_by_run(
        self,
        thread_id,
        run_id,
        *,
        limit=50,
        before_seq=None,
        after_seq=None,
        user_id: str | None | _AutoSentinel = AUTO,
    ):
        resolved_user_id = resolve_user_id(user_id, method_name="DbRunEventStore.list_messages_by_run")
        stmt = select(RunEventRow).where(
            RunEventRow.thread_id == thread_id,
            RunEventRow.run_id == run_id,
            RunEventRow.category == "message",
        )
        if resolved_user_id is not None:
            stmt = stmt.where(RunEventRow.user_id == resolved_user_id)
        if before_seq is not None:
            stmt = stmt.where(RunEventRow.seq < before_seq)
        if after_seq is not None:
            stmt = stmt.where(RunEventRow.seq > after_seq)

        if after_seq is not None:
            stmt = stmt.order_by(RunEventRow.seq.asc()).limit(limit)
            async with self._sf() as session:
                result = await session.execute(stmt)
                return [self._row_to_dict(r) for r in result.scalars()]
        else:
            stmt = stmt.order_by(RunEventRow.seq.desc()).limit(limit)
            async with self._sf() as session:
                result = await session.execute(stmt)
                rows = list(result.scalars())
                return [self._row_to_dict(r) for r in reversed(rows)]

    async def count_messages(
        self,
        thread_id,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
    ):
        resolved_user_id = resolve_user_id(user_id, method_name="DbRunEventStore.count_messages")
        stmt = select(func.count()).select_from(RunEventRow).where(RunEventRow.thread_id == thread_id, RunEventRow.category == "message")
        if resolved_user_id is not None:
            stmt = stmt.where(RunEventRow.user_id == resolved_user_id)
        async with self._sf() as session:
            return await session.scalar(stmt) or 0

    async def delete_by_thread(
        self,
        thread_id,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
    ):
        resolved_user_id = resolve_user_id(user_id, method_name="DbRunEventStore.delete_by_thread")
        async with self._sf() as session:
            count_conditions = [RunEventRow.thread_id == thread_id]
            if resolved_user_id is not None:
                count_conditions.append(RunEventRow.user_id == resolved_user_id)
            count_stmt = select(func.count()).select_from(RunEventRow).where(*count_conditions)
            count = await session.scalar(count_stmt) or 0
            if count > 0:
                await session.execute(delete(RunEventRow).where(*count_conditions))
                await session.commit()
            return count

    async def delete_by_run(
        self,
        thread_id,
        run_id,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
    ):
        resolved_user_id = resolve_user_id(user_id, method_name="DbRunEventStore.delete_by_run")
        async with self._sf() as session:
            count_conditions = [RunEventRow.thread_id == thread_id, RunEventRow.run_id == run_id]
            if resolved_user_id is not None:
                count_conditions.append(RunEventRow.user_id == resolved_user_id)
            count_stmt = select(func.count()).select_from(RunEventRow).where(*count_conditions)
            count = await session.scalar(count_stmt) or 0
            if count > 0:
                await session.execute(delete(RunEventRow).where(*count_conditions))
                await session.commit()
            return count
