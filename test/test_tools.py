"""工具系统测试（M15 + 原有）。

hermetic：
- ``get_available_tools(app_config=...)`` 接受显式配置，注入空 tools 的 AppConfig 即可只返回内置工具；
  配置定义的工具加载用 monkeypatch 桩掉 resolve_variable。
- MCP / ACP 缺包软加载；tool_search / mcp_metadata / sync 纯逻辑；setup/update_agent / skill_manage
  用 ``DEER_FLOW_HOME``→tmp_path 隔离 + LocalSkillStorage(host_path=tmp) 绕单例。

覆盖（对齐 ALIGNMENT_OUTLINE M15）：
- get_available_tools：内置 only / config 工具 / groups 过滤 / host-bash 过滤 / name-mismatch 告警 /
  dedupe（config>builtins>MCP>ACP）/ view_image 条件 / task 条件 / skill_manage 条件 / MCP 软加载→[] / ACP 条件
- mcp_metadata：tag_mcp_tool / is_mcp_tool / key 常量
- sync：_get_runnable_config_param（有/无 config 参数 / partial / 无注解）+ config 注入转发
- tool_search：catalog search（select/keyword/+token/空/非法正则降级）+ hash 稳定/变 +
  assemble fail-closed + prompt section + build_deferred_tool_setup 空分支
- view_image helpers：路径白名单 / 魔数检测
- setup_agent：写 SOUL.md+config.yaml / 空 soul 拒绝 / per-user 目录 / 默认 agent 写 base_dir
- update_agent：部分更新 / 原子 / 未知 model / 无 agent_name / 不存在 / nullish 归一
- skill_manage：create/patch/edit/delete/write_file/remove_file + 安全扫描 block + per-skill 锁
- invoke_acp_agent：build 描述列 agent / soft-load 缺包→安装提示 / 未知 agent
- task_tool：未知子代理类型 → 错误 / bash 无 host-bash → 禁用消息
- ask_clarification 占位
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain.tools import tool
from langchain_core.runnables import RunnableConfig

from deerflow.config import AppConfig
from deerflow.config.model_config import ModelConfig
from deerflow.config.skill_evolution_config import SkillEvolutionConfig
from deerflow.tools import get_available_tools
from deerflow.tools import tools as tools_module

BUILTIN_NAMES = {"present_files", "ask_clarification"}


# ===========================================================================
# 原有：get_available_tools 基础
# ===========================================================================


def test_builtin_tools_only_when_no_config_tools():
    """空 tools 配置 → 只返回内置工具。"""
    tools = get_available_tools(app_config=AppConfig(tools=[]))
    names = {t.name for t in tools}
    assert BUILTIN_NAMES <= names
    assert len(tools) == len(BUILTIN_NAMES)


def test_config_defined_tools_appended(monkeypatch):
    """config.tools 里的工具经 resolve_variable 加载后追加。"""
    fake_tool = SimpleNamespace(name="fake_tool")
    monkeypatch.setattr(tools_module, "resolve_variable", lambda path, expected=None: fake_tool)

    cfg = AppConfig(tools=[{"use": "fake:Tool", "group": "g1"}])
    tools = get_available_tools(app_config=cfg)
    names = {getattr(t, "name", None) for t in tools}
    assert "fake_tool" in names
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


# ===========================================================================
# fake 工具 helper
# ===========================================================================


def _fake_tool(name: str, description: str = "desc", *, coroutine=None):
    """造一个真 BaseTool（带 .name/.func/.coroutine/.metadata）。"""

    @tool
    def _t(query: str) -> str:
        """Fake tool for testing."""
        return f"{name}:{query}"

    _t.name = name
    _t.description = description
    if coroutine is not None:
        _t.func = None
        _t.coroutine = coroutine
    return _t


def _fake_runtime(*, tool_call_id="tc1", context=None, state=None, config=None):
    """造一个工具用的 runtime 鸭子对象。"""
    return SimpleNamespace(
        tool_call_id=tool_call_id,
        context=context or {"user_id": "test-user"},
        state=state or {},
        config=config or {"configurable": {}, "metadata": {}},
    )


# ===========================================================================
# get_available_tools：去重 / host-bash / name-mismatch / 条件 / MCP / ACP
# ===========================================================================


class TestGetAvailableTools:
    def test_dedupe_config_wins_over_builtin(self, monkeypatch):
        """config 工具与内置工具同名 → config 胜（去重 config>builtins，防 #1803）。"""
        config_tool = _fake_tool("present_files")
        monkeypatch.setattr(tools_module, "resolve_variable", lambda path, expected=None: config_tool)
        monkeypatch.setattr(tools_module, "is_host_bash_allowed", lambda config=None: True)
        cfg = AppConfig(tools=[{"use": "fake:present_files", "group": "g", "name": "present_files"}])
        tools = get_available_tools(app_config=cfg)
        names = [t.name for t in tools]
        assert names.count("present_files") == 1
        assert any(t is config_tool for t in tools)

    def test_host_bash_filtered_when_local_sandbox(self, monkeypatch):
        """is_host_bash_allowed=False → host-bash 工具被过滤。"""
        monkeypatch.setattr(tools_module, "is_host_bash_allowed", lambda config=None: False)
        bash_tool = _fake_tool("bash_tool")
        other = _fake_tool("other_tool")
        mapping = {"deerflow.sandbox.tools:bash_tool": bash_tool, "fake:other": other}
        monkeypatch.setattr(tools_module, "resolve_variable", lambda path, expected=None: mapping[path])
        cfg = AppConfig(
            tools=[
                {"use": "deerflow.sandbox.tools:bash_tool", "group": "bash", "name": "bash_tool"},
                {"use": "fake:other", "group": "g", "name": "other_tool"},
            ]
        )
        tools = get_available_tools(app_config=cfg)
        names = {t.name for t in tools}
        assert "bash_tool" not in names
        assert "other_tool" in names

    def test_host_bash_kept_when_allowed(self, monkeypatch):
        """is_host_bash_allowed=True → host-bash 工具保留。"""
        monkeypatch.setattr(tools_module, "is_host_bash_allowed", lambda config=None: True)
        bash_tool = _fake_tool("bash_tool")
        monkeypatch.setattr(tools_module, "resolve_variable", lambda path, expected=None: bash_tool)
        cfg = AppConfig(tools=[{"use": "deerflow.sandbox.tools:bash_tool", "group": "bash", "name": "bash_tool"}])
        tools = get_available_tools(app_config=cfg)
        assert any(t.name == "bash_tool" for t in tools)

    def test_name_mismatch_warning(self, monkeypatch, caplog):
        """config name ≠ tool .name → 告警（#1803 根因）。"""
        real_tool = _fake_tool("actual_name")
        monkeypatch.setattr(tools_module, "resolve_variable", lambda path, expected=None: real_tool)
        monkeypatch.setattr(tools_module, "is_host_bash_allowed", lambda config=None: True)
        cfg = AppConfig(tools=[{"use": "fake:Tool", "group": "g", "name": "configured_name"}])
        with caplog.at_level("WARNING", logger="deerflow.tools.tools"):
            get_available_tools(app_config=cfg)
        assert any("name mismatch" in rec.message.lower() for rec in caplog.records)

    def test_view_image_added_for_vision_model(self, monkeypatch):
        """supports_vision 模型 → 加 view_image。"""
        monkeypatch.setattr(tools_module, "is_host_bash_allowed", lambda config=None: True)
        cfg = AppConfig(models=[ModelConfig(name="vision-model", use="x", model="gpt-4o", supports_vision=True)])
        tools = get_available_tools(model_name="vision-model", app_config=cfg)
        assert any(t.name == "view_image" for t in tools)

    def test_view_image_not_added_for_non_vision_model(self, monkeypatch):
        """非 vision 模型 → 不加 view_image。"""
        monkeypatch.setattr(tools_module, "is_host_bash_allowed", lambda config=None: True)
        cfg = AppConfig(models=[ModelConfig(name="text-model", use="x", model="gpt-3.5", supports_vision=False)])
        tools = get_available_tools(model_name="text-model", app_config=cfg)
        assert not any(t.name == "view_image" for t in tools)

    def test_task_tool_added_when_subagent_enabled(self, monkeypatch):
        """subagent_enabled=True → 加 task。"""
        monkeypatch.setattr(tools_module, "is_host_bash_allowed", lambda config=None: True)
        tools = get_available_tools(subagent_enabled=True, app_config=AppConfig(tools=[]))
        assert any(t.name == "task" for t in tools)

    def test_task_tool_not_added_by_default(self, monkeypatch):
        """默认 → 不加 task。"""
        monkeypatch.setattr(tools_module, "is_host_bash_allowed", lambda config=None: True)
        tools = get_available_tools(app_config=AppConfig(tools=[]))
        assert not any(t.name == "task" for t in tools)

    def test_skill_manage_added_when_evolution_enabled(self, monkeypatch):
        """skill_evolution.enabled=True → 加 skill_manage。"""
        monkeypatch.setattr(tools_module, "is_host_bash_allowed", lambda config=None: True)
        cfg = AppConfig(skill_evolution=SkillEvolutionConfig(enabled=True))
        tools = get_available_tools(app_config=cfg)
        assert any(t.name == "skill_manage" for t in tools)

    def test_mcp_soft_load_returns_empty_when_no_servers(self, monkeypatch):
        """无 MCP 服务器（或适配器缺包）→ 不加 MCP 工具，内置工具正常。"""
        monkeypatch.setattr(tools_module, "is_host_bash_allowed", lambda config=None: True)
        tools = get_available_tools(app_config=AppConfig(tools=[]))
        names = {t.name for t in tools}
        assert BUILTIN_NAMES <= names

    def test_acp_added_when_agents_configured(self, monkeypatch):
        """acp_agents 配置了 → 加 invoke_acp_agent。"""
        monkeypatch.setattr(tools_module, "is_host_bash_allowed", lambda config=None: True)
        cfg = AppConfig(acp_agents={"codex": {"command": "codex-acp", "description": "Codex"}})
        tools = get_available_tools(app_config=cfg)
        assert any(t.name == "invoke_acp_agent" for t in tools)

    def test_acp_not_added_when_no_agents(self, monkeypatch):
        """无 acp_agents → 不加 invoke_acp_agent。"""
        monkeypatch.setattr(tools_module, "is_host_bash_allowed", lambda config=None: True)
        tools = get_available_tools(app_config=AppConfig(tools=[]))
        assert not any(t.name == "invoke_acp_agent" for t in tools)

    def test_include_mcp_false_skips_mcp(self, monkeypatch):
        """include_mcp=False → 跳过 MCP 分支。"""
        monkeypatch.setattr(tools_module, "is_host_bash_allowed", lambda config=None: True)
        tools = get_available_tools(include_mcp=False, app_config=AppConfig(tools=[]))
        assert BUILTIN_NAMES <= {t.name for t in tools}


