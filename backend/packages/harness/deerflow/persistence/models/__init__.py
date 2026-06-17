"""ORM 模型注册入口。

导入本模块确保所有 ORM 模型注册到 ``Base.metadata``，这样 ``init_engine`` 的
``create_all`` 与未来的 Alembic autogenerate 都能发现每张表。

本 Phase 1 注册的模型（按 mini 裁剪：不含 deer 的 feedback / user /
channel_connections，那些本期不做）：

- :class:`RunEventRow` —— run 事件（消息 / 轨迹 / 生命周期）。
- :class:`RunRow` —— run 元数据。
- :class:`ThreadMetaRow` —— 线程元数据（归属 / 标题 / 状态）。

``RunEventRow`` 留在 ``deerflow.persistence.models.run_event``，因为它的存储实现
（``DbRunEventStore``）在 M6 的 ``runtime.events.store.db``，没有对应的实体目录。
``RunRow`` / ``ThreadMetaRow`` 分别在各自实体子包里。
"""

from deerflow.persistence.models.run_event import RunEventRow
from deerflow.persistence.run.model import RunRow
from deerflow.persistence.thread_meta.model import ThreadMetaRow

__all__ = [
    "RunEventRow",
    "RunRow",
    "ThreadMetaRow",
]
