"""Agent 工厂 + 装配测试（M17）。

hermetic：用 monkeypatch 把 langchain 的 ``create_agent``、模型工厂、工具装配等换成记录器 /
桩，验证 ``create_deerflow_agent``（SDK 入口，features 驱动）/ ``make_lead_agent``（config 驱动）
的**组装逻辑**——不真正编译图、不调真实模型。真实模型对话留给 ``*_live`` 冒烟。

覆盖：

- **Part A**：``create_deerflow_agent`` 双模式（features / middleware 全接管）+ 互斥校验 + 工具去重；
- **Part B**：``_assemble_from_features`` feature 开关 + 自定义实例 + Clarification 末位；
- **Part C**：``_insert_extra`` ``@Next``/``@Prev`` 锚点插入 + 冲突检测 + Clarification 末位不变量；
- **Part D**：``make_lead_agent`` config 驱动——tracing 注入 / bootstrap 分支 / custom-agent 分支 /
  工具策略过滤 / 延迟装配 / model_name 回退 / metadata；
- **Part E**：``apply_prompt_template`` 条件段 gating（subagent / soul / self_update / deferred / acp）；
- **Part F**：``thread_state`` reducer（fail-closed ``merge_sandbox`` / ``merge_promoted`` / 清空语义）；
- **Part G**：``Next`` / ``Prev`` 装饰器。
"""

from __future__ import annotations

from deerflow.agents import create_deerflow_agent
from deerflow.agents import factory as factory_module
from deerflow.agents.factory import _assemble_from_features, _insert_extra
from deerflow.agents.features import Next, Prev, RuntimeFeatures
from deerflow.agents.lead_agent import agent as agent_module
from deerflow.agents.lead_agent.prompt import apply_prompt_template
from deerflow.agents.middlewares.clarification_middleware import ClarificationMiddleware
from deerflow.agents.middlewares.dangling_tool_call_middleware import DanglingToolCallMiddleware
from deerflow.agents.middlewares.loop_detection_middleware import LoopDetectionMiddleware
from deerflow.agents.middlewares.tool_error_handling_middleware import ToolErrorHandlingMiddleware
from deerflow.agents.thread_state import (
    PromotedTools,
    SandboxState,
    merge_artifacts,
    merge_promoted,
    merge_sandbox,
    merge_todos,
    merge_viewed_images,
)
from deerflow.config import AppConfig, ModelConfig

# ---------------------------------------------------------------------------
# 共享桩
# ---------------------------------------------------------------------------


class _NamedTool:
    """带 name 的工具桩（避免传裸字符串——factory 会读 ``.name`` 去重）。"""

    def __init__(self, name):
        self.name = name


# AgentMiddlewareSub：测试用中间件基类（避开直接实例化抽象基类的开销）。
from langchain.agents.middleware import AgentMiddleware as _AM  # noqa: E402


class AgentMiddlewareSub(_AM):
    """测试用中间件桩。"""

    pass


# ===========================================================================
# Part A — create_deerflow_agent（SDK 入口，features 驱动）
# ===========================================================================


def _capture_create_agent(monkeypatch) -> dict:
    """桩化 factory.create_agent，返回记录入参的 dict。"""
    captured: dict = {}
    monkeypatch.setattr(factory_module, "create_agent", lambda **k: captured.update(k) or "compiled-graph")
    return captured


def test_create_deerflow_agent_features_default_assembles_chain(monkeypatch):
    """默认 RuntimeFeatures() 装出含 Clarification 末位的链，model/tools 透传。"""
    captured = _capture_create_agent(monkeypatch)

    fake_model = object()
    agent = create_deerflow_agent(model=fake_model, tools=[_NamedTool("t1")])

    assert agent == "compiled-graph"
    assert captured["model"] is fake_model
    # 用户工具 + ask_clarification_tool（Clarification feature 恒定注入）
    tool_names = [getattr(t, "name", t) for t in captured["tools"]]
    assert "t1" in tool_names
    assert "ask_clarification" in tool_names
    # 默认链含 Clarification 末位 + ToolErrorHandling + LoopDetection（默认开）
    names = [type(m).__name__ for m in captured["middleware"]]
    assert names[-1] == "ClarificationMiddleware"
    assert "ToolErrorHandlingMiddleware" in names
    assert "LoopDetectionMiddleware" in names


