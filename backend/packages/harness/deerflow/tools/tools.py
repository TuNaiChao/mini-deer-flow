"""工具加载与组装。

对齐 deer ``tools/tools.py``。``get_available_tools()`` 是所有工具的统一入口，被 make_lead_agent 调用。

工具五类来源：
  1. **配置定义工具** —— 从 config.yaml 的 ``tools[]`` 用 ``resolve_variable(cfg["use"], BaseTool)`` 加载
     （含 M21 community 的 web 搜索/抓取工具，经 ``tools[].use:`` 路径）；
  2. **内置工具** —— present_files / ask_clarification（始终）+ skill_manage（skill_evolution）+
     task（subagent_enabled）+ view_image（supports_vision）；
  3. **MCP 工具** —— 从启用的 MCP 服务器加载（M20，``get_cached_mcp_tools``），加载后 ``tag_mcp_tool`` 标记；
  4. **ACP 工具** —— 配置了 ``acp_agents`` 才加（soft-load ``acp``）；
  5. （setup_agent / update_agent 由 M17 lead_agent factory 按上下文绑定，不在此）

关键不变量：**按 name 去重**（config > builtins > MCP > ACP，防 #1803：重名让 LLM 收到模糊/拼接的
function schema）；**host-bash 过滤**（LocalSandboxProvider 活跃时不暴露宿主 bash）；**name 不一致告警**
（config name ≠ tool .name 是 #1803 根因）；**sync 包装**（async-only 工具补 ``func`` 同步入口）。
"""

import logging

from langchain.tools import BaseTool

from deerflow.config import get_app_config
from deerflow.config.app_config import AppConfig
from deerflow.reflection import resolve_variable
from deerflow.sandbox.security import is_host_bash_allowed
from deerflow.tools.builtins import ask_clarification_tool, present_file_tool, task_tool, view_image_tool
from deerflow.tools.mcp_metadata import tag_mcp_tool
from deerflow.tools.sync import make_sync_tool_wrapper

logger = logging.getLogger(__name__)

# 始终可用的内置工具
BUILTIN_TOOLS = [
    present_file_tool,
    ask_clarification_tool,
]

# 子代理工具（仅 subagent_enabled）
SUBAGENT_TOOLS = [
    task_tool,
    # task_status_tool 不再暴露给 LLM（后端内部轮询）
]


def _is_host_bash_tool(tool: object) -> bool:
    """该 config 工具条目是否代表宿主 bash 执行面。"""
    group = tool.get("group") if isinstance(tool, dict) else getattr(tool, "group", None)
    use = tool.get("use") if isinstance(tool, dict) else getattr(tool, "use", None)
    if group == "bash":
        return True
    if use == "deerflow.sandbox.tools:bash_tool":
        return True
    return False


def _ensure_sync_invocable_tool(tool: BaseTool) -> BaseTool:
    """给 async-only 工具补 ``func`` 同步入口（同步 agent 调用路径需要）。"""
    if getattr(tool, "func", None) is None and getattr(tool, "coroutine", None) is not None:
        tool.func = make_sync_tool_wrapper(tool.coroutine, tool.name)
    return tool


