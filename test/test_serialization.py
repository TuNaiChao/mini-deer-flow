"""M9 serialization + converters 的 hermetic 测试。

覆盖（对齐 ALIGNMENT_OUTLINE M9 测试要求）：
- serialize_lc_object：标量/dict/list 递归、pydantic model_dump、fallback str。
- serialize_channel_values：剥 ``__pregel_*`` / ``__interrupt__``，保留其余键、递归。
- strip_data_url_image_blocks：只剥 hide_from_ui 消息里的 ``data:`` image_url 块，
  保留顺序/数量；text 块、https URL、非 hide_from_ui 消息不动。
- serialize_channel_values_for_api：组合剥 __pregel_* + 剥 base64 图片。
- serialize_messages_tuple：(chunk, metadata) → [serialized, metadata]；非 tuple 兜底。
- serialize mode 分发：messages / values / default。
- converters：human/ai 文本/ai tool_calls/system/tool、finish_reason 推断、completion、批量。

hermetic 约定：纯函数，无 fixture、无 IO、无网络。converters 用 SimpleNamespace 鸭子类型
精确控制（避免 langchain 版本差异）。
"""

from __future__ import annotations

from types import SimpleNamespace

from pydantic import BaseModel

from deerflow.runtime.converters import (
    _infer_finish_reason,
    langchain_messages_to_openai,
    langchain_to_openai_completion,
    langchain_to_openai_message,
)
from deerflow.runtime.serialization import (
    serialize,
    serialize_channel_values,
    serialize_channel_values_for_api,
    serialize_lc_object,
    serialize_messages_tuple,
    strip_data_url_image_blocks,
)

# ---------------------------------------------------------------------------
# serialize_lc_object
# ---------------------------------------------------------------------------


class TestSerializeLcObject:
    def test_primitives_passthrough(self):
        assert serialize_lc_object(None) is None
        assert serialize_lc_object("s") == "s"
        assert serialize_lc_object(42) == 42
        assert serialize_lc_object(3.14) == 3.14
        assert serialize_lc_object(True) is True

    def test_dict_recursion(self):
        out = serialize_lc_object({"a": 1, "b": {"c": [1, 2, 3]}})
        assert out == {"a": 1, "b": {"c": [1, 2, 3]}}

    def test_list_and_tuple_to_list(self):
        assert serialize_lc_object([1, "x", None]) == [1, "x", None]
        # tuple 也变成 list（JSON 没有 tuple）
        assert serialize_lc_object((1, 2)) == [1, 2]

    def test_pydantic_model_dump(self):
        class P(BaseModel):
            name: str
            n: int

        out = serialize_lc_object(P(name="abc", n=5))
        assert out == {"name": "abc", "n": 5}

    def test_nested_pydantic_in_dict(self):
        class Item(BaseModel):
            v: int

        out = serialize_lc_object({"items": [Item(v=1), Item(v=2)]})
        assert out == {"items": [{"v": 1}, {"v": 2}]}

    def test_fallback_str_for_unknown(self):
        # 不可 model_dump 的对象 → str()
        class NoDump:
            def __repr__(self):
                return "<NoDump>"

            def __str__(self):
                return "string-form"

        assert serialize_lc_object(NoDump()) == "string-form"


# ---------------------------------------------------------------------------
# serialize_channel_values（剥 __pregel_*）
# ---------------------------------------------------------------------------


class TestSerializeChannelValues:
    def test_strips_pregel_and_interrupt(self):
        out = serialize_channel_values(
            {
                "__pregel_node_finished": {"x": 1},
                "__interrupt__": ["something"],
                "messages": [{"role": "user", "content": "hi"}],
                "title": "t",
            }
        )
        assert "__pregel_node_finished" not in out
        assert "__interrupt__" not in out
        assert out["messages"] == [{"role": "user", "content": "hi"}]
        assert out["title"] == "t"

    def test_strips_all_pregel_prefixed_keys(self):
        out = serialize_channel_values(
            {
                "__pregel_a": 1,
                "__pregel_xyz": 2,
                "__interrupt__": 3,
                "keep": 4,
            }
        )
        assert out == {"keep": 4}

    def test_recurses_values(self):
        class M(BaseModel):
            v: int

        out = serialize_channel_values({"obj": M(v=9), "lst": [M(v=1)]})
        assert out == {"obj": {"v": 9}, "lst": [{"v": 1}]}

    def test_normal_underscore_keys_kept(self):
        # 单下划线/双下划线非 pregel 的键保留
        out = serialize_channel_values({"_private": 1, "__custom__": 2})
        assert out == {"_private": 1, "__custom__": 2}


