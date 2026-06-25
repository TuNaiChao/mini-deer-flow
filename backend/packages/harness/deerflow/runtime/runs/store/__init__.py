"""run 元数据存储接口 + 实现。

两个实现：

- ``RunRepository``（SQL）：``deerflow.persistence.run.sql``，继承本 ABC。
- ``MemoryRunStore``（内存）：开发 / 测试 / ``database.backend=memory`` 默认用。

之所以把 ABC 提前到 Phase 1，是因为 ``RunRepository(RunStore)`` 要继承它
（详见模块 ``base.py`` 的说明）——把 ABC 提前即打破「持久化 → 运行管理」的循环依赖。
"""

from deerflow.runtime.runs.store.base import RunStore
from deerflow.runtime.runs.store.memory import MemoryRunStore

__all__ = ["RunStore", "MemoryRunStore"]
