"""基于 SQLAlchemy 的线程元数据仓储（ThreadMetaRepository）。"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deerflow.persistence.json_compat import json_match
from deerflow.persistence.thread_meta.base import InvalidMetadataFilterError, ThreadMetaStore
from deerflow.persistence.thread_meta.model import ThreadMetaRow
from deerflow.runtime.user_context import AUTO, _AutoSentinel, resolve_user_id
from deerflow.utils.time import coerce_iso

logger = logging.getLogger(__name__)


class ThreadMetaRepository(ThreadMetaStore):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    @staticmethod
    def _row_to_dict(row: ThreadMetaRow) -> dict[str, Any]:
        d = row.to_dict()
        d["metadata"] = d.pop("metadata_json", None) or {}
        for key in ("created_at", "updated_at"):
            val = d.get(key)
            if isinstance(val, datetime):
                # SQLite 尽管声明了 ``DateTime(timezone=True)`` 仍会丢 tzinfo；
                # ``coerce_iso`` 把 naive 值归一成 UTC，保证线格式始终带 tz。
                d[key] = coerce_iso(val)
        return d

    async def create(
        self,
        thread_id: str,
        *,
        assistant_id: str | None = None,
        user_id: str | None | _AutoSentinel = AUTO,
        display_name: str | None = None,
        metadata: dict | None = None,
    ) -> dict:
        # AUTO 时从 contextvar 解析 user_id；显式 None 创建孤儿行（迁移脚本用）。
        resolved_user_id = resolve_user_id(user_id, method_name="ThreadMetaRepository.create")
        now = datetime.now(UTC)
        row = ThreadMetaRow(
            thread_id=thread_id,
            assistant_id=assistant_id,
            user_id=resolved_user_id,
            display_name=display_name,
            metadata_json=metadata or {},
            created_at=now,
            updated_at=now,
        )
        async with self._sf() as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return self._row_to_dict(row)

    async def get(
        self,
        thread_id: str,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
    ) -> dict | None:
        resolved_user_id = resolve_user_id(user_id, method_name="ThreadMetaRepository.get")
        async with self._sf() as session:
            row = await session.get(ThreadMetaRow, thread_id)
            if row is None:
                return None
            # 除非显式绕过（user_id=None），否则强制属主过滤。
            if resolved_user_id is not None and row.user_id != resolved_user_id:
                return None
            return self._row_to_dict(row)

    async def check_access(self, thread_id: str, user_id: str, *, require_existing: bool = False) -> bool:
        """检查 ``user_id`` 是否有权访问 ``thread_id``。

        两种模式——同一行、按调用方接下来要做什么区分语义：

        - ``require_existing=False``（默认，宽松）：对以下情况返回 True：行缺失
          （未追踪的 legacy 线程）、``row.user_id`` 为 None（共享 / 鉴权前数据）、
          或 ``row.user_id == user_id``。用于**读类**装饰器（把未追踪线程当成可访问
          以保后向兼容）。

        - ``require_existing=True``（严格）：仅当行存在**且**（``row.user_id == user_id``
          或 ``row.user_id is None``）时返回 True。用于**破坏性 / 变更**装饰器
          （DELETE / PATCH / 状态更新），使一个「已被删除」的线程无法被任何调用方
          重新定位——关闭 delete 幂等的跨用户缺口（行消失时别的用户会显得「拥有」它）。
        """
        async with self._sf() as session:
            row = await session.get(ThreadMetaRow, thread_id)
            if row is None:
                return not require_existing
            if row.user_id is None:
                return True
            return row.user_id == user_id

    async def search(
        self,
        *,
        metadata: dict[str, Any] | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
        user_id: str | None | _AutoSentinel = AUTO,
    ) -> list[dict[str, Any]]:
        """带可选 metadata 与 status 过滤的线程搜索。

        默认强制属主过滤：调用方必须在用户上下文里。传 ``user_id=None`` 绕过（迁移 / CLI）。
        """
        resolved_user_id = resolve_user_id(user_id, method_name="ThreadMetaRepository.search")
        stmt = select(ThreadMetaRow).order_by(ThreadMetaRow.updated_at.desc(), ThreadMetaRow.thread_id.desc())
        if resolved_user_id is not None:
            stmt = stmt.where(ThreadMetaRow.user_id == resolved_user_id)
        if status:
            stmt = stmt.where(ThreadMetaRow.status == status)

        if metadata:
            applied = 0
            for key, value in metadata.items():
                try:
                    stmt = stmt.where(json_match(ThreadMetaRow.metadata_json, key, value))
                    applied += 1
                except (ValueError, TypeError) as exc:
                    logger.warning("Skipping metadata filter key %s: %s", ascii(key), exc)
            if applied == 0:
                # 逗号分隔的纯字符串（无 list repr / 嵌套引号），让 Gateway 抛出的
                # 400 detail 对客户端易读。排序以保确定性。
                rejected_keys = ", ".join(sorted(str(k) for k in metadata))
                raise InvalidMetadataFilterError(f"All metadata filter keys were rejected as unsafe: {rejected_keys}")

        stmt = stmt.limit(limit).offset(offset)
        async with self._sf() as session:
            result = await session.execute(stmt)
            return [self._row_to_dict(r) for r in result.scalars()]

    async def _check_ownership(self, session: AsyncSession, thread_id: str, resolved_user_id: str | None) -> bool:
        """行存在且属主匹配（或过滤被绕过）时返回 True。"""
        if resolved_user_id is None:
            return True  # 显式绕过
        row = await session.get(ThreadMetaRow, thread_id)
        return row is not None and row.user_id == resolved_user_id

    async def update_display_name(
        self,
        thread_id: str,
        display_name: str,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
    ) -> None:
        """更新线程的 display_name（标题）。"""
        resolved_user_id = resolve_user_id(user_id, method_name="ThreadMetaRepository.update_display_name")
        async with self._sf() as session:
            if not await self._check_ownership(session, thread_id, resolved_user_id):
                return
            await session.execute(update(ThreadMetaRow).where(ThreadMetaRow.thread_id == thread_id).values(display_name=display_name, updated_at=datetime.now(UTC)))
            await session.commit()

    async def update_status(
        self,
        thread_id: str,
        status: str,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
    ) -> None:
        resolved_user_id = resolve_user_id(user_id, method_name="ThreadMetaRepository.update_status")
        async with self._sf() as session:
            if not await self._check_ownership(session, thread_id, resolved_user_id):
                return
            await session.execute(update(ThreadMetaRow).where(ThreadMetaRow.thread_id == thread_id).values(status=status, updated_at=datetime.now(UTC)))
            await session.commit()

    async def update_metadata(
        self,
        thread_id: str,
        metadata: dict,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
    ) -> None:
        """把 ``metadata`` 合并进 ``metadata_json``。

        在单个 session/事务内 read-modify-write，保证并发调用方看到一致状态。
        行不存在或 user_id 校验失败时 no-op。
        """
        resolved_user_id = resolve_user_id(user_id, method_name="ThreadMetaRepository.update_metadata")
        async with self._sf() as session:
            row = await session.get(ThreadMetaRow, thread_id)
            if row is None:
                return
            if resolved_user_id is not None and row.user_id != resolved_user_id:
                return
            merged = dict(row.metadata_json or {})
            merged.update(metadata)
            row.metadata_json = merged
            row.updated_at = datetime.now(UTC)
            await session.commit()

    async def update_owner(
        self,
        thread_id: str,
        owner_user_id: str,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
    ) -> None:
        """把线程元数据行移到 ``owner_user_id``。"""
        resolved_user_id = resolve_user_id(user_id, method_name="ThreadMetaRepository.update_owner")
        async with self._sf() as session:
            if not await self._check_ownership(session, thread_id, resolved_user_id):
                return
            await session.execute(update(ThreadMetaRow).where(ThreadMetaRow.thread_id == thread_id).values(user_id=owner_user_id, updated_at=datetime.now(UTC)))
            await session.commit()

    async def delete(
        self,
        thread_id: str,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
    ) -> None:
        resolved_user_id = resolve_user_id(user_id, method_name="ThreadMetaRepository.delete")
        async with self._sf() as session:
            row = await session.get(ThreadMetaRow, thread_id)
            if row is None:
                return
            if resolved_user_id is not None and row.user_id != resolved_user_id:
                return
            await session.delete(row)
            await session.commit()
