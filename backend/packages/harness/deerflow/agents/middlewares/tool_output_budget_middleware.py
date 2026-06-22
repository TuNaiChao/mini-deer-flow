"""工具输出预算中间件：防单个工具返回把模型上下文撑爆（M16）。

超过 ``externalize_min_chars`` 字符的工具结果**持久化到磁盘**，替换成精简预览 +
文件引用（``read_file`` 按行号读回）。磁盘不可用时回退为首尾截断。这样一次
``bash cat huge.log`` 不会把整份日志灌进 prompt。

两条 hook：
  - ``wrap_tool_call``：实时预算每个工具返回（外置 / 截断）。
  - ``wrap_model_call``：扫描历史 ToolMessage，把残留的过大结果补截断（兜底，因为
    早期工具结果可能没经过本中间件——比如别处直接写的）。

外置有两套写盘路径：宿主输出目录（local 沙箱是 bind-mount，等价）与沙箱内直接写
（remote AIO 沙箱没有 host mount）。靠 ``provider.uses_thread_data_mounts`` 判分支。
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import replace as dc_replace
from typing import TYPE_CHECKING, Any, override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelCallResult, ModelRequest, ModelResponse
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from deerflow.config.tool_output_config import ToolOutputConfig
from deerflow.sandbox.sandbox_provider import get_sandbox_provider

if TYPE_CHECKING:
    from deerflow.sandbox.sandbox import Sandbox

logger = logging.getLogger(__name__)

# 沙箱内的虚拟输出根。host-mount 沙箱把它 bind 到线程输出目录；remote 沙箱直接写这里。
_VIRTUAL_OUTPUTS_BASE = "/mnt/user-data/outputs"


def _default_config() -> ToolOutputConfig:
    return ToolOutputConfig()


# --------------------------------------------------------------------------- #
# 文本 helper
# --------------------------------------------------------------------------- #


def _message_text(content: Any) -> str | None:
    """从 ToolMessage.content 抽纯文本；多模态 / 非文本返回 None（跳过预算）。"""
    if isinstance(content, str):
        return content
    if content is None:
        return None
    if isinstance(content, list):
        pieces: list[str] = []
        for part in content:
            if isinstance(part, str):
                pieces.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                pieces.append(part["text"])
            else:
                return None
        return "\n".join(pieces) if pieces else None
    return None


def _snap_to_line_boundary(text: str, pos: int) -> int:
    """把 ``pos`` 对齐到最近的上一行行尾，让预览 / 截断落在完整行上。"""
    if pos <= 0 or pos >= len(text):
        return pos
    half = pos // 2
    nl = text.rfind("\n", half, pos)
    if nl >= 0:
        return nl + 1
    return pos


# --------------------------------------------------------------------------- #
# 磁盘持久化
# --------------------------------------------------------------------------- #

_EXT_MAP: dict[str, str] = {
    "bash": "log",
    "bash_tool": "log",
    "web_fetch": "log",
}


def _sanitize_tool_name(name: str) -> str:
    base = os.path.basename(name)
    safe = base.replace("..", "").replace("/", "_").replace("\\", "_")
    return safe or "unknown"


def _build_externalized_filename(*, tool_name: str, tool_call_id: str) -> str:
    safe_name = _sanitize_tool_name(tool_name)
    ext = _EXT_MAP.get(tool_name, "txt")
    short_id = uuid.uuid4().hex[:12]
    return f"{safe_name}-{short_id}.{ext}"


def _externalize(
    content: str,
    *,
    tool_name: str,
    tool_call_id: str,
    outputs_path: str,
    storage_subdir: str,
) -> str | None:
    """把 content 写到宿主输出目录，返回虚拟路径；失败返回 None。"""
    if os.path.isabs(storage_subdir) or ".." in storage_subdir:
        return None
    storage_dir = os.path.join(outputs_path, storage_subdir)
    try:
        os.makedirs(storage_dir, exist_ok=True)
    except OSError:
        return None

    filename = _build_externalized_filename(tool_name=tool_name, tool_call_id=tool_call_id)
    filepath = os.path.join(storage_dir, filename)

    if not os.path.abspath(filepath).startswith(os.path.abspath(storage_dir)):
        return None

    try:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)
    except OSError:
        return None

    return f"{_VIRTUAL_OUTPUTS_BASE}/{storage_subdir}/{filename}"


def _externalize_to_sandbox(
    content: str,
    *,
    tool_name: str,
    tool_call_id: str,
    storage_subdir: str,
    sandbox: Sandbox,
) -> str | None:
    """把 content 写进沙箱文件系统并返回虚拟路径（remote AIO 沙箱无 host mount 时用）。

    见 issue #3416：host 侧虚拟路径在 remote 沙箱里不存在，模型 ``read_file`` 读不回，
    所以要直接写进沙箱。AIO 的 write_file 不建父目录，需先 mkdir；execute_command 失败
    返回字符串而非抛异常，故最后用 ``test -s`` 验证文件确实落地。
    """
    if os.path.isabs(storage_subdir) or ".." in storage_subdir:
        return None
    filename = _build_externalized_filename(tool_name=tool_name, tool_call_id=tool_call_id)
    virtual_dir = f"{_VIRTUAL_OUTPUTS_BASE}/{storage_subdir}"
    virtual_path = f"{virtual_dir}/{filename}"
    try:
        sandbox.execute_command(f"mkdir -p {shlex.quote(virtual_dir)}")
        sandbox.write_file(virtual_path, content)
        check = sandbox.execute_command(f"test -s {shlex.quote(virtual_path)} && echo OK || echo MISSING")
        if not isinstance(check, str) or check.strip() != "OK":
            logger.warning("Sandbox externalize validation failed: path=%s, check=%r", virtual_path, check)
            return None
    except Exception:
        logger.exception("Failed to externalize %s output to sandbox (call_id=%s)", tool_name, tool_call_id)
        return None
    return virtual_path


# --------------------------------------------------------------------------- #
# 预览 / 回退截断构造器
# --------------------------------------------------------------------------- #


def _build_preview(
    content: str,
    *,
    tool_name: str,
    virtual_path: str,
    head_chars: int,
    tail_chars: int,
) -> str:
    total = len(content)
    head_end = _snap_to_line_boundary(content, min(head_chars, total))
    tail_start = max(head_end, total - tail_chars)
    tail_start_snapped = _snap_to_line_boundary(content, tail_start)
    if tail_start_snapped > head_end:
        tail_start = tail_start_snapped

    head = content[:head_end]
    tail = content[tail_start:] if tail_start < total else ""

    omitted = total - len(head) - len(tail)
    ref = f"\n\n[Full {tool_name} output saved to {virtual_path} ({total} chars, ~{total // 4} tokens). Use read_file with start_line and end_line to access specific sections. {omitted} chars omitted from this preview.]\n\n"

    parts = [head, ref]
    if tail:
        parts.append(tail)
    return "".join(parts)


def _build_fallback(
    content: str,
    *,
    tool_name: str,
    max_chars: int,
    head_chars: int,
    tail_chars: int,
) -> str:
    """磁盘不可用时的首尾截断；返回串保证 <= max_chars。"""
    total = len(content)
    if max_chars <= 0 or total <= max_chars:
        return content

    marker_template = "\n\n[... {n} chars omitted from {tn} output. Persistent storage unavailable. Consider narrowing the query or using more specific parameters.]\n\n"
    marker_overhead = len(marker_template.format(n=total, tn=tool_name))

    if marker_overhead >= max_chars:
        return content[:max_chars]

    budget = max_chars - marker_overhead
    effective_head = min(head_chars, budget)
    effective_tail = min(tail_chars, max(0, budget - effective_head))

    head_end = _snap_to_line_boundary(content, min(effective_head, total))
    tail_start = max(head_end, total - effective_tail)
    tail_start_snapped = _snap_to_line_boundary(content, tail_start)
    if tail_start_snapped > head_end:
        tail_start = tail_start_snapped

    head = content[:head_end]
    tail = content[tail_start:] if tail_start < total else ""
    omitted = total - len(head) - len(tail)

    marker = marker_template.format(n=omitted, tn=tool_name)
    parts = [head, marker]
    if tail:
        parts.append(tail)
    return "".join(parts)


# --------------------------------------------------------------------------- #
# 核心预算逻辑
# --------------------------------------------------------------------------- #


def _resolve_outputs_path(request: ToolCallRequest) -> str | None:
    runtime = getattr(request, "runtime", None)
    if runtime is None:
        return None
    state = getattr(runtime, "state", None)
    if state is None:
        return None
    thread_data = state.get("thread_data")
    if not isinstance(thread_data, dict):
        return None
    outputs_path = thread_data.get("outputs_path")
    return outputs_path if isinstance(outputs_path, str) else None


def _resolve_sandbox(request: ToolCallRequest) -> Sandbox | None:
    """从当前 tool call 的 runtime.state 读沙箱（SandboxMiddleware 写的 sandbox_id）。

    **不**调 ``provider.acquire``——acquire 可能触发阻塞 remote IO，而本函数每个 tool call
    都跑。非沙箱工具（web_search / MCP...）这里返 None，调用方回退 inline 截断即可。
    """
    runtime = getattr(request, "runtime", None)
    state = getattr(runtime, "state", None)
    if not isinstance(state, dict):
        return None
    sandbox_state = state.get("sandbox")
    if not isinstance(sandbox_state, dict):
        return None
    sandbox_id = sandbox_state.get("sandbox_id")
    if not sandbox_id:
        return None
    try:
        return get_sandbox_provider().get(sandbox_id)
    except Exception:
        logger.exception("Failed to look up sandbox %s for tool-output externalization", sandbox_id)
        return None


def _budget_content(
    content: str,
    *,
    tool_name: str,
    tool_call_id: str,
    outputs_path: str | None,
    config: ToolOutputConfig,
    sandbox: Sandbox | None = None,
) -> str | None:
    """对 content 应用预算；无需改动返回 None。"""
    threshold = config.tool_overrides.get(tool_name, config.externalize_min_chars)
    if threshold <= 0 and config.fallback_max_chars <= 0:
        return None
    if len(content) <= threshold and len(content) <= config.fallback_max_chars:
        return None

    if threshold > 0 and len(content) > threshold:
        virtual_path: str | None = None
        if sandbox is not None:
            provider = None
            try:
                provider = get_sandbox_provider()
            except Exception:
                logger.exception("Failed to get sandbox provider for tool-output externalization; falling back to inline truncation")
            if provider is not None and getattr(provider, "uses_thread_data_mounts", False):
                # host-mount 沙箱：host 输出路径 bind 进沙箱同一虚拟路径，写 host 等价。
                if outputs_path:
                    virtual_path = _externalize(
                        content,
                        tool_name=tool_name,
                        tool_call_id=tool_call_id,
                        outputs_path=outputs_path,
                        storage_subdir=config.storage_subdir,
                    )
            else:
                virtual_path = _externalize_to_sandbox(
                    content,
                    tool_name=tool_name,
                    tool_call_id=tool_call_id,
                    storage_subdir=config.storage_subdir,
                    sandbox=sandbox,
                )
        elif outputs_path:
            virtual_path = _externalize(
                content,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                outputs_path=outputs_path,
                storage_subdir=config.storage_subdir,
            )
        if virtual_path is not None:
            logger.info("Externalized %s output (%d chars) to %s", tool_name, len(content), virtual_path)
            return _build_preview(
                content,
                tool_name=tool_name,
                virtual_path=virtual_path,
                head_chars=config.preview_head_chars,
                tail_chars=config.preview_tail_chars,
            )

    if config.fallback_max_chars > 0 and len(content) > config.fallback_max_chars:
        logger.warning("Fallback-truncating %s output: %d chars → %d max", tool_name, len(content), config.fallback_max_chars)
        return _build_fallback(
            content,
            tool_name=tool_name,
            max_chars=config.fallback_max_chars,
            head_chars=config.fallback_head_chars,
            tail_chars=config.fallback_tail_chars,
        )

    return None


# --------------------------------------------------------------------------- #
# 结果 patcher
# --------------------------------------------------------------------------- #


def _patch_tool_message(
    msg: ToolMessage,
    config: ToolOutputConfig,
    outputs_path: str | None,
    sandbox: Sandbox | None = None,
) -> ToolMessage:
    tool_name = msg.name or "unknown"
    if tool_name in config.exempt_tools:
        return msg

    text = _message_text(msg.content)
    if text is None:
        return msg

    replacement = _budget_content(
        text,
        tool_name=tool_name,
        tool_call_id=msg.tool_call_id or "",
        outputs_path=outputs_path,
        config=config,
        sandbox=sandbox,
    )
    if replacement is None:
        return msg

    update: dict[str, Any] = {"content": replacement}
    if getattr(msg, "response_metadata", None):
        update["response_metadata"] = dict(msg.response_metadata)
    if getattr(msg, "additional_kwargs", None):
        update["additional_kwargs"] = dict(msg.additional_kwargs)
    return msg.model_copy(update=update)


def _effective_trigger(tool_name: str, config: ToolOutputConfig) -> int:
    """对该工具可能触发预算的最小内容长度；不可能触发返回 -1。"""
    candidates: list[int] = []
    externalize = config.tool_overrides.get(tool_name, config.externalize_min_chars)
    if externalize > 0:
        candidates.append(externalize)
    if config.fallback_max_chars > 0:
        candidates.append(config.fallback_max_chars)
    return min(candidates) if candidates else -1


def _tool_message_over_budget(msg: ToolMessage, config: ToolOutputConfig) -> bool:
    if (msg.name or "") in config.exempt_tools:
        return False
    trigger = _effective_trigger(msg.name or "", config)
    if trigger < 0:
        return False
    text = _message_text(msg.content)
    return text is not None and len(text) > trigger


def _needs_budget(result: ToolMessage | Command, config: ToolOutputConfig) -> bool:
    """便宜预扫：能否触发预算（避免小输出也卸线程）。"""
    if isinstance(result, ToolMessage):
        return _tool_message_over_budget(result, config)
    update = getattr(result, "update", None)
    if isinstance(update, dict):
        for msg in update.get("messages", []):
            if isinstance(msg, ToolMessage) and _tool_message_over_budget(msg, config):
                return True
    return False


def _patch_result(
    result: ToolMessage | Command,
    config: ToolOutputConfig,
    outputs_path: str | None,
    sandbox: Sandbox | None = None,
) -> ToolMessage | Command:
    if isinstance(result, ToolMessage):
        return _patch_tool_message(result, config, outputs_path, sandbox)

    update = getattr(result, "update", None)
    if not isinstance(update, dict):
        return result

    messages = update.get("messages")
    if not isinstance(messages, list):
        return result

    new_messages: list[Any] = []
    changed = False
    for msg in messages:
        if isinstance(msg, ToolMessage):
            patched = _patch_tool_message(msg, config, outputs_path, sandbox)
            if patched is not msg:
                changed = True
            new_messages.append(patched)
        else:
            new_messages.append(msg)

    if not changed:
        return result

    return dc_replace(result, update={**update, "messages": new_messages})


def _patch_model_messages(messages: list[Any], config: ToolOutputConfig) -> list[Any] | None:
    """对历史 ToolMessage 应用预算；无需改动返回 None。

    历史 message 不带 sandbox：真过大结果在 tool-call 时已预算（可能已外置），这里
    只能做 inline fallback 截断，不需要沙箱。
    """
    if not any(isinstance(msg, ToolMessage) and _tool_message_over_budget(msg, config) for msg in messages):
        return None

    updated: list[Any] = []
    changed = False
    for msg in messages:
        if isinstance(msg, ToolMessage):
            patched = _patch_tool_message(msg, config, outputs_path=None)
            if patched is not msg:
                changed = True
            updated.append(patched)
        else:
            updated.append(msg)
    return updated if changed else None


# --------------------------------------------------------------------------- #
# 中间件类
# --------------------------------------------------------------------------- #


class ToolOutputBudgetMiddleware(AgentMiddleware[AgentState]):
    """单个工具结果超预算 → 外置到磁盘 + 精简预览，否则回退首尾截断。"""

    def __init__(self, config: ToolOutputConfig | None = None) -> None:
        super().__init__()
        self._config = config if config is not None else _default_config()

    @classmethod
    def from_app_config(cls, app_config: Any) -> ToolOutputBudgetMiddleware:
        tool_output = getattr(app_config, "tool_output", None)
        if isinstance(tool_output, ToolOutputConfig):
            return cls(config=tool_output)
        return cls()

    @override
    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        result = handler(request)
        if not self._config.enabled:
            return result
        if not _needs_budget(result, self._config):
            return result
        outputs_path = _resolve_outputs_path(request)
        sandbox = _resolve_sandbox(request)
        return _patch_result(result, self._config, outputs_path, sandbox)

    @override
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        result = await handler(request)
        if not self._config.enabled:
            return result
        if not _needs_budget(result, self._config):
            return result
        outputs_path = _resolve_outputs_path(request)
        # _resolve_sandbox 只碰 runtime.state 和 provider 内存注册表，循环安全；
        # 真正的沙箱 IO（mkdir/write/test）在 _patch_result 里，下面 to_thread 卸线程。
        sandbox = _resolve_sandbox(request)
        return await asyncio.to_thread(_patch_result, result, self._config, outputs_path, sandbox)

    @override
    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        if self._config.enabled:
            messages = getattr(request, "messages", None)
            if isinstance(messages, list):
                patched = _patch_model_messages(messages, self._config)
                if patched is not None:
                    request = request.override(messages=patched)
        return handler(request)

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        if self._config.enabled:
            messages = getattr(request, "messages", None)
            if isinstance(messages, list):
                patched = _patch_model_messages(messages, self._config)
                if patched is not None:
                    request = request.override(messages=patched)
        return await handler(request)
