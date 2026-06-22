"""``TodoMiddleware``：扩展 ``TodoListMiddleware``，加上下文丢失检测 + 阻止过早退出（M16）。

两个增强：

1. **上下文丢失检测**：消息历史被摘要（``SummarizationMiddleware``）截断后，原
   ``write_todos`` 工具调用及其 ToolMessage 可能滚出上下文窗口，模型就忘了当前 todo
   列表。``before_model`` 检测此情况并注入一条 reminder HumanMessage，让模型仍知道
   未完成的 todo。

2. **阻止过早退出**：模型产出最终响应（无工具调用）但 todo 还有未完成项时，``after_model``
   队列一条 reminder 给下一次模型请求并 ``jump_to="model"`` 强制继续。reminder 经
   ``wrap_model_call`` 注入而非持久化进状态（防泄漏进用户可见消息流）。重试上限
   ``_MAX_COMPLETION_REMINDERS=2`` 防模型无法推进时死循环。

上下文丢失 reminder 的提醒 + 完成提醒都标 ``hide_from_ui``，不进 UI / IM 流。
"""

from __future__ import annotations

import threading
from collections.abc import Awaitable, Callable
from typing import Any, override

from langchain.agents.middleware import TodoListMiddleware
from langchain.agents.middleware.todo import Todo
from langchain.agents.middleware.types import ModelCallResult, ModelRequest, ModelResponse, hook_config
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.runtime import Runtime

from deerflow.agents.thread_state import ThreadState


def _todos_in_messages(messages: list[Any]) -> bool:
    """消息里有 AIMessage 带 write_todos 调用 → True。"""
    for msg in messages:
        if isinstance(msg, AIMessage) and msg.tool_calls:
            for tc in msg.tool_calls:
                if tc.get("name") == "write_todos":
                    return True
    return False


def _reminder_in_messages(messages: list[Any]) -> bool:
    """已有 todo_reminder HumanMessage → True。"""
    for msg in messages:
        if isinstance(msg, HumanMessage) and getattr(msg, "name", None) == "todo_reminder":
            return True
    return False


def _format_todos(todos: list[Todo]) -> str:
    lines: list[str] = []
    for todo in todos:
        status = todo.get("status", "pending")
        content = todo.get("content", "")
        lines.append(f"- [{status}] {content}")
    return "\n".join(lines)


def _format_completion_reminder(todos: list[Todo]) -> str:
    incomplete = [t for t in todos if t.get("status") != "completed"]
    incomplete_text = "\n".join(f"- [{t.get('status', 'pending')}] {t.get('content', '')}" for t in incomplete)
    return (
        "<system_reminder>\n"
        "You have incomplete todo items that must be finished before giving your final response:\n\n"
        f"{incomplete_text}\n\n"
        "Please continue working on these tasks. Call `write_todos` to mark items as completed "
        "as you finish them, and only respond when all items are done.\n"
        "</system_reminder>"
    )


_TOOL_CALL_FINISH_REASONS = {"tool_calls", "function_call"}


def _has_tool_call_intent_or_error(message: AIMessage) -> bool:
    """AIMessage 不是干净的最终答复（带工具意图 / 错误）→ True。

    todo 完成提醒只在模型给出纯最终答复时触发。provider / 工具解析细节跨 LangChain
    版本多变，故把所有工具意图 / 错误信号收在本 helper 里。
    """
    if message.tool_calls:
        return True
    if getattr(message, "invalid_tool_calls", None):
        return True
    additional_kwargs = getattr(message, "additional_kwargs", {}) or {}
    if additional_kwargs.get("tool_calls") or additional_kwargs.get("function_call"):
        return True
    response_metadata = getattr(message, "response_metadata", {}) or {}
    return response_metadata.get("finish_reason") in _TOOL_CALL_FINISH_REASONS


