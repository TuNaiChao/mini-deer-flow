"""run 元数据存储接口（Phase 1：仅导出 ABC）。

具体实现分两个 Phase 落地：

- ``RunRepository``（SQL）：本 Phase 1，见 ``deerflow.persistence.run.sql``。
- ``MemoryRunStore``（内存）：Phase 8（运行管理层），届时在此导出。

之所以把 ABC 提前到 Phase 1，是因为 ``RunRepository(RunStore)`` 要继承它
（详见模块 ``base.py`` 的说明）。
"""

from deerflow.runtime.runs.store.base import RunStore

__all__ = ["RunStore"]
