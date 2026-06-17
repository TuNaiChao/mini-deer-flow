"""runs 子系统的 Phase 1 基类层。

本包在 Phase 1 只落地「领域词汇表 + 存储 ABC」：

- :class:`RunStatus` / :class:`DisconnectMode`：状态与断连模式枚举。
- :class:`RunStore`：run 元数据存储的抽象接口（SQL 实现见
  ``deerflow.persistence.run.sql.RunRepository``）。

完整的运行管理层（RunManager / RunRecord / worker）在 Phase 8 落地，届时会在此
补充导出。提前导出 ABC 是为了打破「持久化 → 运行管理」的循环依赖：RunRepository
要继承 RunStore，而 RunStore 属于 runs 领域，但 runs 的运行管理又依赖持久化——
把 ABC 提到 Phase 1 即可让 RunRepository 先于 RunManager 存在。
"""

from deerflow.runtime.runs.schemas import DisconnectMode, RunStatus
from deerflow.runtime.runs.store import RunStore

__all__ = [
    "DisconnectMode",
    "RunStatus",
    "RunStore",
]
