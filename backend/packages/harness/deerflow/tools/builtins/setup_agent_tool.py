"""setup_agent 工具——初始化阶段持久化新自定义 agent 的 SOUL.md + config.yaml。

对齐 deer ``tools/builtins/setup_agent_tool.py``。**仅在 is_bootstrap=True 时绑定**（由 M17 lead_agent
factory 决定，非 ``get_available_tools``）。写 SOUL.md + config.yaml 到 per-user 目录
（``{base_dir}/users/{user_id}/agents/{name}/``），依赖 M22 agents_config。

空 soul 在碰文件系统前就拒绝（防 #3549：空 SOUL.md 覆盖全局默认）。失败时若目录是本次新建的，
回滚（删目录）。
"""

import logging
import shutil

import yaml
from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from langgraph.types import Command

from deerflow.config.agents_config import validate_agent_name
from deerflow.config.paths import get_paths
from deerflow.runtime.user_context import resolve_runtime_user_id
from deerflow.tools.types import Runtime

logger = logging.getLogger(__name__)


@tool(parse_docstring=True)
def setup_agent(
    soul: str,
    description: str,
    runtime: Runtime,
    skills: list[str] | None = None,
) -> Command:
    """Setup the custom DeerFlow agent.

    Args:
        soul: Full SOUL.md content defining the agent's personality and behavior.
        description: One-line description of what the agent does.
        skills: Optional list of skill names this agent should use. None means use all enabled skills, empty list means no skills.
    """
    # 空 / 纯空白 soul 在碰文件系统前拒绝——否则会持久化空 SOUL.md 还报成功，让前端进入
    # 「agent 已创建」的不可用状态（#3549）。响亮失败让模型重试，而非静默产出坏工件。
    if not soul or not soul.strip():
        return Command(
            update={
                "messages": [
                    ToolMessage(
                        content="Error: soul content is empty; refusing to create agent with an empty SOUL.md",
                        tool_call_id=runtime.tool_call_id,
                    )
                ]
            }
        )

    agent_name: str | None = runtime.context.get("agent_name") if runtime.context else None
    agent_dir = None
    is_new_dir = False

    try:
        agent_name = validate_agent_name(agent_name)
        paths = get_paths()
        if agent_name:
            # 自定义 agent 持久化到当前用户的桶里，不同用户互不可见。
            user_id = resolve_runtime_user_id(runtime)
            agent_dir = paths.user_agent_dir(user_id, agent_name)
        else:
            # 默认 agent（无 agent_name）：SOUL.md 在全局 base_dir。
            agent_dir = paths.base_dir
        is_new_dir = not agent_dir.exists()
        agent_dir.mkdir(parents=True, exist_ok=True)

        if agent_name:
            config_data: dict = {"name": agent_name}
            if description:
                config_data["description"] = description
            if skills is not None:
                config_data["skills"] = skills

            config_file = agent_dir / "config.yaml"
            with open(config_file, "w", encoding="utf-8") as f:
                yaml.dump(config_data, f, default_flow_style=False, allow_unicode=True)

        soul_file = agent_dir / "SOUL.md"
        soul_file.write_text(soul, encoding="utf-8")

        logger.info("Created agent '%s' at %s", agent_name, agent_dir)
        return Command(
            update={
                "created_agent_name": agent_name,
                "messages": [ToolMessage(content=f"Agent '{agent_name}' created successfully!", tool_call_id=runtime.tool_call_id)],
            },
        )

    except Exception as e:
        if agent_name and is_new_dir and agent_dir is not None and agent_dir.exists():
            # 仅当目录是本次调用新建的才清理（防误删已有数据）。
            shutil.rmtree(agent_dir)
        logger.error("Failed to create agent '%s': %s", agent_name, e, exc_info=True)
        return Command(update={"messages": [ToolMessage(content=f"Error: {e}", tool_call_id=runtime.tool_call_id)]})