def test_create_deerflow_agent_full_takeover_middleware(monkeypatch):
    """middleware=[...] 全接管——原样用（元素相等，新 list 实例）。"""
    captured = _capture_create_agent(monkeypatch)
    mws = [ToolErrorHandlingMiddleware(), ClarificationMiddleware()]

    create_deerflow_agent(model=object(), middleware=mws)

    assert captured["middleware"] == mws
    # tools 不被 feature 注入污染（全接管模式跳过 _assemble_from_features）
    assert captured["tools"] is None or captured["tools"] == []


def test_create_deerflow_agent_rejects_middleware_and_features(monkeypatch):
    """同时给 middleware 和 features → ValueError。"""
    _capture_create_agent(monkeypatch)
    import pytest

    with pytest.raises(ValueError, match="middleware"):
        create_deerflow_agent(model=object(), middleware=[], features=RuntimeFeatures())


def test_create_deerflow_agent_rejects_extra_with_middleware(monkeypatch):
    """middleware（全接管）+ extra_middleware → ValueError。"""
    _capture_create_agent(monkeypatch)
    import pytest

    with pytest.raises(ValueError, match="extra_middleware"):
        create_deerflow_agent(model=object(), middleware=[], extra_middleware=[ToolErrorHandlingMiddleware()])


def test_create_deerflow_agent_rejects_non_middleware_extra(monkeypatch):
    """extra_middleware 里的非 AgentMiddleware 项 → TypeError。"""
    _capture_create_agent(monkeypatch)
    import pytest

    with pytest.raises(TypeError, match="AgentMiddleware"):
        create_deerflow_agent(model=object(), extra_middleware=["not-a-middleware"])


def test_create_deerflow_agent_passes_checkpointer_name_state_schema(monkeypatch):
    """checkpointer / name / state_schema / system_prompt 透传给 create_agent。"""
    captured = _capture_create_agent(monkeypatch)
    from deerflow.agents.thread_state import ThreadState

    sentinel_cp = object()
    create_deerflow_agent(
        model=object(),
        system_prompt="hi",
        checkpointer=sentinel_cp,
        name="my-agent",
        state_schema=ThreadState,
        features=RuntimeFeatures(sandbox=False, loop_detection=False, auto_title=False),
    )

    assert captured["system_prompt"] == "hi"
    assert captured["checkpointer"] is sentinel_cp
    assert captured["name"] == "my-agent"
    assert captured["state_schema"] is ThreadState


# ===========================================================================
# Part B — _assemble_from_features（feature 开关 + 自定义实例）
# ===========================================================================


def _chain_names(feat: RuntimeFeatures, **kw) -> list[str]:
    chain, _ = _assemble_from_features(feat, **kw)
    return [type(m).__name__ for m in chain]


def test_features_default_chain_has_clarification_last():
    """默认 RuntimeFeatures() → Clarification 末位 + 关键骨架在内。"""
    names = _chain_names(RuntimeFeatures())
    assert names[-1] == "ClarificationMiddleware"
    # sandbox 基础设施三件套
    assert "ThreadDataMiddleware" in names
    assert "UploadsMiddleware" in names
    assert "SandboxMiddleware" in names
    assert "DanglingToolCallMiddleware" in names


def test_features_false_disables_middleware():
    """把 feature 设 False → 对应中间件不在链里。"""
    names = _chain_names(RuntimeFeatures(sandbox=False, loop_detection=False))
    assert "SandboxMiddleware" not in names
    assert "ThreadDataMiddleware" not in names
    assert "LoopDetectionMiddleware" not in names


def test_features_custom_instance_replaces_default():
    """传 AgentMiddleware 实例 → 用它替换内置默认。"""
    custom = LoopDetectionMiddleware.__new__(LoopDetectionMiddleware)  # 不调 __init__
    chain, _ = _assemble_from_features(RuntimeFeatures(loop_detection=custom))
    assert custom in chain
    # 只出现一次
    assert sum(1 for m in chain if m is custom) == 1