# ===========================================================================
# mcp_metadata
# ===========================================================================


class TestMcpMetadata:
    def test_key_constant(self):
        from deerflow.tools.mcp_metadata import MCP_TOOL_METADATA_KEY

        assert MCP_TOOL_METADATA_KEY == "deerflow_mcp"

    def test_tag_and_is_mcp(self):
        from deerflow.tools.mcp_metadata import is_mcp_tool, tag_mcp_tool

        t = _fake_tool("mcp_one")
        assert not is_mcp_tool(t)
        returned = tag_mcp_tool(t)
        assert returned is t
        assert is_mcp_tool(t)

    def test_tag_preserves_existing_metadata(self):
        from deerflow.tools.mcp_metadata import is_mcp_tool, tag_mcp_tool

        t = _fake_tool("mcp_two")
        t.metadata = {"custom": "value"}
        tag_mcp_tool(t)
        assert t.metadata["custom"] == "value"
        assert is_mcp_tool(t)

    def test_is_mcp_tool_no_metadata_attr(self):
        from deerflow.tools.mcp_metadata import is_mcp_tool

        plain = SimpleNamespace()
        assert not is_mcp_tool(plain)


# ===========================================================================
# sync._get_runnable_config_param + config 注入
# ===========================================================================


class TestSyncConfigInjection:
    def test_detects_runnable_config_param(self):

        from deerflow.tools.sync import _get_runnable_config_param

        async def fn(query: str, config: RunnableConfig) -> str:
            return query

        assert _get_runnable_config_param(fn) == "config"

    def test_no_config_param_returns_none(self):
        from deerflow.tools.sync import _get_runnable_config_param

        async def fn(query: str) -> str:
            return query

        assert _get_runnable_config_param(fn) is None

    def test_unwraps_functools_partial(self):
        import functools

        from deerflow.tools.sync import _get_runnable_config_param

        async def fn(query: str, run_config: RunnableConfig) -> str:
            return query

        partial = functools.partial(fn)
        assert _get_runnable_config_param(partial) == "run_config"

    def test_wrapper_without_config_runs_coroutine(self):
        from deerflow.tools.sync import make_sync_tool_wrapper

        async def coro(x: int) -> int:
            return x * 2

        wrapper = make_sync_tool_wrapper(coro, "t")
        assert wrapper(3) == 6

    def test_wrapper_with_config_forwards(self):

        from deerflow.tools.sync import make_sync_tool_wrapper

        captured: dict = {}

        async def coro(query: str, config: RunnableConfig) -> str:
            captured["config"] = config
            return f"got {query}"

        wrapper = make_sync_tool_wrapper(coro, "t")
        result = wrapper("hi", config={"configurable": {"thread_id": "t1"}})
        assert result == "got hi"
        assert captured["config"] == {"configurable": {"thread_id": "t1"}}


