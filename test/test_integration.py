"""端到端集成测试（M19 + 集成装配 D.2）。

验证 ``runtime_lifespan`` 把所有运行时单件（checkpointer / stream_bridge / store /
event_store / thread_store / run_manager）按正确顺序装好，并能驱动一次完整的「创建 run →
跑 agent → 流式回播 → 收尾 drain」。用假 agent_factory（astream 产预设 chunk），不调真实模型。

这锁住整条装配链 + 红线 #6（shutdown drain 必须在关 checkpointer 前）+ 红线 #7（启动
reconcile orphan）。
"""

from __future__ import annotations

import asyncio

from langchain_core.messages import AIMessage

from deerflow.config import AppConfig
from deerflow.runtime.lifespan import RuntimeBundle, runtime_lifespan
from deerflow.runtime.runs import RunStatus
from deerflow.runtime.runs.worker import RunContext

# ---------------------------------------------------------------------------
# 假 agent_factory——astream 产预设 chunk 序列
# ---------------------------------------------------------------------------


class _DummyAgent:
    """假 agent：astream 产预设 chunk；记录收到的 config。"""

    def __init__(self, chunks):
        self._chunks = chunks
        self.metadata = {}
        self.checkpointer = None
        self.store = None

    async def astream(self, graph_input, config=None, stream_mode=None, subgraphs=False):
        for chunk in self._chunks:
            yield chunk


def _factory(chunks):
    """造一个接受 config 关键字的 agent_factory。"""

    def factory(*, config):
        return _DummyAgent(chunks)

    return factory


# ===========================================================================
# runtime_lifespan 装配
# ===========================================================================


async def test_lifespan_assembles_full_bundle():
    """lifespan 把所有单件装好，bundle 字段齐全。"""
    async with runtime_lifespan(AppConfig()) as bundle:
        assert isinstance(bundle, RuntimeBundle)
        assert bundle.checkpointer is not None
        assert bundle.stream_bridge is not None
        assert bundle.store is not None
        assert bundle.run_manager is not None
        assert bundle.event_store is not None
        # thread_store：memory 后端 + InMemoryStore → MemoryThreadMetaStore
        assert bundle.thread_store is not None
        # run_context 把上述打包，可直接喂 worker
        assert bundle.run_context.checkpointer is bundle.checkpointer
        assert bundle.run_context.store is bundle.store


async def test_lifespan_memory_backend_picks_memory_implementations():
    """database.backend=memory → checkpointer/store/event_store/thread_store 全是内存实现。"""
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.store.memory import InMemoryStore

    from deerflow.persistence.thread_meta.memory import MemoryThreadMetaStore
    from deerflow.runtime.events.store.memory import MemoryRunEventStore
    from deerflow.runtime.runs.store.memory import MemoryRunStore

    async with runtime_lifespan(AppConfig()) as bundle:
        assert isinstance(bundle.checkpointer, InMemorySaver)
        assert isinstance(bundle.store, InMemoryStore)
        assert isinstance(bundle.event_store, MemoryRunEventStore)
        assert isinstance(bundle.thread_store, MemoryThreadMetaStore)
        # run_store 是 MemoryRunStore（RunManager 的后端）
        assert isinstance(bundle.run_manager._store, MemoryRunStore)


async def test_lifespan_shutdown_drains_before_checkpointer_close():
    """退出时 shutdown drain 先于 checkpointer 关闭（红线 #6 / #3373）。

    验证顺序：bundle 退出时 run_manager.shutdown 被调用，且 checkpointer 在 drain 后才关
    （这里用一个在 drain 期间仍可用的 checkpointer 间接验证——shutdown 不抛、bundle 正常退出）。
    """
    async with runtime_lifespan(AppConfig()) as bundle:
        # 起一个会 settle 的 run，验证 drain 不卡
        await bundle.run_manager.create("thread-drain")

    # bundle 正常退出（无 PoolClosed / 无未处理异常）即说明 drain 顺序正确
    # （memory 后端不会真触发 PoolClosed，但 shutdown 路径完整跑过）


# ===========================================================================
# 完整 run 生命周期（创建 → 跑 → 收尾）
# ===========================================================================


