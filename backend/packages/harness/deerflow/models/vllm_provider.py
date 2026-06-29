"""vLLM 自托管 provider：在 LangChain ``ChatOpenAI`` 之上保留 vLLM 的 ``reasoning`` 字段。

为什么需要这个文件（面向小白）
================================

**vLLM** 是一个用来「自己在 GPU 机器上跑大模型」的开源推理引擎。它对外暴露一个
和 OpenAI 一模一样的 HTTP 接口（OpenAI-compatible endpoint），所以理论上可以直接用
LangChain 自带的 ``langchain_openai.ChatOpenAI`` 去连它。

但 vLLM 0.19.0 跑「会先想一想的模型」（reasoning model，比如 Qwen3 开了思考模式）时，
会在返回结果里多塞一个**非标准**的字段 ``reasoning``（OpenAI 官方接口里没有这个字段）。
问题来了：LangChain 默认的 OpenAI 适配器**不认识**这个字段，于是会把它**丢掉**。

丢掉会怎样？在「想完→调工具→继续想」这种交替流程里，vLLM 期望**上一轮 AI 的思考内容
要在下一轮原样回传给它**（这样模型才知道自己刚才想到了哪）。一旦被 LangChain 丢掉，
下一轮请求里就没了这个字段，vLLM 的行为就会出问题。

所以本文件做的事就是：**继承 ``ChatOpenAI``，重写三个关键方法，把 ``reasoning`` 字段
在三个地方都保住**：

1. **非流式响应**（一次返回完整结果）—— 重写 ``_create_chat_result``；
2. **流式响应**（一块一块吐结果）—— 重写 ``_convert_chunk_to_generation_chunk``；
3. **多轮请求**（把历史 AI 消息再发给模型）—— 重写 ``_get_request_payload``，
   把上一轮 AI 消息里的 ``reasoning`` 重新塞回 outgoing payload。

此外还有一个小兼容处理：DeerFlow 早期文档里用 ``extra_body.chat_template_kwargs.thinking``
来开关 vLLM 的思考，但 vLLM 0.19.0 的 Qwen 解析器读的是 ``enable_thinking``。本文件在
发请求前做一次「归一化」，把旧键名翻译成新键名，让旧配置继续能用。

移植自上游 deer-flow ``models/vllm_provider.py``（MIT），逻辑保持一致；仅把注释改成
面向小白的中文讲解。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, cast

import openai
from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessageChunk,
    ChatMessageChunk,
    FunctionMessageChunk,
    HumanMessageChunk,
    SystemMessageChunk,
    ToolMessageChunk,
)
from langchain_core.messages.tool import tool_call_chunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_openai import ChatOpenAI
from langchain_openai.chat_models.base import _create_usage_metadata


def _normalize_vllm_chat_template_kwargs(payload: dict[str, Any]) -> None:
    """把 DeerFlow 旧的 ``thinking`` 开关翻译成 vLLM 的 ``enable_thinking``。

    背景：DeerFlow 早期文档教用户用 ``extra_body.chat_template_kwargs.thinking``
    来开关 vLLM 的思考模式，但 vLLM 0.19.0 的 Qwen reasoning parser 实际读的是
    ``chat_template_kwargs.enable_thinking``。这里在请求**即将发出前**做一次改名，
    让旧配置继续能用，同时「flash 模式」（快速关思考）也能真正生效。

    直接就地修改 ``payload``，无返回值。若 payload 里没有相关结构，则什么都不做。
    """
    extra_body = payload.get("extra_body")
    if not isinstance(extra_body, dict):
        return

    chat_template_kwargs = extra_body.get("chat_template_kwargs")
    if not isinstance(chat_template_kwargs, dict):
        return

    if "thinking" not in chat_template_kwargs:
        return

    # 复制一份再改，避免动到调用方持有的原对象
    normalized_chat_template_kwargs = dict(chat_template_kwargs)
    normalized_chat_template_kwargs.setdefault("enable_thinking", normalized_chat_template_kwargs["thinking"])
    normalized_chat_template_kwargs.pop("thinking", None)
    extra_body["chat_template_kwargs"] = normalized_chat_template_kwargs


def _reasoning_to_text(reasoning: Any) -> str:
    """尽力从 vLLM 的 ``reasoning`` 字段里抠出「人能读」的文本。

    vLLM 的 ``reasoning`` 可能是好几种形态：纯字符串、字符串列表、或字典。
    本函数递归地把它们都拍平成一段文本。抠不出来时退回到 ``json.dumps``。
    """
    if isinstance(reasoning, str):
        return reasoning

    if isinstance(reasoning, list):
        parts = [_reasoning_to_text(item) for item in reasoning]
        return "".join(part for part in parts if part)

    if isinstance(reasoning, dict):
        # 优先认这几个常见键名
        for key in ("text", "content", "reasoning"):
            value = reasoning.get(key)
            if isinstance(value, str):
                return value
            if value is not None:
                text = _reasoning_to_text(value)
                if text:
                    return text
        # 都没命中，退回 JSON 序列化
        try:
            return json.dumps(reasoning, ensure_ascii=False)
        except TypeError:
            return str(reasoning)

    try:
        return json.dumps(reasoning, ensure_ascii=False)
    except TypeError:
        return str(reasoning)


def _convert_delta_to_message_chunk_with_reasoning(_dict: Mapping[str, Any], default_class: type[BaseMessageChunk]) -> BaseMessageChunk:
    """把一个流式 delta（增量片段）转成 LangChain message chunk，同时保住 reasoning。

    这其实是 LangChain 内部 ``_convert_delta_to_message_chunk`` 的「增强版」：
    原版会丢掉 ``reasoning``，这里把它额外放进 ``additional_kwargs`` 里，并顺手算一份
    纯文本版 ``reasoning_content`` 方便上层展示。
    """
    id_ = _dict.get("id")
    role = cast(str, _dict.get("role"))
    content = cast(str, _dict.get("content") or "")
    additional_kwargs: dict[str, Any] = {}

    if _dict.get("function_call"):
        function_call = dict(_dict["function_call"])
        if "name" in function_call and function_call["name"] is None:
            function_call["name"] = ""
        additional_kwargs["function_call"] = function_call

    reasoning = _dict.get("reasoning")
    if reasoning is not None:
        additional_kwargs["reasoning"] = reasoning
        reasoning_text = _reasoning_to_text(reasoning)
        if reasoning_text:
            additional_kwargs["reasoning_content"] = reasoning_text

    tool_call_chunks = []
    if raw_tool_calls := _dict.get("tool_calls"):
        try:
            tool_call_chunks = [
                tool_call_chunk(
                    name=rtc["function"].get("name"),
                    args=rtc["function"].get("arguments"),
                    id=rtc.get("id"),
                    index=rtc["index"],
                )
                for rtc in raw_tool_calls
            ]
        except KeyError:
            pass

    # 按角色映射到对应的消息类型（与 LangChain 原版逻辑一致）
    if role == "user" or default_class == HumanMessageChunk:
        return HumanMessageChunk(content=content, id=id_)
    if role == "assistant" or default_class == AIMessageChunk:
        return AIMessageChunk(
            content=content,
            additional_kwargs=additional_kwargs,
            id=id_,
            tool_call_chunks=tool_call_chunks,  # type: ignore[arg-type]
        )
    if role in ("system", "developer") or default_class == SystemMessageChunk:
        role_kwargs = {"__openai_role__": "developer"} if role == "developer" else {}
        return SystemMessageChunk(content=content, id=id_, additional_kwargs=role_kwargs)
    if role == "function" or default_class == FunctionMessageChunk:
        return FunctionMessageChunk(content=content, name=_dict["name"], id=id_)
    if role == "tool" or default_class == ToolMessageChunk:
        return ToolMessageChunk(content=content, tool_call_id=_dict["tool_call_id"], id=id_)
    if role or default_class == ChatMessageChunk:
        return ChatMessageChunk(content=content, role=role, id=id_)  # type: ignore[arg-type]
    return default_class(content=content, id=id_)  # type: ignore[call-arg]


def _restore_reasoning_field(payload_msg: dict[str, Any], orig_msg: AIMessage) -> None:
    """把上一轮 AI 消息里的 ``reasoning`` 重新塞回 outgoing payload 的 assistant 消息。

    用于多轮请求：LangChain 在构造发往模型的 payload 时会丢掉 ``reasoning``，
    这里按位置对应地把它补回去。优先用 ``reasoning``，其次 ``reasoning_content``。
    """
    reasoning = orig_msg.additional_kwargs.get("reasoning")
    if reasoning is None:
        reasoning = orig_msg.additional_kwargs.get("reasoning_content")
    if reasoning is not None:
        payload_msg["reasoning"] = reasoning


class VllmChatModel(ChatOpenAI):
    """``ChatOpenAI`` 的子类：跨多轮保住 vLLM 的 ``reasoning`` 字段。

    用法：在 ``config.yaml`` 里把模型的 ``use`` 写成
    ``deerflow.models.vllm_provider:VllmChatModel``（而不是默认的
    ``langchain_openai:ChatOpenAI``），其余配置完全一样。模型工厂
    :func:`deerflow.models.create_chat_model` 会通过反射加载本类。
    """

    model_config = {"arbitrary_types_allowed": True}

    @property
    def _llm_type(self) -> str:
        return "vllm-openai-compatible"

    def _get_request_payload(
        self,
        input_: LanguageModelInput,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """重写请求 payload 构造：在 assistant 消息上补回 ``reasoning``，并归一化 thinking 开关。

        场景：交替「思考→调工具→再思考」时，vLLM 要求上一轮 AI 的 reasoning 原样回传。
        LangChain 默认构造 payload 时会丢掉它，这里按消息位置把它补回去。
        同时调用 :func:`_normalize_vllm_chat_template_kwargs` 把旧键名 ``thinking``
        翻译成 ``enable_thinking``。
        """
        original_messages = self._convert_input(input_).to_messages()
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        _normalize_vllm_chat_template_kwargs(payload)
        payload_messages = payload.get("messages", [])

        if len(payload_messages) == len(original_messages):
            # 长度一致：逐条按位置对应
            for payload_msg, orig_msg in zip(payload_messages, original_messages):
                if payload_msg.get("role") == "assistant" and isinstance(orig_msg, AIMessage):
                    _restore_reasoning_field(payload_msg, orig_msg)
        else:
            # 长度不一致（LangChain 可能插入了 system 消息等）：退回到「只按角色配对」
            ai_messages = [message for message in original_messages if isinstance(message, AIMessage)]
            assistant_payloads = [message for message in payload_messages if message.get("role") == "assistant"]
            for payload_msg, ai_msg in zip(assistant_payloads, ai_messages):
                _restore_reasoning_field(payload_msg, ai_msg)

        return payload

    def _create_chat_result(self, response: dict | openai.BaseModel, generation_info: dict | None = None) -> ChatResult:
        """重写非流式结果解析：把 vLLM ``message.reasoning`` 保住到 ``additional_kwargs``。

        先调父类解析出标准的 ``ChatResult``，再遍历每个 choice，把 vLLM 多出来的
        ``reasoning`` 字段补到对应 ``AIMessage`` 的 ``additional_kwargs`` 上
        （含一份纯文本版 ``reasoning_content``）。
        """
        result = super()._create_chat_result(response, generation_info=generation_info)
        response_dict = response if isinstance(response, dict) else response.model_dump()

        for generation, choice in zip(result.generations, response_dict.get("choices", [])):
            if not isinstance(generation, ChatGeneration):
                continue
            message = generation.message
            if not isinstance(message, AIMessage):
                continue
            reasoning = choice.get("message", {}).get("reasoning")
            if reasoning is None:
                continue
            message.additional_kwargs["reasoning"] = reasoning
            reasoning_text = _reasoning_to_text(reasoning)
            if reasoning_text:
                message.additional_kwargs["reasoning_content"] = reasoning_text

        return result

    def _convert_chunk_to_generation_chunk(
        self,
        chunk: dict,
        default_chunk_class: type,
        base_generation_info: dict | None,
    ) -> ChatGenerationChunk | None:
        """重写流式 chunk 解析：把 vLLM ``delta.reasoning`` 保住到 message chunk。

        流式响应是一块一块到达的（delta），每一块都要保住 reasoning，
        最后拼起来才完整。其余逻辑（finish_reason / usage / logprobs 等）
        与 LangChain 原版一致。
        """
        if chunk.get("type") == "content.delta":
            return None

        token_usage = chunk.get("usage")
        choices = chunk.get("choices", []) or chunk.get("chunk", {}).get("choices", [])
        usage_metadata = _create_usage_metadata(token_usage, chunk.get("service_tier")) if token_usage else None

        if len(choices) == 0:
            generation_chunk = ChatGenerationChunk(
                message=default_chunk_class(content="", usage_metadata=usage_metadata),
                generation_info=base_generation_info,
            )
            if self.output_version == "v1":
                generation_chunk.message.content = []
                generation_chunk.message.response_metadata["output_version"] = "v1"
            return generation_chunk

        choice = choices[0]
        if choice["delta"] is None:
            return None

        message_chunk = _convert_delta_to_message_chunk_with_reasoning(choice["delta"], default_chunk_class)
        generation_info = {**base_generation_info} if base_generation_info else {}

        if finish_reason := choice.get("finish_reason"):
            generation_info["finish_reason"] = finish_reason
            if model_name := chunk.get("model"):
                generation_info["model_name"] = model_name
            if system_fingerprint := chunk.get("system_fingerprint"):
                generation_info["system_fingerprint"] = system_fingerprint
            if service_tier := chunk.get("service_tier"):
                generation_info["service_tier"] = service_tier

        if logprobs := choice.get("logprobs"):
            generation_info["logprobs"] = logprobs

        if usage_metadata and isinstance(message_chunk, AIMessageChunk):
            message_chunk.usage_metadata = usage_metadata

        message_chunk.response_metadata["model_provider"] = "openai"
        return ChatGenerationChunk(message=message_chunk, generation_info=generation_info or None)