# ===========================================================================
# tool_search
# ===========================================================================


def _make_catalog_tools(*names_mcp: str):
    """造一组已标记 MCP 的工具。"""
    from deerflow.tools.mcp_metadata import tag_mcp_tool

    tools = []
    for n in names_mcp:
        t = _fake_tool(n, description=f"tool {n}")
        tag_mcp_tool(t)
        tools.append(t)
    return tools


class TestDeferredToolCatalog:
    def test_search_select_exact_names(self):
        from deerflow.tools.builtins.tool_search import DeferredToolCatalog

        tools = _make_catalog_tools("read_file", "write_file", "list_dir")
        cat = DeferredToolCatalog(tuple(tools))
        matched = cat.search("select:read_file,list_dir")
        assert {t.name for t in matched} == {"read_file", "list_dir"}

    def test_search_keyword(self):
        from deerflow.tools.builtins.tool_search import DeferredToolCatalog

        tools = _make_catalog_tools("notebook_exec", "file_read")
        cat = DeferredToolCatalog(tuple(tools))
        matched = cat.search("notebook")
        assert [t.name for t in matched] == ["notebook_exec"]

    def test_search_keyword_matches_description(self):
        from deerflow.tools.builtins.tool_search import DeferredToolCatalog
        from deerflow.tools.mcp_metadata import tag_mcp_tool

        tools = _make_catalog_tools("t1", "t2")
        tools[1].description = "run jupyter notebook cells"
        tag_mcp_tool(tools[1])
        cat = DeferredToolCatalog(tuple(tools))
        matched = cat.search("jupyter")
        assert [t.name for t in matched] == ["t2"]

    def test_search_require_token(self):
        from deerflow.tools.builtins.tool_search import DeferredToolCatalog

        tools = _make_catalog_tools("slack_send", "slack_read", "email_send")
        cat = DeferredToolCatalog(tuple(tools))
        matched = cat.search("+slack send")
        names = {t.name for t in matched}
        assert "slack_send" in names
        assert "email_send" not in names

    def test_search_bare_plus_returns_empty(self):
        from deerflow.tools.builtins.tool_search import DeferredToolCatalog

        cat = DeferredToolCatalog(tuple(_make_catalog_tools("a", "b")))
        assert cat.search("+") == []

    def test_search_empty_query_returns_empty(self):
        from deerflow.tools.builtins.tool_search import DeferredToolCatalog

        cat = DeferredToolCatalog(tuple(_make_catalog_tools("a")))
        assert cat.search("") == []
        assert cat.search("   ") == []

    def test_search_invalid_regex_falls_back_to_literal(self):
        from deerflow.tools.builtins.tool_search import DeferredToolCatalog

        tools = _make_catalog_tools("read(file")
        cat = DeferredToolCatalog(tuple(tools))
        matched = cat.search("read(file")
        assert [t.name for t in matched] == ["read(file"]

    def test_max_results_is_5(self):
        from deerflow.tools.builtins.tool_search import MAX_RESULTS

        assert MAX_RESULTS == 5

    def test_hash_stable_for_same_tools(self):
        from deerflow.tools.builtins.tool_search import DeferredToolCatalog

        tools = _make_catalog_tools("a", "b", "c")
        cat1 = DeferredToolCatalog(tuple(tools))
        cat2 = DeferredToolCatalog(tuple(tools))
        assert cat1.hash == cat2.hash

    def test_hash_changes_when_tools_differ(self):
        from deerflow.tools.builtins.tool_search import DeferredToolCatalog

        cat1 = DeferredToolCatalog(tuple(_make_catalog_tools("a", "b")))
        cat2 = DeferredToolCatalog(tuple(_make_catalog_tools("a", "c")))
        assert cat1.hash != cat2.hash

    def test_names_cached(self):
        from deerflow.tools.builtins.tool_search import DeferredToolCatalog

        cat = DeferredToolCatalog(tuple(_make_catalog_tools("x", "y")))
        assert cat.names == frozenset({"x", "y"})


