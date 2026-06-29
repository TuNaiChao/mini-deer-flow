"""中间件装配 + 行为测试（M16，23 步链）。

hermetic：``build_middlewares(app_config=...)`` 接显式配置不读全局 config.yaml；中间件 hook
测试用 SimpleNamespace 构造假 state / runtime / request（无真实模型 / 网络）。

覆盖红线：
  - #14 Clarification 永远末位；
  - #3 Uploads 先于 #4 ThreadData，#4 ThreadData 先于 #5 Sandbox（对齐上游顺序）；
  - #15 所有 ``wrap_tool_call`` / ``wrap_model_call`` 透传 ``GraphBubbleUp``；
  - 23 步顺序不变量 + 各 config 开关 gating。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.errors import GraphBubbleUp

from deerflow.agents.middlewares import build_middlewares
from deerflow.agents.middlewares.clarification_middleware import ClarificationMiddleware
from deerflow.agents.middlewares.dangling_tool_call_middleware import DanglingToolCallMiddleware
from deerflow.agents.middlewares.deferred_tool_filter_middleware import DeferredToolFilterMiddleware
from deerflow.agents.middlewares.loop_detection_middleware import (
    LoopDetectionMiddleware,
    _hash_tool_calls,
)
from deerflow.agents.middlewares.safety_finish_reason_middleware import SafetyFinishReasonMiddleware
from deerflow.agents.middlewares.safety_termination_detectors import (
    OpenAICompatibleContentFilterDetector,
    default_detectors,
)
from deerflow.agents.middlewares.subagent_limit_middleware import (
    SubagentLimitMiddleware,
    _clamp_subagent_limit,
)
from deerflow.agents.middlewares.thread_data_middleware import ThreadDataMiddleware
from deerflow.agents.middlewares.tool_call_metadata import clone_ai_message_with_tool_calls
from deerflow.agents.middlewares.tool_error_handling_middleware import (
    ToolErrorHandlingMiddleware,
    _stamp_task_subagent_status,
)
from deerflow.agents.middlewares.tool_output_budget_middleware import (
    _message_text,
    _snap_to_line_boundary,
)
from deerflow.agents.middlewares.uploads_middleware import UploadsMiddleware
from deerflow.config import AppConfig

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _names(mws):
    return [type(m).__name__ for m in mws]


def _idx(mws, cls) -> int:
    for i, m in enumerate(mws):
        if isinstance(m, cls):
            return i
    return -1


class _FakeRequest:
    """带 ``override`` 的可变 request 替身（wrap_model_call / wrap_tool_call 用）。"""

    def __init__(self, **kw):
        self.__dict__.update(kw)

    def override(self, **kw) -> _FakeRequest:
        new = _FakeRequest()
        new.__dict__.update(self.__dict__)
        new.__dict__.update(kw)
        return new


def _runtime(ctx: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(context=ctx or {"thread_id": "t1", "run_id": "r1"})


def _ai(tool_calls=None, content="ok", **kw) -> AIMessage:
    return AIMessage(content=content, tool_calls=tool_calls or [], **kw)


# ---------------------------------------------------------------------------
# 装配顺序 / 不变量（红线 #14、ThreadData→Sandbox）
# ---------------------------------------------------------------------------


def test_default_chain_has_core_middlewares_and_clarification_last():
    """默认链含核心中间件，Clarification 末位（红线 #14）。"""
    mws = build_middlewares(app_config=AppConfig())
    names = _names(mws)
    for must in [
        "InputSanitizationMiddleware",
        "ToolOutputBudgetMiddleware",
        "UploadsMiddleware",
        "ThreadDataMiddleware",
        "SandboxMiddleware",
        "DanglingToolCallMiddleware",
        "LLMErrorHandlingMiddleware",
        "SandboxAuditMiddleware",
        "ToolErrorHandlingMiddleware",
        "DynamicContextMiddleware",
        "SkillActivationMiddleware",
        "TokenUsageMiddleware",
        "TitleMiddleware",
        "MemoryMiddleware",
        "SystemMessageCoalescingMiddleware",
        "LoopDetectionMiddleware",
        "SafetyFinishReasonMiddleware",
        "ClarificationMiddleware",
    ]:
        assert must in names, f"missing {must}"
    assert isinstance(mws[-1], ClarificationMiddleware)
    # InputSanitization 必须是第 0 个（最外层 wrap_model_call）。
    assert names[0] == "InputSanitizationMiddleware"


def test_uploads_precedes_thread_data_and_sandbox():
    """红线：Uploads(#3) → ThreadData(#4) → Sandbox(#5)（对齐上游顺序）。"""
    mws = build_middlewares(app_config=AppConfig())
    assert _idx(mws, UploadsMiddleware) < _idx(mws, ThreadDataMiddleware)
    # SandboxMiddleware 来自 sandbox.middleware；用类名定位（避免硬 import 产生耦合）。
    sandbox_i = next(i for i, m in enumerate(mws) if type(m).__name__ == "SandboxMiddleware")
    assert _idx(mws, ThreadDataMiddleware) < sandbox_i
    assert _idx(mws, UploadsMiddleware) < sandbox_i


