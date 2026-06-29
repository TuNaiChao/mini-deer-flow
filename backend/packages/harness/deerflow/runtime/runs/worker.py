"""后台 agent 执行。

在 ``asyncio.Task`` 里跑一个 agent 图，边产事件边往 :class:`StreamBridge` 发。

用 ``graph.astream(stream_mode=[...])``：``values`` 模式拿到正确的全状态快照、``updates``
拿到正确的 ``{node: writes}``、``messages`` 模式拿到 ``(chunk, metadata)`` 元组。

注意：``events`` 模式不经 gateway 支持——它要 ``graph.astream_events()``，没法同时产
``values`` 快照。JS 开源 LangGraph API server 靠 Python 公开 API 没暴露的内部 checkpoint
回调绕过这个限制。

worker 的核心职责（对齐 deer-flow，红线见 ALIGNMENT_OUTLINE Part E）：

- 注入 ``__pregel_runtime``（langgraph 1.1+ 的 ``runtime.context``）+ ``__run_journal``（中间件
  写审计事件用，如 SafetyFinishReasonMiddleware）；
- run 前捕获 checkpoint 全量快照（含 pending_writes 深拷贝，红线 #5），供 rollback 还原；
- abort：``abort_event`` 在 astream 迭代边界检查，配合 ``record.abort_action``（interrupt /
  rollback）决定终态；
- LLM 兜底：扫流式 chunk 里的 ``deerflow_error_fallback`` 标记（模型调用中间件的兜底 AIMessage
  不一定过 LLM end 回调，但会出现在图状态 chunk 里）；
- 标题回写 thread_meta、bridge cleanup、journal flush + 完成数据持久化。
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import logging
import os
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Literal, cast

from langgraph.checkpoint.base import empty_checkpoint

from deerflow.config.app_config import AppConfig
from deerflow.runtime.serialization import serialize
from deerflow.runtime.stream_bridge import StreamBridge
from deerflow.runtime.user_context import get_effective_user_id
from deerflow.tracing import inject_langfuse_metadata

from .manager import RunManager, RunRecord
from .naming import resolve_root_run_name
from .schemas import RunStatus

logger = logging.getLogger(__name__)

# LangGraph graph.astream() 的合法 stream_mode 值
_VALID_LG_MODES = {"values", "updates", "checkpoints", "tasks", "debug", "messages", "custom"}


def _build_runtime_context(
    thread_id: str,
    run_id: str,
    caller_context: Any | None,
    app_config: AppConfig | None = None,
) -> dict[str, Any]:
    """构建成为本次 run ``ToolRuntime.context`` 的 dict。

    恒含 ``thread_id`` 和 ``run_id``。调用方 ``config['context']`` 里的额外键（如 bootstrap 流程的
    ``agent_name``，issue #2677）合并进来，但**绝不覆盖** ``thread_id``/``run_id``。解析后的
    ``AppConfig`` 由 worker 加进去，让工具不必做 ambient 全局查找就能消费。

    langgraph 1.1+ 经存于 ``config['configurable']['__pregel_runtime']`` 的 parent runtime 把它以
    ``runtime.context`` 暴露——见 ``langgraph.pregel.main`` 里 ``parent_runtime.merge(...)`` 的调用。
    """
    runtime_ctx: dict[str, Any] = {"thread_id": thread_id, "run_id": run_id}
    if isinstance(caller_context, dict):
        for key, value in caller_context.items():
            runtime_ctx.setdefault(key, value)
    if app_config is not None:
        runtime_ctx["app_config"] = app_config
    return runtime_ctx


@dataclass(frozen=True)
class RunContext:
    """单次 agent run 的基础设施依赖。

    把 checkpointer / store / 持久化相关单件打包，让 ``run_agent``（及未来调用方）收到一个对象
    而非一长串 keyword 参数。
    """

    checkpointer: Any
    store: Any | None = field(default=None)
    event_store: Any | None = field(default=None)
    run_events_config: Any | None = field(default=None)
    thread_store: Any | None = field(default=None)
    app_config: AppConfig | None = field(default=None)


def _install_runtime_context(config: dict, runtime_context: dict[str, Any]) -> None:
    existing_context = config.get("context")
    if isinstance(existing_context, dict):
        existing_context.setdefault("thread_id", runtime_context["thread_id"])
        existing_context.setdefault("run_id", runtime_context["run_id"])
        if "app_config" in runtime_context:
            existing_context["app_config"] = runtime_context["app_config"]
        return

    config["context"] = dict(runtime_context)


def _compute_agent_factory_supports_app_config(agent_factory: Any) -> bool:
    try:
        return "app_config" in inspect.signature(agent_factory).parameters
    except (TypeError, ValueError):
        return False


@lru_cache(maxsize=128)
def _cached_agent_factory_supports_app_config(agent_factory: Any) -> bool:
    return _compute_agent_factory_supports_app_config(agent_factory)


def _agent_factory_supports_app_config(agent_factory: Any) -> bool:
    try:
        return _cached_agent_factory_supports_app_config(agent_factory)
    except TypeError:
        # 某些 callable 实例不可哈希；退回直接检查。
        return _compute_agent_factory_supports_app_config(agent_factory)


async def run_agent(
    bridge: StreamBridge,
    run_manager: RunManager,
    record: RunRecord,
    *,
    ctx: RunContext,
    agent_factory: Any,
    graph_input: dict,
    config: dict,
    stream_modes: list[str] | None = None,
    stream_subgraphs: bool = False,
    interrupt_before: list[str] | Literal["*"] | None = None,
    interrupt_after: list[str] | Literal["*"] | None = None,
) -> None:
    """后台执行一个 agent，边产事件边发到 *bridge*。"""

    # 从 RunContext 拆出基础设施依赖。
    checkpointer = ctx.checkpointer
    store = ctx.store
    event_store = ctx.event_store
    run_events_config = ctx.run_events_config
    thread_store = ctx.thread_store

    run_id = record.run_id
    thread_id = record.thread_id
    requested_modes: set[str] = set(stream_modes or ["values"])
    pre_run_checkpoint_id: str | None = None
    pre_run_snapshot: dict[str, Any] | None = None
    snapshot_capture_failed = False
    llm_error_fallback_message: str | None = None

    journal = None

    # 记下「events 被请求但跳过」
    if "events" in requested_modes:
        logger.info(
            "Run %s: 'events' streamMode not supported in gateway (requires astream_events + checkpoint callbacks). Skipping.",
            run_id,
        )

    try:
        # 初始化 RunJournal + 写 human_message 事件。
        # 放 try 里，这样任何异常（如写事件的 DB 错）都走 except/finally 路径，往 SSE bridge 发
        # 一个 "end" 事件——否则这里的失败会让流永远挂着没收尾。
        if event_store is not None:
            from deerflow.runtime.journal import RunJournal

            journal = RunJournal(
                run_id=run_id,
                thread_id=thread_id,
                event_store=event_store,
                track_token_usage=getattr(run_events_config, "track_token_usage", True),
                progress_reporter=lambda snapshot: run_manager.update_run_progress(run_id, **snapshot),
            )

        # 1. 标 running
        await run_manager.set_status(run_id, RunStatus.running)

        # 快照最近的 pre-run checkpoint，供 rollback 还原。
        if checkpointer is not None:
            try:
                config_for_check = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
                ckpt_tuple = await checkpointer.aget_tuple(config_for_check)
                if ckpt_tuple is not None:
                    ckpt_config = getattr(ckpt_tuple, "config", {}).get("configurable", {})
                    pre_run_checkpoint_id = ckpt_config.get("checkpoint_id")
                    pre_run_snapshot = {
                        "checkpoint_ns": ckpt_config.get("checkpoint_ns", ""),
                        "checkpoint": copy.deepcopy(getattr(ckpt_tuple, "checkpoint", {})),
                        "metadata": copy.deepcopy(getattr(ckpt_tuple, "metadata", {})),
                        "pending_writes": copy.deepcopy(getattr(ckpt_tuple, "pending_writes", []) or []),
                    }
            except Exception:
                snapshot_capture_failed = True
                logger.warning("Could not capture pre-run checkpoint snapshot for run %s", run_id, exc_info=True)

        # 2. 发 metadata——useStream 要 run_id 和 thread_id 两个
        await bridge.publish(
            run_id,
            "metadata",
            {
                "run_id": run_id,
                "thread_id": thread_id,
            },
        )

        # 3. 构建 agent
        from langchain_core.runnables import RunnableConfig
        from langgraph.runtime import Runtime

        # 注入 runtime context，让中间件和工具（经 ToolRuntime.context）能访问 thread 级数据。
        # langgraph-cli 自动做这个；这里得手动，因为我们经 ``agent.astream(config=...)`` 驱动图，
        # 没传官方的 ``context=`` 参数。
        runtime_ctx = _build_runtime_context(thread_id, run_id, config.get("context"), ctx.app_config)
        # 把 run 作用域的 journal 挂在一个哨兵键下，让中间件能写审计事件（如
        # SafetyFinishReasonMiddleware 记被压制的 tool 调用）。双下划线前缀标它是 runtime 内部
        # 通道；用户代码不得依赖这个键名。
        if journal is not None:
            runtime_ctx["__run_journal"] = journal
        _install_runtime_context(config, runtime_ctx)
        runtime = Runtime(context=cast(Any, runtime_ctx), store=store)
        config.setdefault("configurable", {})["__pregel_runtime"] = runtime

        # 把 RunJournal 作为 LangChain callback handler 注入。
        # on_llm_end 抓 token 用量；on_chain_start/end 抓生命周期。
        if journal is not None:
            config.setdefault("callbacks", []).append(journal)

        # 注入 Langfuse trace-attribute 元数据，让 langchain CallbackHandler 能把 session_id /
        # user_id / trace_name / tags 提到根 trace 上。与 ``DeerFlowClient.stream`` 共用辅助，
        # 让两个入口保持同步；helper 内部 setdefault 让调用方给的 metadata 优先。
        inject_langfuse_metadata(
            config,
            thread_id=thread_id,
            user_id=get_effective_user_id(),
            assistant_id=record.assistant_id,
            model_name=record.model_name,
            environment=os.environ.get("DEER_FLOW_ENV") or os.environ.get("ENVIRONMENT"),
        )

        # 装好 runtime context 后再解析（这样 context/configurable 反映本次 run 实际执行的 agent 名）。
        config.setdefault("run_name", resolve_root_run_name(config, record.assistant_id))
        runnable_config = RunnableConfig(**config)
        if ctx.app_config is not None and _agent_factory_supports_app_config(agent_factory):
            agent = agent_factory(config=runnable_config, app_config=ctx.app_config)
        else:
            agent = agent_factory(config=runnable_config)

        # 从 agent metadata 抓实际（解析后的）model name。agent.py 里的 _resolve_model_name 可能
        # 因请求名不在 allowlist 而返回默认模型——这次更新确保持久化的 model_name 反映实际用的模型。
        if record.model_name is not None:
            resolved = getattr(agent, "metadata", {}) or {}
            if isinstance(resolved, dict):
                effective = resolved.get("model_name")
                if effective and effective != record.model_name:
                    await run_manager.update_model_name(record.run_id, effective)

        # 4. 挂 checkpointer 和 store
        if checkpointer is not None:
            agent.checkpointer = checkpointer
        if store is not None:
            agent.store = store

        # 5. 设 interrupt 节点
        if interrupt_before:
            agent.interrupt_before_nodes = interrupt_before
        if interrupt_after:
            agent.interrupt_after_nodes = interrupt_after

        # 6. 构建 LangGraph stream_mode 列表
        #    "events" 不是合法 astream 模式——跳过
        #    "messages-tuple" 映射到 LangGraph 的 "messages" 模式
        lg_modes: list[str] = []
        for m in requested_modes:
            if m == "messages-tuple":
                lg_modes.append("messages")
            elif m == "events":
                # 跳过——见上面的日志
                continue
            elif m in _VALID_LG_MODES:
                lg_modes.append(m)
        if not lg_modes:
            lg_modes = ["values"]

        # 去重保序
        seen: set[str] = set()
        deduped: list[str] = []
        for m in lg_modes:
            if m not in seen:
                seen.add(m)
                deduped.append(m)
        lg_modes = deduped

        logger.info("Run %s: streaming with modes %s (requested: %s)", run_id, lg_modes, requested_modes)

        # 7. 用 graph.astream 流式
        if len(lg_modes) == 1 and not stream_subgraphs:
            # 单模式、无 subgraph：astream 产裸 chunk
            single_mode = lg_modes[0]
            async for chunk in agent.astream(graph_input, config=runnable_config, stream_mode=single_mode):
                if record.abort_event.is_set():
                    logger.info("Run %s abort requested — stopping", run_id)
                    break
                llm_error_fallback_message = llm_error_fallback_message or _extract_llm_error_fallback_message(chunk)
                sse_event = _lg_mode_to_sse_event(single_mode)
                await bridge.publish(run_id, sse_event, serialize(chunk, mode=single_mode))
        else:
            # 多模式或 subgraph：astream 产元组
            async for item in agent.astream(
                graph_input,
                config=runnable_config,
                stream_mode=lg_modes,
                subgraphs=stream_subgraphs,
            ):
                if record.abort_event.is_set():
                    logger.info("Run %s abort requested — stopping", run_id)
                    break

                mode, chunk = _unpack_stream_item(item, lg_modes, stream_subgraphs)
                if mode is None:
                    continue

                llm_error_fallback_message = llm_error_fallback_message or _extract_llm_error_fallback_message(chunk)
                sse_event = _lg_mode_to_sse_event(mode)
                await bridge.publish(run_id, sse_event, serialize(chunk, mode=mode))

        # 8. 最终状态
        if record.abort_event.is_set():
            action = record.abort_action
            if action == "rollback":
                await run_manager.set_status(run_id, RunStatus.error, error="Rolled back by user")
                try:
                    await _rollback_to_pre_run_checkpoint(
                        checkpointer=checkpointer,
                        thread_id=thread_id,
                        run_id=run_id,
                        pre_run_checkpoint_id=pre_run_checkpoint_id,
                        pre_run_snapshot=pre_run_snapshot,
                        snapshot_capture_failed=snapshot_capture_failed,
                    )
                    logger.info("Run %s rolled back to pre-run checkpoint %s", run_id, pre_run_checkpoint_id)
                except Exception:
                    logger.warning("Failed to rollback checkpoint for run %s", run_id, exc_info=True)
            else:
                await run_manager.set_status(run_id, RunStatus.interrupted)
        elif llm_error_fallback_message or (journal is not None and journal.had_llm_error_fallback):
            error_msg = llm_error_fallback_message
            if error_msg is None and journal is not None:
                error_msg = journal.llm_error_fallback_message
            error_msg = error_msg or "LLM provider failed after retries"
            await run_manager.set_status(run_id, RunStatus.error, error=error_msg)
        else:
            await run_manager.set_status(run_id, RunStatus.success)

    except asyncio.CancelledError:
        action = record.abort_action
        if action == "rollback":
            await run_manager.set_status(run_id, RunStatus.error, error="Rolled back by user")
            try:
                await _rollback_to_pre_run_checkpoint(
                    checkpointer=checkpointer,
                    thread_id=thread_id,
                    run_id=run_id,
                    pre_run_checkpoint_id=pre_run_checkpoint_id,
                    pre_run_snapshot=pre_run_snapshot,
                    snapshot_capture_failed=snapshot_capture_failed,
                )
                logger.info("Run %s was cancelled and rolled back", run_id)
            except Exception:
                logger.warning("Run %s cancellation rollback failed", run_id, exc_info=True)
        else:
            await run_manager.set_status(run_id, RunStatus.interrupted)
            logger.info("Run %s was cancelled", run_id)

    except Exception as exc:
        error_msg = f"{exc}"
        logger.exception("Run %s failed: %s", run_id, error_msg)
        await run_manager.set_status(run_id, RunStatus.error, error=error_msg)
        await bridge.publish(
            run_id,
            "error",
            {
                "message": error_msg,
                "name": type(exc).__name__,
            },
        )

    finally:
        # flush 缓冲的 journal 事件 + 持久化完成数据
        if journal is not None:
            try:
                await journal.flush()
            except Exception:
                logger.warning("Failed to flush journal for run %s", run_id, exc_info=True)

            try:
                # 持久化 token 用量 + 便利字段到 RunStore
                completion = journal.get_completion_data()
                await run_manager.update_run_completion(run_id, status=record.status.value, **completion)
            except Exception:
                logger.warning("Failed to persist run completion for %s (non-fatal)", run_id, exc_info=True)

        # 从 checkpoint 同步 title 到 threads_meta.display_name
        if checkpointer is not None and thread_store is not None:
            try:
                ckpt_config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
                ckpt_tuple = await checkpointer.aget_tuple(ckpt_config)
                if ckpt_tuple is not None:
                    ckpt = getattr(ckpt_tuple, "checkpoint", {}) or {}
                    title = ckpt.get("channel_values", {}).get("title")
                    if title:
                        await thread_store.update_display_name(thread_id, title)
            except Exception:
                logger.debug("Failed to sync title for thread %s (non-fatal)", thread_id)

        # 据 run 结果更新 threads_meta 状态
        if thread_store is not None:
            try:
                final_status = "idle" if record.status == RunStatus.success else record.status.value
                await thread_store.update_status(thread_id, final_status)
            except Exception:
                logger.debug("Failed to update thread_meta status for %s (non-fatal)", thread_id)

        await bridge.publish_end(run_id)
        asyncio.create_task(bridge.cleanup(run_id, delay=60))


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


async def _call_checkpointer_method(checkpointer: Any, async_name: str, sync_name: str, *args: Any, **kwargs: Any) -> Any:
    """调一个 checkpointer 方法，支持 async 和 sync 变体。"""
    method = getattr(checkpointer, async_name, None) or getattr(checkpointer, sync_name, None)
    if method is None:
        raise AttributeError(f"Missing checkpointer method: {async_name}/{sync_name}")
    result = method(*args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


async def _rollback_to_pre_run_checkpoint(
    *,
    checkpointer: Any,
    thread_id: str,
    run_id: str,
    pre_run_checkpoint_id: str | None,
    pre_run_snapshot: dict[str, Any] | None,
    snapshot_capture_failed: bool,
) -> None:
    """把 thread 状态还原到 run 开始前捕获的 checkpoint 快照（红线 #5）。"""
    if checkpointer is None:
        logger.info("Run %s rollback requested but no checkpointer is configured", run_id)
        return

    if snapshot_capture_failed:
        logger.warning("Run %s rollback skipped: pre-run checkpoint snapshot capture failed", run_id)
        return

    if pre_run_snapshot is None:
        await _call_checkpointer_method(checkpointer, "adelete_thread", "delete_thread", thread_id)
        logger.info("Run %s rollback reset thread %s to empty state", run_id, thread_id)
        return

    checkpoint_to_restore = None
    metadata_to_restore: dict[str, Any] = {}
    checkpoint_ns = ""
    checkpoint = pre_run_snapshot.get("checkpoint")
    if not isinstance(checkpoint, dict):
        logger.warning("Run %s rollback skipped: invalid pre-run checkpoint snapshot", run_id)
        return
    checkpoint_to_restore = checkpoint
    if checkpoint_to_restore.get("id") is None and pre_run_checkpoint_id is not None:
        checkpoint_to_restore = {**checkpoint_to_restore, "id": pre_run_checkpoint_id}
    if checkpoint_to_restore.get("id") is None:
        logger.warning("Run %s rollback skipped: pre-run checkpoint has no checkpoint id", run_id)
        return
    restore_marker = _new_checkpoint_marker()
    checkpoint_to_restore = {
        **checkpoint_to_restore,
        "id": restore_marker["id"],
        "ts": restore_marker["ts"],
    }
    metadata = pre_run_snapshot.get("metadata", {})
    metadata_to_restore = metadata if isinstance(metadata, dict) else {}
    raw_checkpoint_ns = pre_run_snapshot.get("checkpoint_ns")
    checkpoint_ns = raw_checkpoint_ns if isinstance(raw_checkpoint_ns, str) else ""

    channel_versions = checkpoint_to_restore.get("channel_versions")
    new_versions = dict(channel_versions) if isinstance(channel_versions, dict) else {}

    restore_config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": checkpoint_ns}}
    restored_config = await _call_checkpointer_method(
        checkpointer,
        "aput",
        "put",
        restore_config,
        checkpoint_to_restore,
        metadata_to_restore if isinstance(metadata_to_restore, dict) else {},
        new_versions,
    )
    if not isinstance(restored_config, dict):
        raise RuntimeError(f"Run {run_id} rollback restore returned invalid config: expected dict")
    restored_configurable = restored_config.get("configurable", {})
    if not isinstance(restored_configurable, dict):
        raise RuntimeError(f"Run {run_id} rollback restore returned invalid config payload")
    restored_checkpoint_id = restored_configurable.get("checkpoint_id")
    if not restored_checkpoint_id:
        raise RuntimeError(f"Run {run_id} rollback restore did not return checkpoint_id")

    pending_writes = pre_run_snapshot.get("pending_writes", [])
    if not pending_writes:
        return

    writes_by_task: dict[str, list[tuple[str, Any]]] = {}
    for item in pending_writes:
        if not isinstance(item, (tuple, list)) or len(item) != 3:
            raise RuntimeError(f"Run {run_id} rollback failed: pending_write is not a 3-tuple: {item!r}")
        task_id, channel, value = item
        if not isinstance(channel, str):
            raise RuntimeError(f"Run {run_id} rollback failed: pending_write has non-string channel: task_id={task_id!r}, channel={channel!r}")
        writes_by_task.setdefault(str(task_id), []).append((channel, value))

    for task_id, writes in writes_by_task.items():
        await _call_checkpointer_method(
            checkpointer,
            "aput_writes",
            "put_writes",
            restored_config,
            writes,
            task_id=task_id,
        )


