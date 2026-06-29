"""SystemMessageCoalescingMiddleware 单元测试（M16，#3711）。

hermetic：用本地消息对象构造 ModelRequest，无真实模型 / 网络。
覆盖：合并多条 SystemMessage、system_message 字段合并、id / additional_kwargs 保留、
非系统消息保序、dynamic_context_reminder 去重（保留最后一条）、无 SystemMessage 时直通、
sync/async。
"""

from __future__ import annotations

import asyncio

from langchain_core.messages import HumanMessage, SystemMessage

from deerflow.agents.middlewares.system_message_coalescing_middleware import (
    SystemMessageCoalescingMiddleware,
    _coalesce_request,
    _flatten_content,
)

# dynamic_context_reminder 的 additional_kwargs key（与 dynamic_context_middleware 一致）。
_DCR_KEY = "dynamic_context_reminder"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class _FakeRequest:
    """带 override 的可变 request 替身。"""

    def __init__(self, *, messages=None, system_message=None):
        self.messages = list(messages) if messages else []
        self.system_message = system_message

    def override(self, **kw):
        new = _FakeRequest()
        new.messages = self.messages
        new.system_message = self.system_message
        new.__dict__.update(kw)
        return new


def _run_mw(mw, request):
    seen = {}

    def handler(req):
        seen["req"] = req
        return "ok"

    mw.wrap_model_call(request, handler)
    return seen["req"]


# ---------------------------------------------------------------------------
# _flatten_content
# ---------------------------------------------------------------------------


def test_flatten_string():
    assert _flatten_content("hello") == "hello"


def test_flatten_list_of_strings():
    assert _flatten_content(["a", "b"]) == "a\nb"


def test_flatten_list_of_text_blocks():
    assert _flatten_content([{"type": "text", "text": "x"}, {"type": "text", "text": "y"}]) == "x\ny"


def test_flatten_mixed_list():
    out = _flatten_content(["str", {"text": "dict"}, 42])
    assert "str" in out and "dict" in out and "42" in out


def test_flatten_non_str_non_list():
    assert _flatten_content(123) == "123"


# ---------------------------------------------------------------------------
# _coalesce_request
# ---------------------------------------------------------------------------


def test_no_system_message_passthrough():
    """messages 里没有 SystemMessage → 返回 None（直通，保 prefix-cache）。"""
    req = _FakeRequest(messages=[HumanMessage(content="hi"), SystemMessage(content="x")])
    # 有 SystemMessage 时不该返回 None
    assert _coalesce_request(req) is not None
    # 真正没有 SystemMessage
    req2 = _FakeRequest(messages=[HumanMessage(content="hi")])
    assert _coalesce_request(req2) is None


def test_coalesce_multiple_system_messages_into_one():
    """messages 里多条 SystemMessage → 合并成一条 system_message，从 messages 移除。"""
    req = _FakeRequest(messages=[SystemMessage(content="A"), HumanMessage(content="u"), SystemMessage(content="B")])
    out = _coalesce_request(req)
    assert out is not None
    # 合并后只有一条 system_message
    assert isinstance(out.system_message, SystemMessage)
    assert "A" in out.system_message.content
    assert "B" in out.system_message.content
    # messages 里不再有 SystemMessage，非系统消息保序
    assert all(not isinstance(m, SystemMessage) for m in out.messages)
    assert isinstance(out.messages[0], HumanMessage)
    assert out.messages[0].content == "u"


def test_coalesce_merges_system_message_field_and_in_messages():
    """request.system_message（静态 prompt）+ messages 里的 SystemMessage → 全合并。"""
    req = _FakeRequest(
        messages=[SystemMessage(content="dynamic")],
        system_message=SystemMessage(content="static-prompt", id="static-id"),
    )
    out = _coalesce_request(req)
    assert "static-prompt" in out.system_message.content
    assert "dynamic" in out.system_message.content