def test_custom_middlewares_before_clarification():
    """custom_middlewares 插在 Clarification 之前（Safety 默认开，故 custom 非紧邻 Clarification）。"""

    class MyMiddleware(AgentMiddleware):
        pass

    mws = build_middlewares(app_config=AppConfig(), custom_middlewares=[MyMiddleware()])
    assert isinstance(mws[-1], ClarificationMiddleware)
    assert _idx(mws, MyMiddleware) < _idx(mws, ClarificationMiddleware)


def test_safety_registered_after_custom():
    """Safety 在 custom 之后注册（LangChain 倒序 after_model 让 Safety 先看模型输出）。"""

    class MyMiddleware(AgentMiddleware):
        pass

    mws = build_middlewares(app_config=AppConfig(), custom_middlewares=[MyMiddleware()])
    assert _idx(mws, MyMiddleware) < _idx(mws, SafetyFinishReasonMiddleware)


# ---------------------------------------------------------------------------
# 2026-06-27 六维重审新增：3 个补齐的中间件（InputSanitization / SystemMessageCoalescing / TokenBudget）
# ---------------------------------------------------------------------------


def test_input_sanitization_is_first_in_lead_and_subagent_base():
    """InputSanitization(#1) 在 lead 与 subagent 共享前置段都是第 0 个（最外层 wrap_model_call）。"""
    from deerflow.agents.middlewares.input_sanitization_middleware import InputSanitizationMiddleware
    from deerflow.agents.middlewares.tool_error_handling_middleware import (
        build_lead_runtime_middlewares,
        build_subagent_runtime_middlewares,
    )

    lead = build_lead_runtime_middlewares(app_config=AppConfig())
    sub = build_subagent_runtime_middlewares(app_config=AppConfig())
    assert isinstance(lead[0], InputSanitizationMiddleware)
    assert isinstance(sub[0], InputSanitizationMiddleware)


def test_system_message_coalescing_always_present_before_subagent():
    """SystemMessageCoalescing(#20) 始终挂（无开关），且在 SubagentLimit(#21) 之前。"""
    from deerflow.agents.middlewares.system_message_coalescing_middleware import SystemMessageCoalescingMiddleware

    mws = build_middlewares(app_config=AppConfig())
    assert any(isinstance(m, SystemMessageCoalescingMiddleware) for m in mws)
    # 默认不开 subagent 时也挂 SystemMessageCoalescing（始终生效）
    # 开 subagent 后：SystemMessageCoalescing 必须在 SubagentLimit 之前
    mws2 = build_middlewares(config={"configurable": {"subagent_enabled": True}}, app_config=AppConfig())
    assert _idx(mws2, SystemMessageCoalescingMiddleware) < _idx(mws2, SubagentLimitMiddleware)


def test_system_message_coalescing_before_loop_detection():
    """SystemMessageCoalescing(#20) 在 LoopDetection(#22) 之前——合并要在循环检测看消息前完成。"""
    from deerflow.agents.middlewares.system_message_coalescing_middleware import SystemMessageCoalescingMiddleware

    mws = build_middlewares(app_config=AppConfig())
    assert _idx(mws, SystemMessageCoalescingMiddleware) < _idx(mws, LoopDetectionMiddleware)


def test_token_budget_disabled_omits():
    """TokenBudget 默认 enabled=False → 不挂。"""
    from deerflow.agents.middlewares.token_budget_middleware import TokenBudgetMiddleware

    mws = build_middlewares(app_config=AppConfig())
    assert not any(isinstance(m, TokenBudgetMiddleware) for m in mws)


def test_token_budget_enabled_after_loop_before_clarification():
    """TokenBudget(#23) 启用时挂在 LoopDetection(#22) 之后、Clarification(#26) 之前。"""
    from deerflow.agents.middlewares.token_budget_middleware import TokenBudgetMiddleware

    cfg = AppConfig(token_budget={"enabled": True})
    mws = build_middlewares(app_config=cfg)
    assert _idx(mws, LoopDetectionMiddleware) < _idx(mws, TokenBudgetMiddleware)
    assert _idx(mws, TokenBudgetMiddleware) < len(mws) - 1  # 不是末位（Clarification 才是）


# ---------------------------------------------------------------------------
# config 驱动 gating
# ---------------------------------------------------------------------------


def test_title_disabled_noops_but_present():
    """TitleMiddleware 总在链里（对齐 deer）；enabled=False 时 _should_generate 返回 False。"""
    from deerflow.agents.middlewares.title_middleware import TitleMiddleware

    mws = build_middlewares(app_config=AppConfig(title={"enabled": False}))
    assert any(isinstance(m, TitleMiddleware) for m in mws)
    tm = next(m for m in mws if isinstance(m, TitleMiddleware))
    # enabled=False → 任何状态都不生成。
    assert tm._should_generate_title({"messages": [HumanMessage(content="hi"), _ai()]}) is False