class TestAssembleDeferredTools:
    def test_disabled_returns_empty_no_deferral(self):
        from deerflow.tools.builtins.tool_search import assemble_deferred_tools

        tools = _make_catalog_tools("a")
        final, setup = assemble_deferred_tools(list(tools), enabled=False)
        assert setup.tool_search_tool is None
        assert setup.deferred_names == frozenset()
        assert setup.catalog_hash is None
        assert len(final) == 1

    def test_enabled_no_mcp_returns_empty(self):
        from deerflow.tools.builtins.tool_search import assemble_deferred_tools

        plain = _fake_tool("plain")
        final, setup = assemble_deferred_tools([plain], enabled=True)
        assert setup.tool_search_tool is None

    def test_enabled_with_mcp_defers_and_adds_tool_search(self):
        from deerflow.tools.builtins.tool_search import assemble_deferred_tools
        from deerflow.tools.mcp_metadata import is_mcp_tool

        tools = _make_catalog_tools("mcp_a", "mcp_b")
        final, setup = assemble_deferred_tools(list(tools), enabled=True)
        assert setup.tool_search_tool is not None
        assert setup.deferred_names == frozenset({"mcp_a", "mcp_b"})
        assert setup.catalog_hash is not None
        assert any(t.name == "tool_search" for t in final)
        assert any(is_mcp_tool(t) for t in final)

    def test_fail_closed_when_enabled_but_no_deferred_recovered(self, monkeypatch):
        """fail-closed：启用 + 有 MCP 工具 + 但没恢复出延迟集合 → 抛错。"""
        from deerflow.tools.builtins import tool_search as ts_mod

        tools = _make_catalog_tools("mcp_a")
        monkeypatch.setattr(ts_mod, "build_deferred_tool_setup", lambda filtered, *, enabled: ts_mod.DeferredToolSetup(None, frozenset(), None))
        with pytest.raises(RuntimeError, match="fail-closed"):
            ts_mod.assemble_deferred_tools(list(tools), enabled=True)


class TestDeferredPromptSection:
    def test_empty_returns_empty_string(self):
        from deerflow.tools.builtins.tool_search import get_deferred_tools_prompt_section

        assert get_deferred_tools_prompt_section() == ""
        assert get_deferred_tools_prompt_section(deferred_names=frozenset()) == ""

    def test_lists_sorted_names(self):
        from deerflow.tools.builtins.tool_search import get_deferred_tools_prompt_section

        section = get_deferred_tools_prompt_section(deferred_names=frozenset({"b_tool", "a_tool"}))
        assert section.startswith("<available-deferred-tools>")
        assert section.endswith("</available-deferred-tools>")
        assert section.index("a_tool") < section.index("b_tool")


# ===========================================================================
# view_image helpers
# ===========================================================================


class TestViewImageHelpers:
    def test_allowed_image_virtual_paths(self):
        from deerflow.tools.builtins.view_image_tool import _is_allowed_image_virtual_path

        assert _is_allowed_image_virtual_path("/mnt/user-data/workspace/x.png")
        assert _is_allowed_image_virtual_path("/mnt/user-data/uploads/sub/y.jpg")
        assert _is_allowed_image_virtual_path("/mnt/user-data/outputs/z.webp")

    def test_disallowed_image_paths(self):
        from deerflow.tools.builtins.view_image_tool import _is_allowed_image_virtual_path

        assert not _is_allowed_image_virtual_path("/etc/passwd")
        assert not _is_allowed_image_virtual_path("/mnt/skills/x.png")
        assert not _is_allowed_image_virtual_path("/mnt/user-data/x.png")

    @pytest.mark.parametrize(
        "data,expected",
        [
            (b"\xff\xd8\xff\xe8...", "image/jpeg"),
            (b"\x89PNG\r\n\x1a\n...", "image/png"),
            (b"RIFF\x00\x00\x00\x00WEBP...", "image/webp"),
            (b"plain text not image", None),
        ],
    )
    def test_detect_image_mime(self, data, expected):
        from deerflow.tools.builtins.view_image_tool import _detect_image_mime

        assert _detect_image_mime(data) == expected


# ===========================================================================
# present_files（M15：多文件 + 路径归一化 + 穿越校验）
# ===========================================================================


