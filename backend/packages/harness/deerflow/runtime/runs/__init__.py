"""runs 子系统——run 生命周期管理（对齐 LangGraph Platform API）。

Phase 1 只落地了「领域词汇表 + 存储 ABC」（RunStatus / DisconnectMode / RunStore），打破
「持久化 → 运行管理」的循环依赖。Phase 8（M18）补齐运行管理层：

- :class:`RunManager`：内存 run 注册表 + 可选 RunStore 后端，asyncio 锁 + busy 重试 + orphan
  恢复 + shutdown drain；
- :class:`RunRecord`：单次 run 的可变记录；
- :class:`RunContext` + :func:`run_agent`：后台 agent 执行（注入 runtime/journal、rollback 快照、
  abort、LLM 兜底）；
- :class:`MemoryRunStore`：内存 RunStore（默认 / 测试）。
"""

from deerflow.runtime.runs.manager import ConflictError, RunManager, RunRecord, UnsupportedStrategyError
from deerflow.runtime.runs.schemas import DisconnectMode, RunStatus
from deerflow.runtime.runs.store import MemoryRunStore, RunStore
from deerflow.runtime.runs.worker import RunContext, run_agent

__all__ = [
    "ConflictError",
    "DisconnectMode",
    "MemoryRunStore",
    "RunContext",
    "RunManager",
    "RunRecord",
    "RunStatus",
    "RunStore",
    "UnsupportedStrategyError",
    "run_agent",
]
