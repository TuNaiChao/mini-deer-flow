"""请求级用户上下文（用户隔离基石）。

本模块持有一个 :class:`~contextvars.ContextVar`，由**集成层**（未来的
lifespan / 入口）或测试在「鉴权成功后」写入。memory / persistence / sandbox /
checkpointer 等需要 ``user_id`` 的模块，通过本模块读取当前用户，避免到处透传
``user_id`` 样板代码。

mini 没有独立的 app / gateway 层，因此本模块不 import 任何 app 代码；
:class:`CurrentUser` 定义为 :class:`typing.Protocol`——任何带 ``.id: str`` 属性
的对象都结构性地满足它，测试用 :class:`types.SimpleNamespace` 即可。

三态语义（仓库 ``user_id`` 形参的消费侧，persistence/memory 等会用到）
----------------------------------------------------------------
- ``AUTO``（模块私有哨兵，默认值）：从 contextvar 读；未设置时 raise
  :class:`RuntimeError`。
- 显式 ``str``：用给定值，覆盖 contextvar（测试 / 管理覆盖场景）。
- 显式 ``None``：不加 ``user_id`` 过滤——仅用于迁移脚本、管理 CLI 等有意
  绕过隔离的场景。

asyncio 语义
------------
``ContextVar`` 在 asyncio 下是**任务级**（task-local）而非线程级。每个请求
跑在自己的 task 里，天然隔离。``asyncio.create_task`` / ``asyncio.to_thread``
会**继承**父任务的上下文（通常正是想要的）；若某个后台任务**不该**看到前台
用户，用 :func:`contextvars.copy_context` 取一份干净副本再跑。
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Final, Protocol, runtime_checkable


@runtime_checkable
class CurrentUser(Protocol):
    """当前已鉴权用户的结构类型。

    任何带 ``.id: str`` 属性的对象都满足本协议（测试可用 SimpleNamespace）。
    """

    id: str


_current_user: Final[ContextVar[CurrentUser | None]] = ContextVar("deerflow_current_user", default=None)


def set_current_user(user: CurrentUser) -> Token[CurrentUser | None]:
    """为当前 async task 设置用户。

    返回一个 reset token，应在 ``finally`` 块里传给 :func:`reset_current_user`
    以恢复之前的上下文。
    """
    return _current_user.set(user)


def reset_current_user(token: Token[CurrentUser | None]) -> None:
    """把上下文恢复到 ``token`` 捕获时的状态。"""
    _current_user.reset(token)


def get_current_user() -> CurrentUser | None:
    """返回当前用户；未设置时返回 ``None``。

    任意上下文都可安全调用。供「没有用户也能继续」的代码路径（迁移脚本、
    公开端点）使用。
    """
    return _current_user.get()


def require_current_user() -> CurrentUser:
    """返回当前用户；未设置时 raise :class:`RuntimeError`。

    供「必须在鉴权上下文里调用」的仓库代码使用。错误信息措辞方便定位。
    """
    user = _current_user.get()
    if user is None:
        raise RuntimeError("repository accessed without user context")
    return user


# ---------------------------------------------------------------------------
# 有效 user_id 辅助（文件系统隔离用）
# ---------------------------------------------------------------------------

DEFAULT_USER_ID: Final[str] = "default"


def get_effective_user_id() -> str:
    """返回当前用户的 id 字符串；未设置时返回 :data:`DEFAULT_USER_ID`。

    与 :func:`require_current_user` 不同，本函数永不抛错——为文件系统路径解析
    设计（永远需要一个有效的用户桶）。
    """
    user = _current_user.get()
    if user is None:
        return DEFAULT_USER_ID
    return str(user.id)


def resolve_runtime_user_id(runtime: object | None) -> str:
    """工具 / 中间件取「有效 user_id」的单一真相源。

    解析顺序（权威性从高到低）：
      1. ``runtime.context["user_id"]``——由集成层从鉴权后的用户写入。这是唯一
         能在 contextvar 可能丢失的边界（请求 task 之外调度的后台任务、不 copy
         context 的 worker 池、未来的跨进程驱动）存活下来的来源。
      2. ``_current_user`` ContextVar——请求入口由集成层写入。对 task 内工作可靠；
         会被 asyncio 子任务和 ``ContextThreadPoolExecutor`` 复制。
      3. :data:`DEFAULT_USER_ID`——最后的兜底，让无鉴权的 CLI / 迁移 / 测试路径
         不抛错也能继续。

    持久化用户级状态的工具（自定义 agent / memory / uploads）必须调本函数，
    而非直接调 :func:`get_effective_user_id`，从而享受 ``runtime.context`` 通道。
    """
    context = getattr(runtime, "context", None)
    if isinstance(context, dict):
        ctx_user_id = context.get("user_id")
        if ctx_user_id:
            return str(ctx_user_id)
    return get_effective_user_id()


# ---------------------------------------------------------------------------
# 基于哨兵的 user_id 解析
# ---------------------------------------------------------------------------
#
# 仓库方法接受 keyword-only 的 ``user_id`` 形参，默认值为 ``AUTO``。
# 三种取值驱动不同行为；详见 :func:`resolve_user_id` 的 docstring。


class _AutoSentinel:
    """「从 contextvar 解析 user_id」的单例标记。"""

    _instance: _AutoSentinel | None = None

    def __new__(cls) -> _AutoSentinel:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "<AUTO>"


AUTO: Final[_AutoSentinel] = _AutoSentinel()


def resolve_user_id(
    value: str | None | _AutoSentinel,
    *,
    method_name: str = "repository method",
) -> str | None:
    """解析传给仓库方法的 ``user_id`` 形参。

    三态语义：
    - :data:`AUTO`（默认）：从 contextvar 读；无用户时 raise
      :class:`RuntimeError`。请求级调用的常见情况。
    - 显式 ``str``：用给定 id，覆盖任何 contextvar 值。供测试 / 管理覆盖流程用。
    - 显式 ``None``：不过滤——仓库应跳过 ``user_id`` WHERE 子句。仅用于迁移脚本、
      CLI 工具等有意绕过隔离的场景。
    """
    if isinstance(value, _AutoSentinel):
        user = _current_user.get()
        if user is None:
            raise RuntimeError(f"{method_name} called with user_id=AUTO but no user context is set; pass an explicit user_id, set the contextvar via auth, or opt out with user_id=None for migration/CLI paths.")
        # 在边界处 str()：User.id 类型上可能是 UUID，但持久层把 user_id 存成
        # String(64)，aiosqlite 无法把原生 UUID 绑定到 VARCHAR 列（"type 'UUID' is
        # not supported"）。这里按文档返回类型 str()，而非把类型变更扩散到每个调用方。
        return str(user.id)
    return value