def _new_checkpoint_marker() -> dict[str, str]:
    marker = empty_checkpoint()
    return {"id": marker["id"], "ts": marker["ts"]}


def _lg_mode_to_sse_event(mode: str) -> str:
    """把 LangGraph 内部 stream_mode 名映射到 SSE 事件名。

    LangGraph 的 ``astream(stream_mode="messages")`` 产 message 元组。SSE 协议在客户端显式请求时
    叫它 ``messages-tuple``，但 LangGraph Platform 用的默认 SSE 事件名就是 ``"messages"``。
    """
    # 所有 LG 模式与 SSE 事件名 1:1 映射——"messages" 保持 "messages"
    return mode


def _error_fallback_message_from_metadata(metadata: dict[str, Any], content: Any) -> str:
    detail = metadata.get("error_detail")
    if isinstance(detail, str) and detail.strip():
        return detail.strip()
    reason = metadata.get("error_reason")
    if isinstance(reason, str) and reason.strip():
        return reason.strip()
    if isinstance(content, str) and content.strip():
        return content.strip()[:2000]
    return "LLM provider failed after retries"


def _try_extract_from_message(obj: Any) -> str | None:
    """尝试从单个 message 对象或 dict 抽兜底标记。"""
    additional_kwargs = getattr(obj, "additional_kwargs", None)
    if isinstance(additional_kwargs, dict) and additional_kwargs.get("deerflow_error_fallback"):
        return _error_fallback_message_from_metadata(additional_kwargs, getattr(obj, "content", None))

    if isinstance(obj, dict):
        nested_kwargs = obj.get("additional_kwargs")
        if isinstance(nested_kwargs, dict) and nested_kwargs.get("deerflow_error_fallback"):
            return _error_fallback_message_from_metadata(nested_kwargs, obj.get("content"))
    return None