def test_memory_disabled_present_but_noops():
    """MemoryMiddleware 总在链里（对齐 deer）；queue.add 内部按 enabled 跳过。"""
    mws = build_middlewares(app_config=AppConfig(memory={"enabled": False}))
    assert any(type(m).__name__ == "MemoryMiddleware" for m in mws)


def test_loop_detection_disabled_omits_loop():
    mws = build_middlewares(app_config=AppConfig(loop_detection={"enabled": False}))
    assert not any(isinstance(m, LoopDetectionMiddleware) for m in mws)


def test_safety_disabled_omits_safety():
    mws = build_middlewares(app_config=AppConfig(safety_finish_reason={"enabled": False}))
    assert not any(isinstance(m, SafetyFinishReasonMiddleware) for m in mws)


def test_token_usage_disabled_omits_token_usage():
    mws = build_middlewares(app_config=AppConfig(token_usage={"enabled": False}))
    assert not any(type(m).__name__ == "TokenUsageMiddleware" for m in mws)


def test_plan_mode_adds_todo():
    mws = build_middlewares(config={"configurable": {"is_plan_mode": True}}, app_config=AppConfig())
    assert any(type(m).__name__ == "TodoMiddleware" for m in mws)


def test_no_plan_mode_omits_todo():
    mws = build_middlewares(app_config=AppConfig())
    assert not any(type(m).__name__ == "TodoMiddleware" for m in mws)


def test_subagent_enabled_adds_subagent_limit():
    mws = build_middlewares(
        config={"configurable": {"subagent_enabled": True, "max_concurrent_subagents": 3}},
        app_config=AppConfig(),
    )
    assert any(isinstance(m, SubagentLimitMiddleware) for m in mws)


def test_subagent_disabled_omits_subagent_limit():
    mws = build_middlewares(app_config=AppConfig())
    assert not any(isinstance(m, SubagentLimitMiddleware) for m in mws)


def test_vision_model_adds_view_image():
    cfg = AppConfig(models=[{"name": "vl", "use": "langchain_openai:ChatOpenAI", "model": "gpt-4o", "supports_vision": True}])
    mws = build_middlewares(model_name="vl", app_config=cfg)
    assert any(type(m).__name__ == "ViewImageMiddleware" for m in mws)


def test_non_vision_model_omits_view_image():
    cfg = AppConfig(models=[{"name": "txt", "use": "langchain_openai:ChatOpenAI", "model": "gpt-4o", "supports_vision": False}])
    mws = build_middlewares(model_name="txt", app_config=cfg)
    assert not any(type(m).__name__ == "ViewImageMiddleware" for m in mws)


def test_deferred_setup_adds_deferred_filter():
    setup = SimpleNamespace(deferred_names=frozenset({"mcp_a", "mcp_b"}), catalog_hash="abc")
    mws = build_middlewares(deferred_setup=setup, app_config=AppConfig())
    assert any(isinstance(m, DeferredToolFilterMiddleware) for m in mws)


def test_empty_deferred_setup_omits_filter():
    setup = SimpleNamespace(deferred_names=frozenset(), catalog_hash=None)
    mws = build_middlewares(deferred_setup=setup, app_config=AppConfig())
    assert not any(isinstance(m, DeferredToolFilterMiddleware) for m in mws)


def test_summarization_enabled_adds_middleware(monkeypatch):
    """enabled=True 且 models 配好 → 构造 DeerFlowSummarizationMiddleware。

    create_chat_model 经 monkeypatch 返回桩（避免真实例化 provider 模型）。
    """
    import deerflow.models as models_mod

    class _StubModel:
        _llm_type = "fake"

        def with_config(self, **_):
            return self

    monkeypatch.setattr(models_mod, "create_chat_model", lambda *a, **k: _StubModel())
    cfg = AppConfig(models=[{"name": "m", "use": "x:Y", "model": "m"}], summarization={"enabled": True})
    mws = build_middlewares(app_config=cfg)
    assert any(type(m).__name__ == "DeerFlowSummarizationMiddleware" for m in mws)


def test_summarization_disabled_omits():
    mws = build_middlewares(app_config=AppConfig(summarization={"enabled": False}))
    assert not any(type(m).__name__ == "DeerFlowSummarizationMiddleware" for m in mws)


# ---------------------------------------------------------------------------
# 红线 #15：GraphBubbleUp 必须透传
# ---------------------------------------------------------------------------


def test_tool_error_handling_preserves_graphbubbleup_sync():
    """ToolErrorHandlingMiddleware 的 wrap_tool_call 须透传 GraphBubbleUp。"""
    mw = ToolErrorHandlingMiddleware()
    req = _FakeRequest(tool_call={"name": "x", "id": "c1"})

    def handler(_):
        raise GraphBubbleUp()

    with pytest.raises(GraphBubbleUp):
        mw.wrap_tool_call(req, handler)


def test_tool_error_handling_preserves_graphbubbleup_async():
    mw = ToolErrorHandlingMiddleware()
    req = _FakeRequest(tool_call={"name": "x", "id": "c1"})

    async def handler(_):
        raise GraphBubbleUp()

    with pytest.raises(GraphBubbleUp):
        asyncio.run(mw.awrap_tool_call(req, handler))


