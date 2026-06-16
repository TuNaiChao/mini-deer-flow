"""
线程状态模块

ThreadState 继承 langchain 的 AgentState，
使用自定义 reducer 管理特殊字段的合并逻辑。
"""
from typing import Annotated, TypedDict, NotRequired

from langchain.agents import AgentState


def _merge_artifacts(existing: list[str] | None, new: list[str] | None) -> list[str]:
    """合并 artifacts 列表（去重保序）"""
    if existing is None and new is None:
        return []
    combined = (existing or []) + (new or [])
    return list(dict.fromkeys(combined))  # 去重保序


def _merge_todos(existing: list | None, new: list | None) -> list | None:
    """
    合并 todos：如果 new 不为 None，完全替换；
    否则保留 existing。空列表表示清空。
    """
    if new is not None:
        return new
    return existing


def _merge_viewed_images(existing: dict | None, new: dict | None) -> dict:
    """
    合并 viewed_images 字典：浅合并，new 覆盖 existing 的同名键。
    由 ViewImageMiddleware 在阶段5 使用。
    """
    if existing is None and new is None:
        return {}
    combined = dict(existing or {})
    combined.update(new or {})
    return combined


class ThreadState(AgentState):
    """
    Agent 的线程状态，继承 langchain 的 AgentState。

    AgentState 已经包含了：
    - messages: list[BaseMessage]  （消息历史，由 LangGraph 自动管理）
    - （其他 langchain 内部字段）

    我们添加的扩展字段用于中间件和工具间的数据传递。
    """

    # --- 沙箱状态 ---
    sandbox: NotRequired[dict | None]
    """沙箱信息: {"sandbox_id": str | None}"""

    # --- 线程数据 ---
    thread_data: NotRequired[dict | None]
    """线程的目录路径: {"workspace_path": str, "uploads_path": str, "outputs_path": str}"""

    # --- 标题 ---
    title: NotRequired[str | None]
    """线程标题（由 TitleMiddleware 自动生成）"""

    # --- Artifacts ---
    artifacts: NotRequired[Annotated[list[str], _merge_artifacts]]
    """Agent 输出的文件路径列表（使用去重 reducer）"""

    # --- Todos ---
    todos: NotRequired[Annotated[list | None, _merge_todos]]
    """待办事项列表（使用替换 reducer）"""

    # --- 上传文件 ---
    uploaded_files: NotRequired[list[dict] | None]
    """用户上传的文件信息列表"""

    # --- 多模态图片 ---
    viewed_images: NotRequired[Annotated[dict, _merge_viewed_images]]
    """Agent 已查看的图片（Base64 编码），由 ViewImageMiddleware 写入（阶段5）"""