class TodoMiddleware(TodoListMiddleware):
    """扩展 ``TodoListMiddleware`` 加 write_todos 上下文丢失检测。"""

    state_schema = ThreadState

    @override
    def before_model(self, state: ThreadState, runtime: Runtime) -> dict[str, Any] | None:
        """write_todos 已离开上下文窗口时注入 todo-list reminder。"""
        todos: list[Todo] = state.get("todos") or []  # type: ignore[assignment]
        if not todos:
            return None

        messages = state.get("messages") or []
        if _todos_in_messages(messages):
            # write_todos 仍在上下文里 — 无事可做。
            return None

        if _reminder_in_messages(messages):
            # reminder 已注入且未被截断。
            return None

        # todo 列表在状态里但原 write_todos 调用已没了 → 注 reminder。
        formatted = _format_todos(todos)
        reminder = HumanMessage(
            name="todo_reminder",
            additional_kwargs={"hide_from_ui": True},
            content=(
                "<system_reminder>\n"
                "Your todo list from earlier is no longer visible in the current context window, "
                "but it is still active. Here is the current state:\n\n"
                f"{formatted}\n\n"
                "Continue tracking and updating this todo list as you work. "
                "Call `write_todos` whenever the status of any item changes.\n"
                "</system_reminder>"
            ),
        )
        return {"messages": [reminder]}

    @override
    async def abefore_model(self, state: ThreadState, runtime: Runtime) -> dict[str, Any] | None:
        return self.before_model(state, runtime)

    # 完成提醒的每线程每 run 预算。
    _MAX_COMPLETION_REMINDERS = 2
    # 长生命周期中间件实例的 per-run 簿记硬上限。
    _MAX_COMPLETION_REMINDER_KEYS = 4096

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._lock = threading.Lock()
        self._pending_completion_reminders: dict[tuple[str, str], list[str]] = {}
        self._completion_reminder_counts: dict[tuple[str, str], int] = {}
        self._completion_reminder_touch_order: dict[tuple[str, str], int] = {}
        self._completion_reminder_next_order = 0

    @staticmethod
    def _get_thread_id(runtime: Runtime) -> str:
        context = getattr(runtime, "context", None)
        thread_id = context.get("thread_id") if context else None
        return str(thread_id) if thread_id else "default"

    @staticmethod
    def _get_run_id(runtime: Runtime) -> str:
        context = getattr(runtime, "context", None)
        run_id = context.get("run_id") if context else None
        return str(run_id) if run_id else "default"

    def _pending_key(self, runtime: Runtime) -> tuple[str, str]:
        return self._get_thread_id(runtime), self._get_run_id(runtime)

    def _touch_completion_reminder_key_locked(self, key: tuple[str, str]) -> None:
        self._completion_reminder_next_order += 1
        self._completion_reminder_touch_order[key] = self._completion_reminder_next_order

    def _completion_reminder_keys_locked(self) -> set[tuple[str, str]]:
        keys = set(self._pending_completion_reminders)
        keys.update(self._completion_reminder_counts)
        keys.update(self._completion_reminder_touch_order)
        return keys

    def _drop_completion_reminder_key_locked(self, key: tuple[str, str]) -> None:
        self._pending_completion_reminders.pop(key, None)
        self._completion_reminder_counts.pop(key, None)
        self._completion_reminder_touch_order.pop(key, None)

    def _prune_completion_reminder_state_locked(self, protected_key: tuple[str, str]) -> None:
        keys = self._completion_reminder_keys_locked()
        overflow = len(keys) - self._MAX_COMPLETION_REMINDER_KEYS
        if overflow <= 0:
            return

        candidates = [key for key in keys if key != protected_key]
        candidates.sort(key=lambda key: self._completion_reminder_touch_order.get(key, 0))
        for key in candidates[:overflow]:
            self._drop_completion_reminder_key_locked(key)

    def _queue_completion_reminder(self, runtime: Runtime, reminder: str) -> None:
        key = self._pending_key(runtime)
        with self._lock:
            self._pending_completion_reminders.setdefault(key, []).append(reminder)
            self._completion_reminder_counts[key] = self._completion_reminder_counts.get(key, 0) + 1
            self._touch_completion_reminder_key_locked(key)
            self._prune_completion_reminder_state_locked(protected_key=key)

    def _completion_reminder_count_for_runtime(self, runtime: Runtime) -> int:
        key = self._pending_key(runtime)
        with self._lock:
            return self._completion_reminder_counts.get(key, 0)

    def _drain_completion_reminders(self, runtime: Runtime) -> list[str]:
        key = self._pending_key(runtime)
        with self._lock:
            reminders = self._pending_completion_reminders.pop(key, [])
            if reminders or key in self._completion_reminder_counts:
                self._touch_completion_reminder_key_locked(key)
            return reminders

    def _clear_other_run_completion_reminders(self, runtime: Runtime) -> None:
        thread_id, current_run_id = self._pending_key(runtime)
        with self._lock:
            for key in self._completion_reminder_keys_locked():
                if key[0] == thread_id and key[1] != current_run_id:
                    self._drop_completion_reminder_key_locked(key)

    def _clear_current_run_completion_reminders(self, runtime: Runtime) -> None:
        key = self._pending_key(runtime)
        with self._lock:
            self._drop_completion_reminder_key_locked(key)

    @override
    def before_agent(self, state: ThreadState, runtime: Runtime) -> dict[str, Any] | None:
        self._clear_other_run_completion_reminders(runtime)
        return None

    @override
    async def abefore_agent(self, state: ThreadState, runtime: Runtime) -> dict[str, Any] | None:
        self._clear_other_run_completion_reminders(runtime)
        return None

    @hook_config(can_jump_to=["model"])
    @override
    def after_model(self, state: ThreadState, runtime: Runtime) -> dict[str, Any] | None:
        """todo 仍有未完成项时阻止 agent 过早退出。"""
        # 1. 保留基类逻辑（并行 write_todos 检测）。
        base_result = super().after_model(state, runtime)
        if base_result is not None:
            return base_result

        # 2. 只在 agent 想干净退出时介入。工具调用意图 / 解析错误交给工具路径处理。
        messages = state.get("messages") or []
        last_ai = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)
        if not last_ai or _has_tool_call_intent_or_error(last_ai):
            return None

        # 3. 全完成 / 无 todo → 允许退出。
        todos: list[Todo] = state.get("todos") or []  # type: ignore[assignment]
        if not todos or all(t.get("status") == "completed" for t in todos):
            return None

        # 4. 重试上限防死循环。
        if self._completion_reminder_count_for_runtime(runtime) >= self._MAX_COMPLETION_REMINDERS:
            return None

        # 5. 队列 reminder 给下一次模型请求并跳回。不能持久化成普通 HumanMessage——
        #    否则泄漏进用户可见消息流 / 保存的记录。
        self._queue_completion_reminder(runtime, _format_completion_reminder(todos))
        return {"jump_to": "model"}

    @override
    @hook_config(can_jump_to=["model"])
    async def aafter_model(self, state: ThreadState, runtime: Runtime) -> dict[str, Any] | None:
        return self.after_model(state, runtime)

    @staticmethod
    def _format_pending_completion_reminders(reminders: list[str]) -> str:
        return "\n\n".join(dict.fromkeys(reminders))

    def _augment_request(self, request: ModelRequest) -> ModelRequest:
        reminders = self._drain_completion_reminders(request.runtime)
        if not reminders:
            return request
        new_messages = [
            *request.messages,
            HumanMessage(
                content=self._format_pending_completion_reminders(reminders),
                name="todo_completion_reminder",
                additional_kwargs={"hide_from_ui": True},
            ),
        ]
        return request.override(messages=new_messages)

    @override
    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        return handler(self._augment_request(request))

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        return await handler(self._augment_request(request))

    @override
    def after_agent(self, state: ThreadState, runtime: Runtime) -> dict[str, Any] | None:
        self._clear_current_run_completion_reminders(runtime)
        return None

    @override
    async def aafter_agent(self, state: ThreadState, runtime: Runtime) -> dict[str, Any] | None:
        self._clear_current_run_completion_reminders(runtime)
        return None
