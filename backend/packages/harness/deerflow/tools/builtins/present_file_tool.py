"""present_files 工具——把 agent 生成的输出文件展示给用户。

M15（2026-06-27）对齐上游重写：
1. **多文件**：一次调 ``present_files(filepaths=[...])`` 展示多个文件（旧版只收单文件）。
2. **路径归一化 + 安全校验**：每个路径先解析到**物理宿主路径**，再强制落在当前线程的
   ``outputs_path`` 之下（挡 ``..`` 穿越与乱指路径），最后回写成规范的虚拟路径
   ``/mnt/user-data/outputs/<相对>`` 写进 artifacts。旧版只做 ``startswith`` 前缀检查，
   会被 ``/mnt/user-data/outputs/../../etc/passwd`` 这类路径骗过。
3. **失败不抛**：路径不合法时返回 ``ToolMessage`` 报错（不中断 run），成功也回一条
   ``ToolMessage``——与其它内置工具（view_image / tool_search）的反馈风格一致。

这是「需要访问线程上下文」的工具范例——通过 ``runtime`` 参数拿到 thread_id、user_id、state。
"""

from pathlib import Path
from typing import Annotated

from langchain.tools import InjectedToolCallId, tool
from langchain_core.messages import ToolMessage
from langgraph.config import get_config
from langgraph.types import Command

from deerflow.config.paths import VIRTUAL_PATH_PREFIX, get_paths
from deerflow.runtime.user_context import get_effective_user_id
from deerflow.tools.types import Runtime

# 允许展示的虚拟路径前缀（沙箱内视角）：``/mnt/user-data/outputs``。
OUTPUTS_VIRTUAL_PREFIX = f"{VIRTUAL_PATH_PREFIX}/outputs"


def _get_thread_id(runtime: Runtime) -> str | None:
    """三级回退取当前 thread_id：runtime.context → runtime.config → LangGraph RunnableConfig。

    同步 / 异步两条调用路径注入方式不同，故逐级尝试；都拿不到返回 None（调用方报错）。
    """
    thread_id = runtime.context.get("thread_id") if runtime.context else None
    if thread_id:
        return thread_id

    runtime_config = getattr(runtime, "config", None) or {}
    thread_id = runtime_config.get("configurable", {}).get("thread_id")
    if thread_id:
        return thread_id

    try:
        return get_config().get("configurable", {}).get("thread_id")
    except RuntimeError:
        # 无 RunnableConfig 上下文（如非 LangGraph 驱动的直接调用）。
        return None


def _normalize_presented_filepath(runtime: Runtime, filepath: str) -> str:
    """把一条待展示路径归一化为 ``/mnt/user-data/outputs/<相对>`` 虚拟路径。

    接受两种输入：
    - **虚拟沙箱路径**：如 ``/mnt/user-data/outputs/report.md``（agent 视角）；
    - **宿主侧线程输出路径**：如 ``/app/.../users/<uid>/threads/<tid>/user-data/outputs/report.md``。

    流程：解析到物理路径 → 校验落在当前线程 ``outputs_path`` 之下 → 回写规范虚拟路径。
    任何一步失败抛 ``ValueError``（由调用方转成 ``ToolMessage``，不中断 run）。
    """
    if runtime.state is None:
        raise ValueError("Thread runtime state is not available")

    thread_id = _get_thread_id(runtime)
    if not thread_id:
        raise ValueError("Thread ID is not available in runtime context or runtime config")

    thread_data = runtime.state.get("thread_data") or {}
    outputs_path = thread_data.get("outputs_path")
    if not outputs_path:
        raise ValueError("Thread outputs path is not available in runtime state")

    outputs_dir = Path(outputs_path).resolve()
    stripped = filepath.lstrip("/")
    virtual_prefix = VIRTUAL_PATH_PREFIX.lstrip("/")

    if stripped == virtual_prefix or stripped.startswith(virtual_prefix + "/"):
        # 虚拟路径：经 Paths.resolve_virtual_path 解析到物理（含穿越校验），user_id 必传。
        actual_path = get_paths().resolve_virtual_path(thread_id, filepath, user_id=get_effective_user_id())
    else:
        # 宿主侧绝对路径：直接解析（expanduser 兼容 ~）。
        actual_path = Path(filepath).expanduser().resolve()

    try:
        relative_path = actual_path.relative_to(outputs_dir)
    except ValueError as exc:
        raise ValueError(f"Only files in {OUTPUTS_VIRTUAL_PREFIX} can be presented: {filepath}") from exc

    return f"{OUTPUTS_VIRTUAL_PREFIX}/{relative_path.as_posix()}"


@tool("present_files", parse_docstring=True)
def present_file_tool(
    runtime: Runtime,
    filepaths: list[str],
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:
    """Make files visible to the user for viewing and rendering in the client interface.

    When to use the present_files tool:

    - Making any file available for the user to view, download, or interact with
    - Presenting multiple related files at once
    - After creating files that should be presented to the user

    When NOT to use the present_files tool:

    - When you only need to read file contents for your own processing
    - For temporary or intermediate files not meant for user viewing

    Notes:
    - You should call this tool after creating files and moving them to the `/mnt/user-data/outputs` directory.
    - This tool can be safely called in parallel with other tools. State updates are handled by a reducer to prevent conflicts.

    Args:
        filepaths: 要展示的文件**绝对路径列表**。**只有** ``/mnt/user-data/outputs`` 下的文件
            能展示；不在该目录下的会被拒绝并返回错误消息。runtime 与 tool_call_id 由
            LangGraph 自动注入，模型不可见。
    """
    try:
        normalized_paths = [_normalize_presented_filepath(runtime, filepath) for filepath in filepaths]
    except ValueError as exc:
        # 路径不合法：返回 ToolMessage 报错，不中断 run（对齐 view_image / tool_search 风格）。
        return Command(
            update={"messages": [ToolMessage(f"Error: {exc}", tool_call_id=tool_call_id)]},
        )

    # merge_artifacts reducer 会去重合并。
    return Command(
        update={
            "artifacts": normalized_paths,
            "messages": [ToolMessage("Successfully presented files", tool_call_id=tool_call_id)],
        },
    )
