"""``test_subagents.py`` —— 子代理系统（M11）hermetic 测试。

覆盖：
- ``SubagentConfig`` dataclass 默认值 + ``resolve_subagent_model_name`` 三优先级。
- ``SubagentsAppConfig`` 自定义/per-agent 配置 + helper。
- registry：内置 + 自定义 + per-agent 覆盖合并、``get_available_subagent_names`` host-bash 隐藏。
- status_contract：加载 ``contracts/subagent_status_contract.json`` 全 fixture + extract + make_kwargs。
- token_collector：去重 + 多代累计 + snapshot 副本。
- executor：单 scheduler pool + 持久化隔离事件循环复用、后台任务生命周期、协作取消、
  超时、result holder 幂等终态、cleanup、``_filter_tools``、降级（skills/tool_search 缺包）。

hermetic：agent 构造经 monkeypatch 注入 fake（不碰真模型）；隔离循环/后台任务的信号
注册保持原样（daemon 线程，进程退出自动收）；后台任务存储每测试清理。
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration

from deerflow.config.subagents_config import (
    CustomSubagentConfig,
    SubagentOverrideConfig,
    SubagentsAppConfig,
)
from deerflow.subagents import (
    MAX_CONCURRENT_SUBAGENTS,
    SubagentConfig,
    SubagentExecutor,
    SubagentResult,
    SubagentStatus,
    cleanup_background_task,
    get_available_subagent_names,
    get_background_task_result,
    get_subagent_config,
    get_subagent_names,
    list_background_tasks,
    list_subagents,
    request_cancel_background_task,
)
from deerflow.subagents import executor as executor_module
from deerflow.subagents.builtins import BASH_AGENT_CONFIG, BUILTIN_SUBAGENTS, GENERAL_PURPOSE_CONFIG
from deerflow.subagents.config import _default_model_name, resolve_subagent_model_name
from deerflow.subagents.status_contract import (
    SUBAGENT_ERROR_KEY,
    SUBAGENT_STATUS_KEY,
    SUBAGENT_STATUS_VALUES,
    extract_subagent_status,
    make_subagent_additional_kwargs,
)
from deerflow.subagents.token_collector import SubagentTokenCollector

# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------

CONTRACTS_DIR = Path(__file__).resolve().parent.parent / "contracts"
CONTRACT_JSON = CONTRACTS_DIR / "subagent_status_contract.json"


def _load_contract() -> dict:
    """加载 contracts/subagent_status_contract.json（status_contract 测试的单一真相源）。"""
    return json.loads(CONTRACT_JSON.read_text(encoding="utf-8"))


def _make_app_config(
    *,
    models=("gpt-4o",),
    custom_agents: dict | None = None,
    agents: dict | None = None,
    timeout_seconds: int = 1800,
    max_turns: int | None = None,
    sandbox_use: str = "deerflow.sandbox.local:LocalSandboxProvider",
    allow_host_bash: bool = False,
    tool_search_enabled: bool = False,
):
    """造一个轻量 AppConfig 替身（只填 subagents 用到的字段）。"""
    subagents = SubagentsAppConfig(
        timeout_seconds=timeout_seconds,
        max_turns=max_turns,
        agents=agents or {},
        custom_agents=custom_agents or {},
    )
    sandbox = SimpleNamespace(use=sandbox_use, allow_host_bash=allow_host_bash)
    tool_search = SimpleNamespace(enabled=tool_search_enabled)
    model_list = [SimpleNamespace(name=n, supports_vision=False) for n in models]

    def _get_model_config(name):
        for m in model_list:
            if m.name == name:
                return m
        return model_list[0] if model_list else None

    return SimpleNamespace(
        models=model_list,
        subagents=subagents,
        sandbox=sandbox,
        tool_search=tool_search,
        # M16 起子代理运行时中间件链读这些字段（LLMErrorHandling / SafetyFinishReason / Memory）。
        circuit_breaker=SimpleNamespace(failure_threshold=5, recovery_timeout_sec=30),
        safety_finish_reason=SimpleNamespace(enabled=False),
        memory=SimpleNamespace(enabled=False),
        get_model_config=_get_model_config,
    )


@pytest.fixture(autouse=True)
def _clear_background_tasks():
    """每个测试前后清空后台任务全局存储，防跨测试污染。"""
    with executor_module._background_tasks_lock:
        executor_module._background_tasks.clear()
    yield
    with executor_module._background_tasks_lock:
        executor_module._background_tasks.clear()


# ===========================================================================
# SubagentConfig + resolve_subagent_model_name
# ===========================================================================


class TestSubagentConfig:
    def test_dataclass_defaults(self):
        c = SubagentConfig(name="x", description="d")
        assert c.system_prompt is None
        assert c.tools is None
        assert c.disallowed_tools == ["task"]
        assert c.skills is None
        assert c.model == "inherit"
        assert c.max_turns == 50
        assert c.timeout_seconds == 900

    def test_builtin_general_purpose(self):
        assert GENERAL_PURPOSE_CONFIG.name == "general-purpose"
        assert GENERAL_PURPOSE_CONFIG.tools is None  # 继承全部
        assert GENERAL_PURPOSE_CONFIG.max_turns == 150
        assert "task" in GENERAL_PURPOSE_CONFIG.disallowed_tools
        assert "ask_clarification" in GENERAL_PURPOSE_CONFIG.disallowed_tools
        assert "present_files" in GENERAL_PURPOSE_CONFIG.disallowed_tools

    def test_builtin_bash(self):
        assert BASH_AGENT_CONFIG.name == "bash"
        assert BASH_AGENT_CONFIG.tools == ["bash", "ls", "read_file", "write_file", "str_replace"]
        assert BASH_AGENT_CONFIG.max_turns == 60

    def test_builtin_registry_keys(self):
        assert set(BUILTIN_SUBAGENTS.keys()) == {"general-purpose", "bash"}

    def test_resolve_model_explicit(self):
        c = SubagentConfig(name="x", description="d", model="gpt-4o-mini")
        assert resolve_subagent_model_name(c, parent_model="gpt-4o") == "gpt-4o-mini"

    def test_resolve_model_inherit_parent(self):
        c = SubagentConfig(name="x", description="d", model="inherit")
        assert resolve_subagent_model_name(c, parent_model="claude-3") == "claude-3"

    def test_resolve_model_inherit_fallback_app_config(self):
        c = SubagentConfig(name="x", description="d", model="inherit")
        app = _make_app_config(models=("first-model", "second"))
        assert resolve_subagent_model_name(c, parent_model=None, app_config=app) == "first-model"

    def test_resolve_model_inherit_no_models_raises(self):
        c = SubagentConfig(name="x", description="d", model="inherit")
        app = _make_app_config(models=())
        with pytest.raises(ValueError, match="No chat models"):
            resolve_subagent_model_name(c, parent_model=None, app_config=app)

    def test_default_model_name_helper(self):
        app = _make_app_config(models=("a", "b"))
        assert _default_model_name(app) == "a"


# ===========================================================================
# SubagentsAppConfig + helpers
# ===========================================================================


class TestSubagentsAppConfig:
    def test_defaults(self):
        cfg = SubagentsAppConfig()
        assert cfg.enabled is True
        assert cfg.max_concurrent == 3
        assert cfg.timeout_seconds == 1800
        assert cfg.max_turns is None
        assert cfg.agents == {}
        assert cfg.custom_agents == {}

    def test_get_timeout_for_override(self):
        cfg = SubagentsAppConfig(
            timeout_seconds=1000,
            agents={"bash": SubagentOverrideConfig(timeout_seconds=50)},
        )
        assert cfg.get_timeout_for("bash") == 50
        assert cfg.get_timeout_for("general-purpose") == 1000  # 回退全局

    def test_get_model_for_override(self):
        cfg = SubagentsAppConfig(agents={"bash": SubagentOverrideConfig(model="gpt-4o-mini")})
        assert cfg.get_model_for("bash") == "gpt-4o-mini"
        assert cfg.get_model_for("general-purpose") is None

    def test_get_max_turns_for_layers(self):
        cfg = SubagentsAppConfig(
            max_turns=99,
            agents={"bash": SubagentOverrideConfig(max_turns=33)},
        )
        # per-agent 覆盖 > 全局 > 内置默认
        assert cfg.get_max_turns_for("bash", builtin_default=60) == 33
        assert cfg.get_max_turns_for("general-purpose", builtin_default=150) == 99
        # 无全局、无覆盖 -> 内置默认
        cfg2 = SubagentsAppConfig()
        assert cfg2.get_max_turns_for("bash", builtin_default=60) == 60

    def test_get_skills_for_override(self):
        cfg = SubagentsAppConfig(agents={"bash": SubagentOverrideConfig(skills=["s1"])})
        assert cfg.get_skills_for("bash") == ["s1"]
        assert cfg.get_skills_for("general-purpose") is None

    def test_custom_subagent_config_defaults(self):
        c = CustomSubagentConfig(description="d", system_prompt="p")
        assert c.tools is None
        assert c.disallowed_tools == ["task", "ask_clarification", "present_files"]
        assert c.model == "inherit"
        assert c.max_turns == 50
        assert c.timeout_seconds == 900


# ===========================================================================
# registry
# ===========================================================================


class TestRegistry:
    def test_get_builtin_general_purpose(self):
        app = _make_app_config()
        cfg = get_subagent_config("general-purpose", app_config=app)
        assert cfg is not None
        assert cfg.name == "general-purpose"
        assert cfg.max_turns == 150

    def test_get_builtin_applies_global_timeout(self):
        # 内置子代理的 timeout 被全局 timeout_seconds 覆盖（与内置值不同时）
        app = _make_app_config(timeout_seconds=1234)
        cfg = get_subagent_config("general-purpose", app_config=app)
        assert cfg.timeout_seconds == 1234

    def test_get_builtin_global_max_turns_override(self):
        app = _make_app_config(max_turns=77)
        cfg = get_subagent_config("bash", app_config=app)
        assert cfg.max_turns == 77

    def test_get_unknown_returns_none(self):
        app = _make_app_config()
        assert get_subagent_config("nope", app_config=app) is None

    def test_custom_subagent(self):
        custom = {
            "researcher": CustomSubagentConfig(
                description="research stuff",
                system_prompt="you research",
                max_turns=40,
                timeout_seconds=500,
            )
        }
        app = _make_app_config(custom_agents=custom)
        cfg = get_subagent_config("researcher", app_config=app)
        assert cfg is not None
        assert cfg.name == "researcher"
        assert cfg.system_prompt == "you research"
        assert cfg.max_turns == 40
        # 自定义子代理的 timeout 不被全局覆盖（自带默认）
        assert cfg.timeout_seconds == 500

    def test_custom_subagent_global_does_not_override_timeout(self):
        # 自定义子代理用自身 timeout_seconds；全局 1800 不压它（仅压内置）
        custom = {"x": CustomSubagentConfig(description="d", system_prompt="p", timeout_seconds=200)}
        app = _make_app_config(custom_agents=custom, timeout_seconds=9999)
        cfg = get_subagent_config("x", app_config=app)
        assert cfg.timeout_seconds == 200

    def test_per_agent_override_layers_on_builtin(self):
        agents = {"bash": SubagentOverrideConfig(timeout_seconds=11, max_turns=22, model="gpt-4o-mini", skills=["s"])}
        app = _make_app_config(agents=agents)
        cfg = get_subagent_config("bash", app_config=app)
        assert cfg.timeout_seconds == 11
        assert cfg.max_turns == 22
        assert cfg.model == "gpt-4o-mini"
        assert cfg.skills == ["s"]

    def test_per_agent_override_layers_on_custom(self):
        custom = {"researcher": CustomSubagentConfig(description="d", system_prompt="p", max_turns=40)}
        agents = {"researcher": SubagentOverrideConfig(max_turns=88)}
        app = _make_app_config(custom_agents=custom, agents=agents)
        cfg = get_subagent_config("researcher", app_config=app)
        assert cfg.max_turns == 88

    def test_merge_order_builtin_custom_override(self):
        # built-in 优先于 custom（同名时）
        custom = {"bash": CustomSubagentConfig(description="fake", system_prompt="fake")}
        app = _make_app_config(custom_agents=custom)
        cfg = get_subagent_config("bash", app_config=app)
        # 内置 bash 赢，description 是内置的
        assert "Command execution specialist" in cfg.description

    def test_get_subagent_names_builtin_plus_custom(self):
        custom = {"researcher": CustomSubagentConfig(description="d", system_prompt="p")}
        app = _make_app_config(custom_agents=custom)
        names = get_subagent_names(app_config=app)
        assert "general-purpose" in names
        assert "bash" in names
        assert "researcher" in names

    def test_list_subagents(self):
        custom = {"researcher": CustomSubagentConfig(description="d", system_prompt="p")}
        app = _make_app_config(custom_agents=custom)
        configs = list_subagents(app_config=app)
        names = {c.name for c in configs}
        assert names == {"general-purpose", "bash", "researcher"}

    def test_available_names_hides_bash_when_host_bash_disabled(self):
        # LocalSandboxProvider + allow_host_bash=False -> is_host_bash_allowed False -> 隐藏 bash
        app = _make_app_config(sandbox_use="deerflow.sandbox.local:LocalSandboxProvider", allow_host_bash=False)
        names = get_available_subagent_names(app_config=app)
        assert "general-purpose" in names
        assert "bash" not in names

    def test_available_names_shows_bash_when_host_bash_allowed(self):
        app = _make_app_config(allow_host_bash=True)
        names = get_available_subagent_names(app_config=app)
        assert "bash" in names

    def test_available_names_shows_bash_for_non_local_provider(self):
        # 非 Local provider（如 AIO）-> is_host_bash_allowed True -> 不隐藏
        app = _make_app_config(sandbox_use="deerflow.community.aio_sandbox:AioSandboxProvider")
        names = get_available_subagent_names(app_config=app)
        assert "bash" in names


# ===========================================================================
# status_contract（加载 contracts/subagent_status_contract.json 全 fixture）
# ===========================================================================


class TestStatusContract:
    def test_values_match_fixture(self):
        contract = _load_contract()
        # 模块的 SUBAGENT_STATUS_VALUES 与 fixture 的 valid_status_values 必须一致（单一真相源）
        assert set(SUBAGENT_STATUS_VALUES) == set(contract["valid_status_values"])

    def test_all_fixture_cases(self):
        contract = _load_contract()
        # 13 个 fixture case 全映射
        for case in contract["cases"]:
            got = extract_subagent_status(case["content"])
            expected = case["expected_status"]
            assert got == expected, f"case {case['name']!r}: got {got!r}, want {expected!r}"

    def test_make_kwargs_drops_empty_error(self):
        kw = make_subagent_additional_kwargs("completed", error="   ")
        assert SUBAGENT_ERROR_KEY not in kw
        assert kw[SUBAGENT_STATUS_KEY] == "completed"

    def test_make_kwargs_with_error(self):
        kw = make_subagent_additional_kwargs("failed", error="boom")
        assert kw == {SUBAGENT_STATUS_KEY: "failed", SUBAGENT_ERROR_KEY: "boom"}

    def test_make_kwargs_invalid_status_raises(self):
        with pytest.raises(ValueError, match="invalid subagent status"):
            make_subagent_additional_kwargs("running")  # type: ignore[arg-type]

    def test_make_kwargs_rejects_arbitrary_string(self):
        with pytest.raises(ValueError):
            make_subagent_additional_kwargs("typo_status")  # type: ignore[arg-type]

    def test_extract_non_terminal_returns_none(self):
        assert extract_subagent_status("Investigating ...") is None
        assert extract_subagent_status("") is None

    def test_prefix_ordering_polling_before_timed_out(self):
        # "Task polling timed out" 必须先于 "Task timed out" 匹配
        assert extract_subagent_status("Task polling timed out after 15 minutes") == "polling_timed_out"
        assert extract_subagent_status("Task timed out. Error: 900 seconds") == "timed_out"


# ===========================================================================
# token_collector
# ===========================================================================


def _gen(content_msg, usage):
    """造一个 ChatGeneration，含带 usage_metadata 的 message。"""
    msg = AIMessage(content=content_msg)
    msg.usage_metadata = usage
    return ChatGeneration(message=msg)


class TestTokenCollector:
    def test_collects_usage(self):
        col = SubagentTokenCollector(caller="subagent:bash")
        resp = SimpleNamespace(generations=[[_gen("hi", {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})]])
        col.on_llm_end(resp, run_id="r1")
        recs = col.snapshot_records()
        assert len(recs) == 1
        assert recs[0]["caller"] == "subagent:bash"
        assert recs[0]["input_tokens"] == 10
        assert recs[0]["total_tokens"] == 15

    def test_dedup_same_run_id(self):
        col = SubagentTokenCollector(caller="c")
        resp = SimpleNamespace(generations=[[_gen("hi", {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})]])
        col.on_llm_end(resp, run_id="r1")
        col.on_llm_end(resp, run_id="r1")  # 同 run_id 不双计
        assert len(col.snapshot_records()) == 1

    def test_multiple_runs_accumulate(self):
        col = SubagentTokenCollector(caller="c")
        resp1 = SimpleNamespace(generations=[[_gen("a", {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})]])
        resp2 = SimpleNamespace(generations=[[_gen("b", {"input_tokens": 3, "output_tokens": 3, "total_tokens": 6})]])
        col.on_llm_end(resp1, run_id="r1")
        col.on_llm_end(resp2, run_id="r2")
        recs = col.snapshot_records()
        assert len(recs) == 2
        assert {r["source_run_id"] for r in recs} == {"r1", "r2"}

    def test_skips_zero_usage(self):
        col = SubagentTokenCollector(caller="c")
        resp = SimpleNamespace(generations=[[_gen("a", {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0})]])
        col.on_llm_end(resp, run_id="r1")
        assert col.snapshot_records() == []

    def test_total_recomputed_from_input_output(self):
        col = SubagentTokenCollector(caller="c")
        # total_tokens 缺省 -> 从 input+output 补
        resp = SimpleNamespace(generations=[[_gen("a", {"input_tokens": 7, "output_tokens": 3})]])
        col.on_llm_end(resp, run_id="r1")
        recs = col.snapshot_records()
        assert recs[0]["total_tokens"] == 10

    def test_snapshot_is_copy(self):
        col = SubagentTokenCollector(caller="c")
        resp = SimpleNamespace(generations=[[_gen("a", {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})]])
        col.on_llm_end(resp, run_id="r1")
        snap = col.snapshot_records()
        snap.clear()
        # 清副本不影响内部
        assert len(col.snapshot_records()) == 1


# ===========================================================================
# executor machinery（单 scheduler pool + 持久化隔离事件循环）
# ===========================================================================


class _FakeAgent:
    """带 ``.astream`` 的假 agent——产出预设 chunk 后结束。"""

    def __init__(self, chunks, cancel_check=False):
        self._chunks = chunks
        self.cancel_check = cancel_check

    async def astream(self, state, config=None, context=None, stream_mode=None):
        for ch in self._chunks:
            yield ch


def _chunk_with_ai(text, msg_id="m1"):
    return {"messages": [AIMessage(content=text, id=msg_id)]}


class TestExecutorMachinery:
    def test_max_concurrent_constant(self):
        assert MAX_CONCURRENT_SUBAGENTS == 3

    def test_single_scheduler_pool_not_dual(self):
        # 红线 #34：只有 _scheduler_pool，没有 _execution_pool
        assert isinstance(executor_module._scheduler_pool, ThreadPoolExecutor)
        assert executor_module._scheduler_pool._max_workers == 3
        assert not hasattr(executor_module, "_execution_pool"), "executor 不应有第二线程池（红线 #34）"

    def test_isolated_loop_reused_across_calls(self):
        loop1 = executor_module._get_isolated_subagent_loop()
        loop2 = executor_module._get_isolated_subagent_loop()
        assert loop1 is loop2
        assert loop1.is_running()

    def test_isolated_loop_runs_on_daemon_thread(self):
        executor_module._get_isolated_subagent_loop()
        thread = executor_module._isolated_subagent_loop_thread
        assert thread is not None
        assert thread.daemon is True
        assert thread.is_alive()

    def test_filter_tools_allowlist(self):
        # _filter_tools 只读 .name，用 SimpleNamespace 做 tool 替身
        tools = [SimpleNamespace(name=n) for n in ("a", "b", "c")]
        out = executor_module._filter_tools(tools, allowed=["a", "c"], disallowed=None)
        assert [x.name for x in out] == ["a", "c"]

    def test_filter_tools_denylist(self):
        tools = [SimpleNamespace(name=n) for n in ("a", "b", "c")]
        out = executor_module._filter_tools(tools, allowed=None, disallowed=["b"])
        assert [x.name for x in out] == ["a", "c"]

    def test_filter_tools_both(self):
        tools = [SimpleNamespace(name=n) for n in ("a", "b", "c", "d")]
        out = executor_module._filter_tools(tools, allowed=["a", "b", "c"], disallowed=["b"])
        assert [x.name for x in out] == ["a", "c"]


class TestSubagentResult:
    def test_try_set_terminal_once(self):
        r = SubagentResult(task_id="t", trace_id="tr", status=SubagentStatus.RUNNING)
        assert r.try_set_terminal(SubagentStatus.COMPLETED, result="ok") is True
        assert r.status == SubagentStatus.COMPLETED
        assert r.result == "ok"
        assert r.completed_at is not None

    def test_try_set_terminal_idempotent(self):
        r = SubagentResult(task_id="t", trace_id="tr", status=SubagentStatus.RUNNING)
        assert r.try_set_terminal(SubagentStatus.COMPLETED, result="first") is True
        # 第二次终态写入不生效
        assert r.try_set_terminal(SubagentStatus.FAILED, error="late") is False
        assert r.status == SubagentStatus.COMPLETED
        assert r.result == "first"
        assert r.error is None

    def test_try_set_terminal_rejects_non_terminal(self):
        r = SubagentResult(task_id="t", trace_id="tr", status=SubagentStatus.PENDING)
        with pytest.raises(ValueError, match="not terminal"):
            r.try_set_terminal(SubagentStatus.RUNNING)

    def test_is_terminal_property(self):
        assert SubagentStatus.COMPLETED.is_terminal
        assert SubagentStatus.FAILED.is_terminal
        assert SubagentStatus.CANCELLED.is_terminal
        assert SubagentStatus.TIMED_OUT.is_terminal
        assert not SubagentStatus.PENDING.is_terminal
        assert not SubagentStatus.RUNNING.is_terminal


class TestExecutorExecution:
    """执行器执行路径——monkeypatch _create_agent 注入假 agent，不碰真模型。"""

    def _make_executor(self, app_config=None):
        cfg = SubagentConfig(name="general-purpose", description="d", system_prompt="sp", max_turns=5, timeout_seconds=10)
        return SubagentExecutor(
            config=cfg,
            tools=[],
            app_config=app_config or _make_app_config(),
            parent_model="gpt-4o",
            thread_id="thread-1",
        )

    def test_execute_sync_completes(self, monkeypatch):
        ex = self._make_executor()
        monkeypatch.setattr(ex, "_create_agent", lambda tools=None, deferred_setup=None: _FakeAgent([_chunk_with_ai("done", "m1")]))
        result = ex.execute("do something")
        assert result.status == SubagentStatus.COMPLETED
        assert result.result == "done"
        assert result.ai_messages and result.ai_messages[0]["content"] == "done"

    def test_execute_dedups_ai_messages_by_id(self, monkeypatch):
        ex = self._make_executor()
        # 同 id 的 AIMessage 出现两次（values 流式会重发累积态）-> 只记一次
        chunks = [_chunk_with_ai("partial", "m1"), _chunk_with_ai("partial", "m1"), _chunk_with_ai("final", "m1")]
        monkeypatch.setattr(ex, "_create_agent", lambda tools=None, deferred_setup=None: _FakeAgent(chunks))
        result = ex.execute("task")
        assert result.status == SubagentStatus.COMPLETED
        assert len(result.ai_messages) == 1

    def test_execute_failed_on_exception(self, monkeypatch):
        ex = self._make_executor()

        class _BoomAgent:
            async def astream(self, state, config=None, context=None, stream_mode=None):
                raise RuntimeError("boom")
                yield  # 让函数成为 async generator（unreachable，pragma 免覆盖）

        monkeypatch.setattr(ex, "_create_agent", lambda tools=None, deferred_setup=None: _BoomAgent())
        result = ex.execute("task")
        assert result.status == SubagentStatus.FAILED
        assert "boom" in (result.error or "")

    def test_execute_no_final_state(self, monkeypatch):
        ex = self._make_executor()
        monkeypatch.setattr(ex, "_create_agent", lambda tools=None, deferred_setup=None: _FakeAgent([]))
        result = ex.execute("task")
        assert result.status == SubagentStatus.COMPLETED
        assert result.result == "No response generated"

    def test_execute_uses_isolated_loop_when_loop_running(self, monkeypatch):
        ex = self._make_executor()
        called = {}

        def fake_isolated(task, result_holder=None):
            called["isolated"] = True
            return SubagentResult(task_id="t", trace_id="tr", status=SubagentStatus.COMPLETED, result="iso")

        monkeypatch.setattr(ex, "_execute_in_isolated_loop", fake_isolated)

        async def _driver():
            # 在运行中的事件循环里调 execute -> 走隔离循环路径
            return ex.execute("task")

        out = asyncio.run(_driver())
        assert called.get("isolated") is True
        assert out.result == "iso"

    def test_execute_async_background_lifecycle(self, monkeypatch):
        ex = self._make_executor()
        monkeypatch.setattr(ex, "_create_agent", lambda tools=None, deferred_setup=None: _FakeAgent([_chunk_with_ai("bg-done", "m1")]))
        task_id = ex.execute_async("bg task", task_id="bg-1")
        assert task_id == "bg-1"
        # 轮询等完成
        result = get_background_task_result("bg-1")
        assert result is not None
        for _ in range(100):
            result = get_background_task_result("bg-1")
            if result.status.is_terminal:
                break
            time.sleep(0.05)
        assert result.status == SubagentStatus.COMPLETED
        assert result.result == "bg-done"

    def test_request_cancel_sets_cancel_event(self, monkeypatch):
        ex = self._make_executor()

        started = threading.Event()

        class _SlowAgent:
            async def astream(self, state, config=None, context=None, stream_mode=None):
                started.set()
                # 持续慢 yield，让 _aexecute 的 async for 在每次迭代顶部检查到 cancel_event
                for _ in range(50):
                    await asyncio.sleep(0.1)
                    yield {"messages": []}

        monkeypatch.setattr(ex, "_create_agent", lambda tools=None, deferred_setup=None: _SlowAgent())
        ex.execute_async("slow", task_id="c-1")
        # 等它开始
        started.wait(timeout=2)
        request_cancel_background_task("c-1")
        result = get_background_task_result("c-1")
        assert result.cancel_event.is_set()
        for _ in range(100):
            result = get_background_task_result("c-1")
            if result.status.is_terminal:
                break
            time.sleep(0.05)
        assert result.status == SubagentStatus.CANCELLED

    def test_cooperative_cancel_before_stream(self, monkeypatch):
        ex = self._make_executor()
        ex._create_agent = lambda tools=None, deferred_setup=None: _FakeAgent([])  # noqa
        # 先建 holder 并置 cancel_event，再 execute
        holder = SubagentResult(task_id="pre", trace_id=ex.trace_id, status=SubagentStatus.RUNNING, started_at=datetime.now())
        holder.cancel_event.set()
        result = ex.execute("task", result_holder=holder)
        assert result.status == SubagentStatus.CANCELLED

    def test_list_and_cleanup_background_tasks(self, monkeypatch):
        ex = self._make_executor()
        monkeypatch.setattr(ex, "_create_agent", lambda tools=None, deferred_setup=None: _FakeAgent([_chunk_with_ai("ok", "m1")]))
        ex.execute_async("a", task_id="cl-1")
        for _ in range(100):
            if get_background_task_result("cl-1").status.is_terminal:
                break
            time.sleep(0.05)
        assert any(r.task_id == "cl-1" for r in list_background_tasks())
        cleanup_background_task("cl-1")
        assert get_background_task_result("cl-1") is None

    def test_cleanup_skips_non_terminal(self, monkeypatch):
        # 手动塞一个非终态任务
        r = SubagentResult(task_id="nt", trace_id="tr", status=SubagentStatus.RUNNING)
        with executor_module._background_tasks_lock:
            executor_module._background_tasks["nt"] = r
        cleanup_background_task("nt")
        # 非终态不清理
        assert get_background_task_result("nt") is not None

    def test_cleanup_unknown_task_noop(self):
        cleanup_background_task("does-not-exist")  # 不抛

    def test_get_unknown_returns_none(self):
        assert get_background_task_result("nope") is None


class TestExecutorDegradation:
    """skills（M14）/ tool_search（M15）/ build_subagent_runtime_middlewares（M16）
    缺包时的降级行为。asyncio_mode=auto，``async def test_*`` 自动按 asyncio 测试跑。"""

    async def test_load_skills_degrades_when_skills_missing(self):
        ex = SubagentExecutor(
            config=SubagentConfig(name="general-purpose", description="d"),
            tools=[],
            app_config=_make_app_config(),
            parent_model="gpt-4o",
        )
        # skills 包不存在 -> 返回空（不抛）
        skills = await ex._load_skills()
        assert skills == []

    async def test_load_skills_empty_whitelist_returns_empty(self):
        ex = SubagentExecutor(
            config=SubagentConfig(name="x", description="d", skills=[]),
            tools=[],
            app_config=_make_app_config(),
            parent_model="gpt-4o",
        )
        assert await ex._load_skills() == []

    async def test_build_initial_state_without_tool_search(self):
        # tool_search 关闭（M15 落地后模块可用但 enabled=False）-> 空 DeferredToolSetup
        # （tool_search_tool=None / deferred_names=frozenset()），即「不延迟」。state 含 system_prompt + task。
        ex = SubagentExecutor(
            config=SubagentConfig(name="general-purpose", description="d", system_prompt="my prompt"),
            tools=[],
            app_config=_make_app_config(tool_search_enabled=False),
            parent_model="gpt-4o",
        )
        state, final_tools, deferred_setup = await ex._build_initial_state("do thing")
        msgs = state["messages"]
        # 第一条是 SystemMessage（含 system_prompt），第二条是 HumanMessage（task）
        assert "my prompt" in msgs[0].content
        assert msgs[1].content == "do thing"
        # disabled → 空 setup（tool_search_tool 为 None，无延迟名）
        assert deferred_setup is None or deferred_setup.tool_search_tool is None
        assert deferred_setup is None or deferred_setup.deferred_names == frozenset()
        assert final_tools == []

    async def test_build_initial_state_passes_sandbox_thread_data(self):
        sandbox = {"sandbox_id": "local:t1"}
        thread_data = {"workspace_path": "/tmp/ws"}
        ex = SubagentExecutor(
            config=SubagentConfig(name="general-purpose", description="d"),
            tools=[],
            app_config=_make_app_config(),
            parent_model="gpt-4o",
            sandbox_state=sandbox,
            thread_data=thread_data,
        )
        state, _tools, _ds = await ex._build_initial_state("task")
        assert state["sandbox"] == sandbox
        assert state["thread_data"] == thread_data

    def test_resolve_middleware_falls_back_when_helper_missing(self):
        # M16 已落地：build_subagent_runtime_middlewares 真实可用，返回完整子代理中间件链
        # （必含 ToolErrorHandlingMiddleware）。ImportError 分支保留作防御性降级，由下方
        # ``test_resolve_middleware_importerror_falls_back`` 覆盖。
        from deerflow.agents.middlewares.tool_error_handling_middleware import ToolErrorHandlingMiddleware

        mw = executor_module._resolve_subagent_runtime_middlewares(app_config=_make_app_config(), model_name="gpt-4o", lazy_init=True, deferred_setup=None)
        assert isinstance(mw, list)
        assert any(isinstance(m, ToolErrorHandlingMiddleware) for m in mw)

    def test_resolve_middleware_importerror_falls_back(self, monkeypatch):
        # 防御性：helper 导入抛 ImportError（模拟未来重构移除）→ 回退最小集。
        import deerflow.agents.middlewares.tool_error_handling_middleware as teh
        from deerflow.agents.middlewares.tool_error_handling_middleware import ToolErrorHandlingMiddleware

        real = teh.build_subagent_runtime_middlewares

        def _raise_importerror(*a, **k):
            raise ImportError("simulated")

        monkeypatch.setattr(teh, "build_subagent_runtime_middlewares", _raise_importerror)
        try:
            mw = executor_module._resolve_subagent_runtime_middlewares(app_config=_make_app_config(), model_name="gpt-4o", lazy_init=True, deferred_setup=None)
        finally:
            monkeypatch.setattr(teh, "build_subagent_runtime_middlewares", real)
        # ImportError → 最小集（仅一个 ToolErrorHandlingMiddleware）。
        assert len(mw) == 1
        assert isinstance(mw[0], ToolErrorHandlingMiddleware)

    async def test_create_agent_uses_fallback_middleware_set(self, monkeypatch):
        # M16 已落地：_create_agent 用真实子代理中间件链（含 ToolErrorHandling）+ create_chat_model 被替身。
        ex = SubagentExecutor(
            config=SubagentConfig(name="general-purpose", description="d"),
            tools=[],
            app_config=_make_app_config(),
            parent_model="gpt-4o",
        )
        monkeypatch.setattr(executor_module, "create_chat_model", lambda **kw: MagicMock())
        captured = {}

        def fake_create_agent(**kw):
            captured["middleware"] = kw.get("middleware")
            captured["checkpointer"] = kw.get("checkpointer")
            captured["state_schema"] = kw.get("state_schema")
            return MagicMock()

        monkeypatch.setattr(executor_module, "create_agent", fake_create_agent)
        agent = ex._create_agent()
        assert agent is not None
        # checkpointer=False（子代理一次性，红线）
        assert captured["checkpointer"] is False
        assert isinstance(captured["middleware"], list)
        assert len(captured["middleware"]) >= 1