def test_features_vision_adds_view_image_tool():
    """vision=True + sandbox=True → view_image_tool 进 extra_tools。"""
    _, tools = _assemble_from_features(RuntimeFeatures(vision=True))
    assert any(getattr(t, "name", None) == "view_image" for t in tools)


def test_features_subagent_adds_task_tool():
    """subagent=True → task_tool 进 extra_tools。"""
    _, tools = _assemble_from_features(RuntimeFeatures(subagent=True))
    assert any(getattr(t, "name", None) == "task" for t in tools)


def test_features_clarification_always_injects_tool():
    """ask_clarification_tool 恒定注入（无论 feature）。"""
    _, tools = _assemble_from_features(RuntimeFeatures())
    assert any(getattr(t, "name", None) == "ask_clarification" for t in tools)


def test_features_guardrail_true_without_instance_errors():
    """guardrail=True 但没给实例 → ValueError（无内置 GuardrailMiddleware）。"""
    import pytest

    with pytest.raises(ValueError, match="guardrail"):
        _assemble_from_features(RuntimeFeatures(guardrail=True))  # type: ignore[arg-type]


def test_features_summarization_true_without_instance_errors():
    """summarization=True 但没给实例 → ValueError（需要 model 参数）。"""
    import pytest

    with pytest.raises(ValueError, match="summarization"):
        _assemble_from_features(RuntimeFeatures(summarization=True))  # type: ignore[arg-type]


# ===========================================================================
# Part C — _insert_extra（@Next/@Prev 锚点插入 + 冲突检测）
# ===========================================================================


def test_insert_extra_unanchored_before_clarification():
    """无锚点的 extra 插在 Clarification 之前。"""
    chain = [ToolErrorHandlingMiddleware(), ClarificationMiddleware()]
    extra = [DanglingToolCallMiddleware()]
    _insert_extra(chain, extra)

    clar_idx = chain.index(next(m for m in chain if isinstance(m, ClarificationMiddleware)))
    # Dangling 插在 Clarification 之前
    assert isinstance(chain[clar_idx - 1], DanglingToolCallMiddleware)
    # Clarification 仍是末位
    assert isinstance(chain[-1], ClarificationMiddleware)


def test_insert_extra_next_anchor_places_after():
    """@Next(ToolErrorHandling) → 紧跟 ToolErrorHandling 之后。"""

    @Next(ToolErrorHandlingMiddleware)
    class _Custom(AgentMiddlewareSub):  # type: ignore[misc, valid-type]
        pass

    chain = [ToolErrorHandlingMiddleware(), ClarificationMiddleware()]
    _insert_extra(chain, [_Custom()])
    teh_idx = next(i for i, m in enumerate(chain) if isinstance(m, ToolErrorHandlingMiddleware))
    assert isinstance(chain[teh_idx + 1], _Custom)


def test_insert_extra_prev_anchor_places_before():
    """@Prev(Clarification) → 紧贴 Clarification 之前。"""

    @Prev(ClarificationMiddleware)
    class _Custom(AgentMiddlewareSub):  # type: ignore[misc, valid-type]
        pass

    chain = [ToolErrorHandlingMiddleware(), ClarificationMiddleware()]
    _insert_extra(chain, [_Custom()])
    clar_idx = next(i for i, m in enumerate(chain) if isinstance(m, ClarificationMiddleware))
    assert isinstance(chain[clar_idx - 1], _Custom)


def test_insert_extra_both_anchors_errors():
    """同时 @Next 和 @Prev → ValueError。"""
    import pytest

    @Next(ToolErrorHandlingMiddleware)
    @Prev(ClarificationMiddleware)
    class _Custom(AgentMiddlewareSub):  # type: ignore[misc, valid-type]
        pass

    chain = [ToolErrorHandlingMiddleware(), ClarificationMiddleware()]
    with pytest.raises(ValueError, match="both"):
        _insert_extra(chain, [_Custom()])