class TestPresentFileTool:
    """present_files 工具——对齐上游重写后的多文件 + 路径校验逻辑。"""

    @pytest.fixture(autouse=True)
    def _paths_on_tmp(self, tmp_path, monkeypatch):
        """把 present_file_tool 用的 get_paths 钉到 tmp_path，让 resolve_virtual_path
        解析出的物理路径与 thread_data.outputs_path 同根（生产里两者同源）。"""
        import sys

        from deerflow.config.paths import Paths
        from deerflow.runtime.user_context import get_effective_user_id

        # 必须用 sys.modules 取真模块：builtins/__init__ 把 present_file_tool
        # （StructuredTool）重新导出，遮蔽了同名子模块，dotted import 会拿到工具对象。
        pft_mod = sys.modules["deerflow.tools.builtins.present_file_tool"]
        monkeypatch.setattr(pft_mod, "get_paths", lambda: Paths(base_dir=tmp_path))
        self._tmp = tmp_path
        self._user_id = get_effective_user_id()

    def _outputs_dir(self, thread_id="t1"):
        return self._tmp / "users" / self._user_id / "threads" / thread_id / "user-data" / "outputs"

    def _runtime(self, *, thread_id="t1"):
        outputs = self._outputs_dir(thread_id)
        outputs.mkdir(parents=True, exist_ok=True)
        return SimpleNamespace(
            context={"thread_id": thread_id},
            state={"thread_data": {"outputs_path": str(outputs)}},
            config={"configurable": {"thread_id": thread_id}},
        )

    def test_normalize_virtual_path_under_outputs(self):
        from deerflow.tools.builtins.present_file_tool import (
            OUTPUTS_VIRTUAL_PREFIX,
            _normalize_presented_filepath,
        )

        outputs = self._outputs_dir()
        outputs.mkdir(parents=True, exist_ok=True)
        (outputs / "report.md").write_text("x")
        rt = self._runtime()
        normalized = _normalize_presented_filepath(rt, "/mnt/user-data/outputs/report.md")
        assert normalized == f"{OUTPUTS_VIRTUAL_PREFIX}/report.md"

    def test_normalize_host_path_under_outputs(self):
        """宿主侧绝对路径（非 /mnt/user-data 前缀）也能归一化，只要落在 outputs 下。"""
        from deerflow.tools.builtins.present_file_tool import (
            OUTPUTS_VIRTUAL_PREFIX,
            _normalize_presented_filepath,
        )

        outputs = self._outputs_dir()
        outputs.mkdir(parents=True, exist_ok=True)
        (outputs / "chart.png").write_text("x")
        rt = self._runtime()
        normalized = _normalize_presented_filepath(rt, str(outputs / "chart.png"))
        assert normalized == f"{OUTPUTS_VIRTUAL_PREFIX}/chart.png"

    def test_normalize_rejects_traversal(self):
        """/mnt/user-data/outputs/../../etc/passwd 经 resolve_virtual_path 挡穿越。"""
        from deerflow.tools.builtins.present_file_tool import _normalize_presented_filepath

        rt = self._runtime()
        with pytest.raises(ValueError):
            _normalize_presented_filepath(rt, "/mnt/user-data/outputs/../../etc/passwd")

    def test_normalize_rejects_path_outside_outputs(self):
        """合法虚拟前缀但解析后落在 outputs 之外（workspace）→ ValueError。"""
        from deerflow.tools.builtins.present_file_tool import _normalize_presented_filepath

        rt = self._runtime()
        with pytest.raises(ValueError):
            _normalize_presented_filepath(rt, "/mnt/user-data/workspace/secret.md")

    def test_normalize_errors_when_state_missing(self):
        from deerflow.tools.builtins.present_file_tool import _normalize_presented_filepath

        rt = SimpleNamespace(context={"thread_id": "t1"}, state=None, config={"configurable": {}})
        with pytest.raises(ValueError, match="state"):
            _normalize_presented_filepath(rt, "/mnt/user-data/outputs/x.md")

    def test_normalize_errors_when_outputs_path_missing(self):
        from deerflow.tools.builtins.present_file_tool import _normalize_presented_filepath

        rt = SimpleNamespace(
            context={"thread_id": "t1"},
            state={"thread_data": {}},  # 无 outputs_path
            config={"configurable": {}},
        )
        with pytest.raises(ValueError, match="outputs path"):
            _normalize_presented_filepath(rt, "/mnt/user-data/outputs/x.md")

    def test_get_thread_id_fallback_chain(self):
        from deerflow.tools.builtins.present_file_tool import _get_thread_id

        # 1) runtime.context 命中
        assert _get_thread_id(SimpleNamespace(context={"thread_id": "c1"}, config={})) == "c1"
        # 2) context 无 → runtime.config 命中
        assert _get_thread_id(SimpleNamespace(context=None, config={"configurable": {"thread_id": "c2"}})) == "c2"

    def test_tool_multi_file_success_returns_command(self):
        from langgraph.types import Command

        from deerflow.tools.builtins.present_file_tool import OUTPUTS_VIRTUAL_PREFIX, present_file_tool

        outputs = self._outputs_dir()
        outputs.mkdir(parents=True, exist_ok=True)
        (outputs / "a.md").write_text("a")
        (outputs / "b.md").write_text("b")
        rt = self._runtime()
        # 直接调底层函数（绕过 LangChain 注入）：runtime + filepaths + tool_call_id。
        result = present_file_tool.func(rt, ["/mnt/user-data/outputs/a.md", "/mnt/user-data/outputs/b.md"], "call-1")
        assert isinstance(result, Command)
        assert result.update["artifacts"] == [
            f"{OUTPUTS_VIRTUAL_PREFIX}/a.md",
            f"{OUTPUTS_VIRTUAL_PREFIX}/b.md",
        ]
        # 成功也回一条 ToolMessage
        msgs = result.update["messages"]
        assert msgs and msgs[0].tool_call_id == "call-1"

    def test_tool_bad_path_returns_error_toolmessage(self):
        """路径不合法 → 不抛、不写 artifacts，只回错误 ToolMessage。"""
        from langgraph.types import Command

        from deerflow.tools.builtins.present_file_tool import present_file_tool

        rt = self._runtime()
        result = present_file_tool.func(rt, ["/etc/passwd"], "call-2")
        assert isinstance(result, Command)
        assert result.update.get("artifacts") is None  # 不写 artifacts
        msg = result.update["messages"][0]
        assert msg.tool_call_id == "call-2"
        assert "Error" in msg.content


class TestResolveVirtualPath:
    """Paths.resolve_virtual_path（M15 新增）—— 虚拟→物理 + 穿越校验。"""

    def _paths(self, tmp_path):
        from deerflow.config.paths import Paths

        return Paths(base_dir=tmp_path)

    def test_legit_virtual_resolves_under_user_data(self, tmp_path):
        p = self._paths(tmp_path)
        phys = p.resolve_virtual_path("t1", "/mnt/user-data/outputs/r.md", user_id="u1")
        assert phys == (tmp_path / "users" / "u1" / "threads" / "t1" / "user-data" / "outputs" / "r.md").resolve()

    @pytest.mark.parametrize(
        "bad",
        [
            "/mnt/user-data/outputs/../../etc/passwd",  # 穿越
            "/mnt/user-dataX/foo",  # 前缀混淆（段边界）
            "/etc/passwd",  # 完全无关前缀
        ],
    )
    def test_rejects_bad_paths(self, tmp_path, bad):
        p = self._paths(tmp_path)
        with pytest.raises(ValueError):
            p.resolve_virtual_path("t1", bad, user_id="u1")


# ===========================================================================
# sync 包装：functools.partial 绑定参数保留（M15 对齐上游修正）
# ===========================================================================


class TestSyncWrapperPartial:
    def test_preserves_partial_bound_args(self):
        """make_sync_tool_wrapper 直接调 coro(*args)，故 partial 已绑定参数被保留。

        旧版 ``inner = coro.func`` 会丢绑定参数；本测试锁住修正。"""
        import functools

        from deerflow.tools.sync import make_sync_tool_wrapper

        captured = {}

        async def real_impl(a, bound, config=None):
            captured["args"] = (a, bound, config)
            return "ok"

        partial_coro = functools.partial(real_impl, bound="BOUND")
        wrapper = make_sync_tool_wrapper(partial_coro, "test_tool")
        result = wrapper("A")
        assert result == "ok"
        # bound 参数必须保留（旧 bug 会丢、且大概率 TypeError）
        assert captured["args"] == ("A", "BOUND", None)


