"""``models/vllm_provider.py`` 的 hermetic 单元测试。

不依赖 vLLM 服务 / 网络 / config.yaml——直接构造 ``VllmChatModel`` 实例（用 dummy
api_key + base_url，构造阶段不发任何请求），再喂造 OpenAI 风格的 dict 响应/chunk/payload
喂给被重写的三个钩子，验证 ``reasoning`` 字段在三处都被保住。

覆盖的契约：
- 反射加载：``deerflow.models.vllm_provider:VllmChatModel`` 能被 ``resolve_class`` 找到
  且是 ``BaseChatModel`` 的子类（这是 ``config.example.yaml`` 路径 D 能跑的前提）。
- 非流式响应 ``_create_chat_result``：vLLM 多出来的 ``message.reasoning`` 被保住到
  ``AIMessage.additional_kwargs``（含一份纯文本 ``reasoning_content``）。
- 流式 delta ``_convert_chunk_to_generation_chunk``：``delta.reasoning`` 被保住。
- 多轮请求 ``_get_request_payload``：上一轮 AI 消息的 ``reasoning`` 被重新塞回 outgoing
  payload 的 assistant 消息（vLLM 交替「思考→调工具→再思考」需要）。
- 旧键名归一化：``extra_body.chat_template_kwargs.thinking`` → ``enable_thinking``。
- 纯函数 ``_reasoning_to_text`` 的几种输入形态。
"""

from __future__ import annotations

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage

from deerflow.models.vllm_provider import (
    VllmChatModel,
    _normalize_vllm_chat_template_kwargs,
    _reasoning_to_text,
)
from deerflow.reflection import resolve_class

# config.example.yaml 路径 D 里写的 use 字符串——必须能被反射加载（否则 ImportError）
_VLLM_USE_PATH = "deerflow.models.vllm_provider:VllmChatModel"


def _make_model(**kwargs) -> VllmChatModel:
    """构造一个不触网的 VllmChatModel。构造阶段不发起任何 HTTP 请求。"""
    base = {"model": "qwen-test", "api_key": "sk-test", "base_url": "http://localhost:9999/v1"}
    base.update(kwargs)
    return VllmChatModel(**base)


# ---------------------------------------------------------------------------
# 反射加载 + 类型契约（config.example 路径 D 能跑的前提）
# ---------------------------------------------------------------------------


def test_resolve_class_loads_vllm_provider():
    """``deerflow.models.vllm_provider:VllmChatModel`` 能被反射加载——这是消除 config
    dangling 引用的核心保证。缺实现时此处会 ImportError。"""
    cls = resolve_class(_VLLM_USE_PATH, BaseChatModel)

    assert cls is VllmChatModel


def test_vllm_model_is_chatopenai_subclass():
    """VllmChatModel 必须是 ChatOpenAI 的子类（这样它继承了所有 OpenAI 兼容行为，
    只重写三个钩子）。"""
    from langchain_openai import ChatOpenAI

    assert issubclass(VllmChatModel, ChatOpenAI)
    assert issubclass(VllmChatModel, BaseChatModel)


def test_llm_type():
    assert _make_model()._llm_type == "vllm-openai-compatible"


# ---------------------------------------------------------------------------
# 钩子 1：非流式响应 _create_chat_result
# ---------------------------------------------------------------------------


def _full_response(*, reasoning=None, content="hi"):
    """造一个最小但 OpenAI 合规的 chat completion dict。"""
    message: dict = {"role": "assistant", "content": content}
    if reasoning is not None:
        message["reasoning"] = reasoning
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1,
        "model": "qwen-test",
        "choices": [{"index": 0, "finish_reason": "stop", "message": message}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
    }


def test_create_chat_result_preserves_reasoning():
    """非流式响应：vLLM 的 message.reasoning 被保住到 additional_kwargs。"""
    model = _make_model()

    result = model._create_chat_result(_full_response(reasoning="I thought hard"))

    msg = result.generations[0].message
    assert isinstance(msg, AIMessage)
    assert msg.content == "hi"
    assert msg.additional_kwargs["reasoning"] == "I thought hard"
    # 纯文本版也被算出来
    assert msg.additional_kwargs["reasoning_content"] == "I thought hard"


def test_create_chat_result_no_reasoning_is_noop():
    """没有 reasoning 字段时，additional_kwargs 不应被注入 reasoning 相关键。"""
    model = _make_model()

    result = model._create_chat_result(_full_response())

    msg = result.generations[0].message
    assert "reasoning" not in msg.additional_kwargs
    assert "reasoning_content" not in msg.additional_kwargs


# ---------------------------------------------------------------------------
# 钩子 2：流式 delta _convert_chunk_to_generation_chunk
# ---------------------------------------------------------------------------


