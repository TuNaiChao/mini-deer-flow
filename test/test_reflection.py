"""反射解析器测试（Phase 0 重审对齐上游错误处理）。

全部 hermetic：不依赖任何真实 provider 包。
- 成功路径用标准库模块（``json``）验证动态加载；
- 缺包 / 畸形路径 / 属性缺失路径用 monkeypatch 注入伪造的 ``importlib``，验证可操作的安装提示。

错误处理契约（对齐上游 ``test_reflection_resolvers``）：三种失败（路径畸形 / 模块缺失 /
属性不存在）全部归一成 :class:`ImportError` 并附 ``uv add`` 提示；类型校验失败raise
:class:`ValueError`。
"""

from __future__ import annotations

import json
import types
from types import SimpleNamespace

import pytest

from deerflow.reflection import resolver
from deerflow.reflection.resolver import resolve_class, resolve_variable

# ---------------------------------------------------------------------------
# resolve_variable 成功路径（标准库，零外部依赖）
# ---------------------------------------------------------------------------


def test_resolve_variable_loads_attribute_from_stdlib():
    """能从 'module:attr' 加载模块属性。"""
    assert resolve_variable("json:loads") is json.loads


def test_resolve_variable_loads_class():
    """能加载类对象。"""
    assert resolve_variable("json:JSONDecoder") is json.JSONDecoder


def test_resolve_variable_expected_type_passes_for_instance():
    """expected_type 对实例用 isinstance 校验，匹配则通过。"""
    assert resolve_variable("json:loads", expected_type=types.FunctionType) is json.loads


def test_resolve_variable_expected_type_accepts_tuple():
    """expected_type 支持元组——满足其中任一即可（对齐上游）。"""
    assert resolve_variable("json:loads", expected_type=(int, types.FunctionType)) is json.loads


# ---------------------------------------------------------------------------
# resolve_variable 错误路径：路径格式
# ---------------------------------------------------------------------------


def test_resolve_variable_invalid_path_format_raises_import_error():
    """路径不含 ':' → ImportError 带格式示例（不再裸 ValueError）。"""
    with pytest.raises(ImportError) as exc_info:
        resolve_variable("invalid.variable.path")
    msg = str(exc_info.value)
    assert "不像变量路径" in msg or "variable path" in msg.lower()
    assert ":" in msg  # 示例里含 ':' 分隔符


# ---------------------------------------------------------------------------
# resolve_variable 错误路径：缺包
# ---------------------------------------------------------------------------


def _stub_importlib_with(monkeypatch, exc: BaseException) -> None:
    """把 resolver 模块的 importlib 引用替换为抛 exc 的桩，模拟缺包。"""

    def fake_import_module(_module_path: str):
        raise exc

    monkeypatch.setattr(resolver, "importlib", SimpleNamespace(import_module=fake_import_module))


def test_resolve_variable_missing_known_provider_reports_install_hint(monkeypatch):
    """缺已知 provider 包 → ImportError 带可操作安装提示。"""
    _stub_importlib_with(
        monkeypatch,
        ModuleNotFoundError("No module named 'langchain_openai'", name="langchain_openai"),
    )
    with pytest.raises(ImportError) as exc_info:
        resolve_variable("langchain_openai:ChatOpenAI")
    assert "uv add langchain-openai" in str(exc_info.value)


def test_resolve_variable_missing_google_provider_reports_install_hint(monkeypatch):
    """缺 google provider → 提示 langchain-google-genai（对齐上游）。"""
    _stub_importlib_with(
        monkeypatch,
        ModuleNotFoundError("No module named 'langchain_google_genai'", name="langchain_google_genai"),
    )
    with pytest.raises(ImportError) as exc_info:
        resolve_variable("langchain_google_genai:ChatGoogleGenerativeAI")
    assert "uv add langchain-google-genai" in str(exc_info.value)


def test_resolve_variable_missing_transitive_dependency_still_hints_provider(monkeypatch):
    """间接依赖缺失（如 provider 在但 google 缺）→ 仍指向 provider 包（对齐上游）。

    ``_build_missing_dependency_hint`` 用 err.name 定位真正缺的模块，但对已知 provider
    根模块仍优先给 provider 包提示。
    """
    _stub_importlib_with(monkeypatch, ModuleNotFoundError("No module named 'google'", name="google"))
    with pytest.raises(ImportError) as exc_info:
        resolve_variable("langchain_google_genai:ChatGoogleGenerativeAI")
    assert "uv add langchain-google-genai" in str(exc_info.value)


def test_resolve_variable_missing_unknown_module_derives_package_hint(monkeypatch):
    """缺不在 hint 表里的模块 → 用 ``_``→``-`` 推导包名给提示（对齐上游「总给提示」）。"""
    _stub_importlib_with(monkeypatch, ModuleNotFoundError("No module named 'mystery_pkg'", name="mystery_pkg"))
    with pytest.raises(ImportError) as exc_info:
        resolve_variable("mystery_pkg:Thing")
    assert "uv add mystery-pkg" in str(exc_info.value)


# ---------------------------------------------------------------------------
# resolve_variable 错误路径：属性不存在 / 类型不匹配
# ---------------------------------------------------------------------------


def test_resolve_variable_missing_attribute_raises_import_error():
    """模块存在但属性不存在 → 归一成 ImportError（对调用方即「想要的符号拿不到」）。"""
    with pytest.raises(ImportError, match="未定义.*does_not_exist"):
        resolve_variable("json:does_not_exist")


def test_resolve_variable_expected_type_mismatch_raises_value_error():
    """expected_type 与实例不匹配 → ValueError（不再 TypeError）。"""
    with pytest.raises(ValueError, match="不是.*实例"):
        resolve_variable("json:loads", expected_type=int)


# ---------------------------------------------------------------------------
# resolve_class
# ---------------------------------------------------------------------------


def test_resolve_class_loads_class():
    """加载类对象。"""
    assert resolve_class("json:JSONDecoder") is json.JSONDecoder


def test_resolve_class_base_class_validation_passes():
    """是 base_class 的（子）类 → 通过。"""
    assert resolve_class("json:JSONDecoder", base_class=json.JSONDecoder) is json.JSONDecoder


def test_resolve_class_base_class_mismatch_raises_value_error():
    """不是 base_class 子类 → ValueError（不再 TypeError）。"""
    with pytest.raises(ValueError, match="不是.*的子类"):
        resolve_class("json:JSONDecoder", base_class=int)


def test_resolve_class_non_class_raises_value_error():
    """解析得到的不是类（函数/实例）→ ValueError（不再 TypeError）。

    resolve_class 内部用 ``expected_type=type`` 筛掉非类，故函数会在 resolve_variable
    阶段就因 isinstance 校验失败而 ValueError。
    """
    with pytest.raises(ValueError):
        resolve_class("json:loads")