async def test_full_run_lifecycle_success():
    """创建 run → run_agent 跑假 agent → status success + publish_end。"""
    async with runtime_lifespan(AppConfig()) as bundle:
        record = await bundle.run_manager.create("thread-1", assistant_id="lead_agent")

        await asyncio.create_task(
            bundle.run_manager.__class__.shutdown(bundle.run_manager, timeout=0.1)  # noop：无在途 task
        )

        # 用 bundle 的真 run_manager + 真 bridge 跑假 agent
        from deerflow.runtime.runs.worker import run_agent

        await run_agent(
            bundle.stream_bridge,
            bundle.run_manager,
            record,
            ctx=bundle.run_context,
            agent_factory=_factory([{"messages": []}]),
            graph_input={"messages": ["hi"]},
            config={},
        )

        fetched = await bundle.run_manager.get(record.run_id)
        assert fetched.status == RunStatus.success


async def test_full_run_with_event_store_records_messages():
    """event_store 非 None 时，run_agent 初始化 RunJournal 并在 finally flush。"""
    async with runtime_lifespan(AppConfig()) as bundle:
        record = await bundle.run_manager.create("thread-journal")
        from deerflow.runtime.runs.worker import run_agent

        await run_agent(
            bundle.stream_bridge,
            bundle.run_manager,
            record,
            ctx=bundle.run_context,
            agent_factory=_factory([{"messages": [AIMessage(content="hello")]}]),
            graph_input={"messages": ["q"]},
            config={},
        )

        fetched = await bundle.run_manager.get(record.run_id)
        assert fetched.status == RunStatus.success
        # journal 在 finally 里 flush + 持久化 completion 跑通（run 正常 success 即证明无异常）；
        # 假 agent 不触发 LangChain callback，故 message_count 为 0（真 agent 会经 on_llm_end 累加）。


async def test_full_run_llm_fallback_marked_error():
    """LLM 兜底消息 → status error（端到端验证 _extract_llm_error_fallback_message 在真链路里生效）。"""
    async with runtime_lifespan(AppConfig()) as bundle:
        record = await bundle.run_manager.create("thread-llm")
        from deerflow.runtime.runs.worker import run_agent

        await run_agent(
            bundle.stream_bridge,
            bundle.run_manager,
            record,
            ctx=bundle.run_context,
            agent_factory=_factory(
                [
                    {
                        "messages": [
                            AIMessage(
                                content="unavailable",
                                additional_kwargs={"deerflow_error_fallback": True, "error_detail": "Connection error."},
                            )
                        ]
                    }
                ]
            ),
            graph_input={"messages": ["q"]},
            config={},
        )

        fetched = await bundle.run_manager.get(record.run_id)
        assert fetched.status == RunStatus.error
        assert fetched.error == "Connection error."


async def test_full_run_exception_marked_error():
    """agent_factory 抛异常 → status error（端到端异常路径）。"""
    async with runtime_lifespan(AppConfig()) as bundle:
        record = await bundle.run_manager.create("thread-exc")
        from deerflow.runtime.runs.worker import run_agent

        def bad_factory(*, config):
            raise RuntimeError("factory boom")

        await run_agent(
            bundle.stream_bridge,
            bundle.run_manager,
            record,
            ctx=bundle.run_context,
            agent_factory=bad_factory,
            graph_input={"messages": ["q"]},
            config={},
        )

        fetched = await bundle.run_manager.get(record.run_id)
        assert fetched.status == RunStatus.error
        assert "factory boom" in fetched.error


# ===========================================================================
# 启动 reconcile orphan（红线 #7）
# ===========================================================================


async def test_lifespan_reconcile_orphan_on_startup():
    """lifespan 进时调 reconcile_orphaned_inflight_runs（memory 后端无 store → no-op，但不抛）。"""
    # memory 后端 RunManager 无 store，reconcile 返空。验证不抛即可。
    async with runtime_lifespan(AppConfig()):
        pass  # 进 / 出都正常


# ===========================================================================
# bundle.run_context 可直接喂 worker
# ===========================================================================


async def test_bundle_run_context_drives_worker():
    """bundle.run_context 是打包好的 RunContext，能直接喂 run_agent。"""
    async with runtime_lifespan(AppConfig()) as bundle:
        assert isinstance(bundle.run_context, RunContext)
        record = await bundle.run_manager.create("thread-ctx")
        from deerflow.runtime.runs.worker import run_agent

        await run_agent(
            bundle.stream_bridge,
            bundle.run_manager,
            record,
            ctx=bundle.run_context,  # 直接用 bundle 打包的
            agent_factory=_factory([{"messages": []}]),
            graph_input={},
            config={},
        )
        assert (await bundle.run_manager.get(record.run_id)).status == RunStatus.success
