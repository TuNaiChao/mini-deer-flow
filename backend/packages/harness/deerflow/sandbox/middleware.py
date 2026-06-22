"""``SandboxMiddleware``：为 agent 分配并管理沙箱生命周期。

生命周期管理：
- ``lazy_init=True``（默认）：首次工具调用时才 acquire 沙箱（``ensure_sandbox_initialized``）。
- ``lazy_init=False``：首次 agent 调用（``before_agent``）就 acquire。
- 同一线程内跨多轮复用沙箱（不每轮释放，避免重建开销）。
- 清理在应用 shutdown 时由 ``SandboxProvider.shutdown()`` 统一做。

为什么要 ``wrap_tool_call`` 里那段 Command 合并逻辑？

``ensure_sandbox_initialized`` 直接改 ``runtime.state["sandbox"]``——但那是**当前工具调用
局部**的修改，**不会**被 LangGraph 的 channel reducer 捕获，所以后续图步（以及
``ToolOutputBudgetMiddleware``、子代理 ``task_tool`` 等下游消费者）看不到 sandbox_id。
包装工具调用让我们能在「调用前/后」比对 state 快照，发现「首次懒初始化」，再用
``Command(update=...)`` 把 ``sandbox.sandbox_id`` 正式写回图状态。

红线 #15：所有 ``wrap_tool_call`` 实现里调用 handler 若抛 ``GraphBubbleUp`` 必须让它
继续上抛——本中间件的 wrap 只做「检测 + 贴更新」，不吞异常。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace as dc_replace
from typing import NotRequired, override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.runtime import Runtime
from langgraph.types import Command

from deerflow.sandbox import get_sandbox_provider

logger = logging.getLogger(__name__)


class SandboxMiddlewareState(AgentState):
    """与 ``ThreadState`` 兼容的状态片段（sandbox / thread_data 都是普通 dict）。"""

    sandbox: NotRequired[dict | None]
    thread_data: NotRequired[dict | None]


class SandboxMiddleware(AgentMiddleware[SandboxMiddlewareState]):
    """创建沙箱并分配给 agent。"""

    state_schema = SandboxMiddlewareState

    def __init__(self, lazy_init: bool = True):
        """
        Args:
            lazy_init: True 则推迟到首次工具调用才 acquire；False 则在 before_agent 立即 acquire。
                默认 True（性能最优）。
        """
        super().__init__()
        self._lazy_init = lazy_init

    # ------------------------------------------------------------------
    # acquire / release
    # ------------------------------------------------------------------

    def _acquire_sandbox(self, thread_id: str) -> str:
        sandbox_id = get_sandbox_provider().acquire(thread_id)
        logger.info("Acquiring sandbox %s", sandbox_id)
        return sandbox_id

    async def _acquire_sandbox_async(self, thread_id: str) -> str:
        sandbox_id = await get_sandbox_provider().acquire_async(thread_id)
        logger.info("Acquiring sandbox %s", sandbox_id)
        return sandbox_id

    async def _release_sandbox_async(self, sandbox_id: str) -> None:
        await asyncio.to_thread(get_sandbox_provider().release, sandbox_id)

    # ------------------------------------------------------------------
    # before / after agent
    # ------------------------------------------------------------------

    @override
    def before_agent(self, state: SandboxMiddlewareState, runtime: Runtime) -> dict | None:
        if self._lazy_init:
            return super().before_agent(state, runtime)
        # 急切初始化（lazy_init=False）。
        if "sandbox" not in state or state["sandbox"] is None:
            thread_id = (runtime.context or {}).get("thread_id")
            if thread_id is None:
                return super().before_agent(state, runtime)
            sandbox_id = self._acquire_sandbox(thread_id)
            logger.info("Assigned sandbox %s to thread %s", sandbox_id, thread_id)
            return {"sandbox": {"sandbox_id": sandbox_id}}
        return super().before_agent(state, runtime)

    @override
    async def abefore_agent(self, state: SandboxMiddlewareState, runtime: Runtime) -> dict | None:
        if self._lazy_init:
            return await super().abefore_agent(state, runtime)
        # 急切初始化，但走 async provider hook，让阻塞的沙箱启动/轮询跑在线程外。
        if "sandbox" not in state or state["sandbox"] is None:
            thread_id = (runtime.context or {}).get("thread_id")
            if thread_id is None:
                return await super().abefore_agent(state, runtime)
            sandbox_id = await self._acquire_sandbox_async(thread_id)
            logger.info("Assigned sandbox %s to thread %s", sandbox_id, thread_id)
            return {"sandbox": {"sandbox_id": sandbox_id}}
        return await super().abefore_agent(state, runtime)

    @override
    def after_agent(self, state: SandboxMiddlewareState, runtime: Runtime) -> dict | None:
        sandbox = state.get("sandbox")
        if sandbox is not None:
            sandbox_id = sandbox["sandbox_id"]
            logger.info("Releasing sandbox %s", sandbox_id)
            get_sandbox_provider().release(sandbox_id)
            return None
        if (runtime.context or {}).get("sandbox_id") is not None:
            sandbox_id = runtime.context.get("sandbox_id")
            logger.info("Releasing sandbox %s from context", sandbox_id)
            get_sandbox_provider().release(sandbox_id)
            return None
        return super().after_agent(state, runtime)

    @override
    async def aafter_agent(self, state: SandboxMiddlewareState, runtime: Runtime) -> dict | None:
        sandbox = state.get("sandbox")
        if sandbox is not None:
            sandbox_id = sandbox["sandbox_id"]
            logger.info("Releasing sandbox %s", sandbox_id)
            await self._release_sandbox_async(sandbox_id)
            return None
        if (runtime.context or {}).get("sandbox_id") is not None:
            sandbox_id = runtime.context.get("sandbox_id")
            logger.info("Releasing sandbox %s from context", sandbox_id)
            await self._release_sandbox_async(sandbox_id)
            return None
        return await super().aafter_agent(state, runtime)

    # ------------------------------------------------------------------
    # wrap_tool_call：把懒初始化得到的 sandbox_id 贴回图状态
    # ------------------------------------------------------------------

    @staticmethod
    def _read_sandbox_id_from_state(state: object) -> str | None:
        if not isinstance(state, dict):
            return None
        sandbox_state = state.get("sandbox")
        if not isinstance(sandbox_state, dict):
            return None
        sandbox_id = sandbox_state.get("sandbox_id")
        return sandbox_id if isinstance(sandbox_id, str) else None

    @staticmethod
    def _attach_sandbox_update(result: ToolMessage | Command, sandbox_id: str) -> ToolMessage | Command:
        """把 ``result`` 包装/合并，使 ``sandbox.sandbox_id`` 被正式写回状态。

        - ``ToolMessage`` → ``Command(update={"sandbox": ..., "messages": [msg]})``。
        - ``Command``（dict update）→ 合并 ``sandbox`` 键，保留其余字段。
        - ``Command``（非 dict / None update）→ 原样返回，避免未知形状丢数据。
        """
        sandbox_update = {"sandbox": {"sandbox_id": sandbox_id}}

        if isinstance(result, ToolMessage):
            return Command(update={**sandbox_update, "messages": [result]})

        existing_update = result.update
        if isinstance(existing_update, dict):
            merged_update = {**existing_update, **sandbox_update}
            return dc_replace(result, update=merged_update)
        return result

    @staticmethod
    def _read_sandbox_id_from_request(request: ToolCallRequest) -> str | None:
        runtime = request.runtime
        if runtime is None or runtime.state is None:
            return None
        return SandboxMiddleware._read_sandbox_id_from_state(runtime.state)

    @override
    def wrap_tool_call(self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], ToolMessage | Command]) -> ToolMessage | Command:
        prev_sandbox_id = self._read_sandbox_id_from_request(request)
        result = handler(request)
        if prev_sandbox_id is not None:
            return result
        curr_sandbox_id = self._read_sandbox_id_from_request(request)
        if curr_sandbox_id is None:
            return result
        return self._attach_sandbox_update(result, curr_sandbox_id)

    @override
    async def awrap_tool_call(self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]]) -> ToolMessage | Command:
        prev_sandbox_id = self._read_sandbox_id_from_request(request)
        result = await handler(request)
        if prev_sandbox_id is not None:
            return result
        curr_sandbox_id = self._read_sandbox_id_from_request(request)
        if curr_sandbox_id is None:
            return result
        return self._attach_sandbox_update(result, curr_sandbox_id)
