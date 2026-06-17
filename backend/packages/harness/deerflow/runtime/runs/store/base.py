"""run 元数据存储的抽象接口（RunStore ABC）。

RunManager（Phase 8）依赖本接口，不依赖具体实现。已知实现：

- MemoryRunStore：内存字典（开发 / 测试，Phase 8 落地）。
- RunRepository：SQLAlchemy ORM 实现（本 Phase 1 在
  ``deerflow.persistence.run.sql`` 落地），继承本 ABC。

本接口提前到 Phase 1 的原因：``RunRepository(RunStore)`` 要继承它，而
RunRepository 是持久化地基的一部分；若把 ABC 留到 Phase 8 的 runs 模块，
会产生「持久化 → 运行管理 → 持久化」的循环依赖。

所有方法接受可选的 ``user_id`` 用于用户隔离。``user_id=None`` 时不加用户过滤
（单用户 / 迁移场景）。

返回值约定（红线 #12）：
``update_status`` / ``update_run_completion`` 返回 ``False`` 表示「能证明没有行
被更新」；旧版或轻量实现可能返回 ``None``（无法报告 rowcount）。调用方据此判断
是否需要从内存快照重建行。
"""

from __future__ import annotations

import abc
from typing import Any


class RunStore(abc.ABC):
    @abc.abstractmethod
    async def put(
        self,
        run_id: str,
        *,
        thread_id: str,
        assistant_id: str | None = None,
        user_id: str | None = None,
        model_name: str | None = None,
        status: str = "pending",
        multitask_strategy: str = "reject",
        metadata: dict[str, Any] | None = None,
        kwargs: dict[str, Any] | None = None,
        error: str | None = None,
        created_at: str | None = None,
    ) -> None:
        pass

    @abc.abstractmethod
    async def get(
        self,
        run_id: str,
        *,
        user_id: str | None = None,
    ) -> dict[str, Any] | None:
        pass

    @abc.abstractmethod
    async def list_by_thread(
        self,
        thread_id: str,
        *,
        user_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        pass

    @abc.abstractmethod
    async def update_status(
        self,
        run_id: str,
        status: str,
        *,
        error: str | None = None,
    ) -> bool | None:
        """更新 run 状态。

        返回 ``False`` 表示「能证明没有行被更新」。旧版或轻量存储可能返回
        ``None``（无法报告 rowcount）。
        """
        pass

    @abc.abstractmethod
    async def delete(self, run_id: str) -> None:
        pass

    @abc.abstractmethod
    async def update_model_name(
        self,
        run_id: str,
        model_name: str | None,
    ) -> None:
        """更新已存在 run 的 model_name 字段。"""
        pass

    @abc.abstractmethod
    async def update_run_completion(
        self,
        run_id: str,
        *,
        status: str,
        total_input_tokens: int = 0,
        total_output_tokens: int = 0,
        total_tokens: int = 0,
        llm_call_count: int = 0,
        lead_agent_tokens: int = 0,
        subagent_tokens: int = 0,
        middleware_tokens: int = 0,
        message_count: int = 0,
        last_ai_message: str | None = None,
        first_human_message: str | None = None,
        error: str | None = None,
    ) -> bool | None:
        """持久化 run 完成时的最终字段。

        返回 ``False`` 表示「能证明没有行被更新」。
        """
        pass

    async def update_run_progress(
        self,
        run_id: str,
        *,
        total_input_tokens: int | None = None,
        total_output_tokens: int | None = None,
        total_tokens: int | None = None,
        llm_call_count: int | None = None,
        lead_agent_tokens: int | None = None,
        subagent_tokens: int | None = None,
        middleware_tokens: int | None = None,
        message_count: int | None = None,
        last_ai_message: str | None = None,
        first_human_message: str | None = None,
    ) -> None:
        """尽力写入运行中的快照（best-effort），不改 run 状态。

        非抽象：默认实现是 no-op。SQL 实现覆盖它做增量更新；内存实现可不做。
        """
        return None

    @abc.abstractmethod
    async def list_pending(self, *, before: str | None = None) -> list[dict[str, Any]]:
        pass

    @abc.abstractmethod
    async def list_inflight(self, *, before: str | None = None) -> list[dict[str, Any]]:
        """返回仍处于 ``pending`` 或 ``running`` 的已持久化 run。"""
        pass

    @abc.abstractmethod
    async def aggregate_tokens_by_thread(self, thread_id: str, *, include_active: bool = False) -> dict[str, Any]:
        """聚合某 thread 内已完成 run 的 token 用量。

        返回 dict，键：total_tokens / total_input_tokens / total_output_tokens /
        total_runs / by_model（model_name → {tokens, runs}）/ by_caller
        （{lead_agent, subagent, middleware}）。
        """
        pass
