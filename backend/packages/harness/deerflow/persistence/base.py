"""SQLAlchemy 声明式基类，自带通用 ``to_dict``。

所有 DeerFlow app 层 ORM 模型继承本 :class:`Base`。它通过 SQLAlchemy 的
``inspect()`` 提供通用的 ``to_dict()``，让每个模型不必各自写序列化逻辑。

LangGraph checkpointer 的表**不**由本 Base 管理——checkpointer 用它自己的
``BaseCheckpointSaver`` 元数据，物理上与 app 表分离（即使共用一个 .db 文件）。
"""

from __future__ import annotations

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """所有 DeerFlow app ORM 模型的基类。

    提供：
    - 通用的 ``to_dict()``：基于 SQLAlchemy 列检查，遍历映射列属性。
    - 标准的 ``__repr__()``：展示所有列值，便于调试。
    """

    def to_dict(self, *, exclude: set[str] | None = None) -> dict:
        """把 ORM 实例转成纯 dict。

        用 SQLAlchemy 的 ``inspect()`` 遍历映射的列属性。

        Args:
            exclude: 可选的、要省略的列键集合。

        Returns:
            所有映射列的 ``{column_key: value}`` 字典。
        """
        exclude = exclude or set()
        return {c.key: getattr(self, c.key) for c in sa_inspect(type(self)).mapper.column_attrs if c.key not in exclude}

    def __repr__(self) -> str:
        cols = ", ".join(f"{c.key}={getattr(self, c.key)!r}" for c in sa_inspect(type(self)).mapper.column_attrs)
        return f"{type(self).__name__}({cols})"