def test_insert_extra_conflict_two_next_same_anchor_errors():
    """两个 extra 都 @Next 同一锚点 → ValueError。"""
    import pytest

    @Next(ToolErrorHandlingMiddleware)
    class _A(AgentMiddlewareSub):  # type: ignore[misc, valid-type]
        pass

    @Next(ToolErrorHandlingMiddleware)
    class _B(AgentMiddlewareSub):  # type: ignore[misc, valid-type]
        pass

    chain = [ToolErrorHandlingMiddleware(), ClarificationMiddleware()]
    with pytest.raises(ValueError, match="Conflict"):
        _insert_extra(chain, [_A(), _B()])


def test_insert_extra_keeps_clarification_last_when_next_clarification():
    """@Next(Clarification) 会把 Clarification 顶离尾部——_assemble_from_features 须把它移回末位。"""
    # 经 _assemble_from_features 走（它负责「Clarification 末位」修复），不是裸 _insert_extra

    @Next(ClarificationMiddleware)
    class _Custom(AgentMiddlewareSub):  # type: ignore[misc, valid-type]
        pass

    chain, _ = _assemble_from_features(RuntimeFeatures(), extra_middleware=[_Custom()])
    assert isinstance(chain[-1], ClarificationMiddleware)
    assert any(isinstance(m, _Custom) for m in chain)


def test_insert_extra_unresolvable_anchor_errors():
    """锚点不在链里 → ValueError。"""
    import pytest

    @Next(LoopDetectionMiddleware)
    class _Custom(AgentMiddlewareSub):  # type: ignore[misc, valid-type]
        pass

    # 链里没有 LoopDetectionMiddleware
    chain = [ToolErrorHandlingMiddleware(), ClarificationMiddleware()]
    with pytest.raises(ValueError, match="Cannot resolve"):
        _insert_extra(chain, [_Custom()])


# ===========================================================================
# Part D — make_lead_agent（config 驱动）
# ===========================================================================


class _FakeSetup:
    """桩 DeferredToolSetup——只暴露 deferred_names / catalog_hash。"""

    def __init__(self, deferred_names=frozenset()):
        self.deferred_names = deferred_names
        self.catalog_hash = "hash"


def _stub_lead(monkeypatch, *, tracing=None) -> dict:
    """桩化 _make_lead_agent 全部外部依赖，返回记录 create_agent 入参的 dict。

    默认把 filter / assemble_deferred 设成透传，让真实分支逻辑能跑。
    """
    fake_cfg = AppConfig(models=[ModelConfig(name="m", use="x:M", model="mm")])

    def _create_chat_model(**k):
        return ("model", k)

    monkeypatch.setattr(agent_module, "create_chat_model", _create_chat_model)
    monkeypatch.setattr(agent_module, "build_middlewares", lambda *a, **k: ["mw"])
    monkeypatch.setattr(agent_module, "apply_prompt_template", lambda **k: "sys-prompt")
    monkeypatch.setattr(agent_module, "build_tracing_callbacks", lambda: tracing if tracing is not None else [])
    monkeypatch.setattr(agent_module, "load_agent_config", lambda *a, **k: None)
    monkeypatch.setattr(agent_module, "validate_agent_name", lambda n: n)
    # 工具策略过滤透传
    monkeypatch.setattr(agent_module, "filter_tools_by_skill_allowed_tools", lambda tools, skills: list(tools))
    # get_available_tools 返回带 name 的可识别对象
    monkeypatch.setattr("deerflow.tools.get_available_tools", lambda **k: [_NamedTool("plain-tool")])
    monkeypatch.setattr("deerflow.tools.builtins.setup_agent", _NamedTool("setup_agent"))
    monkeypatch.setattr("deerflow.tools.builtins.update_agent", _NamedTool("update_agent"))
    # 延迟装配透传，返回 (tools, setup)
    monkeypatch.setattr(
        "deerflow.tools.builtins.tool_search.assemble_deferred_tools",
        lambda tools, *, enabled: (list(tools), _FakeSetup()),
    )

    captured: dict = {}
    monkeypatch.setattr(agent_module, "create_agent", lambda **k: captured.update(k) or "compiled")
    return captured, fake_cfg


