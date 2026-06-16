"""反射解析器测试。

全部 hermetic：不依赖任何真实 provider 包。
- 成功路径用标准库模块（``json``）验证动态加载；
- 缺包/格式错误路径用 monkeypatch 注入伪造的 ``importlib``，验证可操作的安装提示。
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


def test_resolve_variable_expected_type_mismatch_raises():
    """expected_type 与实例不匹配 → TypeError。"""
    with pytest.raises(TypeError, match="不是"):
        resolve_variable("json:loads", expected_type=int)


# ---------------------------------------------------------------------------
# resolve_variable 错误路径
# ---------------------------------------------------------------------------


def test_resolve_variable_path_without_colon_raises():
    """路径不含 ':' → rsplit 解包失败 → ValueError。"""
    with pytest.raises(ValueError):
        resolve_variable("invalid_no_colon")


def _stub_importlib_with(monkeypatch, exc: BaseException) -> None:
    """把 resolver 模块的 importlib 引用替换为抛 exc 的桩，模拟缺包。"""

    def fake_import_module(_module_path: str):
        raise exc

    monkeypatch.setattr(resolver, "importlib", SimpleNamespace(import_module=fake_import_module))


def test_resolve_variable_missing_known_provider_reports_install_hint(monkeypatch):
    """缺已知 provider 包 → ImportError 带可操作安装提示（对齐 deer test_reflection_resolvers）。"""
    _stub_importlib_with(
        monkeypatch,
        ModuleNotFoundError("No module named 'langchain_openai'", name="langchain_openai"),
    )
    with pytest.raises(ImportError) as exc_info:
        resolve_variable("langchain_openai:ChatOpenAI")
    assert "uv add langchain-openai" in str(exc_info.value)


def test_resolve_variable_missing_unknown_module_reraises_without_hint(monkeypatch):
    """缺不在 hint 表里的模块 → 原样 reraise（不带安装提示）。"""
    _stub_importlib_with(monkeypatch, ModuleNotFoundError("No module named 'mystery_pkg'", name="mystery_pkg"))
    with pytest.raises(ImportError) as exc_info:
        resolve_variable("mystery_pkg:Thing")
    assert "uv add" not in str(exc_info.value)


def test_resolve_variable_missing_attribute_raises_attribute_error():
    """模块存在但属性不存在 → AttributeError。"""
    with pytest.raises(AttributeError):
        resolve_variable("json:does_not_exist")


# ---------------------------------------------------------------------------
# resolve_class
# ---------------------------------------------------------------------------


def test_resolve_class_loads_class():
    """加载类对象。"""
    assert resolve_class("json:JSONDecoder") is json.JSONDecoder


def test_resolve_class_base_class_validation_passes():
    """是 base_class 的（子）类 → 通过。"""
    assert resolve_class("json:JSONDecoder", base_class=json.JSONDecoder) is json.JSONDecoder


def test_resolve_class_base_class_mismatch_raises():
    """不是 base_class 子类 → TypeError。"""
    with pytest.raises(TypeError, match="不是.*的子类"):
        resolve_class("json:JSONDecoder", base_class=int)


def test_resolve_class_non_class_raises():
    """解析得到的不是类（函数/实例）→ TypeError。"""
    with pytest.raises(TypeError, match="不是类"):
        resolve_class("json:loads")
