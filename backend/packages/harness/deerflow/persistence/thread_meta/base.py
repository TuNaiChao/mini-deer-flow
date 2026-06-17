"""线程元数据存储的抽象接口（ThreadMetaStore）。

实现：
- :class:`ThreadMetaRepository`：SQL 后端（sqlite / postgres，经 SQLAlchemy）。
- :class:`MemoryThreadMetaStore`：包一层 LangGraph BaseStore（memory 模式）。

所有变更与查询方法接受 ``user_id`` 形参，三态语义见
:mod:`deerflow.runtime.user_context`：

- ``AUTO``（默认）：从请求级 contextvar 解析。
- 显式 ``str``：用给定值原样。
- 显式 ``None``：绕过属主过滤（仅迁移 / CLI）。
"""

from __future__ import annotations

import abc
from typing import Any

from deerflow.runtime.user_context import AUTO, _AutoSentinel


class InvalidMetadataFilterError(ValueError):
    """当所有客户端提供的 metadata 过滤键都被拒绝时抛出。"""


class ThreadMetaStore(abc.ABC):
    @abc.abstractmethod
    async def create(
        self,
        thread_id: str,
        *,
        assistant_id: str | None = None,
        user_id: str | None | _AutoSentinel = AUTO,
        display_name: str | None = None,
        metadata: dict | None = None,
    ) -> dict:
        pass

    @abc.abstractmethod
    async def get(self, thread_id: str, *, user_id: str | None | _AutoSentinel = AUTO) -> dict | None:
        pass

    @abc.abstractmethod
    async def search(
        self,
        *,
        metadata: dict[str, Any] | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
        user_id: str | None | _AutoSentinel = AUTO,
    ) -> list[dict[str, Any]]:
        pass

    @abc.abstractmethod
    async def update_display_name(self, thread_id: str, display_name: str, *, user_id: str | None | _AutoSentinel = AUTO) -> None:
        pass

    @abc.abstractmethod
    async def update_status(self, thread_id: str, status: str, *, user_id: str | None | _AutoSentinel = AUTO) -> None:
        pass

    @abc.abstractmethod
    async def update_metadata(self, thread_id: str, metadata: dict, *, user_id: str | None | _AutoSentinel = AUTO) -> None:
        """把 ``metadata`` 合并进线程的 metadata 字段。

        已有键被新值覆盖；``metadata`` 中没有的键保留。线程不存在或属主校验失败时
        为 no-op。
        """
        pass

    @abc.abstractmethod
    async def update_owner(self, thread_id: str, owner_user_id: str, *, user_id: str | None | _AutoSentinel = AUTO) -> None:
        """把线程元数据行移到新属主。

        仅供受信任的内部修复 / 迁移路径用。行不存在或调用方未通过属主校验时 no-op。
        """
        pass

    @abc.abstractmethod
    async def check_access(self, thread_id: str, user_id: str, *, require_existing: bool = False) -> bool:
        """检查 ``user_id`` 是否有权访问 ``thread_id``。"""
        pass

    @abc.abstractmethod
    async def delete(self, thread_id: str, *, user_id: str | None | _AutoSentinel = AUTO) -> None:
        pass