# ===========================================================================
# setup_agent
# ===========================================================================


class TestSetupAgent:
    def test_creates_custom_agent_writes_files(self, monkeypatch, tmp_path):
        from deerflow.tools.builtins.setup_agent_tool import setup_agent

        monkeypatch.setenv("DEER_FLOW_HOME", str(tmp_path))
        runtime = _fake_runtime(context={"agent_name": "my-agent", "user_id": "test-user"})

        # 直接调 .func 绕过 args_schema 对 Runtime 的严格校验（SimpleNamespace 鸭子访问对函数体够用）
        result = setup_agent.func(
            soul="# My Agent\nYou are helpful.",
            description="A test agent",
            skills=["bash"],
            runtime=runtime,
        )
        assert result.update["created_agent_name"] == "my-agent"
        agent_dir = tmp_path / "users" / "test-user" / "agents" / "my-agent"
        assert (agent_dir / "SOUL.md").read_text() == "# My Agent\nYou are helpful."
        config_text = (agent_dir / "config.yaml").read_text()
        assert "my-agent" in config_text and "bash" in config_text

    def test_empty_soul_rejected(self, monkeypatch, tmp_path):
        from deerflow.tools.builtins.setup_agent_tool import setup_agent

        monkeypatch.setenv("DEER_FLOW_HOME", str(tmp_path))
        runtime = _fake_runtime(context={"agent_name": "x", "user_id": "u"})
        result = setup_agent.func(soul="   ", description="d", runtime=runtime)
        msg = result.update["messages"][0].content
        assert "empty" in msg.lower()

    def test_default_agent_writes_to_base_dir(self, monkeypatch, tmp_path):
        from deerflow.tools.builtins.setup_agent_tool import setup_agent

        monkeypatch.setenv("DEER_FLOW_HOME", str(tmp_path))
        runtime = _fake_runtime(context={"user_id": "u"})  # 无 agent_name
        setup_agent.func(soul="# Default", description="d", runtime=runtime)
        assert (tmp_path / "SOUL.md").exists()


# ===========================================================================
# update_agent
# ===========================================================================


class TestUpdateAgent:
    def _make_agent(self, tmp_path, name="my-agent", user_id="test-user"):
        """在 tmp 里造一个已存在的自定义 agent（per-user）。"""
        import yaml

        agent_dir = tmp_path / "users" / user_id / "agents" / name
        agent_dir.mkdir(parents=True, exist_ok=True)
        (agent_dir / "SOUL.md").write_text("# Old soul\n", encoding="utf-8")
        cfg = {"name": name, "description": "old"}
        (agent_dir / "config.yaml").write_text(yaml.dump(cfg, allow_unicode=True), encoding="utf-8")
        return agent_dir

    def test_no_fields_provided_errors(self, monkeypatch, tmp_path):
        from deerflow.tools.builtins.update_agent_tool import update_agent

        monkeypatch.setenv("DEER_FLOW_HOME", str(tmp_path))
        self._make_agent(tmp_path)
        runtime = _fake_runtime(context={"agent_name": "my-agent", "user_id": "test-user"})
        result = update_agent.func(runtime=runtime)
        assert "No fields provided" in result.update["messages"][0].content

    def test_no_agent_name_errors(self, monkeypatch, tmp_path):
        from deerflow.tools.builtins.update_agent_tool import update_agent

        monkeypatch.setenv("DEER_FLOW_HOME", str(tmp_path))
        runtime = _fake_runtime(context={"user_id": "u"})
        result = update_agent.func(runtime=runtime, description="x")
        assert "only available inside a custom agent" in result.update["messages"][0].content

    def test_unknown_model_rejected(self, monkeypatch, tmp_path):
        from deerflow.tools.builtins.update_agent_tool import update_agent

        monkeypatch.setenv("DEER_FLOW_HOME", str(tmp_path))
        self._make_agent(tmp_path)
        runtime = _fake_runtime(context={"agent_name": "my-agent", "user_id": "test-user"})
        result = update_agent.func(runtime=runtime, model="nonexistent-model")
        assert "Unknown model" in result.update["messages"][0].content

    def test_partial_update_soul_only(self, monkeypatch, tmp_path):
        from deerflow.tools.builtins.update_agent_tool import update_agent

        monkeypatch.setenv("DEER_FLOW_HOME", str(tmp_path))
        agent_dir = self._make_agent(tmp_path)
        runtime = _fake_runtime(context={"agent_name": "my-agent", "user_id": "test-user"})
        result = update_agent.func(runtime=runtime, soul="# Brand new soul\n")
        assert "updated successfully" in result.update["messages"][0].content.lower()
        assert (agent_dir / "SOUL.md").read_text() == "# Brand new soul\n"
        import yaml

        cfg = yaml.safe_load((agent_dir / "config.yaml").read_text())
        assert cfg["description"] == "old"  # 未传 description → 保持

    def test_update_description_and_model(self, monkeypatch, tmp_path):
        import yaml

        from deerflow.tools.builtins.update_agent_tool import update_agent

        monkeypatch.setenv("DEER_FLOW_HOME", str(tmp_path))
        agent_dir = self._make_agent(tmp_path)
        runtime = _fake_runtime(context={"agent_name": "my-agent", "user_id": "test-user"})
        monkeypatch.setattr("deerflow.tools.builtins.update_agent_tool.get_app_config", lambda: AppConfig(models=[ModelConfig(name="gpt-4o", use="x", model="gpt-4o")]))
        result = update_agent.func(runtime=runtime, description="new desc", model="gpt-4o")
        assert "updated successfully" in result.update["messages"][0].content.lower()
        cfg = yaml.safe_load((agent_dir / "config.yaml").read_text())
        assert cfg["description"] == "new desc"
        assert cfg["model"] == "gpt-4o"

    def test_nullish_string_normalized(self):
        from deerflow.tools.builtins.update_agent_tool import _normalize_nullish_string

        assert _normalize_nullish_string("null") is None
        assert _normalize_nullish_string("none") is None
        assert _normalize_nullish_string("undefined") is None
        assert _normalize_nullish_string("real value") == "real value"
        assert _normalize_nullish_string(None) is None

    def test_agent_not_found_errors(self, monkeypatch, tmp_path):
        from deerflow.tools.builtins.update_agent_tool import update_agent

        monkeypatch.setenv("DEER_FLOW_HOME", str(tmp_path))
        runtime = _fake_runtime(context={"agent_name": "ghost", "user_id": "u"})
        result = update_agent.func(runtime=runtime, description="x")
        assert "does not exist" in result.update["messages"][0].content


