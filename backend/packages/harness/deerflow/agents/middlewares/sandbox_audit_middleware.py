"""``SandboxAuditMiddleware`` —— bash 命令安全审计中间件。

它不是沙箱的隔离边界（那是 LocalSandbox 的虚拟路径翻译 + M10b 的容器），而是**命令分级
审计闸**：在每个 ``bash`` 工具调用前，把命令分成三档——

- **block（高危）**：``rm -rf /``、``curl url | bash``、``dd if=``、fork bomb、覆盖系统二进制 /
  shell 启动文件、动态链接劫持（``LD_PRELOAD``）…直接拦下，返回错误 ToolMessage，不执行。
- **warn（中危）**：``pip install``、``chmod 777``、``sudo/su``、改 ``PATH``…照常执行，但往
  工具结果里追加一条警告，让 LLM 知道「这条命令动了运行时环境」。
- **pass（安全）**：放行，不附加任何东西。

每条 bash 调用都会写一条结构化 JSON 审计日志（thread_id / command / verdict），便于事后
排查。复合命令（``cmd1 && cmd2 ; cmd3``）会被 quote-aware 地拆开逐条分级，取最严档位；
但跨语句的结构性攻击（如 ``while true; do bash & done``）先做整串扫描再拆，避免拆分破坏
模式上下文。

输入消毒：空命令、超长命令（>10KB，几乎必是 payload 注入）、含 NUL 字节的命令直接 block。

红线 #15：``wrap_tool_call`` 调用 handler 若抛 ``GraphBubbleUp`` 必须让它继续上抛——本中间件
的 wrap 只做「分级 + 审计 + 贴警告」，不吞异常（非 bash 工具直接透传 handler）。
"""

from __future__ import annotations

import json
import logging
import re
import shlex
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import override

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from deerflow.agents.thread_state import ThreadState

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 命令分级规则（import 时编译一次）
# ---------------------------------------------------------------------------

# 高危：拦下不执行。
_HIGH_RISK_PATTERNS: list[re.Pattern[str]] = [
    # --- 原始规则 ---
    re.compile(r"rm\s+-[^\s]*r[^\s]*\s+(/\*?|~/?\*?|/home\b|/root\b)\s*$"),
    re.compile(r"dd\s+if="),
    re.compile(r"mkfs"),
    re.compile(r"cat\s+/etc/shadow"),
    re.compile(r">+\s*/etc/"),
    # --- 管道喂给 sh/bash（泛化旧 curl|sh 规则）---
    re.compile(r"\|\s*(ba)?sh\b"),
    # --- 命令替换（只盯危险可执行）---
    re.compile(r"[`$]\(?\s*(curl|wget|bash|sh|python|ruby|perl|base64)"),
    # --- base64 解码后管道执行 ---
    re.compile(r"base64\s+.*-d.*\|"),
    # --- 覆盖系统二进制 ---
    re.compile(r">+\s*(/usr/bin/|/bin/|/sbin/)"),
    # --- 覆盖 shell 启动文件 ---
    re.compile(r">+\s*~/?\.(bashrc|profile|zshrc|bash_profile)"),
    # --- 进程环境泄露 ---
    re.compile(r"/proc/[^/]+/environ"),
    # --- 动态链接劫持（一步提权）---
    re.compile(r"\b(LD_PRELOAD|LD_LIBRARY_PATH)\s*="),
    # --- bash 内建网络（绕过工具白名单）---
    re.compile(r"/dev/tcp/"),
    # --- fork bomb ---
    re.compile(r"\S+\(\)\s*\{[^}]*\|\s*\S+\s*&"),  # :(){ :|:& };:
    re.compile(r"while\s+true.*&\s*done"),  # while true; do bash & done
]

# 中危：照常执行，但附警告。
_MEDIUM_RISK_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"chmod\s+777"),
    re.compile(r"pip3?\s+install"),
    re.compile(r"apt(-get)?\s+install"),
    # sudo/su：Docker root 下是 no-op；警告让 LLM 知晓。
    re.compile(r"\b(sudo|su)\b"),
    # 改 PATH：长攻击链，警告而非拦。
    re.compile(r"\bPATH\s*="),
]


