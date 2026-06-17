"""run 事件存储的抽象接口（RunEventStore）。

RunEventStore 是 run 事件流的统一存储接口。消息（前端展示）与执行轨迹
（调试 / 审计）走同一接口，靠 ``category`` 字段区分。

实现：
- :class:`MemoryRunEventStore`：内存字典（开发 / 测试）。
- :class:`JsonlRunEventStore`：追加写 JSONL 文件（单节点轻量持久化）。
- :class:`DbRunEventStore`：SQLAlchemy ORM（生产，完整查询能力）。

所有实现必须保证：
1. ``put()`` 的事件在后续查询里能取到。
2. **同一 thread 内 seq 严格递增**（红线 #3）。
3. ``list_messages()`` 只返回 ``category="message"`` 的事件。
4. ``list_events()`` 返回指定 run 的全部事件。
5. 返回的 dict 符合 RunEvent 字段结构。
"""

from __future__ import annotations

import abc


class RunEventStore(abc.ABC):
    @abc.abstractmethod
    async def put(
        self,
        *,
        thread_id: str,
        run_id: str,
        event_type: str,
        category: str,
        content: str | dict = "",
        metadata: dict | None = None,
        created_at: str | None = None,
    ) -> dict:
        """写一条事件，自动分配 seq，返回完整记录。"""

    @abc.abstractmethod
    async def put_batch(self, events: list[dict]) -> list[dict]:
        """批量写事件。给 RunJournal flush buffer 用。

        每个 dict 的键与 :meth:`put` 的关键字参数一致。返回带 seq 的完整记录列表。
        """

    @abc.abstractmethod
    async def list_messages(
        self,
        thread_id: str,
        *,
        limit: int = 50,
        before_seq: int | None = None,
        after_seq: int | None = None,
    ) -> list[dict]:
        """返回一个 thread 的可展示消息（category=message），按 seq 升序。

        支持双向游标分页：
        - before_seq：返回 seq < before_seq 的最后 ``limit`` 条（升序）。
        - after_seq：返回 seq > after_seq 的前 ``limit`` 条（升序）。
        - 都不给：返回最近 ``limit`` 条（升序）。
        """

    @abc.abstractmethod
    async def list_events(
        self,
        thread_id: str,
        run_id: str,
        *,
        event_types: list[str] | None = None,
        limit: int = 500,
    ) -> list[dict]:
        """返回一个 run 的完整事件流，按 seq 升序。

        可选按 event_types 过滤。
        """

    @abc.abstractmethod
    async def list_messages_by_run(
        self,
        thread_id: str,
        run_id: str,
        *,
        limit: int = 50,
        before_seq: int | None = None,
        after_seq: int | None = None,
    ) -> list[dict]:
        """返回某个 run 的可展示消息（category=message），按 seq 升序。

        支持双向游标分页（同 :meth:`list_messages`）。
        """

    @abc.abstractmethod
    async def count_messages(self, thread_id: str) -> int:
        """统计一个 thread 内的可展示消息（category=message）数。"""

    @abc.abstractmethod
    async def delete_by_thread(self, thread_id: str) -> int:
        """删除一个 thread 的全部事件。返回删除条数。"""

    @abc.abstractmethod
    async def delete_by_run(self, thread_id: str, run_id: str) -> int:
        """删除某个 run 的全部事件。返回删除条数。"""