def test_coalesce_preserves_first_id():
    """合并后保留第一条 SystemMessage 的 id（通常是静态 prompt）。"""
    req = _FakeRequest(
        messages=[SystemMessage(content="dynamic")],
        system_message=SystemMessage(content="static", id="prompt-id"),
    )
    out = _coalesce_request(req)
    assert out.system_message.id == "prompt-id"


def test_coalesce_merges_additional_kwargs():
    """所有 SystemMessage 的 additional_kwargs 合并到结果上（保留 hide_from_ui / reminder 标记）。"""
    s1 = SystemMessage(content="A", additional_kwargs={"hide_from_ui": True})
    s2 = SystemMessage(content="B", additional_kwargs={_DCR_KEY: True})
    req = _FakeRequest(messages=[s1, s2])
    out = _coalesce_request(req)
    assert out.system_message.additional_kwargs.get("hide_from_ui") is True
    assert out.system_message.additional_kwargs.get(_DCR_KEY) is True


def test_coalesce_dedup_reminders_keeps_last():
    """多条 dynamic_context_reminder（跨午夜）→ 只保留最后一条（最新日期）。"""
    r1 = SystemMessage(content="old date", additional_kwargs={_DCR_KEY: True})
    r2 = SystemMessage(content="new date", additional_kwargs={_DCR_KEY: True})
    req = _FakeRequest(messages=[r1, HumanMessage(content="u"), r2])
    out = _coalesce_request(req)
    merged = out.system_message.content
    # 旧日期被丢掉，新日期保留
    assert "new date" in merged
    assert "old date" not in merged


def test_coalesce_single_reminder_kept():
    """只有一条 reminder → 不丢。"""
    r = SystemMessage(content="the date", additional_kwargs={_DCR_KEY: True})
    req = _FakeRequest(messages=[r])
    out = _coalesce_request(req)
    assert "the date" in out.system_message.content


def test_coalesce_non_reminder_systems_all_kept():
    """非 reminder 的多条 SystemMessage 全部保留（不去重）。"""
    req = _FakeRequest(messages=[SystemMessage(content="rule A"), SystemMessage(content="rule B")])
    out = _coalesce_request(req)
    assert "rule A" in out.system_message.content
    assert "rule B" in out.system_message.content


# ---------------------------------------------------------------------------
# 中间件 wrap_model_call
# ---------------------------------------------------------------------------


def test_wrap_coalesces_and_calls_handler():
    mw = SystemMessageCoalescingMiddleware()
    req = _FakeRequest(messages=[SystemMessage(content="s1"), SystemMessage(content="s2"), HumanMessage(content="u")])
    out = _run_mw(mw, req)
    # handler 看到合并后的请求：一条 system_message，messages 只剩 HumanMessage
    assert isinstance(out.system_message, SystemMessage)
    assert "s1" in out.system_message.content and "s2" in out.system_message.content
    assert len(out.messages) == 1 and isinstance(out.messages[0], HumanMessage)


def test_wrap_passthrough_when_no_system_in_messages():
    """messages 无 SystemMessage → handler 收到原请求（零改动，保 cache）。"""
    mw = SystemMessageCoalescingMiddleware()
    orig = _FakeRequest(messages=[HumanMessage(content="hi")], system_message=SystemMessage(content="prompt"))
    seen = []

    def handler(req):
        seen.append(req)
        return "ok"

    mw.wrap_model_call(orig, handler)
    # system_message 原样未被替换（同一对象）
    assert seen[0].system_message is orig.system_message


def test_awrap_coalesces():
    mw = SystemMessageCoalescingMiddleware()
    req = _FakeRequest(messages=[SystemMessage(content="a"), SystemMessage(content="b")])
    seen = {}

    async def handler(r):
        seen["sm"] = r.system_message
        return "ok"

    asyncio.run(mw.awrap_model_call(req, handler))
    assert "a" in seen["sm"].content and "b" in seen["sm"].content