def _split_compound_command(command: str) -> list[str]:
    """把复合命令拆成子命令（quote-aware）。

    扫描原始串，让未加引号的 shell 控制符（``&&``/``||``/``;``）即使无空格也能识别
    （如 ``safe;rm -rf /`` 或 ``rm -rf /&&echo ok``）。引号内的操作符忽略。若命令以未闭合
    引号或悬空转义结尾，原样返回整串（fail-closed——整串分级比分段丢更安全）。
    """
    parts: list[str] = []
    current: list[str] = []
    in_single_quote = False
    in_double_quote = False
    escaping = False
    index = 0

    while index < len(command):
        char = command[index]

        if escaping:
            current.append(char)
            escaping = False
            index += 1
            continue

        if char == "\\" and not in_single_quote:
            current.append(char)
            escaping = True
            index += 1
            continue

        if char == "'" and not in_double_quote:
            in_single_quote = not in_single_quote
            current.append(char)
            index += 1
            continue

        if char == '"' and not in_single_quote:
            in_double_quote = not in_double_quote
            current.append(char)
            index += 1
            continue

        if not in_single_quote and not in_double_quote:
            if command.startswith("&&", index) or command.startswith("||", index):
                part = "".join(current).strip()
                if part:
                    parts.append(part)
                current = []
                index += 2
                continue
            if char == ";":
                part = "".join(current).strip()
                if part:
                    parts.append(part)
                current = []
                index += 1
                continue

        current.append(char)
        index += 1

    # 未闭合引号或悬空转义 → fail-closed，返回整串。
    if in_single_quote or in_double_quote or escaping:
        return [command]

    part = "".join(current).strip()
    if part:
        parts.append(part)
    return parts if parts else [command]


def _classify_single_command(command: str) -> str:
    """分级单条（非复合）命令。返回 'block' / 'warn' / 'pass'。"""
    normalized = " ".join(command.split())

    for pattern in _HIGH_RISK_PATTERNS:
        if pattern.search(normalized):
            return "block"

    # 再用 shlex 解析的 token 试一遍高危检测。
    try:
        tokens = shlex.split(command)
        joined = " ".join(tokens)
        for pattern in _HIGH_RISK_PATTERNS:
            if pattern.search(joined):
                return "block"
    except ValueError:
        # heredoc 与其它多行 shell 形式可能是合法 bash 但 shlex 解析不了。
        # 原始高危模式已在上面检查过（normalized），这里不直接 block——继续走中危模式检查（#3786）。
        pass

    for pattern in _MEDIUM_RISK_PATTERNS:
        if pattern.search(normalized):
            return "warn"

    return "pass"


def _classify_command(command: str) -> str:
    """返回 'block' / 'warn' / 'pass'。

    策略：
    1. 先对整串原始命令扫高危——捕获跨多语句的结构性攻击（``while true; do bash & done``、
       ``:(){ :|:& };:``），这些在 ``;`` 拆分后会丢模式上下文。
    2. 再拆复合命令逐条分级，取最严档位。
    """
    # 第 1 轮：整串高危扫描。
    normalized = " ".join(command.split())
    for pattern in _HIGH_RISK_PATTERNS:
        if pattern.search(normalized):
            return "block"

    # 第 2 轮：逐子命令分级。
    sub_commands = _split_compound_command(command)
    worst = "pass"
    for sub in sub_commands:
        verdict = _classify_single_command(sub)
        if verdict == "block":
            return "block"  # 短路：没有更严的了
        if verdict == "warn":
            worst = "warn"
    return worst


# ---------------------------------------------------------------------------
# 中间件
# ---------------------------------------------------------------------------


