"""线程状态模块。

``ThreadState`` 继承 langchain 的 ``AgentState``，用自定义 **reducer** 管理扩展字段
的合并语义。reducer 是 LangGraph 的合并协议：同一图步里多个节点都写同一个 key 时，
框架调 reducer 把它们合并成一个值。

为什么每个字段都要 reducer？

- **sandbox**：多个沙箱工具可能在同一图步懒初始化并经 ``Command(update=...)`` 写回
  同一个 ``sandbox_id``。LangGraph 需要显式 reducer 合并这些写。**不同 sandbox_id
  意味着隔离 / 生命周期 bug，所以 fail-closed**（红线 #16）——宁可抛错也不静默选一个。
- **promoted**：``tool_search`` 把命中的延迟工具写回这里。reducer 按 ``catalog_hash``
  scope，防止陈旧提升在工具目录改名后暴露成另一个工具。
- **artifacts / viewed_images / todos**：去重 / 浅合并 / 替换语义。
"""

from typing import Annotated, NotRequired, TypedDict

from langchain.agents import AgentState


class SandboxState(TypedDict):
    """沙箱状态——仅幂等写（同一线程只允许同一个 sandbox_id）。"""

    sandbox_id: NotRequired[str | None]


class ThreadDataState(TypedDict):
    """每线程隔离目录路径（workspace / uploads / outputs）。"""

    workspace_path: NotRequired[str | None]
    uploads_path: NotRequired[str | None]
    outputs_path: NotRequired[str | None]


class ViewedImageData(TypedDict):
    """一张已被查看的图片（view_image 工具写入，ViewImageMiddleware 注入）。"""

    base64: str
    mime_type: str


class PromotedTools(TypedDict):
    """延迟工具提升记录——按 ``catalog_hash`` scope（防陈旧提升，红线 #16）。"""

    catalog_hash: str
    names: list[str]


def merge_sandbox(existing: SandboxState | None, new: SandboxState | None) -> SandboxState | None:
    """sandbox reducer——只接受幂等写。

    多个沙箱工具可能在同一图步懒初始化，经 ``Command(update=...)`` 发出同一个
    ``sandbox_id``。LangGraph 需要显式 reducer 合并这种共享 key 的写。同一线程出现
    不同 sandbox_id 说明是生命周期 / 隔离 bug，所以 **fail-closed**——抛错而不是
    静默选一个（红线 #16）。
    """
    if new is None:
        return existing
    if existing is None:
        return new

    existing_id = existing.get("sandbox_id")
    new_id = new.get("sandbox_id")
    if existing_id == new_id:
        return existing
    raise ValueError(f"Conflicting sandbox state updates: {existing_id!r} != {new_id!r}")


def merge_artifacts(existing: list[str] | None, new: list[str] | None) -> list[str]:
    """artifacts reducer——合并去重保序。"""
    if existing is None:
        return new or []
    if new is None:
        return existing
    # dict.fromkeys 去重保序
    return list(dict.fromkeys(existing + new))


def merge_viewed_images(existing: dict[str, ViewedImageData] | None, new: dict[str, ViewedImageData] | None) -> dict[str, ViewedImageData]:
    """viewed_images reducer——浅合并，new 覆盖 existing 同名键。

    特例：``new={}``（空 dict）清空全部已查看图片——让中间件处理完后能重置状态。
    """
    if existing is None:
        return new or {}
    if new is None:
        return existing
    # 空 dict = 清空
    if len(new) == 0:
        return {}
    # 浅合并：new 覆盖 existing 的同名键
    return {**existing, **new}


def merge_todos(existing: list | None, new: list | None) -> list | None:
    """todos reducer——保留最后一个非 None 值。

    - ``new`` 为 None（节点没动 todos）→ 保留 ``existing``；
    - ``new`` 给了（哪怕是空 list）→ 显式更新，覆盖 ``existing``（空 list = 清空）。
    """
    if new is None:
        return existing
    return new


def merge_promoted(existing: PromotedTools | None, new: PromotedTools | None) -> PromotedTools | None:
    """延迟工具提升 reducer——按 ``catalog_hash`` scope。

    - ``new`` 为 None / 空 → 保留 existing（节点没动提升）；
    - ``catalog_hash`` 变了 → 整体替换，丢弃陈旧 names（防一条持久化的裸 name 在
      目录漂移后暴露成另一个工具）；
    - 同 ``catalog_hash`` → 求 names 并集，去重保序。
    """
    if not new:
        return existing
    if existing is None or existing.get("catalog_hash") != new["catalog_hash"]:
        return {
            "catalog_hash": new["catalog_hash"],
            "names": list(dict.fromkeys(new["names"])),
        }
    return {
        "catalog_hash": existing["catalog_hash"],
        "names": list(dict.fromkeys(existing["names"] + new["names"])),
    }


class ThreadState(AgentState):
    """Agent 的线程状态，继承 langchain 的 ``AgentState``。

    ``AgentState`` 已含 ``messages``（消息历史，LangGraph 自动管理）+ langchain 内部
    字段。下面是 mini 的扩展字段，reducer 决定同一图步多个写怎么合并。
    """

    # --- 沙箱状态（fail-closed reducer，红线 #16）---
    sandbox: Annotated[NotRequired[SandboxState | None], merge_sandbox]
    """沙箱信息：``{"sandbox_id": str | None}``。"""

    # --- 线程数据（每线程隔离目录路径）---
    thread_data: NotRequired[ThreadDataState | None]
    """``{"workspace_path", "uploads_path", "outputs_path"}``。"""

    # --- 标题（TitleMiddleware 自动生成）---
    title: NotRequired[str | None]

    # --- Artifacts（产物路径，去重 reducer）---
    artifacts: Annotated[list[str], merge_artifacts]
    """Agent 输出的文件路径列表。"""

    # --- Todos（plan_mode，替换 reducer）---
    todos: Annotated[list | None, merge_todos]
    """待办事项列表。"""

    # --- 上传文件 ---
    uploaded_files: NotRequired[list[dict] | None]
    """用户上传的文件信息列表。"""

    # --- 多模态图片（ViewImageMiddleware 写入）---
    viewed_images: Annotated[dict[str, ViewedImageData], merge_viewed_images]
    """Agent 已查看的图片（base64），``image_path -> {base64, mime_type}``。"""

    # --- 延迟工具提升（M15 tool_search / M16 DeferredToolFilterMiddleware）---
    promoted: Annotated[PromotedTools | None, merge_promoted]
    """``tool_search`` 把命中的延迟工具写回这里，供 DeferredToolFilterMiddleware 读取。

    形如 ``{"catalog_hash": str, "names": [str, ...]}``——catalog_hash 把提升记录
    scope 到本次工具目录，防陈旧提升暴露改名 / 漂移过的工具（M15）。DeferredToolFilter
    据此把已提升的延迟工具 schema 重新暴露给模型绑定。
    """
