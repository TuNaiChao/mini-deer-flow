"""invoke_acp_agent 工具——调外部 ACP 兼容 agent。

对齐 deer ``tools/builtins/invoke_acp_agent_tool.py``。**soft-load** ``acp``（``agent-client-protocol``）
包——缺包时工具仍能构造（描述里列配置的 agent），真正调用才检测，缺包返可操作安装提示。

mini 适配（mini 无独立 ``acp_config`` 模块 / ``paths.acp_workspace_dir``）：
- ACP agent 配置从 ``app_config.acp_agents``（dict，AppConfig ``extra="allow"`` 允许）读，
  每个 agent 是 dict（``command``/``args``/``env``/``model``/``auto_approve_permissions``/``description``）。
- per-thread 工作目录内联计算：``{base_dir}/users/{user_id}/threads/{thread_id}/acp-workspace/``，
  无 thread_id 时回退 ``{base_dir}/acp-workspace/``。
- MCP servers 经 M20 的 ``build_servers_config`` 转 ACP 线格式。

agent 输出文件经虚拟路径 ``/mnt/acp-workspace/``（只读）对 lead agent 可见（路径翻译在 M10/M17）。
"""

import logging
import os
import shutil
from typing import Annotated, Any

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, InjectedToolArg, StructuredTool
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

_INSTALL_HINT = "agent-client-protocol package is not installed. Run `pip install agent-client-protocol` (or `uv sync`) to enable ACP agents."


class _InvokeACPAgentInput(BaseModel):
    agent: str = Field(description="Name of the ACP agent to invoke")
    prompt: str = Field(description="The concise task prompt to send to the agent")


def _agent_attr(agent_config: Any, name: str, default: Any = None) -> Any:
    """从 dict 或对象读 ACP agent 配置字段（duck-typed：dict 用 .get，对象用 getattr）。"""
    if isinstance(agent_config, dict):
        return agent_config.get(name, default)
    return getattr(agent_config, name, default)


def _get_work_dir(thread_id: str | None) -> str:
    """计算 per-thread ACP 工作目录。

    每个线程隔离工作区在 ``{base_dir}/users/{user_id}/threads/{thread_id}/acp-workspace/``，
    防并发会话互读/覆盖。无 thread_id（内嵌/直接调用）回退全局 ``{base_dir}/acp-workspace/``。
    目录不存在自动创建。
    """
    from deerflow.config.paths import get_paths
    from deerflow.runtime.user_context import get_effective_user_id

    paths = get_paths()
    if thread_id:
        work_dir = paths.base_dir / "users" / get_effective_user_id() / "threads" / thread_id / "acp-workspace"
    else:
        work_dir = paths.base_dir / "acp-workspace"
    work_dir.mkdir(parents=True, exist_ok=True)
    logger.info("ACP agent work_dir: %s", work_dir)
    return str(work_dir)


def _build_acp_mcp_servers() -> list[dict[str, Any]]:
    """把 DeerFlow 启用的 MCP 服务器转成 ACP ``new_session`` 的 ``mcpServers`` 列表线格式。

    ACP client 期望 server 对象列表；M20 的 ``build_servers_config`` 返回 name→config 的 dict。
    这里转换 + 校验必需字段，坏配置记 warning 跳过（不拖垮 ACP 调用）。
    """
    from deerflow.config.extensions_config import ExtensionsConfig
    from deerflow.mcp.client import build_servers_config

    mcp_servers: list[dict[str, Any]] = []
    try:
        servers_config = build_servers_config(ExtensionsConfig.from_file())
    except Exception as e:  # noqa: BLE001
        logger.warning("Failed to build MCP servers config for ACP: %s", e)
        return mcp_servers

    for name, server_config in servers_config.items():
        transport_type = server_config.get("transport", "stdio")
        payload: dict[str, Any] = {"name": name, "type": transport_type}

        if transport_type == "stdio":
            command = server_config.get("command")
            if not command:
                logger.warning("MCP server '%s' with stdio transport requires 'command'; skipping for ACP", name)
                continue
            payload["command"] = command
            payload["args"] = server_config.get("args", [])
            env = server_config.get("env", {})
            payload["env"] = [{"name": k, "value": v} for k, v in env.items()]
        elif transport_type in ("http", "sse"):
            url = server_config.get("url")
            if not url:
                logger.warning("MCP server '%s' with %s transport requires 'url'; skipping for ACP", name, transport_type)
                continue
            payload["url"] = url
            headers = server_config.get("headers", {})
            payload["headers"] = [{"name": k, "value": v} for k, v in headers.items()]
        else:
            logger.warning("MCP server '%s' has unsupported transport '%s'; skipping for ACP", name, transport_type)
            continue

        mcp_servers.append(payload)

    return mcp_servers


def _build_permission_response(options: list[Any], *, auto_approve: bool) -> Any:
    """构造 ACP 权限响应。auto_approve=True 选首个 allow_once/allow_always；False 一律 cancel。"""
    from acp import RequestPermissionResponse
    from acp.schema import AllowedOutcome, DeniedOutcome

    if auto_approve:
        for preferred_kind in ("allow_once", "allow_always"):
            for option in options:
                if getattr(option, "kind", None) != preferred_kind:
                    continue
                option_id = getattr(option, "option_id", None) or getattr(option, "optionId", None)
                if option_id is None:
                    continue
                return RequestPermissionResponse(outcome=AllowedOutcome(outcome="selected", optionId=option_id))

    return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))