def test_tool_error_handling_wraps_generic_exception_sync():
    mw = ToolErrorHandlingMiddleware()
    req = _FakeRequest(tool_call={"name": "x", "id": "c1"})

    def handler(_):
        raise RuntimeError("boom")

    res = mw.wrap_tool_call(req, handler)
    assert isinstance(res, ToolMessage)
    assert res.status == "error"
    assert "Error: Tool 'x' failed" in res.content


def test_clarification_passes_through_non_clarification():
    """非 ask_clarification 调用：ClarificationMiddleware 不拦截。"""
    mw = ClarificationMiddleware()
    req = _FakeRequest(tool_call={"name": "bash", "id": "c1", "args": {}})
    sentinel = ToolMessage(content="out", tool_call_id="c1", name="bash")
    res = mw.wrap_tool_call(req, lambda r: sentinel)
    assert res is sentinel


def test_clarification_intercepts_ask_clarification():
    mw = ClarificationMiddleware()
    req = _FakeRequest(tool_call={"name": "ask_clarification", "id": "c1", "args": {"question": "which?", "options": ["a", "b"]}})
    res = mw.wrap_tool_call(req, lambda r: ToolMessage(content="x", tool_call_id="c1", name="ask_clarification"))
    # Command(goto=END) — 检查有 goto + update messages
    assert hasattr(res, "goto")
    assert res.goto is not None
    msgs = res.update.get("messages", []) if isinstance(res.update, dict) else []
    assert msgs and "which?" in msgs[0].content


def test_clarification_stable_id_replaces_not_appends():
    """重试的澄清用确定性 id（替换而非追加）。"""
    mw = ClarificationMiddleware()
    mid = mw._stable_message_id("c1", "msg")
    assert mid == "clarification:c1"
    mid2 = mw._stable_message_id("", "msg")
    assert mid2.startswith("clarification:") and mid2 != "clarification:"


def test_clarification_options_json_string_normalized():
    mw = ClarificationMiddleware()
    msg = mw._format_clarification_message({"question": "q?", "options": '["x","y"]'})
    assert "1. x" in msg and "2. y" in msg


# ---------------------------------------------------------------------------
# tool_call_metadata helper
# ---------------------------------------------------------------------------


def test_clone_ai_message_syncs_raw_tool_calls():
    base = AIMessage(
        content="c",
        tool_calls=[{"id": "a", "name": "t", "args": {}}, {"id": "b", "name": "t2", "args": {}}],
        additional_kwargs={"tool_calls": [{"id": "a", "function": {"name": "t"}}, {"id": "b", "function": {"name": "t2"}}]},
        response_metadata={"finish_reason": "tool_calls"},
    )
    cloned = clone_ai_message_with_tool_calls(base, [{"id": "a", "name": "t", "args": {}}])
    assert [tc["id"] for tc in cloned.tool_calls] == ["a"]
    # raw 也同步：只剩 id=a。
    assert [rt["id"] for rt in cloned.additional_kwargs["tool_calls"]] == ["a"]


def test_clone_ai_message_empty_clears_finish_reason():
    base = AIMessage(content="c", tool_calls=[{"id": "a", "name": "t", "args": {}}], response_metadata={"finish_reason": "tool_calls"})
    cloned = clone_ai_message_with_tool_calls(base, [])
    assert cloned.tool_calls == []
    assert cloned.response_metadata["finish_reason"] == "stop"
    # 清空后 function_call 也删（若存在）。
    assert "function_call" not in cloned.additional_kwargs


# ---------------------------------------------------------------------------
# ToolErrorHandling — task 子代理状态贴标
# ---------------------------------------------------------------------------


def test_stamp_task_subagent_status_only_for_task():
    """非 task 工具不贴 subagent_status。"""
    msg = ToolMessage(content="completed\nresult", tool_call_id="c1", name="bash")
    out = _stamp_task_subagent_status(msg, tool_name="bash")
    assert "subagent_status" not in (out.additional_kwargs or {})


def test_stamp_task_subagent_status_for_task_terminal():
    """task 工具终态内容 → 贴 subagent_status（用契约认可的终态前缀）。"""
    msg = ToolMessage(content="Task Succeeded. Result: Done.", tool_call_id="c1", name="task")
    out = _stamp_task_subagent_status(msg, tool_name="task")
    ak = out.additional_kwargs or {}
    assert "subagent_status" in ak


def test_stamp_task_non_terminal_no_stamp():
    """非终态（如 'running\n...'）不贴标。"""
    msg = ToolMessage(content="running\nstill going", tool_call_id="c1", name="task")
    out = _stamp_task_subagent_status(msg, tool_name="task")
    assert "subagent_status" not in (out.additional_kwargs or {})


# ---------------------------------------------------------------------------
# LoopDetection — 哈希 + 频率
# ---------------------------------------------------------------------------


def test_hash_order_independent():
    a = _hash_tool_calls([{"name": "bash", "args": {"command": "ls"}, "id": "1"}, {"name": "read_file", "args": {"path": "x"}, "id": "2"}])
    b = _hash_tool_calls([{"name": "read_file", "args": {"path": "x"}, "id": "2"}, {"name": "bash", "args": {"command": "ls"}, "id": "1"}])
    assert a == b


