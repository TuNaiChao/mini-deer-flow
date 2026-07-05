"""严格 blocking-IO 运行时检测上下文（纯 Python inline 实现，无第三方依赖）。

机制（等价于 blockbuster，但自实现以避免引入外部库）：
1. 进入上下文时，patch 一组同步阻塞原语（``open``/``os.stat``/``os.listdir``/``os.walk``/...），
   每个原语包一层 guard。
2. guard 被调用时检查两件事：
   a. **当前是否在运行中的 asyncio 事件循环里**（``asyncio.get_running_loop()``，
      不在循环里则直接放行——同步上下文的 IO 不算违规）；
   b. **调用栈是否经过 scanned_modules**（默认 ``deerflow``，遍历栈帧的 ``__name__``）。
   两者同时满足 → 抛 ``BlockingError``。
3. 退出上下文时（含异常路径）在 finally 里还原所有 patch，绝不污染全局。

效果：把「同步阻塞 IO 不能跑在事件循环里、尤其不能发自 deerflow 业务代码」变成
可断言的事实。测试基础设施（pytest/langchain/第三方库）自身触发的不算违规——
因为它们的栈帧不在 ``deerflow.*``。

由 ``test/blocking_io/conftest.py`` 在 ``pytest_runtest_protocol`` hookwrapper 中激活，
覆盖 setup + call + teardown 全流程。

对外接口与 blockbuster 等价：``detect_blocking_io_strict()`` + ``BlockingError``，
故 conftest / 测试代码不依赖具体实现。
"""

from __future__ import annotations

import asyncio
import builtins
import os
import select
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager


class BlockingError(RuntimeError):
    """在事件循环里、发自业务代码的同步阻塞 IO 调用。"""


# 默认视为「业务代码」的模块前缀。mini 无 app 层，只扫描 deerflow。
_DEFAULT_SCANNED_MODULES: tuple[str, ...] = ("deerflow",)

# 被 patch 的同步阻塞原语：(模块对象, 属性名)。
# 覆盖后续模块的文件 IO：open/stat/listdir/makedirs/walk 服务 jsonl/memory/skill/sqlite；
# sleep/select 作为通用阻塞原语。socket 系列不在此列（签名复杂，文件 IO 用不到）。
# ``os.getcwd`` 对齐 langgraph dev 运行期的 blockbuster 检测：``Path.cwd()`` 会落到
# 它，运行期发自 deerflow 的 getcwd 同样算阻塞（§1 冒烟曾因此暴露 get_app_config
# 在事件循环里 stat/getcwd 的违规）。
_BLOCKING_TARGETS: tuple[tuple[object, str], ...] = (
    (builtins, "open"),
    (os, "stat"),
    (os, "lstat"),
    (os, "fstat"),
    (os, "listdir"),
    (os, "scandir"),
    (os, "read"),
    (os, "write"),
    (os, "mkdir"),
    (os, "makedirs"),
    (os, "walk"),
    (os, "getcwd"),
    (time, "sleep"),
    (select, "select"),
)

# 栈遍历深度上限，防御异常深栈导致的长扫描。
_MAX_STACK_DEPTH = 50


def _caller_in_scope(start_frame: object, scanned_modules: tuple[str, ...]) -> bool:
    """从 start_frame 向上遍历调用栈，检查是否经过 scanned_modules。"""
    frame = start_frame
    depth = 0
    while frame is not None and depth < _MAX_STACK_DEPTH:
        module = (frame.f_globals.get("__name__", "") or "") if hasattr(frame, "f_globals") else ""
        for prefix in scanned_modules:
            if module == prefix or module.startswith(prefix + "."):
                return True
        frame = frame.f_back if hasattr(frame, "f_back") else None
        depth += 1
    return False


def _make_guard(display_name: str, original_fn, scanned_modules: tuple[str, ...]):
    """为单个阻塞原语创建 guard 包装。guard 透传参数，仅做「在循环里 + 在业务栈里」判定。"""

    def guard(*args, **kwargs):
        # ① 不在运行中的事件循环里 → 同步上下文，放行（guard 自身绝不能阻塞/递归）。
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return original_fn(*args, **kwargs)

        # ② 在事件循环里：若调用栈经过业务代码，判定为违规。
        # sys._getframe(1) = guard 的直接调用者，从它开始向上找业务帧。
        try:
            caller = sys._getframe(1)
        except ValueError:  # pragma: no cover - guard 必有调用者
            caller = None
        if caller is not None and _caller_in_scope(caller, scanned_modules):
            raise BlockingError(f"同步阻塞 IO '{display_name}' 在事件循环里被调用，且调用栈经过业务代码 {scanned_modules}。请用 asyncio.to_thread 卸载。")
        return original_fn(*args, **kwargs)

    return guard


@contextmanager
def detect_blocking_io_strict(scanned_modules=_DEFAULT_SCANNED_MODULES) -> Iterator[None]:
    """激活严格 blocking-IO 检测上下文。

    Args:
        scanned_modules: 视为业务代码的模块前缀；仅这些模块发起的阻塞 IO 会被判定违规。

    退出时自动还原所有 patch，异常路径也还原。
    """
    scanned = tuple(scanned_modules)
    saved: dict[tuple[object, str], object] = {}

    try:
        for module, attr in _BLOCKING_TARGETS:
            display_name = f"{getattr(module, '__name__', module)}.{attr}"
            original_fn = getattr(module, attr)
            saved[(module, attr)] = original_fn
            setattr(module, attr, _make_guard(display_name, original_fn, scanned))
        yield
    finally:
        for (module, attr), original_fn in saved.items():
            setattr(module, attr, original_fn)


__all__ = ["BlockingError", "detect_blocking_io_strict"]