class SandboxAuditMiddleware(AgentMiddleware[ThreadState]):
    """bash 命令安全审计中间件。

    对每个 ``bash`` 工具调用：
    1. **命令分级**：正则 + shlex 分析，分 block / warn / pass 三档。
    2. **审计日志**：每条 bash 调用记一条结构化 JSON（经标准 logger，落在 langgraph.log）。

    高危命令（如 ``rm -rf /``、``curl url | bash``）被拦下：不调 handler，返回错误
    ``ToolMessage`` 让 agent loop 优雅继续。中危命令（如 ``pip install``、``chmod 777``）
    照常执行，往结果追加警告让 LLM 知晓。
    """

    state_schema = ThreadState

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _get_thread_id(self, request: ToolCallRequest) -> str | None:
        runtime = request.runtime  # ToolRuntime；测试里可能 None-ish
        if runtime is None:
            return None
        ctx = getattr(runtime, "context", None) or {}
        thread_id = ctx.get("thread_id") if isinstance(ctx, dict) else None
        if thread_id is None:
            cfg = getattr(runtime, "config", None) or {}
            thread_id = cfg.get("configurable", {}).get("thread_id")
        return thread_id

    _AUDIT_COMMAND_LIMIT = 200

    def _write_audit(self, thread_id: str | None, command: str, verdict: str, *, truncate: bool = False) -> None:
        audited_command = command
        if truncate and len(command) > self._AUDIT_COMMAND_LIMIT:
            audited_command = f"{command[: self._AUDIT_COMMAND_LIMIT]}... ({len(command)} chars)"
        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "thread_id": thread_id or "unknown",
            "command": audited_command,
            "verdict": verdict,
        }
        logger.info("[SandboxAudit] %s", json.dumps(record, ensure_ascii=False))

    def _build_block_message(self, request: ToolCallRequest, reason: str) -> ToolMessage:
        tool_call_id = str(request.tool_call.get("id") or "missing_id")
        return ToolMessage(
            content=f"Command blocked: {reason}. Please use a safer alternative approach.",
            tool_call_id=tool_call_id,
            name="bash",
            status="error",
        )

    def _append_warn_to_result(self, result: ToolMessage | Command, command: str) -> ToolMessage | Command:
        """中危命令：往工具结果追加警告。"""
        if not isinstance(result, ToolMessage):
            return result
        warning = f"\n\n⚠️ Warning: `{command}` is a medium-risk command that may modify the runtime environment."
        if isinstance(result.content, list):
            new_content = list(result.content) + [{"type": "text", "text": warning}]
        else:
            new_content = str(result.content) + warning
        return ToolMessage(
            content=new_content,
            tool_call_id=result.tool_call_id,
            name=result.name,
            status=result.status,
        )

    # ------------------------------------------------------------------
    # 输入消毒
    # ------------------------------------------------------------------

    # 正常 bash 命令很少超几百字符。10000 远超任何合理用例，又只是 Linux ARG_MAX 的零头。
    # 超过几乎必是 payload 注入或 base64 攻击串。
    _MAX_COMMAND_LENGTH = 10_000

    def _validate_input(self, command: str) -> str | None:
        """返回 None 表示可接受，否则返回拒绝原因。"""
        if not command.strip():
            return "empty command"
        if len(command) > self._MAX_COMMAND_LENGTH:
            return "command too long"
        if "\x00" in command:
            return "null byte detected"
        return None

    # ------------------------------------------------------------------
    # 核心逻辑（sync / async 共用）
    # ------------------------------------------------------------------

    def _pre_process(self, request: ToolCallRequest) -> tuple[str, str | None, str, str | None]:
        """返回 (command, thread_id, verdict, reject_reason)。

        verdict 为 'block'/'warn'/'pass'；reject_reason 仅在输入消毒拒绝时非 None。
        """
        args = request.tool_call.get("args", {})
        raw_command = args.get("command")
        command = raw_command if isinstance(raw_command, str) else ""
        thread_id = self._get_thread_id(request)

        # ① 输入消毒——正则分析前先拒畸形输入。
        reject_reason = self._validate_input(command)
        if reject_reason:
            self._write_audit(thread_id, command, "block", truncate=True)
            logger.warning("[SandboxAudit] INVALID INPUT thread=%s reason=%s", thread_id, reject_reason)
            return command, thread_id, "block", reject_reason

        # ② 分级。
        verdict = _classify_command(command)

        # ③ 审计日志。
        self._write_audit(thread_id, command, verdict)

        if verdict == "block":
            logger.warning("[SandboxAudit] BLOCKED thread=%s cmd=%r", thread_id, command)
        elif verdict == "warn":
            logger.warning("[SandboxAudit] WARN (medium-risk) thread=%s cmd=%r", thread_id, command)

        return command, thread_id, verdict, None

    # ------------------------------------------------------------------
    # wrap_tool_call hooks
    # ------------------------------------------------------------------

    @override
    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        # 非 bash 工具直接透传 handler（GraphBubbleUp 自然上抛）。
        if request.tool_call.get("name") != "bash":
            return handler(request)

        command, _, verdict, reject_reason = self._pre_process(request)
        if verdict == "block":
            reason = reject_reason or "security violation detected"
            return self._build_block_message(request, reason)
        result = handler(request)
        if verdict == "warn":
            result = self._append_warn_to_result(result, command)
        return result

    @override
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        if request.tool_call.get("name") != "bash":
            return await handler(request)

        command, _, verdict, reject_reason = self._pre_process(request)
        if verdict == "block":
            reason = reject_reason or "security violation detected"
            return self._build_block_message(request, reason)
        result = await handler(request)
        if verdict == "warn":
            result = self._append_warn_to_result(result, command)
        return result
