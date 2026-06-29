"""Token 预算中间件——给单个 run 的 token 用量设上限（M16）。

做什么（面向小白）
==================
一次 agent 运行（run）里，模型每次被调用都会消耗 token（大模型计费 / 上下文的基本单位）。
本中间件**累计单个 run 内所有模型调用的 token 用量**（input / output / total 三本账），
到「软阈值」提醒模型收尾、到「硬上限」剥掉 tool_calls 逼它直接给最终文本答复——防一个跑飞的
run 把 token 烧爆。

怎么累计（关键技巧：按消息 id 记账，算增量）
----------------------------------------------
每次模型响应后，遍历**当前线程历史里所有 AIMessage** 的 ``usage_metadata``。但直接累加会重复
计数（同一批消息每轮都会被遍历到）。所以用「已见消息」账本 ``_seen_messages``：

1. ``before_agent``（run 开始时）：把历史里**已有**的 AIMessage 全标记为「已见」——它们属于**上一轮**
   run，不算进**本轮**预算。
2. ``after_model``（每次模型响应后）：对每条 AIMessage，用 ``usage_metadata`` 减去上次记录的值，
   得到**本次新增的 token**（``diff``），只把增量加进累计。

这个「算增量」的写法还顺带解决了**子代理 token 回灌**：``TokenUsageMiddleware`` 会把子代理消耗
**回溯**写进历史 AIMessage（按消息位置）。本中间件用 ``max(0, 当前 - 上次记录)`` 捕捉这种「事后
增加」的 token，不会漏计。

触发
-----
- 累计用量 / 上限 的最高比例 ≥ ``hard_stop_threshold`` → 剥 tool_calls，硬停；
- ≥ ``warn_threshold``（且本轮还没警告过）→ 队列一条提醒。

为什么警告要延迟注入（after_model 排队、wrap_model_call 注入）
--------------------------------------------------------------
和 :class:`LoopDetectionMiddleware` 同一套路：``after_model`` 在 AIMessage 带 tool_calls 后立刻
触发，此时工具节点还没跑、没有配对的 ToolMessage——在这里插消息会落在 assistant tool_calls 和它的
响应**之间**，OpenAI/Moonshot 校验器报 ``tool_call_ids did not have response messages``。
所以 ``after_model`` 只**排队**、不改状态；``wrap_model_call`` 在下次调用时把提醒作为 HumanMessage
**追加到末尾**（所有 ToolMessage 之后），配对完整、不破坏 AIMessage 语义。

内存安全
--------
所有账本（``_seen_messages`` / ``_pending_warnings`` / ``_warned`` / ``_cumulative_usage``）
按 ``run_id`` 分桶，并用 :class:`BoundedDict`（容量 1000 的 LRU）兜底——被遗弃的 run 不会让状态
无限增长。``after_agent``（run 结束）清掉该 run 的账本。

移植自上游 deer-flow ``agents/middlewares/token_budget_middleware.py``（MIT），逻辑保持一致，
注释改为面向小白的中文讲解。
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelCallResult, ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.runtime import Runtime

from deerflow.config.token_budget_config import TokenBudgetConfig

logger = logging.getLogger(__name__)

_BUDGET_WARNING_MSG = (
    "[TOKEN BUDGET WARNING] You have used {used:,} of your {budget:,} {reason} token budget ({percent:.0f}%). Wrap up your current work and produce a final answer. Avoid starting new tool calls unless absolutely necessary."
)
_BUDGET_EXCEEDED_MSG = "[TOKEN BUDGET EXCEEDED] The {reason} token usage ({used:,}) has exceeded the safety limit ({budget:,}). Producing final answer with results collected so far."


@dataclass
class TokenUsage:
    """单个 run 的累计 token 用量（input / output / total 三本账）。"""

    input: int = 0
    output: int = 0
    total: int = 0


class BoundedDict(OrderedDict):
    """有上限的字典（LRU），防被遗弃 run 的状态无限增长。"""

    def __init__(self, maxsize=1000, *args, **kwds):
        self.maxsize = maxsize
        super().__init__(*args, **kwds)

    def __setitem__(self, key, value):
        if key not in self:
            if len(self) >= self.maxsize:
                self.popitem(last=False)
        super().__setitem__(key, value)


class TokenBudgetMiddleware(AgentMiddleware[AgentState]):
    """强制单个 run 的 token 预算上限。"""

    def __init__(self, config: TokenBudgetConfig) -> None:
        super().__init__()
        self._config = config
        self._lock = threading.Lock()

        # 严格按 run_id 分桶（同 run_id 重跑会覆盖，安全）+ 有上限（防泄漏）
        self._warned: BoundedDict[str, bool] = BoundedDict(1000)
        self._pending_warnings: BoundedDict[str, list[str]] = BoundedDict(1000)
        self._seen_messages: BoundedDict[str, dict[str, tuple[int, int]]] = BoundedDict(1000)
        self._cumulative_usage: BoundedDict[str, TokenUsage] = BoundedDict(1000)

    @classmethod
    def from_config(cls, config: TokenBudgetConfig) -> TokenBudgetMiddleware:
        return cls(config=config)

    def reset(self) -> None:
        with self._lock:
            self._warned.clear()
            self._pending_warnings.clear()
            self._seen_messages.clear()
            self._cumulative_usage.clear()

    @staticmethod
    def _get_run_id(runtime: Runtime) -> str:
        ctx = getattr(runtime, "context", None)
        if isinstance(ctx, dict) and "run_id" in ctx:
            return ctx["run_id"]
        # 兜底用 runtime 对象 id，防嵌入式 client 的多次 run 撞同一 key。
        return str(id(runtime))

    def _clear_run_state(self, run_id: str) -> None:
        with self._lock:
            self._warned.pop(run_id, None)
            self._pending_warnings.pop(run_id, None)
            self._seen_messages.pop(run_id, None)
            self._cumulative_usage.pop(run_id, None)

    @override
    def before_agent(self, state: AgentState, runtime: Runtime) -> None:
        if not self._config.enabled:
            return

        # 把上一轮 run 已有的消息全标记为「已见」，这样它们不计入**本轮**预算。
        messages = state.get("messages", [])
        if not messages:
            return

        run_id = self._get_run_id(runtime)
        with self._lock:
            seen = self._seen_messages.setdefault(run_id, {})
            self._cumulative_usage.setdefault(run_id, TokenUsage())

            for msg in messages:
                if isinstance(msg, AIMessage) and msg.id and hasattr(msg, "usage_metadata"):
                    usage = msg.usage_metadata or {}
                    input_tokens = usage.get("input_tokens", 0)
                    output_tokens = usage.get("output_tokens", 0)
                    seen[msg.id] = (input_tokens, output_tokens)

    @override
    async def abefore_agent(self, state: AgentState, runtime: Runtime) -> None:
        self.before_agent(state, runtime)

    @override
    def after_agent(self, state: AgentState, runtime: Runtime) -> None:
        if not self._config.enabled:
            return
        self._clear_run_state(self._get_run_id(runtime))

    @override
    async def aafter_agent(self, state: AgentState, runtime: Runtime) -> None:
        self.after_agent(state, runtime)

    @staticmethod
    def _append_text(content: str | list[dict | None] | None, stop_msg: str) -> str | list[dict | str]:
        """把一段停止提示追加到 AIMessage.content（兼容 str / list / None）。"""
        if content is None:
            return stop_msg
        if isinstance(content, str):
            if content:
                return f"{content}\n\n{stop_msg}"
            return f"\n\n{stop_msg}"
        if isinstance(content, list):
            new_content = list(content)
            new_content.append({"type": "text", "text": f"\n\n{stop_msg}"})
            return new_content
        return f"{content}\n\n{stop_msg}"

    def _build_hard_stop_update(self, msg: AIMessage, stop_msg: str) -> dict[str, Any]:
        """构造硬停的状态更新：剥 tool_calls、改 finish_reason、追加停止提示。"""
        updated_content = self._append_text(msg.content, stop_msg)
        kwargs = dict(msg.additional_kwargs) if msg.additional_kwargs else {}
        if "tool_calls" in kwargs:
            del kwargs["tool_calls"]
        if "function_call" in kwargs:
            del kwargs["function_call"]

        response_metadata = dict(getattr(msg, "response_metadata", {}) or {})

        if response_metadata.get("finish_reason") == "tool_calls":
            response_metadata["finish_reason"] = "stop"

        stopped_msg = msg.model_copy(update={"content": updated_content, "tool_calls": [], "additional_kwargs": kwargs, "response_metadata": response_metadata})
        return {"messages": [stopped_msg]}

    def _apply(self, state: AgentState, runtime: Runtime) -> dict | None:
        if not self._config.enabled:
            return None

        messages = state.get("messages", [])
        if not messages:
            return None

        last_msg = messages[-1]
        if not isinstance(last_msg, AIMessage):
            return None

        run_id = self._get_run_id(runtime)

        with self._lock:
            seen = self._seen_messages.setdefault(run_id, {})
            usage_accum = self._cumulative_usage.setdefault(run_id, TokenUsage())

            for msg in messages:
                if isinstance(msg, AIMessage) and msg.id and hasattr(msg, "usage_metadata"):
                    usage = msg.usage_metadata or {}

                    input_tokens = usage.get("input_tokens", 0)
                    output_tokens = usage.get("output_tokens", 0)

                    # 这条消息上次记录了多少
                    prev_input, prev_output = seen.get(msg.id, (0, 0))

                    # 算本次新增的 token（顺带捕捉子代理 token 的事后回灌）
                    diff_input = max(0, input_tokens - prev_input)
                    diff_output = max(0, output_tokens - prev_output)

                    if diff_input > 0 or diff_output > 0:
                        usage_accum.input += diff_input
                        usage_accum.output += diff_output
                        usage_accum.total += diff_input + diff_output
                        seen[msg.id] = (input_tokens, output_tokens)

            if usage_accum.total <= 0:
                return None

            fractions = [("total", usage_accum.total, self._config.max_tokens)]
            if self._config.max_input_tokens:
                fractions.append(("input", usage_accum.input, self._config.max_input_tokens))
            if self._config.max_output_tokens:
                fractions.append(("output", usage_accum.output, self._config.max_output_tokens))

            highest_fraction = 0.0
            trigger_reason = ""
            trigger_used = 0
            trigger_budget = 0

            for reason, used, limit in fractions:
                frac = used / limit
                if frac > highest_fraction:
                    highest_fraction = frac
                    trigger_reason = reason
                    trigger_used = used
                    trigger_budget = limit

            if highest_fraction >= self._config.hard_stop_threshold:
                logger.warning("Token budget hard stop triggered for run %s: %s limit exceeded", run_id, trigger_reason)
                stop_text = _BUDGET_EXCEEDED_MSG.format(reason=trigger_reason, used=trigger_used, budget=trigger_budget)
                return self._build_hard_stop_update(last_msg, stop_text)

            if highest_fraction >= self._config.warn_threshold and not self._warned.get(run_id, False):
                self._warned[run_id] = True
                percent = highest_fraction * 100
                warn_text = _BUDGET_WARNING_MSG.format(reason=trigger_reason, used=trigger_used, budget=trigger_budget, percent=percent)
                logger.info("Token budget warning triggered for run %s: %s limit at %.1f%%", run_id, trigger_reason, percent)
                # 排队给 wrap_model_call 注入
                warnings = self._pending_warnings.setdefault(run_id, [])
                warnings.append(warn_text)
                return None

            return None

    @override
    def after_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self._apply(state, runtime)

    @override
    async def aafter_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self._apply(state, runtime)

    def _drain_pending_warnings(self, runtime: Runtime) -> list[str]:
        if not self._config.enabled:
            return []

        run_id = self._get_run_id(runtime)
        with self._lock:
            warnings = self._pending_warnings.pop(run_id, None)
        return warnings or []

    def _inject_warnings(self, request: ModelRequest, warnings: list[str]) -> ModelRequest:
        if not warnings:
            return request

        merged_text = "\n\n".join(warnings)
        warning_msg = HumanMessage(content=merged_text, name="budget_warning")

        messages = getattr(request, "messages", [])
        new_messages = list(messages) + [warning_msg]
        return request.override(messages=new_messages)

    @override
    def wrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], ModelResponse]) -> ModelCallResult:
        warnings = self._drain_pending_warnings(request.runtime)
        request = self._inject_warnings(request, warnings)

        return handler(request)

    @override
    async def awrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]) -> ModelCallResult:
        warnings = self._drain_pending_warnings(request.runtime)
        request = self._inject_warnings(request, warnings)
        return await handler(request)