# ===========================================================================
# skill_manage
# ===========================================================================


class TestSkillManage:
    @pytest.fixture(autouse=True)
    def _isolated_storage(self, tmp_path, monkeypatch):
        """隔离技能存储：LocalSkillStorage(host_path=tmp) + 扫描器 allow + 缓存刷新 no-op。"""
        from deerflow.skills.security_scanner import ScanResult
        from deerflow.skills.storage import LocalSkillStorage, reset_skill_storage
        from deerflow.tools import skill_manage_tool as sm_mod

        reset_skill_storage()
        storage = LocalSkillStorage(host_path=str(tmp_path))
        monkeypatch.setattr(sm_mod, "get_or_new_skill_storage", lambda *a, **kw: storage)

        async def _allow_scan(content, *, executable=False, location="SKILL.md", app_config=None):
            return ScanResult(decision="allow", reason="test allow")

        monkeypatch.setattr(sm_mod, "scan_skill_content", _allow_scan)

        async def _noop_refresh():
            return None

        monkeypatch.setattr(sm_mod, "refresh_skills_system_prompt_cache_async", _noop_refresh)
        self._storage = storage
        yield storage
        reset_skill_storage()

    def _runtime(self):
        return _fake_runtime(context={"user_id": "u", "thread_id": "t1"})

    @pytest.mark.asyncio
    async def test_create_skill(self):
        from deerflow.tools.skill_manage_tool import _skill_manage_impl

        result = await _skill_manage_impl(self._runtime(), action="create", name="my-skill", content="---\nname: my-skill\ndescription: d\n---\n\nbody")
        assert "Created" in result
        # LocalSkillStorage 历史在 <root>/custom/.history/<name>.jsonl（非 per-skill 子目录）
        history_file = self._storage._host_root / "custom" / ".history" / "my-skill.jsonl"
        assert history_file.exists()
        # SKILL.md 写入
        assert self._storage.get_custom_skill_file("my-skill").exists()

    @pytest.mark.asyncio
    async def test_create_duplicate_errors(self):
        from deerflow.tools.skill_manage_tool import _skill_manage_impl

        await _skill_manage_impl(self._runtime(), action="create", name="dup", content="---\nname: dup\ndescription: d\n---\n\nx")
        with pytest.raises(ValueError, match="already exists"):
            await _skill_manage_impl(self._runtime(), action="create", name="dup", content="---\nname: dup\ndescription: d\n---\n\nx")

    @pytest.mark.asyncio
    async def test_patch_skill(self):
        from deerflow.tools.skill_manage_tool import _skill_manage_impl

        await _skill_manage_impl(self._runtime(), action="create", name="patchme", content="---\nname: patchme\ndescription: d\n---\n\nhello world")
        result = await _skill_manage_impl(self._runtime(), action="patch", name="patchme", find="hello", replace="goodbye")
        assert "1 replacement" in result
        content = self._storage.get_custom_skill_file("patchme").read_text()
        assert "goodbye world" in content

    @pytest.mark.asyncio
    async def test_patch_target_not_found(self):
        from deerflow.tools.skill_manage_tool import _skill_manage_impl

        await _skill_manage_impl(self._runtime(), action="create", name="p2", content="---\nname: p2\ndescription: d\n---\n\nfoo")
        with pytest.raises(ValueError, match="not found"):
            await _skill_manage_impl(self._runtime(), action="patch", name="p2", find="nonexistent", replace="x")

    @pytest.mark.asyncio
    async def test_edit_skill(self):
        from deerflow.tools.skill_manage_tool import _skill_manage_impl

        await _skill_manage_impl(self._runtime(), action="create", name="editme", content="---\nname: editme\ndescription: d\n---\n\nold")
        result = await _skill_manage_impl(self._runtime(), action="edit", name="editme", content="---\nname: editme\ndescription: d\n---\n\nnew")
        assert "Updated" in result
        assert "new" in self._storage.get_custom_skill_file("editme").read_text()

    @pytest.mark.asyncio
    async def test_delete_skill(self):
        from deerflow.tools.skill_manage_tool import _skill_manage_impl

        await _skill_manage_impl(self._runtime(), action="create", name="delme", content="---\nname: delme\ndescription: d\n---\n\nx")
        result = await _skill_manage_impl(self._runtime(), action="delete", name="delme")
        assert "Deleted" in result
        assert not self._storage.get_custom_skill_file("delme").exists()

    @pytest.mark.asyncio
    async def test_write_and_remove_file(self):
        from deerflow.tools.skill_manage_tool import _skill_manage_impl

        await _skill_manage_impl(self._runtime(), action="create", name="filedemo", content="---\nname: filedemo\ndescription: d\n---\n\nx")
        result = await _skill_manage_impl(self._runtime(), action="write_file", name="filedemo", path="scripts/run.sh", content="#!/bin/bash\necho hi")
        assert "Wrote" in result
        target = self._storage.get_custom_skill_file("filedemo").parent / "scripts" / "run.sh"
        assert target.exists()
        result = await _skill_manage_impl(self._runtime(), action="remove_file", name="filedemo", path="scripts/run.sh")
        assert "Removed" in result
        assert not target.exists()

    @pytest.mark.asyncio
    async def test_security_block_rejects_create(self, monkeypatch):
        from deerflow.skills.security_scanner import ScanResult
        from deerflow.tools import skill_manage_tool as sm_mod

        async def _block_scan(content, *, executable=False, location="SKILL.md", app_config=None):
            return ScanResult(decision="block", reason="malicious")

        monkeypatch.setattr(sm_mod, "scan_skill_content", _block_scan)
        from deerflow.tools.skill_manage_tool import _skill_manage_impl

        with pytest.raises(ValueError, match="blocked"):
            await _skill_manage_impl(self._runtime(), action="create", name="bad", content="---\nname: bad\ndescription: d\n---\n\nx")

    @pytest.mark.asyncio
    async def test_unsupported_action_errors(self):
        from deerflow.tools.skill_manage_tool import _skill_manage_impl

        with pytest.raises(ValueError, match="Unsupported action"):
            await _skill_manage_impl(self._runtime(), action="frobnicate", name="x")

    def test_tool_has_sync_func(self):
        """skill_manage_tool 补了同步入口（make_sync_tool_wrapper）。"""
        from deerflow.tools.skill_manage_tool import skill_manage_tool

        assert skill_manage_tool.func is not None


