"""把多条 SystemMessage 合并成一条「领头」的 SystemMessage（#3711）。

为什么需要（面向小白）
======================
**SystemMessage**（系统消息）是放在对话最开头、用来给模型下「人设 / 规则 / 全局指令」的消息——
比如「你是一个乐于助人的助手，回答要简洁」。它和用户消息（HumanMessage）地位不同：模型会把
SystemMessage 当**最高优先级的背景规则**来遵守。

**严格的 OpenAI 兼容后端**（vLLM / SGLang / Qwen 自部署，以及 Anthropic）要求 SystemMessage
**只能出现在对话最开头**、且通常只能有一条。如果对话中间冒出 SystemMessage，它们会直接报错：
``"System message must be at the beginning"`` / ``"Received multiple non-consecutive system messages"``。
官方 OpenAI API 比较宽容（容忍对话中途的 SystemMessage），所以这个问题只在自部署 / Anthropic 上才暴露。

DeerFlow 为什么会攒出多条 SystemMessage
-----------------------------------------
lead agent 在运行中会**动态注入** SystemMessage：
- **DynamicContextMiddleware** 用「ID-swap」技巧：把当前日期 / 记忆等框架元数据塞进一个
  SystemMessage 提醒块（``<system-reminder>``），而不是塞进 HumanMessage——因为框架注入的内容
  不能伪装成用户输入（OWASP LLM01）。于是对话里就多了一条 SystemMessage。
- **跨午夜**时，日期更新会再注入**第二条** SystemMessage（最新日期）。

另外，langchain ≥ 1.2.15 把「静态系统提示词」放在单独的 ``request.system_message`` 字段里（不在
``request.messages`` 列表里），到模型调用的最后一刻才 ``[request.system_message, *messages]`` 拍平。

本中间件做什么
--------------
在 ``wrap_model_call``（拍平之前的那一刻）里，把 ``request.system_message`` 加上
``request.messages`` 里的**所有** SystemMessage，合并成**一条**领头的 SystemMessage，通过
``system_message`` 字段交回。这样：

- 后端看到的永远是「一条领头的 SystemMessage」——严格后端不再报错；
- **只改出站请求、不动持久状态（checkpoint）**——所以靠标记扫描历史的中中间件
  （如 ``is_dynamic_context_reminder``）继续正常工作。

跨午夜的 ``dynamic_context_reminder`` 去重
------------------------------------------
合并后，原本被对话轮次隔开的「两条日期提醒」会变成相邻的两块 ``<current_date>``——一个旧一个新，
模型得猜该信哪个。所以合并时**只保留最后一条提醒**（最新日期），丢掉更早的。

注：这和 ``claude_provider._coalesce_system_messages`` 里给 Claude 做的「逐请求合并」是同一件事，
只是提到 provider 无关的中间件层，让所有后端共用一处修复，而不是每个 provider 各打一个补丁。

移植自上游 deer-flow ``agents/middlewares/system_message_coalescing_middleware.py``（MIT），
逻辑保持一致，注释改为面向小白的中文讲解。
"""

from collections.abc import Awaitable, Callable
from typing import override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelCallResult, ModelRequest, ModelResponse
from langchain_core.messages import SystemMessage

from deerflow.agents.middlewares.dynamic_context_middleware import is_dynamic_context_reminder


def _flatten_content(content) -> str:
    """把消息 content 拍平成纯字符串，兼容 str 和 list 两种形态。

    langchain 消息的 content 可以是多模态列表（如 ``[{"type": "text", "text": "..."}]``）。
    DeerFlow 里的 SystemMessage 永远是纯字符串，但这个 helper 保证对任意 content 形态都不崩。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and "text" in item:
                parts.append(item["text"])
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return str(content)


def _coalesce_request(request: ModelRequest) -> ModelRequest | None:
    """把 ``request.system_message`` 和 ``messages`` 里的 SystemMessage 合并成一条。

    langchain ≥ 1.2.15：静态系统提示词在单独的 ``request.system_message`` 字段、不在 ``messages`` 里。
    模型调用 handler 在最后一刻才 ``[system_message, *messages]`` 拍平——所以一个只扫 ``messages``
    的中间件看不到它、等于 no-op。本函数两边都看：把所有 SystemMessage 合成一条，经
    ``system_message`` 字段交回，让 handler 仍能正确前置。

    返回 None 表示 ``messages`` 里没有 SystemMessage——此时 ``system_message``（若有）已是唯一的
    领头系统块，请求原样放行即可（零改动，保 prefix-cache 命中）。
    """
    in_msg_systems = [m for m in request.messages if isinstance(m, SystemMessage)]
    if not in_msg_systems:
        return None

    # 合并 system_message（若有）+ messages 里的所有 SystemMessage。
    parts: list[SystemMessage] = []
    if request.system_message is not None:
        parts.append(request.system_message)
    parts.extend(in_msg_systems)

    # dynamic_context_reminder 去重：只保留最后一条（最新日期），丢掉更早的。
    # 跨午夜的合并内容里否则会有两块相邻、又没有时间锚点的 <current_date>——原本隔开它们的轮次
    # 在合并后没了，模型只能瞎猜该忽略哪个。只给最新日期。
    reminder_indices = [i for i, p in enumerate(parts) if is_dynamic_context_reminder(p)]
    if len(reminder_indices) > 1:
        keep_last = reminder_indices[-1]
        parts = [p for i, p in enumerate(parts) if i not in reminder_indices[:-1] or i == keep_last]

    # 保留第一条 SystemMessage 的 id（通常是静态 system_prompt），让按领头系统消息 id 索引的下游
    # 消费者不受影响。合并所有部分的 additional_kwargs，让 hide_from_ui / dynamic_context_reminder
    # 等标记都保留到合并后的块上。
    first = parts[0]
    merged_kwargs: dict = {}
    for p in parts:
        merged_kwargs.update(p.additional_kwargs or {})
    merged = SystemMessage(
        content="\n\n".join(_flatten_content(p.content) for p in parts),
        id=first.id,
        additional_kwargs=merged_kwargs,
    )

    non_system = [m for m in request.messages if not isinstance(m, SystemMessage)]
    return request.override(system_message=merged, messages=non_system)


class SystemMessageCoalescingMiddleware(AgentMiddleware[AgentState]):
    """把所有 SystemMessage 合并成一条领头的 SystemMessage。

    用 ``wrap_model_call``（不是 ``before_agent``），让合并发生在**最终的请求 payload** 上——
    此时 ``system_message`` 和 ``messages`` 还是两个独立字段——并且绝不碰持久化的 state["messages"]。
    这样 checkpoint 结构对所有扫描历史的中中间件（记忆构造器 / 日志 / 摘要 / 动态上下文检测）保持原样。
    """

    @staticmethod
    def _maybe_coalesce(request: ModelRequest) -> ModelRequest:
        coalesced = _coalesce_request(request)
        if coalesced is None:
            return request
        return coalesced

    @override
    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        return handler(self._maybe_coalesce(request))

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        return await handler(self._maybe_coalesce(request))
