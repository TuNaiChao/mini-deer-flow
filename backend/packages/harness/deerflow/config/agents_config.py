"""自定义 agent 配置与加载器（M22 agents_config）。

自定义 agent = 一份 **SOUL.md**（人格 / 价值观 / 行为约束，注入 lead agent 的系统提示）
+ 一份 **config.yaml**（工具组 / 技能白名单 / 模型 / 描述）。用户可在对话里用
``setup_agent``（仅引导回合）创建、用 ``update_agent``（仅自定义 agent 回合）自更新，
见 M15 工具与 M17 lead_agent 的 custom-agent 分支。

**per-user 隔离**：每个用户的 agent 存在各自的
``{base_dir}/users/{user_id}/agents/{name}/``，互不影响。一份 **legacy 共享布局**
``{base_dir}/agents/{name}/`` 作为只读回退，兼容 per-user-isolation 之前（或未跑
``migrate_user_isolation.py``）的安装——新写一律走 per-user。

对齐 deer-flow ``config/agents_config.py``，全面对标（v1.2）：``SOUL_FILENAME`` /
``AGENT_NAME_PATTERN`` / ``AgentConfig`` / ``validate_agent_name`` / ``resolve_agent_dir``
/ ``load_agent_config`` / ``load_agent_soul`` / ``list_custom_agents`` 全部 1:1 移植。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from deerflow.config.paths import get_paths
from deerflow.runtime.user_context import get_effective_user_id

logger = logging.getLogger(__name__)

#: 自定义 agent 人格文件名（注入 lead agent 系统提示）。
SOUL_FILENAME = "SOUL.md"

#: agent 名称允许的字符集：字母 + 数字 + 连字符。被 setup/update_agent 工具、
#: memory storage、client 共用（红线 #32）——校验后再拼文件系统路径，防穿越 / 注入。
AGENT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9-]+$")


def validate_agent_name(name: str | None) -> str | None:
    """校验自定义 agent 名称，可用于文件系统路径前。

    - ``None`` → ``None``（表示「默认 agent」，合法）。
    - 非字符串 → ``ValueError``。
    - 不匹配 :data:`AGENT_NAME_PATTERN`（含空格 / 下划线 / 斜杠 / 空 …）→ ``ValueError``。

    通过校验的原样返回（保留大小写；磁盘目录的小写归一在 :class:`Paths` 里做）。
    """
    if name is None:
        return None
    if not isinstance(name, str):
        raise ValueError("Invalid agent name. Expected a string or None.")
    if not AGENT_NAME_PATTERN.fullmatch(name):
        raise ValueError(f"Invalid agent name '{name}'. Must match pattern: {AGENT_NAME_PATTERN.pattern}")
    return name


class AgentConfig(BaseModel):
    """单个自定义 agent 的配置（读自 config.yaml）。

    - ``name``：agent 名（校验后保留大小写；目录小写归一见 :class:`Paths`）。
    - ``description``：描述，给 lead agent 列可用 agent 时用。
    - ``model``：可选模型名覆盖；``None`` = 跟随全局默认。
    - ``tool_groups``：可选工具组白名单；``None`` = 全部工具组。
    - ``skills``：技能加载控制——

      * ``None``（缺省）：加载全部启用的技能（默认回退行为）；
      * ``[]``（显式空列表）：禁用全部技能；
      * ``["s1", "s2"]``：仅加载指定技能。
    """

    name: str
    description: str = ""
    model: str | None = None
    tool_groups: list[str] | None = None
    skills: list[str] | None = None


def resolve_agent_dir(name: str, *, user_id: str | None = None) -> Path:
    """返回 agent 在磁盘上的目录，per-user 优先、legacy 只读回退。

    解析顺序：

    1. ``{base_dir}/users/{user_id}/agents/{name}/``（per-user，当前布局）。
    2. ``{base_dir}/agents/{name}/``（legacy 共享布局，只读回退）。

    两个都要求**同时存在目录与 ``config.yaml``** 才算「真 agent 目录」——仅由 memory
    写入产生的目录（只有 ``memory.json``、没有 ``config.yaml``）不算（见 deer #3390：
    首轮对话会给某 agent 建一个只含 ``memory.json`` 的 per-user 目录，下一回合若把它当
    agent 目录就会读到「空配置」）。

    两个都不存在时返回 per-user 路径，让打算新建该 agent 的调用方写进新布局。

    Args:
        name: 已校验的 agent 名。
        user_id: agent 所有者；缺省取当前请求上下文的有效 user（无鉴权回退 ``"default"``）。
    """

    paths = get_paths()
    effective_user = user_id or get_effective_user_id()
    user_path = paths.user_agent_dir(effective_user, name)
    # 要求 config.yaml 才认：防 memory/storage 写残留的目录被误当 agent 目录（#3390）。
    if user_path.exists() and (user_path / "config.yaml").exists():
        return user_path

    legacy_path = paths.agent_dir(name)
    if legacy_path.exists() and (legacy_path / "config.yaml").exists():
        return legacy_path

    return user_path


def load_agent_config(name: str | None, *, user_id: str | None = None) -> AgentConfig | None:
    """加载自定义或默认 agent 的 config.yaml。

    先 per-user，再 legacy 回退（兼容未迁移安装）。

    Args:
        name: agent 名。
        user_id: 所有者；缺省取当前请求上下文的有效 user。

    Returns:
        :class:`AgentConfig`；``name`` 为 ``None`` 时返回 ``None``。

    Raises:
        FileNotFoundError: agent 目录或 config.yaml 不存在。
        ValueError: config.yaml 解析失败。
    """

    if name is None:
        return None

    name = validate_agent_name(name)
    agent_dir = resolve_agent_dir(name, user_id=user_id)
    config_file = agent_dir / "config.yaml"

    if not agent_dir.exists():
        raise FileNotFoundError(f"Agent directory not found: {agent_dir}")

    if not config_file.exists():
        raise FileNotFoundError(f"Agent config not found: {config_file}")

    try:
        with open(config_file, encoding="utf-8") as f:
            data: dict[str, Any] = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"Failed to parse agent config {config_file}: {e}") from e

    # config.yaml 没有 name 字段时，用目录名兜底。
    if "name" not in data:
        data["name"] = name

    # 喂给 pydantic 前剥未知字段（向前兼容），例如 legacy 的 prompt_file。
    known_fields = set(AgentConfig.model_fields.keys())
    data = {k: v for k, v in data.items() if k in known_fields}

    return AgentConfig(**data)


def load_agent_soul(agent_name: str | None, *, user_id: str | None = None) -> str | None:
    """读自定义 agent 的 SOUL.md（若存在）。

    SOUL.md 定义 agent 的人格 / 价值观 / 行为约束，注入 lead agent 系统提示作为附加上下文。

    Args:
        agent_name: agent 名；``None`` 表示默认 agent（读 ``base_dir`` 下的 SOUL.md）。
        user_id: 所有者；缺省取当前请求上下文的有效 user。

    Returns:
        SOUL.md 文本（已 strip）；文件不存在或内容为空（纯空白）时返回 ``None``。
    """
    if agent_name:
        agent_dir = resolve_agent_dir(agent_name, user_id=user_id)
    else:
        agent_dir = get_paths().base_dir
    soul_path = agent_dir / SOUL_FILENAME
    if not soul_path.exists():
        return None
    content = soul_path.read_text(encoding="utf-8").strip()
    return content or None


def list_custom_agents(*, user_id: str | None = None) -> list[AgentConfig]:
    """扫描 agent 目录，返回全部合法的自定义 agent。

    返回 per-user 布局与 legacy 共享布局的**并集**——未迁移安装在迁移前仍可见。
    同名时 **per-user 覆盖 legacy**（per-user 先扫，命中后进 ``seen``，legacy 同名跳过）。

    Args:
        user_id: 列谁名下的 agent；缺省取当前请求上下文的有效 user。

    Returns:
        合法 agent 目录对应的 :class:`AgentConfig` 列表，按 ``name`` 升序。
    """
    paths = get_paths()
    effective_user = user_id or get_effective_user_id()

    seen: set[str] = set()
    agents: list[AgentConfig] = []

    user_root = paths.user_agents_dir(effective_user)
    legacy_root = paths.agents_dir

    for root in (user_root, legacy_root):
        if not root.exists():
            continue
        for entry in sorted(root.iterdir()):
            if not entry.is_dir():
                continue
            if entry.name in seen:
                continue
            config_file = entry / "config.yaml"
            if not config_file.exists():
                logger.debug(f"Skipping {entry.name}: no config.yaml")
                continue

            try:
                agent_cfg = load_agent_config(entry.name, user_id=effective_user)
                if agent_cfg is None:
                    continue
                agents.append(agent_cfg)
                seen.add(entry.name)
            except Exception as e:
                logger.warning(f"Skipping agent '{entry.name}': {e}")

    agents.sort(key=lambda a: a.name)
    return agents
