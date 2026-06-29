"""TokenBudgetMiddleware 单元测试（M16，单 run token 预算）。

hermetic：用本地 AIMessage（带 usage_metadata）+ SimpleNamespace runtime，无真实模型 / 网络。
覆盖：config 校验、before_agent 标记「已见」、after_model 增量累计、软提醒排队、硬停剥 tool_calls、
子代理 token 回灌（事后增量）、wrap_model_call 注入提醒、after_agent 清状态、enabled=False no-op、
多上限（input/output/total）、run_id 隔离。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from deerflow.agents.middlewares.token_budget_middleware import (
    BoundedDict,
    TokenBudgetMiddleware,
    TokenUsage,
)
from deerflow.config.token_budget_config import TokenBudgetConfig

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _cfg(**over) -> TokenBudgetConfig:
    base = {"enabled": True, "max_tokens": 1000, "warn_threshold": 0.5, "hard_stop_threshold": 1.0}
    base.update(over)
    return TokenBudgetConfig(**base)


def _runtime(run_id="r1", thread_id="t1"):
    return SimpleNamespace(context={"run_id": run_id, "thread_id": thread_id})


def _ai(content="x", *, input_tokens=0, output_tokens=0, total=None, tool_calls=None, id_="m1", finish="stop"):
    total = input_tokens + output_tokens if total is None else total
    return AIMessage(
        content=content,
        id=id_,
        tool_calls=tool_calls or [],
        usage_metadata={"input_tokens": input_tokens, "output_tokens": output_tokens, "total_tokens": total},
        response_metadata={"finish_reason": finish} if finish else {},
    )


# ---------------------------------------------------------------------------
# config 校验
# ---------------------------------------------------------------------------


def test_config_hard_below_warn_raises():
    with pytest.raises(ValueError):
        TokenBudgetConfig(enabled=True, max_tokens=1000, warn_threshold=0.8, hard_stop_threshold=0.5)


def test_config_defaults_disabled():
    c = TokenBudgetConfig()
    assert c.enabled is False
    assert c.max_tokens == 200000


def test_config_ok_when_hard_ge_warn():
    TokenBudgetConfig(warn_threshold=0.8, hard_stop_threshold=0.8)
    TokenBudgetConfig(warn_threshold=0.5, hard_stop_threshold=1.0)


# ---------------------------------------------------------------------------
# BoundedDict
# ---------------------------------------------------------------------------


def test_bounded_dict_evicts_oldest():
    d = BoundedDict(maxsize=3)
    d["a"] = 1
    d["b"] = 2
    d["c"] = 3
    d["d"] = 4  # 超限 → 驱逐最旧的 a
    assert "a" not in d
    assert d["b"] == 2 and d["d"] == 4
    assert len(d) == 3


def test_token_usage_dataclass():
    u = TokenUsage()
    assert u.input == 0 and u.output == 0 and u.total == 0


# ---------------------------------------------------------------------------
# before_agent：标记上一轮已见
# ---------------------------------------------------------------------------


def test_before_agent_marks_prior_messages_seen():
    mw = TokenBudgetMiddleware(_cfg())
    prior = _ai(input_tokens=80, output_tokens=20, id_="old")
    mw.before_agent({"messages": [prior]}, _runtime())
    # 标记为已见后，after_model 不应再累计它
    update = mw.after_model({"messages": [prior]}, _runtime())
    assert update is None
    assert mw._cumulative_usage["r1"].total == 0


# ---------------------------------------------------------------------------
# after_model：增量累计 + 软提醒
# ---------------------------------------------------------------------------


def test_after_model_accumulates_new_usage_and_warns():
    mw = TokenBudgetMiddleware(_cfg(max_tokens=1000, warn_threshold=0.5))
    msg = _ai(input_tokens=300, output_tokens=300, id_="new")  # total 600 → 0.6 ≥ 0.5
    update = mw.after_model({"messages": [msg]}, _runtime())
    # 软提醒：返回 None（不改状态），但排队了提醒
    assert update is None
    assert mw._cumulative_usage["r1"].total == 600
    assert len(mw._pending_warnings["r1"]) == 1
    assert "TOKEN BUDGET WARNING" in mw._pending_warnings["r1"][0]


def test_after_model_warn_only_once_per_run():
    mw = TokenBudgetMiddleware(_cfg(max_tokens=2000, warn_threshold=0.5))
    msg = _ai(input_tokens=1100, id_="m1")  # 0.55 ≥ 0.5 → 警告一次
    mw.after_model({"messages": [msg]}, _runtime())
    assert len(mw._pending_warnings["r1"]) == 1
    # 再累计更多、仍未硬停（< 2000）、已警告过 → 不再排队
    msg2 = _ai(input_tokens=500, id_="m2")  # 累计 1600 < 2000，不硬停
    mw.after_model({"messages": [msg, msg2]}, _runtime())
    assert len(mw._pending_warnings["r1"]) == 1


def test_after_model_hard_stop_strips_tool_calls():
    mw = TokenBudgetMiddleware(_cfg(max_tokens=1000, hard_stop_threshold=1.0))
    msg = _ai(
        input_tokens=1100,
        output_tokens=0,
        id_="big",
        tool_calls=[{"name": "t", "args": {}, "id": "c1"}],
        finish="tool_calls",
    )
    update = mw.after_model({"messages": [msg]}, _runtime())
    assert update is not None
    stopped = update["messages"][0]
    assert stopped.tool_calls == []
    # finish_reason 从 tool_calls 改成 stop
    assert stopped.response_metadata["finish_reason"] == "stop"
    assert "TOKEN BUDGET EXCEEDED" in stopped.content


def test_after_model_hard_stop_clears_raw_tool_calls_kwargs():
    """硬停也清掉 additional_kwargs 里的原始 tool_calls（防 provider 序列化残留）。"""
    mw = TokenBudgetMiddleware(_cfg(max_tokens=1000, hard_stop_threshold=1.0))
    msg = _ai(input_tokens=1100, id_="big")
    msg.additional_kwargs = {"tool_calls": [{"id": "c1"}], "function_call": {"name": "f"}}
    update = mw.after_model({"messages": [msg]}, _runtime())
    stopped = update["messages"][0]
    assert "tool_calls" not in stopped.additional_kwargs
    assert "function_call" not in stopped.additional_kwargs


def test_after_model_captures_retroactive_subagent_tokens():
    """子代理 token 事后回灌到已见消息 → 增量（max(0, now - prev)）捕捉到。"""
    mw = TokenBudgetMiddleware(_cfg(max_tokens=100000, warn_threshold=0.9))
    msg = _ai(input_tokens=100, output_tokens=0, id_="m1")
    # 第一次：累计 100
    mw.after_model({"messages": [msg]}, _runtime())
    assert mw._cumulative_usage["r1"].total == 100
    # 事后回灌：同一条消息 usage 增加（模拟 TokenUsageMiddleware 回写子代理 token）
    msg.usage_metadata = {"input_tokens": 400, "output_tokens": 0, "total_tokens": 400}
    mw.after_model({"messages": [msg]}, _runtime())
    # 增量 = 400 - 100 = 300，累计 = 400
    assert mw._cumulative_usage["r1"].total == 400


def test_after_model_no_usage_passthrough():
    """最后一条不是 AIMessage 或无 usage → None。"""
    mw = TokenBudgetMiddleware(_cfg())
    assert mw.after_model({"messages": [HumanMessage(content="hi")]}, _runtime()) is None
    assert mw.after_model({"messages": []}, _runtime()) is None


def test_after_model_below_warn_no_queue():
    mw = TokenBudgetMiddleware(_cfg(warn_threshold=0.5))
    msg = _ai(input_tokens=100, id_="m1")  # 0.1 < 0.5
    assert mw.after_model({"messages": [msg]}, _runtime()) is None
    assert mw._pending_warnings.get("r1", []) == []


# ---------------------------------------------------------------------------
# 多上限：input / output / total 取最高比例
# ---------------------------------------------------------------------------


def test_input_limit_triggers_when_highest_fraction():
    # max_tokens=10000（total 远未到），max_input_tokens=200 → input 200/200=1.0 硬停
    cfg = _cfg(max_tokens=10000, max_input_tokens=200, hard_stop_threshold=1.0, warn_threshold=0.5)
    mw = TokenBudgetMiddleware(cfg)
    msg = _ai(input_tokens=200, output_tokens=0, id_="m1")
    update = mw.after_model({"messages": [msg]}, _runtime())
    assert update is not None  # input 上限触发硬停
    assert "input" in update["messages"][0].content or "TOKEN BUDGET EXCEEDED" in update["messages"][0].content


def test_output_limit_triggers_warning():
    # output 60/100=0.6 ≥ 0.5 警告；但 < 1.0 不硬停
    cfg = _cfg(max_tokens=10000, max_output_tokens=100, warn_threshold=0.5, hard_stop_threshold=1.0)
    mw = TokenBudgetMiddleware(cfg)
    msg = _ai(input_tokens=10, output_tokens=60, id_="m1")
    assert mw.after_model({"messages": [msg]}, _runtime()) is None
    assert len(mw._pending_warnings["r1"]) == 1


# ---------------------------------------------------------------------------
# wrap_model_call：排空 + 注入提醒
# ---------------------------------------------------------------------------


def test_wrap_model_call_injects_queued_warning():
    mw = TokenBudgetMiddleware(_cfg(warn_threshold=0.5))
    msg = _ai(input_tokens=600, id_="m1")
    mw.after_model({"messages": [msg]}, _runtime())  # 排队提醒
    assert mw._pending_warnings["r1"]

    seen = {}

    def handler(req):
        seen["messages"] = req.messages
        return "ok"

    # 用简单 request 对象 + 手写 override
    class _Req:
        def __init__(self):
            self.messages = [HumanMessage(content="u")]
            self.runtime = _runtime()

        def override(self, **kw):
            r = _Req()
            r.messages = kw.get("messages", self.messages)
            r.runtime = kw.get("runtime", self.runtime)
            return r

    mw.wrap_model_call(_Req(), handler)
    # 提醒作为 HumanMessage 追加到末尾
    assert any(isinstance(m, HumanMessage) and "TOKEN BUDGET WARNING" in (m.content if isinstance(m.content, str) else "") for m in seen["messages"])
    # 排空后队列清空
    assert mw._pending_warnings.get("r1", []) == []


def test_wrap_model_call_no_warning_passthrough():
    mw = TokenBudgetMiddleware(_cfg())

    class _Req:
        def __init__(self):
            self.messages = [HumanMessage(content="u")]
            self.runtime = _runtime()

        def override(self, **kw):
            return self  # 不会被调用

    seen = []

    def handler(req):
        seen.append(req)
        return "ok"

    mw.wrap_model_call(_Req(), handler)
    assert seen[0].messages == [seen[0].messages[0]]  # 原样


# ---------------------------------------------------------------------------
# after_agent：清状态
# ---------------------------------------------------------------------------


def test_after_agent_clears_run_state():
    mw = TokenBudgetMiddleware(_cfg(warn_threshold=0.5))
    mw.after_model({"messages": [_ai(input_tokens=600, id_="m1")]}, _runtime())
    assert "r1" in mw._cumulative_usage
    mw.after_agent({"messages": []}, _runtime())
    assert "r1" not in mw._cumulative_usage


# ---------------------------------------------------------------------------
# enabled=False → 全 no-op
# ---------------------------------------------------------------------------


def test_disabled_noop():
    mw = TokenBudgetMiddleware(TokenBudgetConfig(enabled=False))
    assert mw.before_agent({"messages": [_ai(input_tokens=9999, id_="m1")]}, _runtime()) is None
    assert mw.after_model({"messages": [_ai(input_tokens=9999, id_="m1")]}, _runtime()) is None
    assert mw.after_agent({"messages": []}, _runtime()) is None
    assert mw._drain_pending_warnings(_runtime()) == []


# ---------------------------------------------------------------------------
# run_id 隔离
# ---------------------------------------------------------------------------


def test_run_id_isolation():
    mw = TokenBudgetMiddleware(_cfg(warn_threshold=0.5))
    mw.after_model({"messages": [_ai(input_tokens=600, id_="m1")]}, _runtime(run_id="r1"))
    mw.after_model({"messages": [_ai(input_tokens=10, id_="m1b")]}, _runtime(run_id="r2"))
    assert mw._cumulative_usage["r1"].total == 600
    assert mw._cumulative_usage["r2"].total == 10
    # r1 排队了提醒，r2 没有
    assert len(mw._pending_warnings["r1"]) == 1
    assert mw._pending_warnings.get("r2", []) == []


def test_reset_clears_all():
    mw = TokenBudgetMiddleware(_cfg())
    mw._cumulative_usage["r1"] = TokenUsage(input=10)
    mw.reset()
    assert len(mw._cumulative_usage) == 0


# ---------------------------------------------------------------------------
# async
# ---------------------------------------------------------------------------


def test_aafter_model_works():
    mw = TokenBudgetMiddleware(_cfg(warn_threshold=0.5))
    msg = _ai(input_tokens=600, id_="m1")
    out = asyncio.run(mw.aafter_model({"messages": [msg]}, _runtime()))
    assert out is None
    assert len(mw._pending_warnings["r1"]) == 1