def get_available_tools(
    groups: list[str] | None = None,
    include_mcp: bool = True,
    model_name: str | None = None,
    subagent_enabled: bool = False,
    *,
    app_config: AppConfig | None = None,
) -> list[BaseTool]:
    """
    获取所有可用工具。

    Args:
        groups: 可选的工具分组过滤（对应 config.yaml 的 tool_groups）。
        include_mcp: 是否包含 MCP 工具（默认 True）。
        model_name: 模型名，用于决定是否加 view_image 工具（supports_vision）。
        subagent_enabled: 是否包含子代理 task 工具。
        app_config: 显式配置，为 None 时用全局 get_app_config()。

    Returns:
        按 name 去重后的工具列表（config > builtins > MCP > ACP）。
    """
    config = app_config or get_app_config()

    # 1. 配置定义工具：经 resolve_variable 动态加载（含 M21 community 的 web 工具）。
    tool_configs = [tool for tool in config.tools if groups is None or tool.get("group") in groups]

    # LocalSandboxProvider 活跃时不暴露宿主 bash（非安全边界，红线）。
    if not is_host_bash_allowed(config):
        tool_configs = [tool for tool in tool_configs if not _is_host_bash_tool(tool)]

    loaded_tools_raw = [(cfg, resolve_variable(cfg["use"], BaseTool)) for cfg in tool_configs]

    # config name ≠ tool .name 告警——这是 #1803 根因（LLM schema 一个名，路由认另一个名 → "not a valid tool"）。
    for cfg, loaded in loaded_tools_raw:
        if cfg.get("name") != loaded.name:
            logger.warning(
                "Tool name mismatch: config name %r does not match tool .name %r (use: %s). The tool's own .name will be used for binding.",
                cfg.get("name"),
                loaded.name,
                cfg.get("use"),
            )

    loaded_tools = [_ensure_sync_invocable_tool(t) for _, t in loaded_tools_raw]

    # 2. 条件加内置工具
    builtin_tools = BUILTIN_TOOLS.copy()

    skill_evolution_config = getattr(config, "skill_evolution", None)
    if getattr(skill_evolution_config, "enabled", False):
        from deerflow.tools.skill_manage_tool import skill_manage_tool

        builtin_tools.append(skill_manage_tool)

    if subagent_enabled:
        builtin_tools.extend(SUBAGENT_TOOLS)
        logger.info("Including subagent tools (task)")

    # 无 model_name 时用第一个模型（默认）
    if model_name is None and config.models:
        model_name = config.models[0].name

    # view_image 仅 supports_vision 模型
    model_config = config.get_model_config(model_name) if model_name else None
    if model_config is not None and model_config.supports_vision:
        builtin_tools.append(view_image_tool)
        logger.info("Including view_image_tool for model '%s' (supports_vision=True)", model_name)

    # 3. MCP 工具（M20）——用 ExtensionsConfig.from_file() 而非 config.extensions，总是读盘最新配置
    #    （Gateway API 在另一进程改 extensions_config.json，读盘立即生效）。
    mcp_tools: list[BaseTool] = []
    if include_mcp:
        try:
            from deerflow.config.extensions_config import ExtensionsConfig
            from deerflow.mcp.cache import get_cached_mcp_tools

            extensions_config = ExtensionsConfig.from_file()
            if extensions_config.get_enabled_mcp_servers():
                mcp_tools = get_cached_mcp_tools()
                if mcp_tools:
                    logger.info("Using %d cached MCP tool(s)", len(mcp_tools))
                    # 标记 MCP 来源，供延迟工具装配（tool_search，在 agent 构建处、工具策略过滤之后）识别。
                    # 不建 ContextVar / 注册表——延迟目录 + tool_search 工具按 per-agent 从策略过滤后的工具列表装配。
                    for t in mcp_tools:
                        tag_mcp_tool(t)
        except ImportError:
            logger.warning("MCP module not available. Install 'langchain-mcp-adapters' package to enable MCP tools.")
        except Exception as e:  # noqa: BLE001
            logger.error("Failed to get cached MCP tools: %s", e)

    # 4. ACP 工具——配置了 acp_agents 才加（soft-load acp）
    acp_tools: list[BaseTool] = []
    try:
        from deerflow.tools.builtins.invoke_acp_agent_tool import build_invoke_acp_agent_tool

        acp_agents = getattr(config, "acp_agents", {}) or {}
        if acp_agents:
            acp_tools.append(build_invoke_acp_agent_tool(acp_agents))
            logger.info("Including invoke_acp_agent tool (%d agent(s): %s)", len(acp_agents), list(acp_agents.keys()))
    except Exception as e:  # noqa: BLE001
        logger.warning("Failed to load ACP tool: %s", e)

    logger.info("Total tools loaded: %d, built-in tools: %d, MCP tools: %d, ACP tools: %d", len(loaded_tools), len(builtin_tools), len(mcp_tools), len(acp_tools))

    # 按 name 去重：config > builtins > MCP > ACP。重名让 LLM 收到模糊/拼接的 function schema（#1803）。
    all_tools = [_ensure_sync_invocable_tool(t) for t in loaded_tools + builtin_tools + mcp_tools + acp_tools]
    seen_names: set[str] = set()
    unique_tools: list[BaseTool] = []
    for t in all_tools:
        if t.name not in seen_names:
            unique_tools.append(t)
            seen_names.add(t.name)
        else:
            logger.warning(
                "Duplicate tool name %r detected and skipped — check your config.yaml and MCP server registrations (issue #1803).",
                t.name,
            )
    return unique_tools
