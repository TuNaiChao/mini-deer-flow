"""声明式 feature flag + 中间件定位装饰器，供 ``create_deerflow_agent`` 用。

纯数据类 + 装饰器——没有 IO、没有副作用。``RuntimeFeatures`` 把「这个 agent 要
开哪些行为」用一组 flag 表达；``@Next`` / ``@Prev`` 给自定义中间件声明「插在链里
哪个锚点旁边」，让 ``_insert_extra`` 能定位插入位置（见 [factory.py](factory.py)）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from langchain.agents.middleware import AgentMiddleware


@dataclass
class RuntimeFeatures:
    """``create_deerflow_agent`` 的声明式 feature flag。

    大多数 feature 接受三类值：

    - ``True``：用内置的默认中间件；
    - ``False``：关掉；
    - 一个 ``AgentMiddleware`` 实例：用这个自定义实现替换默认。

    ``summarization`` 和 ``guardrail`` 没有内置默认——只接受 ``False``（关）或一个
    ``AgentMiddleware`` 实例（自定义）。
    """

    sandbox: bool | AgentMiddleware = True
    memory: bool | AgentMiddleware = False
    summarization: Literal[False] | AgentMiddleware = False
    subagent: bool | AgentMiddleware = False
    vision: bool | AgentMiddleware = False
    auto_title: bool | AgentMiddleware = False
    guardrail: Literal[False] | AgentMiddleware = False
    loop_detection: bool | AgentMiddleware = True
    token_budget: bool | AgentMiddleware = False


# ---------------------------------------------------------------------------
# 中间件定位装饰器
# ---------------------------------------------------------------------------


def Next(anchor: type[AgentMiddleware]):
    """声明本中间件应排在 *anchor* **之后**（紧随其后）。"""
    if not (isinstance(anchor, type) and issubclass(anchor, AgentMiddleware)):
        raise TypeError(f"@Next expects an AgentMiddleware subclass, got {anchor!r}")

    def decorator(cls: type[AgentMiddleware]) -> type[AgentMiddleware]:
        cls._next_anchor = anchor  # type: ignore[attr-defined]
        return cls

    return decorator


def Prev(anchor: type[AgentMiddleware]):
    """声明本中间件应排在 *anchor* **之前**（紧贴其前）。"""
    if not (isinstance(anchor, type) and issubclass(anchor, AgentMiddleware)):
        raise TypeError(f"@Prev expects an AgentMiddleware subclass, got {anchor!r}")

    def decorator(cls: type[AgentMiddleware]) -> type[AgentMiddleware]:
        cls._prev_anchor = anchor  # type: ignore[attr-defined]
        return cls

    return decorator
