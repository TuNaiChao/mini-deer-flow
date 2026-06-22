"""带去抖的记忆更新队列（M13 memory）。

对齐 deer ``agents/memory/queue.py`` 1:1。

核心机制：
- **去抖**（debounce）：``add`` 后等 ``debounce_seconds``（默认 30s）再处理；窗口内多次
  ``add`` 同一 ``(thread_id, user_id, agent_name)`` 会**合并**为一条（最新消息覆盖旧的，
  correction/reinforcement 标志取或）。
- **user_id 跨 Timer 捕获**：``add`` 时把 ``user_id`` 存进 ``ConversationContext``——
  ``threading.Timer`` 在另一线程触发，ContextVar 不会跨裸线程传播，必须显式存。
- **add_nowait**：立即处理（0s 定时器），供 summarization_hook 等需要在摘要前抢拍的场景。
- 处理时延迟导入 ``MemoryUpdater``（防循环依赖），逐条调 ``update_memory``。
"""

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from deerflow.config.memory_config import get_memory_config

logger = logging.getLogger(__name__)


@dataclass
class ConversationContext:
    """待处理的一轮对话上下文。"""

    thread_id: str
    messages: list[Any]
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    agent_name: str | None = None
    user_id: str | None = None
    correction_detected: bool = False
    reinforcement_detected: bool = False