def _stream_chunk(*, reasoning=None, content="hi", finish_reason=None):
    delta: dict = {"role": "assistant", "content": content}
    if reasoning is not None:
        delta["reasoning"] = reasoning
    return {
        "id": "chunk-1",
        "model": "qwen-test",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


def test_convert_chunk_preserves_reasoning():
    """流式 delta：delta.reasoning 被保住到 message chunk 的 additional_kwargs。"""
    model = _make_model()

    gc = model._convert_chunk_to_generation_chunk(
        _stream_chunk(reasoning="thinking..."),
        default_chunk_class=AIMessageChunk,
        base_generation_info=None,
    )

    assert gc is not None
    assert isinstance(gc.message, AIMessageChunk)
    assert gc.message.additional_kwargs["reasoning"] == "thinking..."
    assert gc.message.additional_kwargs["reasoning_content"] == "thinking..."


def test_convert_chunk_records_finish_reason():
    """finish_reason 被记到 generation_info（与 LangChain 原版一致）。"""
    model = _make_model()

    gc = model._convert_chunk_to_generation_chunk(
        _stream_chunk(finish_reason="stop"),
        default_chunk_class=AIMessageChunk,
        base_generation_info=None,
    )

    assert gc is not None
    assert gc.generation_info["finish_reason"] == "stop"


# ---------------------------------------------------------------------------
# 钩子 3：多轮请求 _get_request_payload（reasoning 回灌 + thinking 归一化）
# ---------------------------------------------------------------------------


def test_get_request_payload_reinjects_reasoning_on_assistant():
    """多轮：上一轮 AI 消息的 reasoning 被重新塞回 payload 的 assistant 消息。"""
    model = _make_model()
    msgs = [
        HumanMessage(content="hi"),
        AIMessage(content="ok", additional_kwargs={"reasoning": "prev thought"}),
    ]

    payload = model._get_request_payload(msgs)

    roles = [pm.get("role") for pm in payload["messages"]]
    assert roles == ["user", "assistant"]
    assistant_payload = payload["messages"][1]
    assert assistant_payload["reasoning"] == "prev thought"


def test_get_request_payload_no_reasoning_is_noop():
    """AI 消息没有 reasoning 时，payload assistant 消息不应出现 reasoning 键。"""
    model = _make_model()
    msgs = [HumanMessage(content="hi"), AIMessage(content="ok")]

    payload = model._get_request_payload(msgs)

    assert "reasoning" not in payload["messages"][1]


def test_get_request_payload_normalizes_legacy_thinking_toggle():
    """旧键名 thinking 在发请求前被归一化成 enable_thinking。"""
    model = _make_model(extra_body={"chat_template_kwargs": {"thinking": True}})

    payload = model._get_request_payload([HumanMessage(content="hi")])

    ct = payload["extra_body"]["chat_template_kwargs"]
    assert ct == {"enable_thinking": True}
    # 旧键名必须被移除，否则 vLLM 解析器仍会困惑
    assert "thinking" not in ct


# ---------------------------------------------------------------------------
# 纯函数：_normalize_vllm_chat_template_kwargs
# ---------------------------------------------------------------------------


def test_normalize_maps_legacy_thinking_to_enable_thinking():
    payload = {"extra_body": {"chat_template_kwargs": {"thinking": False}}}

    _normalize_vllm_chat_template_kwargs(payload)

    assert payload["extra_body"]["chat_template_kwargs"] == {"enable_thinking": False}


def test_normalize_is_noop_without_thinking_key():
    payload = {"extra_body": {"chat_template_kwargs": {"enable_thinking": True}}}

    _normalize_vllm_chat_template_kwargs(payload)

    assert payload["extra_body"]["chat_template_kwargs"] == {"enable_thinking": True}


def test_normalize_is_noop_without_extra_body():
    payload = {"messages": []}

    _normalize_vllm_chat_template_kwargs(payload)  # 不应抛异常

    assert payload == {"messages": []}


def test_normalize_is_noop_when_chat_template_kwargs_not_dict():
    payload = {"extra_body": {"chat_template_kwargs": "not-a-dict"}}

    _normalize_vllm_chat_template_kwargs(payload)  # 不应抛异常

    assert payload["extra_body"]["chat_template_kwargs"] == "not-a-dict"


# ---------------------------------------------------------------------------
# 纯函数：_reasoning_to_text
# ---------------------------------------------------------------------------


def test_reasoning_to_text_string_passthrough():
    assert _reasoning_to_text("hello") == "hello"


def test_reasoning_to_text_list_join():
    assert _reasoning_to_text(["a", "b", "", "c"]) == "abc"


def test_reasoning_to_text_dict_by_key():
    assert _reasoning_to_text({"text": "from-text"}) == "from-text"
    assert _reasoning_to_text({"content": "from-content"}) == "from-content"


def test_reasoning_to_text_dict_fallback_json():
    # 没有可识别键 → 退回 json 序列化
    assert _reasoning_to_text({"weird": 1}) == '{"weird": 1}'


@pytest.mark.parametrize("value", [None, 123, 4.5])
def test_reasoning_to_text_scalar_fallback(value):
    # 标量（非 str/list/dict）→ json 序列化
    import json

    assert _reasoning_to_text(value) == json.dumps(value, ensure_ascii=False)
