"""utils 模块测试（time + messages）。

hermetic：纯函数，无文件 IO / 无网络 / 无全局状态，直接断言输入输出。
覆盖 now_iso 时区、coerce_iso 各分支、消息内容三态抽取。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

from deerflow.utils import (
    ORIGINAL_USER_CONTENT_KEY,
    coerce_iso,
    get_original_user_content_text,
    message_content_to_text,
    now_iso,
)

# ---------------------------------------------------------------------------
# now_iso
# ---------------------------------------------------------------------------


def test_now_iso_is_utc_iso8601():
    """now_iso 返回带 UTC 偏移的 ISO 8601 字符串。"""
    s = now_iso()
    assert isinstance(s, str)
    # ISO 8601：含 'T' 分隔符与 +00:00 偏移
    assert "T" in s
    assert s.endswith("+00:00")
    # 可被 datetime.fromisoformat 解析回来且带时区
    parsed = datetime.fromisoformat(s)
    assert parsed.tzinfo is not None


# ---------------------------------------------------------------------------
# coerce_iso
# ---------------------------------------------------------------------------


def test_coerce_iso_none_and_empty_return_empty():
    assert coerce_iso(None) == ""
    assert coerce_iso("") == ""


def test_coerce_iso_bool_stringified_not_treated_as_timestamp():
    """bool 是 int 子类，应被当垃圾值 str() 而非转成 unix 时间戳。"""
    assert coerce_iso(True) == "True"
    assert coerce_iso(False) == "False"


def test_coerce_iso_naive_datetime_assumed_utc():
    """无时区 datetime 视为 UTC，输出 ISO（T 分隔）。"""
    naive = datetime(2026, 4, 27, 3, 19, 46)
    assert coerce_iso(naive) == "2026-04-27T03:19:46+00:00"


def test_coerce_iso_aware_datetime_normalized_to_utc():
    """有时区 datetime 归一到 UTC。"""
    aware = datetime(2026, 4, 27, 5, 19, 46, tzinfo=timezone(timedelta(hours=2)))
    assert coerce_iso(aware) == "2026-04-27T03:19:46+00:00"


def test_coerce_iso_int_unix_timestamp():
    ts = 1745724000
    assert coerce_iso(ts) == datetime.fromtimestamp(ts, UTC).isoformat()


def test_coerce_iso_float_unix_timestamp():
    ts = 1745724000.5
    assert coerce_iso(ts) == datetime.fromtimestamp(ts, UTC).isoformat()


def test_coerce_iso_legacy_unix_string():
    """旧版 str(time.time()) 的 10 位 unix 字符串归一成 ISO。"""
    s = "1745724000"
    assert coerce_iso(s) == datetime.fromtimestamp(1745724000, UTC).isoformat()


def test_coerce_iso_iso_string_passthrough():
    """已是 ISO 字符串原样返回。"""
    iso = "2026-04-27T03:19:46+00:00"
    assert coerce_iso(iso) == iso


def test_coerce_iso_short_digit_string_not_treated_as_timestamp():
    """不足 10 位的数字串（如年份 '2026'）不当作 unix 时间戳，原样返回。"""
    assert coerce_iso("2026") == "2026"


def test_coerce_iso_arbitrary_string_passthrough():
    assert coerce_iso("some arbitrary text") == "some arbitrary text"


def test_coerce_iso_other_type_stringified():
    """无法识别的类型兜底 str()。"""
    obj = object()
    assert coerce_iso(obj) == str(obj)


# ---------------------------------------------------------------------------
# message_content_to_text
# ---------------------------------------------------------------------------


def test_message_content_string():
    assert message_content_to_text("hello") == "hello"


def test_message_content_list_of_strings():
    assert message_content_to_text(["a", "b"]) == "a\nb"


def test_message_content_list_of_text_dicts():
    content = [{"type": "text", "text": "hello"}, {"type": "text", "text": "world"}]
    assert message_content_to_text(content) == "hello\nworld"


def test_message_content_list_mixed_skips_non_text_blocks():
    """非 text 块（如 image_url）与无 text 字段块被跳过。"""
    content = ["intro", {"type": "image_url", "image_url": {"url": "..."}}, {"type": "text", "text": "tail"}]
    assert message_content_to_text(content) == "intro\ntail"


def test_message_content_empty_list():
    assert message_content_to_text([]) == ""


def test_message_content_other_type_stringified():
    assert message_content_to_text(123) == "123"


# ---------------------------------------------------------------------------
# get_original_user_content_text
# ---------------------------------------------------------------------------


def test_original_user_content_key_constant():
    assert ORIGINAL_USER_CONTENT_KEY == "original_user_content"


def test_get_original_user_content_text_prefers_additional_kwargs():
    """additional_kwargs 有 original_user_content（str）时优先用它。"""
    assert get_original_user_content_text("current", {"original_user_content": "original"}) == "original"


def test_get_original_user_content_text_falls_back_to_content():
    """无 original key 或 additional_kwargs 为 None/空时回退到 content 抽取。"""
    assert get_original_user_content_text("current", None) == "current"
    assert get_original_user_content_text("current", {}) == "current"


def test_get_original_user_content_text_non_string_key_ignored():
    """original_user_content 非 str（如 dict）时不用它，回退 content。"""
    content = [{"type": "text", "text": "from-content"}]
    assert get_original_user_content_text(content, {"original_user_content": {"x": 1}}) == "from-content"


def test_get_original_user_content_text_content_list_path():
    """回退时走 message_content_to_text 的 list 分支。"""
    content = [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]
    assert get_original_user_content_text(content, None) == "a\nb"