# ---------------------------------------------------------------------------
# strip_data_url_image_blocks
# ---------------------------------------------------------------------------


def _hidden_msg_with_data_image():
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": "看这张图"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0K"}},
        ],
        "additional_kwargs": {"hide_from_ui": True},
    }


class TestStripDataUrlImageBlocks:
    def test_strips_data_url_from_hidden_message(self):
        msgs = [_hidden_msg_with_data_image()]
        out = strip_data_url_image_blocks(msgs)
        assert len(out) == 1  # 消息数量不变
        # 只剩 text 块
        assert [b["type"] for b in out[0]["content"]] == ["text"]

    def test_preserves_order_and_count(self):
        visible = {"role": "user", "content": "hi", "additional_kwargs": {}}
        hidden = _hidden_msg_with_data_image()
        out = strip_data_url_image_blocks([visible, hidden])
        assert len(out) == 2  # 数量不变
        assert out[0] is visible or out[0] == visible  # 顺序不变
        # 第二条（hidden）的 data image 被剥
        assert all(not (b.get("type") == "image_url" and str(b.get("image_url", {}).get("url", "")).startswith("data:")) for b in out[1]["content"])

    def test_leaves_non_hidden_messages_untouched(self):
        # 非 hide_from_ui 的消息，即使有 data: image_url，也不剥
        msg = {
            "role": "user",
            "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,xxx"}}],
            "additional_kwargs": {},
        }
        out = strip_data_url_image_blocks([msg])
        assert out[0] == msg  # 原样

    def test_leaves_https_image_urls(self):
        # hide_from_ui 但 https URL 的图片块保留
        msg = {
            "role": "user",
            "content": [{"type": "image_url", "image_url": {"url": "https://example.com/a.png"}}],
            "additional_kwargs": {"hide_from_ui": True},
        }
        out = strip_data_url_image_blocks([msg])
        assert out[0]["content"] == msg["content"]  # 不剥

    def test_hide_from_ui_must_be_true(self):
        # hide_from_ui=False / 缺失都不剥
        msg = {
            "role": "user",
            "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,x"}}],
            "additional_kwargs": {"hide_from_ui": False},
        }
        out = strip_data_url_image_blocks([msg])
        assert out[0]["content"] == msg["content"]

    def test_non_list_content_untouched(self):
        msg = {"role": "user", "content": "plain text", "additional_kwargs": {"hide_from_ui": True}}
        out = strip_data_url_image_blocks([msg])
        assert out[0] == msg

    def test_non_dict_message_passed_through(self):
        out = strip_data_url_image_blocks(["raw", None, 42])  # type: ignore[list-item]
        assert out == ["raw", None, 42]


# ---------------------------------------------------------------------------
# serialize_channel_values_for_api
# ---------------------------------------------------------------------------


class TestSerializeForApi:
    def test_combines_strip_pregel_and_images(self):
        hidden = _hidden_msg_with_data_image()
        channel_values = {
            "__pregel_internal": "x",
            "messages": [hidden],
            "title": "t",
        }
        out = serialize_channel_values_for_api(channel_values)
        assert "__pregel_internal" not in out
        assert out["title"] == "t"
        # messages 里的 data image 被剥
        assert all(b["type"] == "text" for b in out["messages"][0]["content"])

    def test_no_messages_key_ok(self):
        out = serialize_channel_values_for_api({"__pregel_x": 1, "title": "t"})
        assert out == {"title": "t"}


# ---------------------------------------------------------------------------
# serialize_messages_tuple + serialize mode 分发
# ---------------------------------------------------------------------------


class TestMessagesTuple:
    def test_two_tuple(self):
        chunk = SimpleNamespace(model_dump=lambda: {"role": "ai", "content": "hi"})
        metadata = {"seq": 1}
        out = serialize_messages_tuple((chunk, metadata))
        assert out == [{"role": "ai", "content": "hi"}, {"seq": 1}]

    def test_two_tuple_non_dict_metadata(self):
        chunk = "text"
        out = serialize_messages_tuple((chunk, "not-a-dict"))
        # metadata 非 dict → 兜底成 {}
        assert out == ["text", {}]

    def test_non_tuple_falls_back(self):
        assert serialize_messages_tuple({"a": 1}) == {"a": 1}
        assert serialize_messages_tuple("plain") == "plain"