def test_make_lead_agent_assembles_graph_from_config(monkeypatch):
    """make_lead_agent 从 RunnableConfig 解析参数并组装图。"""
    captured, fake_cfg = _stub_lead(monkeypatch)

    config = {"configurable": {"model_name": "m", "thread_id": "t1", "app_config": fake_cfg}}
    agent = agent_module.make_lead_agent(config)

    assert agent == "compiled"
    assert captured["middleware"] == ["mw"]
    assert captured["system_prompt"] == "sys-prompt"
    assert captured["state_schema"] is agent_module.ThreadState


def test_make_lead_agent_forwards_thinking_flag(monkeypatch):
    """thinking_enabled 透传给 create_chat_model（模型须 supports_thinking，否则被门控降级）。"""
    seen: dict = {}
    _, _ = _stub_lead(monkeypatch)

    # 用 supports_thinking=True 的模型，防 thinking 被门控降级成 False
    fake_cfg = AppConfig(models=[ModelConfig(name="m", use="x:M", model="mm", supports_thinking=True)])

    # 覆盖 create_chat_model 记录 kwargs
    monkeypatch.setattr(agent_module, "create_chat_model", lambda **k: seen.update(k) or ("model", k))

    config = {"configurable": {"model_name": "m", "thinking_enabled": True, "app_config": fake_cfg}}
    agent_module.make_lead_agent(config)

    assert seen["thinking_enabled"] is True


def test_make_lead_agent_attach_tracing_false(monkeypatch):
    """图内 create_chat_model 调用必须传 attach_tracing=False（tracing 不变量）。"""
    seen: dict = {}
    _, fake_cfg = _stub_lead(monkeypatch)
    monkeypatch.setattr(agent_module, "create_chat_model", lambda **k: seen.update(k) or ("model", k))

    config = {"configurable": {"model_name": "m", "app_config": fake_cfg}}
    agent_module.make_lead_agent(config)

    assert seen["attach_tracing"] is False


def test_make_lead_agent_model_name_fallback(monkeypatch):
    """请求的模型名不在 config → 回退默认模型。"""
    _, fake_cfg = _stub_lead(monkeypatch)

    config = {"configurable": {"model_name": "nonexistent", "app_config": fake_cfg}}
    # 不抛错即说明走了回退（_resolve_model_name 返回默认 "m"）
    agent_module.make_lead_agent(config)


def test_make_lead_agent_no_models_configured_raises(monkeypatch):
    """无模型配置 → ValueError。"""
    import pytest

    monkeypatch.setattr(agent_module, "build_tracing_callbacks", lambda: [])
    monkeypatch.setattr(agent_module, "load_agent_config", lambda *a, **k: None)
    monkeypatch.setattr(agent_module, "validate_agent_name", lambda n: n)

    empty_cfg = AppConfig(models=[])
    config = {"configurable": {"app_config": empty_cfg}}
    with pytest.raises(ValueError, match="No chat models"):
        agent_module.make_lead_agent(config)


def test_make_lead_agent_tracing_callbacks_injected(monkeypatch):
    """build_tracing_callbacks 非空 → config["callbacks"] 被扩展。"""
    sentinel_cb = object()
    _, fake_cfg = _stub_lead(monkeypatch, tracing=[sentinel_cb])

    # callbacks 在 config 顶层（不在 configurable 里）——_make_lead_agent 读 config.get("callbacks")
    config = {"configurable": {"model_name": "m", "app_config": fake_cfg}, "callbacks": ["pre"]}
    agent_module.make_lead_agent(config)

    assert sentinel_cb in config["callbacks"]
    assert "pre" in config["callbacks"]


def test_make_lead_agent_no_tracing_when_empty(monkeypatch):
    """build_tracing_callbacks 返回空 → callbacks 不动。"""
    _, fake_cfg = _stub_lead(monkeypatch, tracing=[])

    config = {"configurable": {"model_name": "m", "app_config": fake_cfg}}
    agent_module.make_lead_agent(config)

    assert "callbacks" not in config


