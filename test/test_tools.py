"""工具系统测试。

hermetic：``get_available_tools(app_config=...)`` 接受显式配置，注入空 tools 的
AppConfig 即可只返回内置工具，不读全局 config.yaml。配置定义的工具加载用
monkeypatch 桩掉 resolve_variable，不引入真实工具包。
"""

from __future__ import annotations

from types import SimpleNamespace

from deerflow.config import AppConfig
from deerflow.tools import get_available_tools
from deerflow.tools import tools as tools_module

BUILTIN_NAMES = {"present_files", "ask_clarification"}


def test_builtin_tools_only_when_no_config_tools():
    """空 tools 配置 → 只返回内置工具。"""
    tools = get_available_tools(app_config=AppConfig(tools=[]))
    names = {t.name for t in tools}
    assert BUILTIN_NAMES <= names
    assert len(tools) == len(BUILTIN_NAMES)


def test_config_defined_tools_appended(monkeypatch):
    """config.tools 里的工具经 resolve_variable 加载后追加到内置工具之后。"""
    fake_tool = SimpleNamespace(name="fake_tool")
    monkeypatch.setattr(tools_module, "resolve_variable", lambda path, expected=None: fake_tool)

    cfg = AppConfig(tools=[{"use": "fake:Tool", "group": "g1"}])
    tools = get_available_tools(app_config=cfg)
    names = {getattr(t, "name", None) for t in tools}
    assert "fake_tool" in names
    # 内置工具仍在
    assert BUILTIN_NAMES <= names


def test_groups_filter_excludes_other_groups(monkeypatch):
    """groups 过滤：只保留匹配分组的配置工具。"""
    kept = SimpleNamespace(name="kept")
    dropped = SimpleNamespace(name="dropped")

    def fake_resolve(path, expected=None):
        return kept if "kept" in path else dropped

    monkeypatch.setattr(tools_module, "resolve_variable", fake_resolve)
    cfg = AppConfig(
        tools=[
            {"use": "fake:kept", "group": "g1"},
            {"use": "fake:dropped", "group": "g2"},
        ]
    )
    tools = get_available_tools(groups=["g1"], app_config=cfg)
    names = {getattr(t, "name", None) for t in tools}
    assert "kept" in names
    assert "dropped" not in names


def test_clarification_tool_returns_placeholder():
    """ask_clarification 占位实现返回字符串（真正中断由 ClarificationMiddleware 处理）。"""
    from deerflow.tools.builtins import ask_clarification_tool

    result = ask_clarification_tool.invoke(
        {
            "question": "需要哪个文件？",
            "clarification_type": "missing_info",
            "context": "未指定文件",
        }
    )
    assert isinstance(result, str)
    assert result  # 非空
