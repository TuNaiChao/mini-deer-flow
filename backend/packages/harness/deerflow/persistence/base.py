"""SQLAlchemy 声明式基类，自带通用 ``to_dict``。

所有 DeerFlow app 层 ORM 模型继承本 :class:`Base`。它通过 SQLAlchemy 的
``inspect()`` 提供通用的 ``to_dict()``，让每个模型不必各自写序列化逻辑。

LangGraph checkpointer 的表**不**由本 Base 管理——checkpointer 用它自己的
``BaseCheckpointSaver`` 元数据，物理上与 app 表分离（即使共用一个 .db 文件）。
"""

from __future__ import annotations

from functools import cache

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import DeclarativeBase


@cache
def _column_keys(cls: type) -> tuple[str, ...]:
    """缓存某个 ORM 类的列键元组。

    SQLAlchemy 的 ``inspect(cls).mapper.column_attrs`` 每次都要走 mapper 内省，
    而 ``to_dict()`` / ``__repr__()`` 在序列化每一行时都会调（list 端点可能成百上千
    行）。列集合在类定义后就不变，所以按类缓存一份元组，把每次内省省掉。
    """
    return tuple(c.key for c in sa_inspect(cls).mapper.column_attrs)


class Base(DeclarativeBase):
    """所有 DeerFlow app ORM 模型的基类。

    提供：
    - 通用的 ``to_dict()``：基于 SQLAlchemy 列检查，遍历映射列属性。
    - 标准的 ``__repr__()``：展示所有列值，便于调试。
    """

    def to_dict(self, *, exclude: set[str] | None = None) -> dict:
        """把 ORM 实例转成纯 dict。

        用 SQLAlchemy 的 ``inspect()`` 遍历映射的列属性（结果按类缓存）。

        Args:
            exclude: 可选的、要省略的列键集合。

        Returns:
            所有映射列的 ``{column_key: value}`` 字典。
        """
        keys = _column_keys(type(self))
        if exclude:
            return {k: getattr(self, k) for k in keys if k not in exclude}
        return {k: getattr(self, k) for k in keys}

    def __repr__(self) -> str:
        cols = ", ".join(f"{k}={getattr(self, k)!r}" for k in _column_keys(type(self)))
        return f"{type(self).__name__}({cols})"