def test_make_lead_agent_bootstrap_branch_binds_setup_agent(monkeypatch):
    """is_bootstrap=True → setup_agent 进工具，技能白名单={"bootstrap"}。"""
    captured, fake_cfg = _stub_lead(monkeypatch)
    skills_seen: dict = {}
    monkeypatch.setattr(
        agent_module,
        "build_middlewares",
        lambda *a, **k: skills_seen.update({"available_skills": k.get("available_skills")}) or ["mw"],
    )

    config = {"configurable": {"model_name": "m", "is_bootstrap": True, "app_config": fake_cfg}}
    agent_module.make_lead_agent(config)

    tool_names = [getattr(t, "name", t) for t in captured["tools"]]
    assert "setup_agent" in tool_names
    assert "update_agent" not in tool_names  # bootstrap 不绑 update_agent
    assert skills_seen["available_skills"] == {"bootstrap"}


def test_make_lead_agent_custom_agent_binds_update_agent(monkeypatch):
    """agent_name 设了（非 bootstrap）→ update_agent 进工具。"""
    captured, fake_cfg = _stub_lead(monkeypatch)

    config = {"configurable": {"model_name": "m", "agent_name": "myagent", "app_config": fake_cfg}}
    agent_module.make_lead_agent(config)

    tool_names = [getattr(t, "name", t) for t in captured["tools"]]
    assert "update_agent" in tool_names
    assert "setup_agent" not in tool_names


def test_make_lead_agent_default_no_update_agent(monkeypatch):
    """默认 agent（无 agent_name）→ 不绑 update_agent。"""
    captured, fake_cfg = _stub_lead(monkeypatch)

    config = {"configurable": {"model_name": "m", "app_config": fake_cfg}}
    agent_module.make_lead_agent(config)

    tool_names = [getattr(t, "name", t) for t in captured["tools"]]
    assert "update_agent" not in tool_names
    assert "setup_agent" not in tool_names


def test_make_lead_agent_tool_policy_filter_applied(monkeypatch):
    """filter_tools_by_skill_allowed_tools 被调用（工具策略白名单收紧）。"""
    _, fake_cfg = _stub_lead(monkeypatch)
    filtered: dict = {}
    monkeypatch.setattr(
        agent_module,
        "filter_tools_by_skill_allowed_tools",
        lambda tools, skills: filtered.update({"n": len(tools), "skills": skills}) or [tools[0]],
    )

    config = {"configurable": {"model_name": "m", "app_config": fake_cfg}}
    agent_module.make_lead_agent(config)

    assert filtered["n"] >= 1  # 至少有 plain-tool 进来


def test_make_lead_agent_deferred_setup_passed_to_build_middlewares(monkeypatch):
    """assemble_deferred_tools 的 setup 透传给 build_middlewares。"""
    sentinel_setup = _FakeSetup(deferred_names=frozenset({"x"}))
    _, fake_cfg = _stub_lead(monkeypatch)
    monkeypatch.setattr(
        "deerflow.tools.builtins.tool_search.assemble_deferred_tools",
        lambda tools, *, enabled: (list(tools), sentinel_setup),
    )
    mw_seen: dict = {}
    monkeypatch.setattr(
        agent_module,
        "build_middlewares",
        lambda *a, **k: mw_seen.update({"setup": k.get("deferred_setup")}) or ["mw"],
    )

    config = {"configurable": {"model_name": "m", "app_config": fake_cfg}}
    agent_module.make_lead_agent(config)

    assert mw_seen["setup"] is sentinel_setup


def test_make_lead_agent_metadata_injected(monkeypatch):
    """config["metadata"] 被注入 agent_name / model_name / 各 flag。"""
    _, fake_cfg = _stub_lead(monkeypatch)

    config = {"configurable": {"model_name": "m", "app_config": fake_cfg}}
    agent_module.make_lead_agent(config)

    md = config["metadata"]
    assert md["model_name"] == "m"
    assert md["agent_name"] == "default"
    assert "thinking_enabled" in md
    assert "subagent_enabled" in md