class TestSerializeModeDispatch:
    def test_messages_mode(self):
        out = serialize(("hi", {"seq": 2}), mode="messages")
        assert out == ["hi", {"seq": 2}]

    def test_values_mode_dict(self):
        out = serialize({"__pregel_x": 1, "keep": 2}, mode="values")
        assert out == {"keep": 2}

    def test_values_mode_non_dict(self):
        # 非 dict 走 serialize_lc_object
        assert serialize([1, 2], mode="values") == [1, 2]

    def test_default_mode(self):
        out = serialize({"a": [1, 2]})
        assert out == {"a": [1, 2]}
        # default 不剥 __pregel_*
        out2 = serialize({"__pregel_x": 1}, mode="")
        assert out2 == {"__pregel_x": 1}


# ---------------------------------------------------------------------------
# converters
# ---------------------------------------------------------------------------


def _msg(type_, **kw):
    """构造一个鸭子类型的 LangChain 消息（用 SimpleNamespace）。"""
    return SimpleNamespace(type=type_, **kw)


class TestConverters:
    def test_human_to_user(self):
        m = _msg("human", content="hello")
        assert langchain_to_openai_message(m) == {"role": "user", "content": "hello"}

    def test_system(self):
        m = _msg("system", content="be helpful")
        assert langchain_to_openai_message(m) == {"role": "system", "content": "be helpful"}

    def test_ai_text_only(self):
        m = _msg("ai", content="sure", tool_calls=[])
        assert langchain_to_openai_message(m) == {"role": "assistant", "content": "sure"}

    def test_ai_with_tool_calls_content_null(self):
        m = _msg(
            "ai",
            content="",
            tool_calls=[{"id": "tc1", "name": "search", "args": {"q": "x"}}],
        )
        out = langchain_to_openai_message(m)
        assert out["role"] == "assistant"
        assert out["content"] is None  # 无文本 → null
        assert out["tool_calls"] == [{"id": "tc1", "type": "function", "function": {"name": "search", "arguments": '{"q": "x"}'}}]

    def test_ai_text_plus_tool_calls(self):
        m = _msg(
            "ai",
            content="thinking",
            tool_calls=[{"id": "tc1", "name": "run", "args": {}}],
        )
        out = langchain_to_openai_message(m)
        assert out["content"] == "thinking"
        assert out["tool_calls"][0]["function"]["name"] == "run"
        # 空 dict args → "{}"
        assert out["tool_calls"][0]["function"]["arguments"] == "{}"

    def test_ai_tool_call_string_args_passthrough(self):
        m = _msg("ai", content="", tool_calls=[{"id": "t", "name": "f", "args": '{"pre": "serialized"}'}])
        out = langchain_to_openai_message(m)
        assert out["tool_calls"][0]["function"]["arguments"] == '{"pre": "serialized"}'

    def test_ai_list_content_preserved(self):
        m = _msg("ai", content=[{"type": "text", "text": "x"}], tool_calls=[])
        out = langchain_to_openai_message(m)
        assert out["content"] == [{"type": "text", "text": "x"}]

    def test_tool_message(self):
        m = _msg("tool", content="result", tool_call_id="tc9")
        assert langchain_to_openai_message(m) == {"role": "tool", "tool_call_id": "tc9", "content": "result"}

    def test_unknown_role_passthrough(self):
        m = _msg("custom", content="x")
        assert langchain_to_openai_message(m) == {"role": "custom", "content": "x"}

    def test_messages_to_openai_batch(self):
        msgs = [_msg("human", content="hi"), _msg("ai", content="yo", tool_calls=[])]
        out = langchain_messages_to_openai(msgs)
        assert out == [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}]


class TestInferFinishReason:
    def test_tool_calls_present(self):
        m = _msg("ai", content="", tool_calls=[{"id": "x", "name": "f", "args": {}}])
        assert _infer_finish_reason(m) == "tool_calls"

    def test_from_response_metadata(self):
        m = _msg("ai", content="x", tool_calls=[], response_metadata={"finish_reason": "length"})
        assert _infer_finish_reason(m) == "length"

    def test_default_stop(self):
        m = _msg("ai", content="x", tool_calls=[])
        assert _infer_finish_reason(m) == "stop"


class TestCompletion:
    def test_completion_with_usage(self):
        m = _msg(
            "ai",
            content="answer",
            id="msg-1",
            tool_calls=[],
            response_metadata={"model_name": "gpt-4"},
            usage_metadata={"input_tokens": 10, "output_tokens": 5},
        )
        out = langchain_to_openai_completion(m)
        assert out["id"] == "msg-1"
        assert out["model"] == "gpt-4"
        assert out["choices"][0]["message"]["content"] == "answer"
        assert out["choices"][0]["finish_reason"] == "stop"
        assert out["usage"] == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}

    def test_completion_without_usage(self):
        m = _msg("ai", content="x", id="m2", tool_calls=[], response_metadata={})
        out = langchain_to_openai_completion(m)
        assert out["usage"] is None
        assert out["model"] is None