def _format_invocation_error(agent: str, cmd: str, exc: Exception) -> str:
    """返回面向用户的 ACP 调用错误 + 可操作修复建议。"""
    if not isinstance(exc, FileNotFoundError):
        return f"Error invoking ACP agent '{agent}': {exc}"

    message = f"Error invoking ACP agent '{agent}': Command '{cmd}' was not found on PATH."
    if cmd == "codex-acp" and shutil.which("codex"):
        return f"{message} The installed `codex` CLI does not speak ACP directly. Install a Codex ACP adapter (for example `npx @zed-industries/codex-acp`) or update `acp_agents.codex.command` and `args` in config.yaml."

    return f"{message} Install the agent binary or update `acp_agents.{agent}.command` in config.yaml."


def build_invoke_acp_agent_tool(agents: dict) -> BaseTool:
    """构造 ``invoke_acp_agent`` 工具，描述由配置的 agent 生成。

    工具描述列出可用 agent，让 LLM 知道能调哪些（无需硬编码名字）。

    Args:
        agents: agent 名 → 配置（dict 或对象，含 command/args/env/model/description/auto_approve_permissions）。
    """
    agent_lines = "\n".join(f"- {name}: {_agent_attr(cfg, 'description', '')}" for name, cfg in agents.items())
    description = (
        "Invoke an external ACP-compatible agent and return its final response.\n\n"
        "Available agents:\n"
        f"{agent_lines}\n\n"
        "IMPORTANT: ACP agents operate in their own independent workspace. "
        "Do NOT include /mnt/user-data paths in the prompt. "
        "Give the agent a self-contained task description — it will produce results in its own workspace. "
        "After the agent completes, its output files are accessible at /mnt/acp-workspace/ (read-only)."
    )

    _agents = dict(agents)

    async def _invoke(agent: str, prompt: str, config: Annotated[RunnableConfig, InjectedToolArg] = None) -> str:
        logger.info("Invoking ACP agent %s (prompt length: %d)", agent, len(prompt))
        if agent not in _agents:
            available = ", ".join(_agents.keys())
            return f"Error: Unknown agent '{agent}'. Available: {available}"

        agent_config = _agents[agent]
        thread_id: str | None = ((config or {}).get("configurable") or {}).get("thread_id")

        try:
            from acp import PROTOCOL_VERSION, Client, text_block
            from acp.schema import ClientCapabilities, Implementation
        except ImportError:
            return f"Error: {_INSTALL_HINT}"

        class _CollectingClient(Client):
            """最小 ACP Client，从 session update 收集流式文本。"""

            def __init__(self) -> None:
                self._chunks: list[str] = []

            @property
            def collected_text(self) -> str:
                return "".join(self._chunks)

            async def session_update(self, session_id: str, update, **kwargs) -> None:  # type: ignore[override]
                try:
                    from acp.schema import TextContentBlock

                    if hasattr(update, "content") and isinstance(update.content, TextContentBlock):
                        self._chunks.append(update.content.text)
                except Exception:
                    pass

            async def request_permission(self, options, session_id: str, tool_call, **kwargs):  # type: ignore[override]
                response = _build_permission_response(options, auto_approve=bool(_agent_attr(agent_config, "auto_approve_permissions", False)))
                outcome = response.outcome.outcome
                if outcome == "selected":
                    logger.info("ACP permission auto-approved for tool call %s in session %s", tool_call.tool_call_id, session_id)
                else:
                    logger.warning("ACP permission denied for tool call %s in session %s (set auto_approve_permissions: true in config.yaml to enable)", tool_call.tool_call_id, session_id)
                return response

        client = _CollectingClient()
        cmd = _agent_attr(agent_config, "command")
        args = _agent_attr(agent_config, "args", []) or []
        physical_cwd = _get_work_dir(thread_id)
        try:
            mcp_servers = _build_acp_mcp_servers()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Invalid MCP server configuration for ACP agent '%s'; continuing without MCP servers: %s", agent, exc)
            mcp_servers = []
        agent_env: dict[str, str] | None = None
        env_cfg = _agent_attr(agent_config, "env")
        if env_cfg:
            agent_env = {k: (os.environ.get(v[1:], "") if isinstance(v, str) and v.startswith("$") else v) for k, v in env_cfg.items()}

        try:
            from acp import spawn_agent_process

            async with spawn_agent_process(client, cmd, *args, env=agent_env, cwd=physical_cwd) as (conn, proc):
                logger.info("Spawning ACP agent '%s' with command '%s' and args %s in cwd %s", agent, cmd, args, physical_cwd)
                await conn.initialize(
                    protocol_version=PROTOCOL_VERSION,
                    client_capabilities=ClientCapabilities(),
                    client_info=Implementation(name="deerflow", title="DeerFlow", version="0.1.0"),
                )
                session_kwargs: dict[str, Any] = {"cwd": physical_cwd, "mcp_servers": mcp_servers}
                model = _agent_attr(agent_config, "model")
                if model:
                    session_kwargs["model"] = model
                session = await conn.new_session(**session_kwargs)
                await conn.prompt(session_id=session.session_id, prompt=[text_block(prompt)])
            result = client.collected_text
            logger.info("ACP agent '%s' returned %d characters", agent, len(result))
            return result or "(no response)"
        except Exception as e:  # noqa: BLE001
            logger.error("ACP agent '%s' invocation failed: %s", agent, e)
            return _format_invocation_error(agent, cmd, e)

    return StructuredTool.from_function(
        name="invoke_acp_agent",
        description=description,
        coroutine=_invoke,
        args_schema=_InvokeACPAgentInput,
    )