def test_make_lead_agent_tool_groups_passed_for_custom_agent(monkeypatch):
    """自定义 agent 的 tool_groups 透传给 get_available_tools + 进 metadata。"""
    captured, fake_cfg = _stub_lead(monkeypatch)

    # 让 load_agent_config 返回带 tool_groups 的配置
    from deerflow.config.agents_config import AgentConfig

    monkeypatch.setattr(
        agent_module,
        "load_agent_config",
        lambda *a, **k: AgentConfig(name="myagent", tool_groups=["web"]),
    )
    groups_seen: dict = {}
    monkeypatch.setattr(
        "deerflow.tools.get_available_tools",
        lambda **k: groups_seen.update({"groups": k.get("groups")}) or [_NamedTool("t")],
    )

    config = {"configurable": {"model_name": "m", "agent_name": "myagent", "app_config": fake_cfg}}
    agent_module.make_lead_agent(config)

    assert groups_seen["groups"] == ["web"]
    assert config["metadata"]["tool_groups"] == ["web"]


# ===========================================================================
# Part E — apply_prompt_template（条件段 gating）
# ===========================================================================


def test_apply_prompt_template_minimal_has_no_feature_sections():
    """所有 feature 关 → 无 subagent / soul / self_update 段。"""
    prompt = apply_prompt_template()
    assert "<subagent_system>" not in prompt
    assert "<self_update>" not in prompt
    assert "<soul>" not in prompt
    # 角色段恒在
    assert "<role>" in prompt


def test_apply_prompt_template_subagent_section_gated():
    """subagent_enabled=True → <subagent_system> 段 + HARD LIMIT 出现。"""
    prompt = apply_prompt_template(subagent_enabled=True, max_concurrent_subagents=3)
    assert "<subagent_system>" in prompt
    assert "MAXIMUM 3" in prompt
    # critical_reminders 的 orchestrator 提醒
    assert "Orchestrator Mode" in prompt
    # thinking_style 的分解检查
    assert "DECOMPOSITION CHECK" in prompt


def test_apply_prompt_template_subagent_concurrency_in_prompt():
    """并发上限 N 填进提示词的 HARD LIMIT。"""
    prompt = apply_prompt_template(subagent_enabled=True, max_concurrent_subagents=4)
    assert "MAXIMUM 4" in prompt


def test_apply_prompt_template_self_update_only_for_named_agent():
    """agent_name 设了 → <self_update> 段；没设 → 无。"""
    with_name = apply_prompt_template(agent_name="myagent")
    assert "<self_update>" in with_name
    assert "myagent" in with_name

    without = apply_prompt_template()
    assert "<self_update>" not in without


def test_apply_prompt_template_agent_name_in_role():
    """agent_name 填进 <role>；None 用默认 "DeerFlow 2.0"。"""
    assert "myagent" in apply_prompt_template(agent_name="myagent")
    assert "DeerFlow 2.0" in apply_prompt_template()


def test_apply_prompt_template_acp_empty_by_default():
    """无 acp_agents → 无 ACP 段。"""
    prompt = apply_prompt_template(app_config=AppConfig())
    assert "ACP Agent Tasks" not in prompt


def test_apply_prompt_template_acp_section_when_configured():
    """配了 acp_agents → ACP 段出现。"""
    cfg = AppConfig()
    cfg.acp_agents = {"codex": {}}  # extra="allow"
    prompt = apply_prompt_template(app_config=cfg)
    assert "ACP Agent Tasks" in prompt


# ===========================================================================
# Part F — thread_state reducer
# ===========================================================================


def test_merge_sandbox_new_none_preserves_existing():
    """new=None → 保留 existing。"""
    existing: SandboxState = {"sandbox_id": "abc"}
    assert merge_sandbox(existing, None) is existing


def test_merge_sandbox_existing_none_returns_new():
    """existing=None → 返回 new。"""
    new: SandboxState = {"sandbox_id": "abc"}
    assert merge_sandbox(None, new) is new