class MemoryUpdateQueue:
    """带去抖的记忆更新队列。

    收集对话上下文，经可配置的去抖窗口后批量处理。去抖窗口内同一目标的多次入队合并为一条。
    """

    def __init__(self) -> None:
        self._queue: list[ConversationContext] = []
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._processing = False

    @staticmethod
    def _queue_key(
        thread_id: str,
        user_id: str | None,
        agent_name: str | None,
    ) -> tuple[str, str | None, str | None]:
        """返回一次记忆更新目标的去抖身份。"""
        return (thread_id, user_id, agent_name)

    def add(
        self,
        thread_id: str,
        messages: list[Any],
        agent_name: str | None = None,
        user_id: str | None = None,
        correction_detected: bool = False,
        reinforcement_detected: bool = False,
    ) -> None:
        """入队一轮对话，重置去抖定时器。

        Args:
            thread_id: 线程 ID。
            messages: 对话消息。
            agent_name: 非 None 时存 per-agent 记忆；None 用全局记忆。
            user_id: 入队时捕获的 user id，存进 ConversationContext 以跨 ``threading.Timer``
                边界存活（ContextVar 不跨裸线程传播）。
            correction_detected: 近轮是否含显式纠正信号。
            reinforcement_detected: 近轮是否含正向强化信号。
        """
        config = get_memory_config()
        if not config.enabled:
            return

        with self._lock:
            self._enqueue_locked(
                thread_id=thread_id,
                messages=messages,
                agent_name=agent_name,
                user_id=user_id,
                correction_detected=correction_detected,
                reinforcement_detected=reinforcement_detected,
            )
            self._reset_timer()

        logger.info("Memory update queued for thread %s, queue size: %d", thread_id, len(self._queue))

    def add_nowait(
        self,
        thread_id: str,
        messages: list[Any],
        agent_name: str | None = None,
        user_id: str | None = None,
        correction_detected: bool = False,
        reinforcement_detected: bool = False,
    ) -> None:
        """入队并立即在后台开始处理（0s 定时器）。"""
        config = get_memory_config()
        if not config.enabled:
            return

        with self._lock:
            self._enqueue_locked(
                thread_id=thread_id,
                messages=messages,
                agent_name=agent_name,
                user_id=user_id,
                correction_detected=correction_detected,
                reinforcement_detected=reinforcement_detected,
            )
            self._schedule_timer(0)

        logger.info("Memory update queued for immediate processing on thread %s, queue size: %d", thread_id, len(self._queue))

    def _enqueue_locked(
        self,
        *,
        thread_id: str,
        messages: list[Any],
        agent_name: str | None,
        user_id: str | None,
        correction_detected: bool,
        reinforcement_detected: bool,
    ) -> None:
        queue_key = self._queue_key(thread_id, user_id, agent_name)
        existing_context = next(
            (context for context in self._queue if self._queue_key(context.thread_id, context.user_id, context.agent_name) == queue_key),
            None,
        )
        merged_correction_detected = correction_detected or (existing_context.correction_detected if existing_context is not None else False)
        merged_reinforcement_detected = reinforcement_detected or (existing_context.reinforcement_detected if existing_context is not None else False)
        context = ConversationContext(
            thread_id=thread_id,
            messages=messages,
            agent_name=agent_name,
            user_id=user_id,
            correction_detected=merged_correction_detected,
            reinforcement_detected=merged_reinforcement_detected,
        )

        self._queue = [context for context in self._queue if self._queue_key(context.thread_id, context.user_id, context.agent_name) != queue_key]
        self._queue.append(context)

    def _reset_timer(self) -> None:
        """重置去抖定时器。"""
        config = get_memory_config()
        self._schedule_timer(config.debounce_seconds)

        logger.debug("Memory update timer set for %ss", config.debounce_seconds)

    def _schedule_timer(self, delay_seconds: float) -> None:
        """按给定延迟调度队列处理。"""
        if self._timer is not None:
            self._timer.cancel()

        self._timer = threading.Timer(
            delay_seconds,
            self._process_queue,
        )
        self._timer.daemon = True
        self._timer.start()

    def _process_queue(self) -> None:
        """处理所有排队的对话上下文。"""
        # 延迟导入防循环依赖
        from deerflow.agents.memory.updater import MemoryUpdater

        with self._lock:
            if self._processing:
                # 即使有另一 worker 在跑，也保留立即冲刷语义。
                self._schedule_timer(0)
                return

            if not self._queue:
                return

            self._processing = True
            contexts_to_process = self._queue.copy()
            self._queue.clear()
            self._timer = None

        logger.info("Processing %d queued memory updates", len(contexts_to_process))

        try:
            updater = MemoryUpdater()

            for context in contexts_to_process:
                try:
                    logger.info("Updating memory for thread %s", context.thread_id)
                    success = updater.update_memory(
                        messages=context.messages,
                        thread_id=context.thread_id,
                        agent_name=context.agent_name,
                        correction_detected=context.correction_detected,
                        reinforcement_detected=context.reinforcement_detected,
                        user_id=context.user_id,
                    )
                    if success:
                        logger.info("Memory updated successfully for thread %s", context.thread_id)
                    else:
                        logger.warning("Memory update skipped/failed for thread %s", context.thread_id)
                except Exception as e:
                    logger.error("Error updating memory for thread %s: %s", context.thread_id, e)

                # 多条之间小延迟，防限流
                if len(contexts_to_process) > 1:
                    time.sleep(0.5)

        finally:
            with self._lock:
                self._processing = False

    def flush(self) -> None:
        """强制立即处理队列（测试 / 优雅关闭用）。"""
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

        self._process_queue()

    def flush_nowait(self) -> None:
        """立即在后台线程开始处理。"""
        with self._lock:
            # daemon 线程：进程退出前 _process_queue 没跑完会丢消息——best-effort 记忆更新可接受。
            self._schedule_timer(0)

    def clear(self) -> None:
        """清空队列不处理（测试用）。"""
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            self._queue.clear()
            self._processing = False

    @property
    def pending_count(self) -> int:
        """待处理的更新数。"""
        with self._lock:
            return len(self._queue)

    @property
    def is_processing(self) -> bool:
        """队列是否正在处理。"""
        with self._lock:
            return self._processing


# 全局单例
_memory_queue: MemoryUpdateQueue | None = None
_queue_lock = threading.Lock()


def get_memory_queue() -> MemoryUpdateQueue:
    """返回全局记忆更新队列单例。"""
    global _memory_queue
    with _queue_lock:
        if _memory_queue is None:
            _memory_queue = MemoryUpdateQueue()
        return _memory_queue


def reset_memory_queue() -> None:
    """重置全局记忆队列（测试用）。"""
    global _memory_queue
    with _queue_lock:
        if _memory_queue is not None:
            _memory_queue.clear()
        _memory_queue = None