def test_loop_detection_warn_then_hard_stop():
    mw = LoopDetectionMiddleware(warn_threshold=2, hard_limit=3, window_size=10)
    runtime = _runtime()
    tc = [{"name": "bash", "args": {"command": "ls"}, "id": "1"}]
    state = {"messages": [_ai(tool_calls=tc)]}

    # 第 1 次：无警告无硬停。
    w, h = mw._track_and_check(state, runtime)
    assert w is None and h is False
    # 第 2 次：达 warn_threshold → 警告。
    w, h = mw._track_and_check(state, runtime)
    assert w is not None and h is False
    # 第 3 次：达 hard_limit → 硬停。
    w, h = mw._track_and_check(state, runtime)
    assert h is True


def test_loop_detection_hard_stop_strips_tool_calls():
    mw = LoopDetectionMiddleware(warn_threshold=1, hard_limit=2, window_size=10)
    runtime = _runtime()
    tc = [{"name": "bash", "args": {"command": "ls"}, "id": "1"}]
    state = {"messages": [_ai(tool_calls=tc)]}
    mw._track_and_check(state, runtime)  # warn
    res = mw._apply(state, runtime)  # hard stop
    assert res is not None
    stripped = res["messages"][0]
    assert stripped.tool_calls == []
    assert "tool_calls" not in (stripped.additional_kwargs or {})


def test_loop_detection_tool_freq_layer():
    """频率层：同一工具不同参数调多次也触发。"""
    mw = LoopDetectionMiddleware(tool_freq_warn=2, tool_freq_hard_limit=3, window_size=10)
    runtime = _runtime()
    # 不同 path → 不同哈希（哈希层不触发），但同工具类型 → 频率层触发。
    for i in range(2):
        state = {"messages": [_ai(tool_calls=[{"name": "read_file", "args": {"path": f"f{i}"}, "id": str(i)}])]}
        w, h = mw._track_and_check(state, runtime)
    assert w is not None and "read_file" in w


def test_loop_detection_from_config():
    from deerflow.config import LoopDetectionConfig

    mw = LoopDetectionMiddleware.from_config(LoopDetectionConfig())
    assert mw.warn_threshold == 3 and mw.hard_limit == 5


# ---------------------------------------------------------------------------
# SafetyFinishReason
# ---------------------------------------------------------------------------


def test_default_detectors_count():
    ds = default_detectors()
    assert len(ds) == 3


def test_openai_content_filter_detector_hits():
    d = OpenAICompatibleContentFilterDetector()
    msg = AIMessage(content="x", tool_calls=[{"id": "1", "name": "t", "args": {}}], response_metadata={"finish_reason": "content_filter"})
    term = d.detect(msg)
    assert term is not None and term.reason_value == "content_filter"


def test_safety_middleware_suppresses_tool_calls_on_termination():
    mw = SafetyFinishReasonMiddleware()
    runtime = _runtime()
    msg = AIMessage(
        content="partial",
        tool_calls=[{"id": "1", "name": "write_file", "args": {}}],
        response_metadata={"finish_reason": "content_filter"},
    )
    state = {"messages": [msg]}
    res = mw._apply(state, runtime)
    assert res is not None
    patched = res["messages"][0]
    assert patched.tool_calls == []
    assert "safety_termination" in (patched.additional_kwargs or {})


def test_safety_middleware_no_tool_calls_passthrough():
    mw = SafetyFinishReasonMiddleware()
    runtime = _runtime()
    # content_filter 但无 tool_calls → 原样放行。
    msg = AIMessage(content="partial", tool_calls=[], response_metadata={"finish_reason": "content_filter"})
    assert mw._apply({"messages": [msg]}, runtime) is None


def test_safety_from_config_rejects_empty_detectors():
    from deerflow.config import SafetyFinishReasonConfig

    with pytest.raises(ValueError):
        SafetyFinishReasonMiddleware.from_config(SafetyFinishReasonConfig(detectors=[]))


def test_safety_from_config_none_uses_builtins():
    from deerflow.config import SafetyFinishReasonConfig

    mw = SafetyFinishReasonMiddleware.from_config(SafetyFinishReasonConfig(detectors=None))
    assert len(mw._detectors) == 3


# ---------------------------------------------------------------------------
# SubagentLimit
# ---------------------------------------------------------------------------


def test_clamp_subagent_limit_range():
    assert _clamp_subagent_limit(1) == 2
    assert _clamp_subagent_limit(3) == 3
    assert _clamp_subagent_limit(10) == 4


def test_subagent_limit_truncates_excess_task_calls():
    mw = SubagentLimitMiddleware(max_concurrent=2)
    tool_calls = [{"name": "task", "args": {}, "id": str(i)} for i in range(5)]
    state = {"messages": [_ai(tool_calls=tool_calls)]}
    res = mw._truncate_task_calls(state)
    assert res is not None
    kept = res["messages"][0].tool_calls
    assert len(kept) == 2
    assert all(tc["name"] == "task" for tc in kept)