def _extract_llm_error_fallback_message(value: Any) -> str | None:
    """在流式 LangGraph chunk 里找 LLM 兜底标记。

    模型调用中间件返回的兜底消息不保证过 LLM end 回调，但会出现在图状态 chunk 里。
    """
    # 快路径：stream_mode="values" 产的大状态 chunk 有顶层 "messages" list。只扫那个 list 避免
    # 对大状态 dict 做昂贵的深递归。
    if isinstance(value, dict):
        messages = value.get("messages")
        if isinstance(messages, (list, tuple)):
            for msg in messages:
                result = _try_extract_from_message(msg)
                if result is not None:
                    return result
            # 兜底标记贴在 messages channel 的 AI message 上；它绝不会出现在 values chunk 的别处。
            return None
        # 无顶层 "messages"——这大概是个 "updates" chunk（按节点名索引的小 dict）。落到深遍历，
        # 对这些 payload 代价不大。

    # updates / messages / tuple / list 模式的深遍历。payload 小，全递归可接受。
    seen: set[int] = set()

    def walk(obj: Any) -> str | None:
        oid = id(obj)
        if oid in seen:
            return None
        seen.add(oid)

        result = _try_extract_from_message(obj)
        if result is not None:
            return result

        if isinstance(obj, dict):
            for item in obj.values():
                result = walk(item)
                if result is not None:
                    return result
            return None

        if isinstance(obj, (list, tuple, set)):
            for item in obj:
                result = walk(item)
                if result is not None:
                    return result
        return None

    return walk(value)


def _unpack_stream_item(
    item: Any,
    lg_modes: list[str],
    stream_subgraphs: bool,
) -> tuple[str | None, Any]:
    """把多模式或 subgraph 的流项拆成 (mode, chunk)。

    无法解析时返回 ``(None, None)``。
    """
    if stream_subgraphs:
        if isinstance(item, tuple) and len(item) == 3:
            _ns, mode, chunk = item
            return str(mode), chunk
        if isinstance(item, tuple) and len(item) == 2:
            mode, chunk = item
            return str(mode), chunk
        return None, None

    if isinstance(item, tuple) and len(item) == 2:
        mode, chunk = item
        return str(mode), chunk

    # 兜底：首个模式的单元素输出
    return lg_modes[0] if lg_modes else None, item
