"""运行时集成装配（lifespan）——把所有运行时单件串成一个可跑的 bundle。

mini 没有 Gateway（app/）层，所以本模块是**框架级**的装配上下文管理器：把 checkpointer /
stream_bridge / event_store / thread_store / run_manager / store 按正确顺序建好、关停时按正确
顺序 drain（对齐 ALIGNMENT_OUTLINE Part D.2）。

用法（CLI 入口 / 测试 / 未来 Gateway 都可复用）::

    from deerflow.runtime.lifespan import runtime_lifespan

    async with runtime_lifespan() as bundle:
        # bundle.checkpointer / bundle.stream_bridge / bundle.run_manager / bundle.store ...
        record = await bundle.run_manager.create("thread-1")
        await run_agent(bridge, bundle.run_manager, record, ctx=RunContext(...), ...)

关键顺序（红线 #6 / #3373）：

1. 进：init_engine → make_checkpointer + make_stream_bridge + make_store（并行 async CM）→
   build event_store / thread_store / run_store → RunManager(store=run_store) → reconcile orphan；
2. 出：先 ``run_manager.shutdown(timeout=5)`` drain 在途 run（**必须在关 checkpointer 前**，
   否则 langgraph 内部 task 对已关连接池写 checkpoint → PoolClosed 未处理异常）→ 再关各 CM →
   close_engine。
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass
from typing import Any, AsyncIterator

from deerflow.config.app_config import AppConfig, get_app_config
from deerflow.runtime.events import make_run_event_store
from deerflow.runtime.runs import MemoryRunStore, RunManager
from deerflow.runtime.runs.worker import RunContext

logger = logging.getLogger(__name__)


@dataclass
class RuntimeBundle:
    """一次运行时装配的产物——所有运行时单件打包。

    给 ``run_agent`` / CLI / 未来 Gateway 复用。``run_context`` 是把 checkpointer / store /
    event_store / thread_store 打包好的 :class:`RunContext`，可直接喂给 worker。
    """

    checkpointer: Any
    stream_bridge: Any
    store: Any | None
    run_manager: RunManager
    event_store: Any | None
    thread_store: Any | None
    run_events_config: Any | None
    app_config: AppConfig
    run_context: RunContext


@contextlib.asynccontextmanager
async def runtime_lifespan(app_config: AppConfig | None = None) -> AsyncIterator[RuntimeBundle]:
    """装配整个运行时，yield :class:`RuntimeBundle`，退出时按序 drain + 清理。

    Args:
        app_config: 显式配置；None 读全局 ``get_app_config()``。

    Yields:
        装好的 :class:`RuntimeBundle`。
    """
    if app_config is None:
        app_config = get_app_config()

    # --- 进：按依赖序建单件 ---
    # engine 先建（SQL 后端需要它给 session factory；memory → no-op）
    from deerflow.persistence.engine import close_engine, init_engine_from_config

    await init_engine_from_config(app_config.database)

    # checkpointer / stream_bridge / store 是 async CM，并行起、并行关
    async with contextlib.AsyncExitStack() as stack:
        from deerflow.runtime.checkpointer.async_provider import make_checkpointer
        from deerflow.runtime.store.async_provider import make_store
        from deerflow.runtime.stream_bridge.async_provider import make_stream_bridge

        checkpointer = await stack.enter_async_context(make_checkpointer(app_config))
        stream_bridge = await stack.enter_async_context(make_stream_bridge(app_config))
        store = await stack.enter_async_context(make_store(app_config))

        # run_store：SQL 后端用 RunRepository，memory 后端用 MemoryRunStore
        run_store = await _build_run_store(app_config)
        # event_store / thread_store：按 config + session factory 挑实现
        run_events_config = getattr(app_config, "run_events", None)
        event_store = make_run_event_store(run_events_config)
        thread_store = await _build_thread_store(app_config, store)

        run_manager = RunManager(store=run_store)

        # 启动恢复：把持久化了但本 worker 无 task 的 inflight run 标 error（红线 #7）
        try:
            await run_manager.reconcile_orphaned_inflight_runs(error="Worker restarted")
        except Exception:
            logger.warning("Failed to reconcile orphaned inflight runs on startup", exc_info=True)

        run_context = RunContext(
            checkpointer=checkpointer,
            store=store,
            event_store=event_store,
            run_events_config=run_events_config,
            thread_store=thread_store,
            app_config=app_config,
        )

        bundle = RuntimeBundle(
            checkpointer=checkpointer,
            stream_bridge=stream_bridge,
            store=store,
            run_manager=run_manager,
            event_store=event_store,
            thread_store=thread_store,
            run_events_config=run_events_config,
            app_config=app_config,
            run_context=run_context,
        )

        logger.info(
            "Runtime bundle ready: checkpointer=%s stream_bridge=%s store=%s run_manager=RunManager(store=%s)",
            type(checkpointer).__name__,
            type(stream_bridge).__name__,
            type(store).__name__ if store is not None else None,
            type(run_store).__name__,
        )

        try:
            yield bundle
        finally:
            # --- 出：先 drain 在途 run，再关 checkpointer（红线 #6 / #3373）---
            try:
                await run_manager.shutdown(timeout=5.0)
            except Exception:
                logger.warning("Error during run_manager shutdown drain", exc_info=True)

    # AsyncExitStack 退出时会关 checkpointer / stream_bridge / store
    # 最后关 engine
    try:
        await close_engine()
    except Exception:
        logger.warning("Error during engine close", exc_info=True)


async def _build_run_store(app_config: AppConfig) -> Any:
    """挑 run 元数据存储：SQL 后端用 RunRepository，memory 后端用 MemoryRunStore。

    给了 session factory（SQL）→ 持久化 RunRepository；否则 → 内存 MemoryRunStore（重启丢失）。
    """
    from deerflow.persistence.engine import get_session_factory

    sf = get_session_factory()
    if sf is not None:
        try:
            from deerflow.persistence.run.sql import RunRepository

            return RunRepository(sf)
        except Exception:
            logger.warning("Failed to build RunRepository; falling back to MemoryRunStore", exc_info=True)
    return MemoryRunStore()


async def _build_thread_store(app_config: AppConfig, store: Any | None) -> Any | None:
    """挑 thread 元数据存储：SQL 后端用 ThreadMetaRepository，否则用 MemoryThreadMetaStore。

    无 session factory 又无 store 时返回 None（worker 的标题 / 状态回写会跳过）。
    """
    from deerflow.persistence.engine import get_session_factory
    from deerflow.persistence.thread_meta import make_thread_store

    sf = get_session_factory()
    try:
        return make_thread_store(sf, store)
    except ValueError:
        # 两者都没有——worker 的 thread_store 操作是 best-effort（非致命），返 None 跳过
        logger.info("No session_factory or store available; thread_meta updates will be skipped")
        return None