def test_subagent_limit_no_truncation_when_within_limit():
    mw = SubagentLimitMiddleware(max_concurrent=3)
    state = {"messages": [_ai(tool_calls=[{"name": "task", "args": {}, "id": "1"}])]}
    assert mw._truncate_task_calls(state) is None


def test_subagent_limit_ignores_non_task_calls():
    mw = SubagentLimitMiddleware(max_concurrent=2)
    tcs = [{"name": "bash", "args": {}, "id": str(i)} for i in range(5)]
    state = {"messages": [_ai(tool_calls=tcs)]}
    assert mw._truncate_task_calls(state) is None


# ---------------------------------------------------------------------------
# DeferredToolFilter
# ---------------------------------------------------------------------------


def test_deferred_filter_hides_unpromoted_from_tools():
    mw = DeferredToolFilterMiddleware(frozenset({"mcp_a", "mcp_b"}), "hash1")
    tools = [SimpleNamespace(name="mcp_a"), SimpleNamespace(name="bash"), SimpleNamespace(name="mcp_b")]
    # 无提升 → 两个 mcp 都隐藏。
    req = _FakeRequest(tools=tools, state={})
    filtered = mw._filter_tools(req)
    assert [getattr(t, "name", t) for t in filtered.tools] == ["bash"]


def test_deferred_filter_promoted_reveals_tool():
    mw = DeferredToolFilterMiddleware(frozenset({"mcp_a"}), "hash1")
    tools = [SimpleNamespace(name="mcp_a"), SimpleNamespace(name="bash")]
    state = {"promoted": {"catalog_hash": "hash1", "names": ["mcp_a"]}}
    req = _FakeRequest(tools=tools, state=state)
    filtered = mw._filter_tools(req)
    assert {getattr(t, "name", t) for t in filtered.tools} == {"mcp_a", "bash"}


def test_deferred_filter_stale_hash_ignores_promotion():
    """catalog_hash 不匹配 → 视为陈旧提升，仍隐藏。"""
    mw = DeferredToolFilterMiddleware(frozenset({"mcp_a"}), "new_hash")
    tools = [SimpleNamespace(name="mcp_a")]
    state = {"promoted": {"catalog_hash": "old_hash", "names": ["mcp_a"]}}
    req = _FakeRequest(tools=tools, state=state)
    filtered = mw._filter_tools(req)
    assert filtered.tools == []


def test_deferred_filter_blocks_unpromoted_tool_call():
    mw = DeferredToolFilterMiddleware(frozenset({"mcp_a"}), "hash1")
    req = _FakeRequest(tool_call={"name": "mcp_a", "id": "c1", "args": {}}, state={})
    res = mw.wrap_tool_call(req, lambda r: ToolMessage(content="x", tool_call_id="c1", name="mcp_a"))
    assert isinstance(res, ToolMessage)
    assert res.status == "error" and "deferred" in res.content.lower()


def test_deferred_filter_allows_promoted_tool_call():
    mw = DeferredToolFilterMiddleware(frozenset({"mcp_a"}), "hash1")
    state = {"promoted": {"catalog_hash": "hash1", "names": ["mcp_a"]}}
    req = _FakeRequest(tool_call={"name": "mcp_a", "id": "c1", "args": {}}, state=state)
    sentinel = ToolMessage(content="ok", tool_call_id="c1", name="mcp_a")
    res = mw.wrap_tool_call(req, lambda r: sentinel)
    assert res is sentinel


# ---------------------------------------------------------------------------
# DanglingToolCall
# ---------------------------------------------------------------------------


def test_dangling_patches_missing_tool_response():
    mw = DanglingToolCallMiddleware()
    msgs = [_ai(tool_calls=[{"name": "bash", "args": {}, "id": "c1"}])]
    out = mw._build_patched_messages(msgs)
    assert out is not None
    # AIMessage 后紧跟一条合成 ToolMessage。
    assert isinstance(out[1], ToolMessage)
    assert out[1].tool_call_id == "c1"


def test_dangling_no_op_when_complete():
    mw = DanglingToolCallMiddleware()
    msgs = [_ai(tool_calls=[{"name": "bash", "args": {}, "id": "c1"}]), ToolMessage(content="out", tool_call_id="c1", name="bash")]
    assert mw._build_patched_messages(msgs) is None


def test_dangling_synthetic_content_invalid():
    mw = DanglingToolCallMiddleware()
    content = mw._synthetic_tool_message_content({"invalid": True, "name": "read_file"})
    assert "invalid" in content


# ---------------------------------------------------------------------------
# ToolOutputBudget
# ---------------------------------------------------------------------------


def test_message_text_extracts_string():
    assert _message_text("hello") == "hello"
    assert _message_text(None) is None
    assert _message_text([{"type": "text", "text": "a"}, "b"]) == "a\nb"
    assert _message_text([{"type": "image", "url": "x"}]) is None  # 多模态未知块 → None


def test_snap_to_line_boundary():
    assert _snap_to_line_boundary("abc\ndef\nghi", 6) == 4  # 对齐到 \n+1
    assert _snap_to_line_boundary("abcdef", 3) == 3  # 无换行 → 原 pos