def test_merge_sandbox_idempotent_same_id():
    """同 sandbox_id 幂等写 → 返回 existing。"""
    existing: SandboxState = {"sandbox_id": "abc"}
    new: SandboxState = {"sandbox_id": "abc"}
    assert merge_sandbox(existing, new) is existing


def test_merge_sandbox_conflict_raises():
    """不同 sandbox_id → fail-closed 抛错（红线 #16）。"""
    import pytest

    existing: SandboxState = {"sandbox_id": "abc"}
    new: SandboxState = {"sandbox_id": "xyz"}
    with pytest.raises(ValueError, match="Conflicting sandbox"):
        merge_sandbox(existing, new)


def test_merge_promoted_preserves_when_new_none():
    """new=None/空 → 保留 existing。"""
    existing: PromotedTools = {"catalog_hash": "h", "names": ["a"]}
    assert merge_promoted(existing, None) is existing
    assert merge_promoted(existing, {}) is existing  # type: ignore[arg-type]


def test_merge_promoted_catalog_hash_change_replaces():
    """catalog_hash 变了 → 整体替换，丢陈旧 names。"""
    existing: PromotedTools = {"catalog_hash": "old", "names": ["a", "b"]}
    new: PromotedTools = {"catalog_hash": "new", "names": ["c"]}
    result = merge_promoted(existing, new)
    assert result == {"catalog_hash": "new", "names": ["c"]}


def test_merge_promoted_same_hash_unions_dedup():
    """同 catalog_hash → 求 names 并集，去重保序。"""
    existing: PromotedTools = {"catalog_hash": "h", "names": ["a", "b"]}
    new: PromotedTools = {"catalog_hash": "h", "names": ["b", "c", "c"]}
    result = merge_promoted(existing, new)
    assert result == {"catalog_hash": "h", "names": ["a", "b", "c"]}


def test_merge_viewed_images_empty_dict_clears():
    """new={} → 清空全部已查看图片。"""
    existing = {"img1": {"base64": "x", "mime_type": "png"}}
    assert merge_viewed_images(existing, {}) == {}


def test_merge_viewed_images_merges():
    """new 覆盖 existing 同名键。"""
    existing = {"img1": {"base64": "x", "mime_type": "png"}}
    new = {"img1": {"base64": "y", "mime_type": "png"}, "img2": {"base64": "z", "mime_type": "jpg"}}
    result = merge_viewed_images(existing, new)
    assert result["img1"]["base64"] == "y"
    assert result["img2"]["base64"] == "z"


def test_merge_artifacts_dedup_preserves_order():
    assert merge_artifacts(["a", "b"], ["b", "c"]) == ["a", "b", "c"]


def test_merge_todos_new_replaces():
    assert merge_todos(["a"], ["b"]) == ["b"]
    assert merge_todos(["a"], None) == ["a"]
    assert merge_todos(["a"], []) == []


# ===========================================================================
# Part G — Next / Prev 装饰器
# ===========================================================================


def test_next_sets_next_anchor():
    """@Next 把锚点记到类属性 _next_anchor。"""

    @Next(ToolErrorHandlingMiddleware)
    class _C(AgentMiddlewareSub):
        pass

    assert _C._next_anchor is ToolErrorHandlingMiddleware


def test_prev_sets_prev_anchor():
    """@Prev 把锚点记到类属性 _prev_anchor。"""

    @Prev(ClarificationMiddleware)
    class _C(AgentMiddlewareSub):
        pass

    assert _C._prev_anchor is ClarificationMiddleware


def test_next_rejects_non_middleware():
    """@Next 传非 AgentMiddleware 类 → TypeError。"""
    import pytest

    with pytest.raises(TypeError, match="AgentMiddleware"):
        Next(dict)  # type: ignore[arg-type]


def test_prev_rejects_non_middleware():
    """@Prev 传非 AgentMiddleware 类 → TypeError。"""
    import pytest

    with pytest.raises(TypeError, match="AgentMiddleware"):
        Prev(int)  # type: ignore[arg-type]
