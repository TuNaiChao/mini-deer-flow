"""user_context 模块测试（用户隔离基石）。

hermetic：用 types.SimpleNamespace 构造结构化的 CurrentUser（鸭子类型，无需 app
层），通过 set_current_user 显式控制 contextvar，并在 try/finally 里 reset 自己
的 token（避免与 conftest 的 autouse user-context fixture 形成嵌套 token）。
测「无 user」场景用 ``@pytest.mark.no_auto_user`` 关闭 autouse 注入。
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest

from deerflow.runtime.user_context import (
    AUTO,
    DEFAULT_USER_ID,
    get_current_user,
    get_effective_user_id,
    require_current_user,
    reset_current_user,
    resolve_runtime_user_id,
    resolve_user_id,
    set_current_user,
)


def _user(uid: str | UUID = "u-1") -> SimpleNamespace:
    """构造一个结构化的 CurrentUser（仅 .id 属性即可满足 Protocol）。"""
    return SimpleNamespace(id=uid)


# ---------------------------------------------------------------------------
# CurrentUser Protocol（结构性）
# ---------------------------------------------------------------------------


def test_current_user_protocol_structural():
    """任何带 .id 属性的对象都满足 CurrentUser（鸭子类型协议）。"""
    from deerflow.runtime.user_context import CurrentUser

    assert isinstance(_user("u-1"), CurrentUser)
    # 缺 id 属性的对象不满足
    assert not isinstance(SimpleNamespace(name="x"), CurrentUser)


# ---------------------------------------------------------------------------
# set / reset / get / require
# ---------------------------------------------------------------------------


def test_set_and_get_current_user():
    token = set_current_user(_user("u-1"))
    try:
        assert get_current_user() is not None
        assert get_current_user().id == "u-1"
    finally:
        reset_current_user(token)


def test_reset_restores_previous():
    """reset(token) 恢复到 token 捕获时的旧值（嵌套 set 语义）。"""
    token = set_current_user(_user("first"))
    try:
        inner = set_current_user(_user("second"))
        assert get_current_user().id == "second"
        reset_current_user(inner)
        assert get_current_user().id == "first"
    finally:
        reset_current_user(token)


@pytest.mark.no_auto_user
def test_get_current_user_none_when_unset():
    assert get_current_user() is None


@pytest.mark.no_auto_user
def test_require_current_user_raises_when_unset():
    with pytest.raises(RuntimeError, match="without user context"):
        require_current_user()


def test_require_current_user_returns_user_when_set():
    token = set_current_user(_user("u-1"))
    try:
        assert require_current_user().id == "u-1"
    finally:
        reset_current_user(token)


# ---------------------------------------------------------------------------
# DEFAULT_USER_ID + get_effective_user_id
# ---------------------------------------------------------------------------


def test_default_user_id_constant():
    assert DEFAULT_USER_ID == "default"


@pytest.mark.no_auto_user
def test_effective_user_id_falls_back_to_default_when_unset():
    assert get_effective_user_id() == "default"


def test_effective_user_id_returns_str_id():
    token = set_current_user(_user("u-1"))
    try:
        assert get_effective_user_id() == "u-1"
    finally:
        reset_current_user(token)


def test_effective_user_id_coerces_uuid_to_str():
    """User.id 可能是 UUID，边界处 str() 化（红线 #10 UUID→str）。"""
    uid = UUID("12345678-1234-5678-1234-567812345678")
    token = set_current_user(_user(uid))
    try:
        result = get_effective_user_id()
        assert result == str(uid)
        assert isinstance(result, str)
    finally:
        reset_current_user(token)


# ---------------------------------------------------------------------------
# resolve_runtime_user_id（三优先级）
# ---------------------------------------------------------------------------


def test_resolve_runtime_user_id_prefers_context():
    """优先级 1：runtime.context["user_id"] 最高。"""
    runtime = SimpleNamespace(context={"user_id": "ctx-user"})
    token = set_current_user(_user("cv-user"))
    try:
        assert resolve_runtime_user_id(runtime) == "ctx-user"
    finally:
        reset_current_user(token)


def test_resolve_runtime_user_id_falls_back_to_contextvar():
    """优先级 2：runtime 无 user_id 时回退 contextvar。"""
    runtime = SimpleNamespace(context={})
    token = set_current_user(_user("cv-user"))
    try:
        assert resolve_runtime_user_id(runtime) == "cv-user"
    finally:
        reset_current_user(token)


def test_resolve_runtime_user_id_no_context_attr():
    """runtime 无 context 属性 → 回退 effective（contextvar）。"""
    token = set_current_user(_user("cv-user"))
    try:
        assert resolve_runtime_user_id(SimpleNamespace()) == "cv-user"
    finally:
        reset_current_user(token)


def test_resolve_runtime_user_id_runtime_none():
    """runtime 为 None → 回退 effective。"""
    token = set_current_user(_user("cv-user"))
    try:
        assert resolve_runtime_user_id(None) == "cv-user"
    finally:
        reset_current_user(token)


@pytest.mark.no_auto_user
def test_resolve_runtime_user_id_unset_contextvar_returns_default():
    assert resolve_runtime_user_id(None) == "default"


def test_resolve_runtime_user_id_context_value_coerced_to_str():
    """context["user_id"] 非 str 时 str() 化（边界）。"""
    runtime = SimpleNamespace(context={"user_id": 12345})
    assert resolve_runtime_user_id(runtime) == "12345"


def test_resolve_runtime_user_id_empty_context_user_id_ignored():
    """context["user_id"] 为空（None/""）时不用它，回退 effective。"""
    runtime = SimpleNamespace(context={"user_id": None})
    token = set_current_user(_user("cv-user"))
    try:
        assert resolve_runtime_user_id(runtime) == "cv-user"
    finally:
        reset_current_user(token)


# ---------------------------------------------------------------------------
# AUTO sentinel + resolve_user_id（三态）
# ---------------------------------------------------------------------------


def test_auto_sentinel_is_singleton():
    """_AutoSentinel.__new__ 保证单例；repr 是 <AUTO>。"""
    from deerflow.runtime.user_context import _AutoSentinel

    assert _AutoSentinel() is _AutoSentinel()
    assert _AutoSentinel() is AUTO
    assert repr(AUTO) == "<AUTO>"


def test_resolve_user_id_auto_with_user():
    token = set_current_user(_user("u-1"))
    try:
        assert resolve_user_id(AUTO) == "u-1"
    finally:
        reset_current_user(token)


@pytest.mark.no_auto_user
def test_resolve_user_id_auto_without_user_raises():
    with pytest.raises(RuntimeError, match="user_id=AUTO"):
        resolve_user_id(AUTO, method_name="some_repo_method")


def test_resolve_user_id_explicit_string_overrides_contextvar():
    """显式 str 覆盖 contextvar（测试 / 管理覆盖场景）。"""
    token = set_current_user(_user("cv-user"))
    try:
        assert resolve_user_id("explicit") == "explicit"
    finally:
        reset_current_user(token)


def test_resolve_user_id_none_returns_none():
    """显式 None → 返回 None（迁移 / CLI 绕过隔离场景）。"""
    token = set_current_user(_user("cv-user"))
    try:
        assert resolve_user_id(None) is None
    finally:
        reset_current_user(token)


def test_resolve_user_id_auto_coerces_uuid():
    uid = UUID("12345678-1234-5678-1234-567812345678")
    token = set_current_user(_user(uid))
    try:
        assert resolve_user_id(AUTO) == str(uid)
    finally:
        reset_current_user(token)