def test_tool_output_budget_externalizes_oversized(tmp_path):
    from deerflow.agents.middlewares.tool_output_budget_middleware import _budget_content
    from deerflow.config import ToolOutputConfig

    cfg = ToolOutputConfig(externalize_min_chars=10, fallback_max_chars=0)
    big = "x" * 100
    outputs = str(tmp_path)
    out = _budget_content(big, tool_name="bash", tool_call_id="c1", outputs_path=outputs, config=cfg)
    assert out is not None
    assert "saved to /mnt/user-data/outputs" in out


def test_tool_output_budget_small_passthrough():
    from deerflow.agents.middlewares.tool_output_budget_middleware import _budget_content
    from deerflow.config import ToolOutputConfig

    cfg = ToolOutputConfig(externalize_min_chars=1000, fallback_max_chars=1000)
    assert _budget_content("tiny", tool_name="bash", tool_call_id="c1", outputs_path=None, config=cfg) is None


def test_tool_output_budget_exempt_tools_skipped():
    from deerflow.agents.middlewares.tool_output_budget_middleware import _patch_tool_message
    from deerflow.config import ToolOutputConfig

    cfg = ToolOutputConfig(externalize_min_chars=1, exempt_tools=["read_file"])
    msg = ToolMessage(content="x" * 5000, tool_call_id="c1", name="read_file")
    out = _patch_tool_message(msg, cfg, outputs_path=None, sandbox=None)
    assert out is msg  # 免预算 → 原样


# ---------------------------------------------------------------------------
# ThreadData
# ---------------------------------------------------------------------------


def test_thread_data_computes_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("DEER_FLOW_HOME", str(tmp_path / "home"))
    mw = ThreadDataMiddleware(lazy_init=True)
    runtime = _runtime({"thread_id": "t1", "run_id": "r1"})
    state = {"messages": [HumanMessage(content="hi")]}
    res = mw.before_agent(state, runtime)
    td = res["thread_data"]
    assert "workspace_path" in td and "uploads_path" in td and "outputs_path" in td
    assert "t1" in td["workspace_path"]


def test_thread_data_requires_thread_id():
    import pytest as _pytest

    mw = ThreadDataMiddleware()
    runtime = SimpleNamespace(context={})  # 无 thread_id，也无 config
    with _pytest.raises(ValueError):
        mw.before_agent({"messages": []}, runtime)


def test_thread_data_stamps_run_id_on_human(monkeypatch, tmp_path):
    monkeypatch.setenv("DEER_FLOW_HOME", str(tmp_path / "home"))
    mw = ThreadDataMiddleware()
    runtime = _runtime({"thread_id": "t1", "run_id": "r1"})
    state = {"messages": [HumanMessage(content="hi", id="h1")]}
    res = mw.before_agent(state, runtime)
    last = res["messages"][-1]
    assert last.additional_kwargs.get("run_id") == "r1"


# ---------------------------------------------------------------------------
# Uploads
# ---------------------------------------------------------------------------


def test_uploads_no_files_noop():
    mw = UploadsMiddleware()
    runtime = _runtime({"thread_id": "t1"})
    state = {"messages": [HumanMessage(content="hi")]}
    assert mw.before_agent(state, runtime) is None


def test_uploads_files_from_kwargs_filters_traversal():
    mw = UploadsMiddleware()
    hm = HumanMessage(content="hi", additional_kwargs={"files": [{"filename": "../etc/passwd", "size": 1}]})
    # Path("../etc/passwd").name != "../etc/passwd" → 被过滤。
    assert mw._files_from_kwargs(hm) is None


def test_uploads_injects_block_when_files_present(monkeypatch, tmp_path):
    monkeypatch.setenv("DEER_FLOW_HOME", str(tmp_path / "home"))
    mw = UploadsMiddleware()
    tid = "t1"
    # conftest autouse user 上下文已注入；用同一 get_effective_user_id 建物理文件。
    from deerflow.config.paths import get_paths
    from deerflow.runtime.user_context import get_effective_user_id

    uid = get_effective_user_id()
    uploads_dir = get_paths().sandbox_uploads_dir(tid, user_id=uid)
    uploads_dir.mkdir(parents=True, exist_ok=True)
    (uploads_dir / "a.pdf").write_bytes(b"%PDF-1.4 stub")
    hm = HumanMessage(content="hi", additional_kwargs={"files": [{"filename": "a.pdf", "size": 13}]})
    runtime = _runtime({"thread_id": tid})
    res = mw.before_agent({"messages": [hm]}, runtime)
    assert res is not None
    assert "<uploaded_files>" in res["messages"][-1].content


# ---------------------------------------------------------------------------
# TitleMiddleware
# ---------------------------------------------------------------------------


def test_title_normalize_content():
    from deerflow.agents.middlewares.title_middleware import TitleMiddleware

    mw = TitleMiddleware(app_config=AppConfig())
    assert mw._normalize_content("abc") == "abc"
    assert mw._normalize_content([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]) == "a\nb"
    assert mw._normalize_content({"text": "x"}) == "x"