# ===========================================================================
# invoke_acp_agent
# ===========================================================================


class TestInvokeAcpAgent:
    def test_build_tool_description_lists_agents(self):
        from deerflow.tools.builtins.invoke_acp_agent_tool import build_invoke_acp_agent_tool

        tool_obj = build_invoke_acp_agent_tool(
            {
                "codex": {"command": "codex-acp", "description": "Codex coding agent"},
                "claude": {"command": "claude-acp", "description": "Claude agent"},
            }
        )
        assert tool_obj.name == "invoke_acp_agent"
        assert "codex" in tool_obj.description
        assert "claude" in tool_obj.description

    @pytest.mark.asyncio
    async def test_missing_acp_package_returns_install_hint(self):
        from deerflow.tools.builtins.invoke_acp_agent_tool import build_invoke_acp_agent_tool

        tool_obj = build_invoke_acp_agent_tool({"codex": {"command": "codex-acp", "description": "d"}})
        result = await tool_obj.coroutine(agent="codex", prompt="hi", config=None)
        assert "not installed" in result
        assert "agent-client-protocol" in result

    @pytest.mark.asyncio
    async def test_unknown_agent_errors(self):
        from deerflow.tools.builtins.invoke_acp_agent_tool import build_invoke_acp_agent_tool

        tool_obj = build_invoke_acp_agent_tool({"codex": {"command": "codex-acp", "description": "d"}})
        result = await tool_obj.coroutine(agent="ghost", prompt="hi", config=None)
        assert "Unknown agent" in result

    def test_agent_attr_duck_typed(self):
        from deerflow.tools.builtins.invoke_acp_agent_tool import _agent_attr

        assert _agent_attr({"command": "x"}, "command") == "x"
        assert _agent_attr({"command": "x"}, "missing", "default") == "default"
        obj = SimpleNamespace(command="y")
        assert _agent_attr(obj, "command") == "y"


# ===========================================================================
# task_tool（hermetic 子集：未知类型 / host-bash 门控）
# ===========================================================================


class TestTaskTool:
    @pytest.mark.asyncio
    async def test_unknown_subagent_type_returns_error(self, monkeypatch):
        # 注意：builtins/__init__ 导出 task_tool（工具对象），遮蔽了模块名 task_tool。
        # 用 importlib 拿到模块本身来 patch 它绑定的 get_subagent_config 等。
        import importlib

        task_mod = importlib.import_module("deerflow.tools.builtins.task_tool")
        monkeypatch.setattr(task_mod, "get_subagent_config", lambda *a, **kw: None)
        monkeypatch.setattr(task_mod, "get_available_subagent_names", lambda *a, **kw: ["general-purpose"])
        runtime = _fake_runtime()
        result = await task_mod.task_tool.coroutine(
            runtime=runtime,
            description="do thing",
            prompt="do it",
            subagent_type="ghost",
            tool_call_id="tc1",
        )
        assert "Unknown subagent type" in result

    @pytest.mark.asyncio
    async def test_bash_without_host_bash_disabled(self, monkeypatch):
        import importlib

        from deerflow.subagents.config import SubagentConfig

        task_mod = importlib.import_module("deerflow.tools.builtins.task_tool")
        monkeypatch.setattr(task_mod, "get_subagent_config", lambda *a, **kw: SubagentConfig(name="bash", description="bash specialist"))
        monkeypatch.setattr(task_mod, "is_host_bash_allowed", lambda *a, **kw: False)
        monkeypatch.setattr(task_mod, "get_available_subagent_names", lambda *a, **kw: ["general-purpose", "bash"])
        runtime = _fake_runtime()
        result = await task_mod.task_tool.coroutine(
            runtime=runtime,
            description="run cmd",
            prompt="ls",
            subagent_type="bash",
            tool_call_id="tc1",
        )
        assert result.startswith("Error:")


# ===========================================================================
# _ensure_sync_invocable_tool / _is_host_bash_tool
# ===========================================================================


class TestSyncWrapperAttachment:
    def test_async_only_tool_gets_sync_func(self):
        from deerflow.tools.tools import _ensure_sync_invocable_tool

        @tool
        async def async_only(query: str) -> str:
            """Async only."""
            return query

        async_only.func = None
        result = _ensure_sync_invocable_tool(async_only)
        assert result.func is not None

    def test_sync_tool_unchanged(self):
        from deerflow.tools.tools import _ensure_sync_invocable_tool

        @tool
        def sync_tool(query: str) -> str:
            """Sync tool."""
            return query

        original_func = sync_tool.func
        result = _ensure_sync_invocable_tool(sync_tool)
        assert result.func is original_func


class TestIsHostBashTool:
    def test_detection(self):
        from deerflow.tools.tools import _is_host_bash_tool

        assert _is_host_bash_tool({"group": "bash", "use": "x"})
        assert _is_host_bash_tool({"group": "other", "use": "deerflow.sandbox.tools:bash_tool"})
        assert not _is_host_bash_tool({"group": "other", "use": "x"})
