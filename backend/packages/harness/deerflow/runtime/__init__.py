"""运行时公共基础设施（用户上下文等）。

当前仅包含 ``user_context``（用户隔离基石）。后续 Phase 会在此包下增加
checkpointer / events / journal / stream_bridge / serialization / runs 等运行时组件。
"""

from .user_context import (
    AUTO,
    DEFAULT_USER_ID,
    CurrentUser,
    get_current_user,
    get_effective_user_id,
    require_current_user,
    reset_current_user,
    resolve_runtime_user_id,
    resolve_user_id,
    set_current_user,
)

__all__ = [
    "AUTO",
    "DEFAULT_USER_ID",
    "CurrentUser",
    "get_current_user",
    "get_effective_user_id",
    "require_current_user",
    "reset_current_user",
    "resolve_runtime_user_id",
    "resolve_user_id",
    "set_current_user",
]
