"""子代理委派系统（M11）。

导出配置 / 执行器 / 注册表查询。真实 agent 构造依赖 Phase 7，但本包的注册表、
状态契约、执行机制（单 scheduler pool + 持久化隔离事件循环）在 Phase 2 即可用。
"""

from .config import SubagentConfig
from .executor import (
    MAX_CONCURRENT_SUBAGENTS,
    SubagentExecutor,
    SubagentResult,
    SubagentStatus,
    cleanup_background_task,
    get_background_task_result,
    list_background_tasks,
    request_cancel_background_task,
)
from .registry import (
    get_available_subagent_names,
    get_subagent_config,
    get_subagent_names,
    list_subagents,
)

__all__ = [
    "SubagentConfig",
    "SubagentExecutor",
    "SubagentResult",
    "SubagentStatus",
    "MAX_CONCURRENT_SUBAGENTS",
    "get_subagent_config",
    "get_subagent_names",
    "get_available_subagent_names",
    "list_subagents",
    "request_cancel_background_task",
    "get_background_task_result",
    "list_background_tasks",
    "cleanup_background_task",
]
