"""``ThreadDataMiddleware``：为每个线程算 / 建隔离目录（M16）。

每个线程启动时算出 ``workspace`` / ``uploads`` / ``outputs`` 三个目录路径（按
(user_id, thread_id) 物理隔离），写进 ``state["thread_data"]``，供沙箱、上传、
``ToolOutputBudgetMiddleware``（外置大输出到 ``outputs_path``）共用。

``lazy_init=True``（默认）：只算路径不建目录（各消费者 mkdir on demand）。
``lazy_init=False``：``before_agent`` 立即建目录。

为何是链的第 2 步（在 SandboxMiddleware 之前）：SandboxMiddleware / ToolOutputBudget
都依赖 ``thread_data`` 里已写好的路径。

红线：路径算 / 建的**唯一真相源**是 :class:`Paths`（``sandbox_work_dir`` /
``sandbox_uploads_dir`` / ``sandbox_outputs_dir`` / ``ensure_thread_dirs``），本中间件
只读 Paths 不自拼，防 uploads / sandbox / 此处三处漂移。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import NotRequired, override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage
from langgraph.config import get_config
from langgraph.runtime import Runtime

from deerflow.config.paths import Paths, get_paths
from deerflow.runtime.user_context import get_effective_user_id

logger = logging.getLogger(__name__)


class ThreadDataMiddlewareState(AgentState):
    """兼容 ``ThreadState``：thread_data 是普通 dict。"""

    thread_data: NotRequired[dict | None]


class ThreadDataMiddleware(AgentMiddleware[ThreadDataMiddlewareState]):
    """为每次线程执行算 / 建线程数据目录。

    目录布局（唯一真相源见 :class:`Paths`）::

        {base_dir}/users/{user_id}/threads/{thread_id}/user-data/
            ├── workspace   ← /mnt/user-data/workspace（agent 写代码 / 文件）
            ├── uploads     ← /mnt/user-data/uploads（M23 上传，agent 读）
            └── outputs     ← /mnt/user-data/outputs（present_files / 外置大输出）
    """

    state_schema = ThreadDataMiddlewareState

    def __init__(self, base_dir: str | None = None, lazy_init: bool = True):
        """Args:
        base_dir: 运行时根目录；None 用 ``get_paths()`` 解析（``runtime_home``）。
        lazy_init: True 只算路径（默认，性能最优）；False 在 ``before_agent`` 立即建目录。
        """
        super().__init__()
        self._paths = Paths(base_dir) if base_dir else get_paths()
        self._lazy_init = lazy_init

    def _get_thread_paths(self, thread_id: str, *, user_id: str) -> dict[str, str]:
        return {
            "workspace_path": str(self._paths.sandbox_work_dir(thread_id, user_id=user_id)),
            "uploads_path": str(self._paths.sandbox_uploads_dir(thread_id, user_id=user_id)),
            "outputs_path": str(self._paths.sandbox_outputs_dir(thread_id, user_id=user_id)),
        }

    def _create_thread_directories(self, thread_id: str, *, user_id: str) -> dict[str, str]:
        self._paths.ensure_thread_dirs(thread_id, user_id=user_id)
        return self._get_thread_paths(thread_id, user_id=user_id)

    @override
    def before_agent(self, state: ThreadDataMiddlewareState, runtime: Runtime) -> dict | None:
        context = runtime.context or {}
        thread_id = context.get("thread_id")
        if thread_id is None:
            try:
                config = get_config()
            except RuntimeError:
                config = {}
            thread_id = config.get("configurable", {}).get("thread_id")

        if thread_id is None:
            raise ValueError("Thread ID is required in runtime context or config.configurable")

        user_id = get_effective_user_id()

        if self._lazy_init:
            paths = self._get_thread_paths(thread_id, user_id=user_id)
        else:
            paths = self._create_thread_directories(thread_id, user_id=user_id)
            logger.debug("Created thread data directories for thread %s", thread_id)

        # 给最后一条 HumanMessage 贴 run_id / 时间戳元数据（对齐 deer；供 journal / 审计用）。
        messages = list(state.get("messages", []))
        last_message = messages[-1] if messages else None

        if last_message and isinstance(last_message, HumanMessage):
            messages[-1] = HumanMessage(
                content=last_message.content,
                id=last_message.id,
                name=last_message.name or "user-input",
                additional_kwargs={
                    **last_message.additional_kwargs,
                    "run_id": context.get("run_id"),
                    "timestamp": datetime.now(UTC).isoformat(),
                },
            )

        return {"thread_data": {**paths}, "messages": messages}
