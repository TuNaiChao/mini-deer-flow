"""Agent 模块——提供 Agent 的创建和管理。

双入口：

- [create_deerflow_agent](factory.py)：SDK 级纯参数化入口（features 驱动）；
- [make_lead_agent](lead_agent/agent.py)：config 驱动的应用工厂（LangGraph 图入口）。
"""

from .factory import create_deerflow_agent
from .features import Next, Prev, RuntimeFeatures
from .lead_agent import make_lead_agent
from .lead_agent.prompt import prime_enabled_skills_cache
from .thread_state import PromotedTools, SandboxState, ThreadDataState, ThreadState, ViewedImageData

# LangGraph 注册图时会 import deerflow.agents。这里预热 enabled-skills 缓存，
# 让请求路径通常能读到热缓存，不必在 prompt 模块 import 时强制同步扫盘。
prime_enabled_skills_cache()

__all__ = [
    "create_deerflow_agent",
    "RuntimeFeatures",
    "Next",
    "Prev",
    "make_lead_agent",
    "SandboxState",
    "ThreadDataState",
    "ViewedImageData",
    "PromotedTools",
    "ThreadState",
]
