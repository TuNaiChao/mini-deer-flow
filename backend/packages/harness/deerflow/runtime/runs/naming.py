"""run 命名辅助——给 LangChain / LangSmith tracing 用。

决定一次 run 在 trace 里的**根名字**：优先用运行时上下文里的 ``agent_name``（自定义 agent
走自己的名），没有就用 ``assistant_id``，再没有就兜底 ``"lead_agent"``。这让不同 agent 的
trace 在 LangSmith / Langfuse 里能一眼区分。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def resolve_root_run_name(config: Mapping[str, Any], assistant_id: str | None) -> str:
    """解析 run 的根名字。

    按优先级查 ``context`` 和 ``configurable`` 两个容器里的 ``agent_name``（context 优先，
    因为 lead_agent 把 agent 名解析后写进了 context）。找到非空字符串就用它；否则用
    ``assistant_id``；都没有就 ``"lead_agent"``。
    """
    for container_name in ("context", "configurable"):
        container = config.get(container_name)
        if isinstance(container, Mapping):
            agent_name = container.get("agent_name")
            if isinstance(agent_name, str) and agent_name.strip():
                return agent_name
    return assistant_id or "lead_agent"