def test_title_strip_think_tags():
    from deerflow.agents.middlewares.title_middleware import TitleMiddleware

    mw = TitleMiddleware(app_config=AppConfig())
    assert mw._strip_think_tags("a<think>hidden</think>b") == "ab"


def test_title_should_generate_only_after_first_exchange():
    from deerflow.agents.middlewares.title_middleware import TitleMiddleware

    mw = TitleMiddleware(app_config=AppConfig())
    # 只有用户消息 → False。
    assert mw._should_generate_title({"messages": [HumanMessage(content="hi")]}) is False
    # 用户 + AI 且已有标题 → False（幂等）。
    assert mw._should_generate_title({"title": "x", "messages": [HumanMessage(content="hi"), _ai()]}) is False
    # 首轮完整 → True。
    assert mw._should_generate_title({"messages": [HumanMessage(content="hi"), _ai()]}) is True


def test_title_fallback_local():
    from deerflow.agents.middlewares.title_middleware import TitleMiddleware

    mw = TitleMiddleware(app_config=AppConfig())
    assert mw._fallback_title("short") == "short"
    long = "x" * 200
    fb = mw._fallback_title(long)
    assert fb.endswith("...")


# ---------------------------------------------------------------------------
# LLMErrorHandling
# ---------------------------------------------------------------------------


def test_llm_error_classifies_quota_auth_transient():
    from deerflow.agents.middlewares.llm_error_handling_middleware import LLMErrorHandlingMiddleware

    mw = LLMErrorHandlingMiddleware(app_config=AppConfig())
    assert mw._classify_error(RuntimeError("insufficient_quota"))[1] == "quota"
    assert mw._classify_error(RuntimeError("invalid api key"))[1] == "auth"
    assert mw._classify_error(RuntimeError("rate limit exceeded"))[1] == "transient" or mw._classify_error(RuntimeError("rate limit exceeded"))[0] is True


def test_llm_error_circuit_breaker_opens_after_threshold():
    from deerflow.agents.middlewares.llm_error_handling_middleware import LLMErrorHandlingMiddleware

    mw = LLMErrorHandlingMiddleware(app_config=AppConfig(circuit_breaker={"failure_threshold": 2, "recovery_timeout_sec": 30}))
    # 两次失败 → 熔断打开。
    mw._record_failure()
    mw._record_failure()
    assert mw._check_circuit() is True


def test_llm_error_success_resets_circuit():
    from deerflow.agents.middlewares.llm_error_handling_middleware import LLMErrorHandlingMiddleware

    mw = LLMErrorHandlingMiddleware(app_config=AppConfig(circuit_breaker={"failure_threshold": 1, "recovery_timeout_sec": 30}))
    mw._record_failure()
    assert mw._check_circuit() is True
    mw._record_success()
    assert mw._check_circuit() is False


def test_llm_error_wraps_generic_to_fallback():
    from deerflow.agents.middlewares.llm_error_handling_middleware import LLMErrorHandlingMiddleware

    mw = LLMErrorHandlingMiddleware(app_config=AppConfig())
    req = _FakeRequest(messages=[])
    attempts = {"n": 0}

    def handler(_):
        attempts["n"] += 1
        raise RuntimeError("fatal boom")

    res = mw.wrap_model_call(req, handler)
    assert isinstance(res, AIMessage)
    assert res.additional_kwargs.get("deerflow_error_fallback") is True
    # fatal 错误不重试，只调一次。
    assert attempts["n"] == 1


def test_llm_error_retries_transient(monkeypatch):
    from deerflow.agents.middlewares.llm_error_handling_middleware import LLMErrorHandlingMiddleware

    mw = LLMErrorHandlingMiddleware(app_config=AppConfig())
    monkeypatch.setattr("time.sleep", lambda _: None)
    req = _FakeRequest(messages=[])
    calls = {"n": 0}

    def handler(_):
        calls["n"] += 1
        if calls["n"] < 2:
            raise RuntimeError("The server is overloaded")
        return AIMessage(content="recovered")

    res = mw.wrap_model_call(req, handler)
    assert isinstance(res, AIMessage)
    assert res.content == "recovered"
    assert calls["n"] == 2


# ---------------------------------------------------------------------------
# build_subagent_runtime_middlewares
# ---------------------------------------------------------------------------


def test_subagent_runtime_omits_uploads():
    from deerflow.agents.middlewares.tool_error_handling_middleware import build_subagent_runtime_middlewares

    mws = build_subagent_runtime_middlewares(app_config=AppConfig())
    names = _names(mws)
    assert "UploadsMiddleware" not in names  # subagent 不含 uploads
    assert "ThreadDataMiddleware" in names
    assert "ToolErrorHandlingMiddleware" in names


def test_subagent_runtime_adds_safety_when_enabled():
    from deerflow.agents.middlewares.tool_error_handling_middleware import build_subagent_runtime_middlewares

    mws = build_subagent_runtime_middlewares(app_config=AppConfig())
    assert any(isinstance(m, SafetyFinishReasonMiddleware) for m in mws)
