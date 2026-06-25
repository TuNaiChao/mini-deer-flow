"""worker（run_agent）测试（M18）。

hermetic：用 ``SimpleNamespace`` + ``AsyncMock`` 造假 bridge / agent_factory / checkpointer，
覆盖后台 agent 执行的全部关键路径：runtime/journal 注入、rollback 快照还原（红线 #5）、
abort（interrupt/rollback）、LLM 兜底消息抽取、run_name 解析、异常终态、多模式流。
不跑真实模型、不连真实 checkpointer。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.base import empty_checkpoint

from deerflow.runtime.runs.manager import RunManager
from deerflow.runtime.runs.naming import resolve_root_run_name
from deerflow.runtime.runs.schemas import RunStatus
from deerflow.runtime.runs.worker import (
    RunContext,
    _agent_factory_supports_app_config,
    _build_runtime_context,
    _extract_llm_error_fallback_message,
    _install_runtime_context,
    _rollback_to_pre_run_checkpoint,
    _try_extract_from_message,
    run_agent,
)

# ---------------------------------------------------------------------------
# 桩
# ---------------------------------------------------------------------------


def _make_bridge():
    return SimpleNamespace(
        publish=AsyncMock(),
        publish_end=AsyncMock(),
        cleanup=AsyncMock(),
    )


class _DummyAgent:
    """假 agent——astream 产预设 chunk 序列。"""

    def __init__(self, chunks, *, capture=None):
        self._chunks = chunks
        self._capture = capture
        self.metadata = {}

    async def astream(self, graph_input, config=None, stream_mode=None, subgraphs=False):
        if self._capture is not None:
            self._capture["astream_context"] = config.get("context")
            self._capture["astream_run_name"] = config.get("run_name")
        for chunk in self._chunks:
            yield chunk


class _FakeCheckpointer:
    """假 checkpointer——aget_tuple 返 None（无 pre-run 快照）；aput/aput_writes/adelete_thread 可设。"""

    def __init__(self, *, put_result=None, snapshot=None):
        self.aget_tuple = AsyncMock(return_value=snapshot)
        self.adelete_thread = AsyncMock()
        self.aput = AsyncMock(return_value=put_result or {"configurable": {"checkpoint_id": "restored-1"}})
        self.aput_writes = AsyncMock()


def _make_checkpoint(checkpoint_id: str, messages: list[str], version: int):
    checkpoint = empty_checkpoint()
    checkpoint["id"] = checkpoint_id
    checkpoint["channel_values"] = {"messages": messages}
    checkpoint["channel_versions"] = {"messages": version}
    return checkpoint


# ===========================================================================
# 辅助函数（同步，快）
# ===========================================================================


def test_build_runtime_context_defaults_thread_and_run_id():
    ctx = _build_runtime_context("thread-1", "run-1", None)
    assert ctx == {"thread_id": "thread-1", "run_id": "run-1"}


def test_build_runtime_context_includes_app_config():
    app_config = object()
    ctx = _build_runtime_context("thread-1", "run-1", None, app_config)
    assert ctx["app_config"] is app_config


def test_build_runtime_context_merges_caller_context():
    ctx = _build_runtime_context("thread-1", "run-1", {"agent_name": "finalis"})
    assert ctx["agent_name"] == "finalis"


def test_build_runtime_context_caller_cannot_override_thread_or_run_id():
    """caller context 不能覆盖 thread_id/run_id（setdefault）。"""
    ctx = _build_runtime_context("thread-1", "run-1", {"thread_id": "evil", "run_id": "evil"})
    assert ctx["thread_id"] == "thread-1"
    assert ctx["run_id"] == "run-1"


def test_build_runtime_context_ignores_non_dict_caller():
    ctx = _build_runtime_context("thread-1", "run-1", "not-a-dict")
    assert ctx == {"thread_id": "thread-1", "run_id": "run-1"}


def test_install_runtime_context_preserves_existing_thread_id():
    """已有 thread_id 保留；run_id + app_config 注入。"""
    app_config = object()
    config = {"context": {"thread_id": "caller-thread"}}
    _install_runtime_context(config, {"thread_id": "record-thread", "run_id": "run-1", "app_config": app_config})
    assert config["context"]["thread_id"] == "caller-thread"
    assert config["context"]["run_id"] == "run-1"
    assert config["context"]["app_config"] is app_config


def test_install_runtime_context_creates_context_when_absent():
    config = {}
    _install_runtime_context(config, {"thread_id": "t1", "run_id": "r1"})
    assert config["context"]["thread_id"] == "t1"
    assert config["context"]["run_id"] == "r1"


def test_agent_factory_supports_app_config_detects_signature():
    def factory_with(*, config, app_config):
        pass

    def factory_without(*, config):
        pass

    assert _agent_factory_supports_app_config(factory_with) is True
    assert _agent_factory_supports_app_config(factory_without) is False


def test_try_extract_from_message_finds_fallback_on_message_object():
    msg = AIMessage(content="x", additional_kwargs={"deerflow_error_fallback": True, "error_detail": "boom"})
    assert _try_extract_from_message(msg) == "boom"


def test_try_extract_from_message_finds_fallback_on_dict():
    obj = {"content": "x", "additional_kwargs": {"deerflow_error_fallback": True, "error_reason": "transient"}}
    assert _try_extract_from_message(obj) == "transient"


def test_try_extract_from_message_returns_none_for_normal():
    msg = AIMessage(content="normal")
    assert _try_extract_from_message(msg) is None


def test_extract_llm_error_fallback_in_messages_list():
    """values chunk 的 messages list 里有兜底标记 → 抽出。"""
    chunk = {
        "messages": [
            AIMessage(content="ok"),
            AIMessage(content="x", additional_kwargs={"deerflow_error_fallback": True, "error_detail": "Connection error."}),
        ]
    }
    assert _extract_llm_error_fallback_message(chunk) == "Connection error."


def test_extract_llm_error_fallback_large_state_no_fallback():
    """values chunk 的 messages list 无标记 → None（不深扫别处）。"""
    chunk = {"messages": [AIMessage(content="ok")], "other": {"deep": "x"}}
    assert _extract_llm_error_fallback_message(chunk) is None


def test_extract_llm_error_fallback_in_raw_dict():
    """非 values chunk（无顶层 messages）→ 深扫找到。"""
    chunk = {"node": {"additional_kwargs": {"deerflow_error_fallback": True, "error_detail": "deep"}}}
    assert _extract_llm_error_fallback_message(chunk) == "deep"


def test_extract_llm_error_fallback_in_tuple():
    chunk = ({"additional_kwargs": {"deerflow_error_fallback": True, "error_reason": "r"}},)
    assert _extract_llm_error_fallback_message(chunk) == "r"


def test_extract_llm_error_fallback_empty_returns_none():
    assert _extract_llm_error_fallback_message({}) is None
    assert _extract_llm_error_fallback_message(None) is None


def test_resolve_root_run_name_from_assistant_id():
    assert resolve_root_run_name({}, "lead_agent") == "lead_agent"


def test_resolve_root_run_name_from_context():
    assert resolve_root_run_name({"context": {"agent_name": "finalis"}}, "lead_agent") == "finalis"


def test_resolve_root_run_name_from_configurable():
    assert resolve_root_run_name({"configurable": {"agent_name": "finalis"}}, "lead_agent") == "finalis"


def test_resolve_root_run_name_context_takes_precedence():
    """context 优先于 configurable。"""
    config = {"context": {"agent_name": "from-context"}, "configurable": {"agent_name": "from-configurable"}}
    assert resolve_root_run_name(config, "lead_agent") == "from-context"


def test_resolve_root_run_name_default_when_nothing():
    assert resolve_root_run_name({}, None) == "lead_agent"


# ===========================================================================
# run_agent（async，桩化）
# ===========================================================================


async def test_run_agent_success_path():
    """正常完成 → status success + publish_end + metadata 发布。"""
    rm = RunManager()
    record = await rm.create("thread-1", assistant_id="lead_agent")
    bridge = _make_bridge()

    def factory(*, config):
        return _DummyAgent([{"messages": []}])

    await run_agent(bridge, rm, record, ctx=RunContext(checkpointer=None), agent_factory=factory, graph_input={}, config={})

    fetched = await rm.get(record.run_id)
    assert fetched.status == RunStatus.success
    bridge.publish_end.assert_awaited_once_with(record.run_id)
    # metadata 发布（含 run_id + thread_id）
    publish_calls = [c for c in bridge.publish.await_args_list if c.args[1] == "metadata"]
    assert len(publish_calls) == 1
    assert publish_calls[0].args[2] == {"run_id": record.run_id, "thread_id": "thread-1"}


async def test_run_agent_marks_llm_error_fallback_as_error():
    """LLM 兜底消息（chunk 里的 deerflow_error_fallback）→ status error + 错误详情。"""
    rm = RunManager()
    record = await rm.create("thread-1")
    bridge = _make_bridge()

    class DummyAgent:
        async def astream(self, graph_input, config=None, stream_mode=None, subgraphs=False):
            yield {
                "messages": [
                    AIMessage(
                        content="unavailable",
                        additional_kwargs={"deerflow_error_fallback": True, "error_detail": "Connection error."},
                    )
                ]
            }

    await run_agent(bridge, rm, record, ctx=RunContext(checkpointer=None), agent_factory=lambda *, config: DummyAgent(), graph_input={}, config={})

    fetched = await rm.get(record.run_id)
    assert fetched.status == RunStatus.error
    assert fetched.error == "Connection error."


async def test_run_agent_exception_marks_error_and_publishes():
    """agent_factory 抛异常 → status error + error 事件发布。"""
    rm = RunManager()
    record = await rm.create("thread-1")
    bridge = _make_bridge()

    def factory(*, config):
        raise RuntimeError("factory boom")

    await run_agent(bridge, rm, record, ctx=RunContext(checkpointer=None), agent_factory=factory, graph_input={}, config={})

    fetched = await rm.get(record.run_id)
    assert fetched.status == RunStatus.error
    assert "factory boom" in fetched.error
    # error 事件发布
    error_calls = [c for c in bridge.publish.await_args_list if c.args[1] == "error"]
    assert len(error_calls) == 1


async def test_run_agent_run_name_from_assistant_id():
    """run_name 默认从 assistant_id 解析。"""
    rm = RunManager()
    record = await rm.create("thread-1", assistant_id="lead_agent")
    bridge = _make_bridge()
    captured = {}

    def factory(*, config):
        return _DummyAgent([{"messages": []}], capture=captured)

    await run_agent(bridge, rm, record, ctx=RunContext(checkpointer=None), agent_factory=factory, graph_input={}, config={})

    assert captured["astream_run_name"] == "lead_agent"


async def test_run_agent_run_name_from_context_agent_name():
    """context 里的 agent_name 覆盖 assistant_id 作 run_name。"""
    rm = RunManager()
    record = await rm.create("thread-1", assistant_id="lead_agent")
    bridge = _make_bridge()
    captured = {}

    def factory(*, config):
        return _DummyAgent([{"messages": []}], capture=captured)

    await run_agent(bridge, rm, record, ctx=RunContext(checkpointer=None), agent_factory=factory, graph_input={}, config={"context": {"agent_name": "finalis"}})

    assert captured["astream_run_name"] == "finalis"


async def test_run_agent_threads_app_config_into_factory():
    """RunContext.app_config 注入支持 app_config 的 factory。"""
    rm = RunManager()
    record = await rm.create("thread-1")
    bridge = _make_bridge()
    app_config = object()
    captured = {}

    def factory(*, config, app_config=None):
        captured["factory_app_config"] = app_config
        return _DummyAgent([{"messages": []}])

    await run_agent(bridge, rm, record, ctx=RunContext(checkpointer=None, app_config=app_config), agent_factory=factory, graph_input={}, config={})

    assert captured["factory_app_config"] is app_config


async def test_run_agent_injects_pregel_runtime():
    """config 里注入 __pregel_runtime + run 作用域 context。"""
    rm = RunManager()
    record = await rm.create("thread-1")
    bridge = _make_bridge()
    captured = {}

    def factory(*, config):
        return _DummyAgent([{"messages": []}], capture=captured)

    await run_agent(bridge, rm, record, ctx=RunContext(checkpointer=None), agent_factory=factory, graph_input={}, config={})

    runtime = captured["astream_context"]
    assert runtime["thread_id"] == "thread-1"
    assert runtime["run_id"] == record.run_id


async def test_run_agent_multi_mode_stream():
    """多模式流：astream 产 (mode, chunk) 元组，逐个发布。"""
    rm = RunManager()
    record = await rm.create("thread-1")
    bridge = _make_bridge()

    class DummyAgent:
        async def astream(self, graph_input, config=None, stream_mode=None, subgraphs=False):
            yield ("values", {"messages": []})
            yield ("updates", {"agent": {"messages": []}})

    await run_agent(
        bridge,
        rm,
        record,
        ctx=RunContext(checkpointer=None),
        agent_factory=lambda *, config: DummyAgent(),
        graph_input={},
        config={},
        stream_modes=["values", "updates"],
    )

    fetched = await rm.get(record.run_id)
    assert fetched.status == RunStatus.success
    # values + updates 各发布一次（metadata 是额外那次）
    event_types = [c.args[1] for c in bridge.publish.await_args_list]
    assert "values" in event_types
    assert "updates" in event_types


# ===========================================================================
# rollback 快照还原（红线 #5）
# ===========================================================================


async def test_rollback_restores_snapshot_without_deleting_thread():
    """有 pre_run_snapshot → aput 还原 checkpoint，不 adelete_thread。"""
    checkpointer = _FakeCheckpointer(
        put_result={"configurable": {"thread_id": "thread-1", "checkpoint_ns": "", "checkpoint_id": "restored-1"}},
    )
    snapshot = {
        "checkpoint_ns": "",
        "checkpoint": _make_checkpoint("ckpt-1", ["msg"], 1),
        "metadata": {"step": 0},
        "pending_writes": [],
    }

    await _rollback_to_pre_run_checkpoint(
        checkpointer=checkpointer,
        thread_id="thread-1",
        run_id="run-1",
        pre_run_checkpoint_id="ckpt-1",
        pre_run_snapshot=snapshot,
        snapshot_capture_failed=False,
    )

    checkpointer.aput.assert_awaited_once()
    checkpointer.adelete_thread.assert_not_awaited()


async def test_rollback_deletes_thread_when_no_snapshot():
    """无 pre_run_snapshot（首次 run）→ adelete_thread 清空。"""
    checkpointer = _FakeCheckpointer()

    await _rollback_to_pre_run_checkpoint(
        checkpointer=checkpointer,
        thread_id="thread-1",
        run_id="run-1",
        pre_run_checkpoint_id=None,
        pre_run_snapshot=None,
        snapshot_capture_failed=False,
    )

    checkpointer.adelete_thread.assert_awaited_once_with("thread-1")
    checkpointer.aput.assert_not_awaited()


async def test_rollback_skipped_when_snapshot_capture_failed():
    """快照捕获失败 → 跳过 rollback（不 aput 不 adelete）。"""
    checkpointer = _FakeCheckpointer()

    await _rollback_to_pre_run_checkpoint(
        checkpointer=checkpointer,
        thread_id="thread-1",
        run_id="run-1",
        pre_run_checkpoint_id=None,
        pre_run_snapshot=None,
        snapshot_capture_failed=True,
    )

    checkpointer.aput.assert_not_awaited()
    checkpointer.adelete_thread.assert_not_awaited()


async def test_rollback_skipped_when_no_checkpointer():
    """无 checkpointer → 直接返（rollback 无的放矢）。"""
    # 不抛即通过
    await _rollback_to_pre_run_checkpoint(
        checkpointer=None,
        thread_id="thread-1",
        run_id="run-1",
        pre_run_checkpoint_id=None,
        pre_run_snapshot=None,
        snapshot_capture_failed=False,
    )


async def test_rollback_restores_pending_writes():
    """快照含 pending_writes → aput_writes 还原。"""
    checkpointer = _FakeCheckpointer(
        put_result={"configurable": {"thread_id": "thread-1", "checkpoint_ns": "", "checkpoint_id": "restored-1"}},
    )
    snapshot = {
        "checkpoint_ns": "",
        "checkpoint": _make_checkpoint("ckpt-1", [], 1),
        "metadata": {},
        "pending_writes": [("task-1", "messages", "value")],
    }

    await _rollback_to_pre_run_checkpoint(
        checkpointer=checkpointer,
        thread_id="thread-1",
        run_id="run-1",
        pre_run_checkpoint_id="ckpt-1",
        pre_run_snapshot=snapshot,
        snapshot_capture_failed=False,
    )

    checkpointer.aput_writes.assert_awaited_once()


async def test_rollback_raises_on_malformed_pending_write():
    """pending_write 不是 3-tuple → RuntimeError。"""
    checkpointer = _FakeCheckpointer(
        put_result={"configurable": {"checkpoint_id": "restored-1"}},
    )
    snapshot = {
        "checkpoint_ns": "",
        "checkpoint": _make_checkpoint("ckpt-1", [], 1),
        "metadata": {},
        "pending_writes": [("task-1", "messages")],  # 2-tuple，非法
    }

    with pytest.raises(RuntimeError, match="not a 3-tuple"):
        await _rollback_to_pre_run_checkpoint(
            checkpointer=checkpointer,
            thread_id="thread-1",
            run_id="run-1",
            pre_run_checkpoint_id="ckpt-1",
            pre_run_snapshot=snapshot,
            snapshot_capture_failed=False,
        )


async def test_rollback_raises_when_restore_returns_no_checkpoint_id():
    """aput 返回的 config 无 checkpoint_id → RuntimeError。"""
    checkpointer = _FakeCheckpointer(put_result={"configurable": {}})  # 无 checkpoint_id
    snapshot = {
        "checkpoint_ns": "",
        "checkpoint": _make_checkpoint("ckpt-1", [], 1),
        "metadata": {},
        "pending_writes": [],
    }

    with pytest.raises(RuntimeError, match="did not return checkpoint_id"):
        await _rollback_to_pre_run_checkpoint(
            checkpointer=checkpointer,
            thread_id="thread-1",
            run_id="run-1",
            pre_run_checkpoint_id="ckpt-1",
            pre_run_snapshot=snapshot,
            snapshot_capture_failed=False,
        )


# ===========================================================================
# run_agent + rollback 集成（abort）
# ===========================================================================


async def test_run_agent_abort_interrupt_marks_interrupted():
    """astream 期间 abort_event 被 set + action=interrupt → status interrupted。

    模拟：在 astream 第一个 chunk 前触发 cancel（set abort_event）。
    """
    rm = RunManager()
    record = await rm.create("thread-1")
    bridge = _make_bridge()

    class DummyAgent:
        async def astream(self, graph_input, config=None, stream_mode=None, subgraphs=False):
            # 模拟 worker 在迭代边界检测到 abort
            record.abort_event.set()
            yield {"messages": []}

    await run_agent(bridge, rm, record, ctx=RunContext(checkpointer=None), agent_factory=lambda *, config: DummyAgent(), graph_input={}, config={})

    fetched = await rm.get(record.run_id)
    assert fetched.status == RunStatus.interrupted


# ===========================================================================
# Langfuse metadata 注入（审查补齐——deer test_worker_langfuse_metadata 对齐）
# ===========================================================================


async def test_run_agent_injects_langfuse_metadata_with_record_fields(monkeypatch):
    """worker 调 inject_langfuse_metadata 时传 record 的 thread_id/assistant_id/model_name +
    get_effective_user_id() 的 user_id（红线 #17 trace 属性经图根注入）。"""
    captured: dict = {}

    def _fake_inject(config, *, thread_id, user_id, assistant_id, model_name, environment):
        captured.update(
            thread_id=thread_id,
            user_id=user_id,
            assistant_id=assistant_id,
            model_name=model_name,
            environment=environment,
        )

    monkeypatch.setattr("deerflow.runtime.runs.worker.inject_langfuse_metadata", _fake_inject)

    rm = RunManager()
    record = await rm.create("thread-xyz", assistant_id="lead-agent")
    record.model_name = "gpt-4o"  # create 不收 model_name；模拟 create_or_reject / update_model_name 设的
    bridge = _make_bridge()

    def factory(*, config):
        return _DummyAgent([{"messages": []}])

    await run_agent(bridge, rm, record, ctx=RunContext(checkpointer=None), agent_factory=factory, graph_input={}, config={})

    assert captured["thread_id"] == "thread-xyz"
    assert captured["assistant_id"] == "lead-agent"
    assert captured["model_name"] == "gpt-4o"
    # user_id 来自 get_effective_user_id()（conftest autouse 设 test-user-autouse）
    assert captured["user_id"] is not None


async def test_run_agent_langfuse_user_id_from_effective_user_id(monkeypatch):
    """worker 的 langfuse user_id 来自 get_effective_user_id()（三态 user_id 单一真相源）。

    deer 的 test_run_agent_falls_back_to_default_user_when_unset 对齐——monkeypatch
    get_effective_user_id 验证它就是 user_id 的来源。
    """
    captured: dict = {}

    def _fake_inject(config, *, thread_id, user_id, assistant_id, model_name, environment):
        captured["user_id"] = user_id

    monkeypatch.setattr("deerflow.runtime.runs.worker.inject_langfuse_metadata", _fake_inject)
    monkeypatch.setattr("deerflow.runtime.runs.worker.get_effective_user_id", lambda: "explicit-user-123")

    rm = RunManager()
    record = await rm.create("thread-1", assistant_id="lead-agent")
    bridge = _make_bridge()

    def factory(*, config):
        return _DummyAgent([{"messages": []}])

    await run_agent(bridge, rm, record, ctx=RunContext(checkpointer=None), agent_factory=factory, graph_input={}, config={})

    assert captured["user_id"] == "explicit-user-123"
