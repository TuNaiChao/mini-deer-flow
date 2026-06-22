"""同 (沙箱, 路径) 写操作的串行化锁。

为什么要这个锁？

- ``write_file`` / ``str_replace`` 是「读-改-写」组合：先读旧内容、再写新内容。
- 若同一进程内两个工具调用并发改**同一文件**（同沙箱同路径），可能互相覆盖丢数据。
- 所以按 ``(sandbox_id, path)`` 维度取一把 ``threading.Lock``，让同一文件的写串行；
  不同沙箱 / 不同路径互不争用，并发度不受影响。

为什么用 ``WeakValueDictionary``？

- key 是 ``(sandbox_id, path)``，长跑进程里 thread_id 无界增长，锁对象也会无界增长。
- ``WeakValueDictionary`` 在锁无强引用（没有任何线程持有 / 等待它）时自动回收条目，
  避免内存泄漏。``_FILE_OPERATION_LOCKS_GUARD`` 保护「取 or 建锁」的 check-then-insert。
"""

from __future__ import annotations

import threading
import weakref

from deerflow.sandbox.sandbox import Sandbox

# key = (sandbox_id, path)。锁无引用时自动回收，长跑进程不泄漏。
_LockKey = tuple[str, str]
_FILE_OPERATION_LOCKS: weakref.WeakValueDictionary[_LockKey, threading.Lock] = weakref.WeakValueDictionary()
_FILE_OPERATION_LOCKS_GUARD = threading.Lock()


def get_file_operation_lock_key(sandbox: Sandbox, path: str) -> tuple[str, str]:
    """计算某 (沙箱, 路径) 的锁 key。sandbox 无 id 时回退到对象 id 隔离。"""
    sandbox_id = getattr(sandbox, "id", None)
    if not sandbox_id:
        sandbox_id = f"instance:{id(sandbox)}"
    return sandbox_id, path


def get_file_operation_lock(sandbox: Sandbox, path: str) -> threading.Lock:
    """取某 (沙箱, 路径) 的写锁（不存在则建）。同一 key 永远返回同一把锁。"""
    lock_key = get_file_operation_lock_key(sandbox, path)
    with _FILE_OPERATION_LOCKS_GUARD:
        lock = _FILE_OPERATION_LOCKS.get(lock_key)
        if lock is None:
            lock = threading.Lock()
            _FILE_OPERATION_LOCKS[lock_key] = lock
        return lock
