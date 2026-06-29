"""动态上下文中间件（M13 memory，重写）。

把动态信息（当前日期 + per-user 记忆）作为 ``<system-reminder>`` 注入对话。基础系统提示词
保持**静态**以最大化前缀缓存复用；动态部分用 ID-swap 技术冻结首条 HumanMessage，使整个会话
里首条消息内容永不变 → 后续每轮都能命中缓存。

对齐 deer ``agents/middlewares/dynamic_context_middleware.py``：

- **before_agent** 注入（非 wrap_model_call），每轮在 agent 执行前注入。
- **ID-swap 冻结**：首轮把完整提醒（记忆 + 日期）作为独立 HumanMessage 注入到首条用户消息
  前，复用原消息 ID 让 add_messages 原地替换；原内容用 ``{id}__user`` 派生 ID 紧随其后。
  之后首条消息内容永不变 → 缓存友好。
- **跨午夜**：对话跨过午夜时检测日期变化，给当前轮注入轻量日期更新提醒（独立 HumanMessage）。
- **async to_thread 5s 超时**：``_inject`` 做同步文件 IO（读 memory JSON）+ 可能阻塞的网络
  调用（首次 tiktoken BPE 下载）；用 ``asyncio.to_thread`` 卸载 + ``wait_for`` 限时，超时
  优雅降级（不注入）而非挂起（见 issue #3402）。

注入格式：

    <system-reminder>
    <memory>...</memory>

    <current_date>2026-05-08, Friday</current_date>
    </system-reminder>

日期更新格式：

    <system-reminder>
    <current_date>2026-05-09, Saturday</current_date>
    </system-reminder>
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage, SystemMessage

if TYPE_CHECKING:
    from deerflow.config.app_config import AppConfig

logger = logging.getLogger(__name__)

# 单次 _inject() 卸载的上限（秒）。若 gateway 启动时的预热静默失败，首个请求可能仍撞上
# 冷 tiktoken BPE 下载，阻塞到 OS TCP 超时（~26 分钟）。此上限让请求优雅降级而非挂起。
_INJECT_TIMEOUT_SECONDS = 5.0

_DATE_RE = re.compile(r"<current_date>([^<]+)</current_date>")
_DYNAMIC_CONTEXT_REMINDER_KEY = "dynamic_context_reminder"
_SUMMARY_MESSAGE_NAME = "summary"


def _extract_date(content: str) -> str | None:
    """返回 content 里第一个 ``<current_date>`` 值，无则 None。"""
    m = _DATE_RE.search(content)
    return m.group(1) if m else None


def is_dynamic_context_reminder(message: object) -> bool:
    """判断 message 是否为隐藏的动态上下文提醒。

    同时认 HumanMessage 和 SystemMessage：上游已把动态上下文提醒从 HumanMessage 迁到
    SystemMessage（HumanMessage 形态仅旧 checkpoint 残留，上游注释标注 deprecated）。mini 当前
    仍以 HumanMessage 注入（见 :meth:`DynamicContextMiddleware._make_reminder_and_user_messages`），
    但此处放开类型，让 :class:`SystemMessageCoalescingMiddleware` 的「重复日期提醒去重」对两种
    形态都生效——对齐上游 + 向前兼容（mini 若日后也迁到 SystemMessage 注入，此处无需再改）。
    """
    return isinstance(message, (HumanMessage, SystemMessage)) and bool(message.additional_kwargs.get(_DYNAMIC_CONTEXT_REMINDER_KEY))


def _last_injected_date(messages: list) -> str | None:
    """逆序扫描消息，返回最近注入的日期。

    用 ``dynamic_context_reminder`` additional_kwargs 标志检测而非内容子串匹配，故含
    ``<system-reminder>`` 的用户消息不会被误判为注入提醒。
    """
    for msg in reversed(messages):
        if is_dynamic_context_reminder(msg):
            content_str = msg.content if isinstance(msg.content, str) else str(msg.content)
            return _extract_date(content_str)
    return None


def _is_user_injection_target(message: object) -> bool:
    """判断 message 能否接收动态上下文提醒。"""
    return isinstance(message, HumanMessage) and not is_dynamic_context_reminder(message) and message.name != _SUMMARY_MESSAGE_NAME


class DynamicContextMiddleware(AgentMiddleware):
    """把记忆与当前日期作为 ``<system-reminder>`` 注入 HumanMessage。

    首轮把完整提醒（记忆 + 日期）前置于首条 HumanMessage 并持久化（同消息 ID）。首条消息随后
    整个会话冻结——内容永不再变，故前缀缓存每轮都能命中。

    跨午夜时当前日期与早先注入的日期不同，此时给**当前**（最后一条）HumanMessage 前置轻量
    日期更新提醒并持久化。新天里的后续轮次看到修正后的历史日期，跳过重复注入。
    """

    def __init__(self, agent_name: str | None = None, *, app_config: AppConfig | None = None):
        super().__init__()
        self._agent_name = agent_name
        self._app_config = app_config

    def _build_full_reminder(self) -> str:
        # 延迟导入防循环（lead_agent.prompt ↔ memory）
        from deerflow.agents.lead_agent.prompt import _get_memory_context

        # 记忆注入由 injection_enabled 门控；日期总是注入。
        injection_enabled = self._app_config.memory.injection_enabled if self._app_config else True
        memory_context = _get_memory_context(self._agent_name, app_config=self._app_config) if injection_enabled else ""
        current_date = datetime.now().strftime("%Y-%m-%d, %A")

        lines: list[str] = ["<system-reminder>"]
        if memory_context:
            lines.append(memory_context.strip())
            lines.append("")  # 记忆与日期间的空行
        lines.append(f"<current_date>{current_date}</current_date>")
        lines.append("</system-reminder>")

        return "\n".join(lines)

    def _build_date_update_reminder(self) -> str:
        current_date = datetime.now().strftime("%Y-%m-%d, %A")
        return "\n".join(
            [
                "<system-reminder>",
                f"<current_date>{current_date}</current_date>",
                "</system-reminder>",
            ]
        )

    @staticmethod
    def _make_reminder_and_user_messages(original: HumanMessage, reminder_content: str) -> tuple[HumanMessage, HumanMessage]:
        """返回 (reminder_msg, user_msg)，用 ID-swap 技术。

        reminder_msg 取原消息 ID，让 add_messages 原地替换（保位置）。user_msg 带原内容 +
        派生 ``{id}__user`` ID，由 add_messages 紧随其后 append。

        原消息无 ID 时生成稳定 UUID，使派生 ``{id}__user`` ID 永不塌成歧义的 ``None__user`` 串。
        """
        stable_id = original.id or str(uuid.uuid4())
        reminder_msg = HumanMessage(
            content=reminder_content,
            id=stable_id,
            additional_kwargs={"hide_from_ui": True, _DYNAMIC_CONTEXT_REMINDER_KEY: True},
        )
        user_msg = HumanMessage(
            content=original.content,
            id=f"{stable_id}__user",
            name=original.name,
            additional_kwargs=original.additional_kwargs,
        )
        return reminder_msg, user_msg

    def _inject(self, state) -> dict | None:
        messages = list(state.get("messages", []))
        if not messages:
            return None

        current_date = datetime.now().strftime("%Y-%m-%d, %A")
        last_date = _last_injected_date(messages)
        logger.debug(
            "DynamicContextMiddleware._inject: msg_count=%d last_date=%r current_date=%r",
            len(messages),
            last_date,
            current_date,
        )

        if last_date is None:
            # ── 首轮：作为独立 HumanMessage 注入完整提醒 ─────
            first_idx = next((i for i, m in enumerate(messages) if _is_user_injection_target(m)), None)
            if first_idx is None:
                return None
            full_reminder = self._build_full_reminder()
            logger.info(
                "DynamicContextMiddleware: injecting full reminder (len=%d, has_memory=%s) into first HumanMessage id=%r",
                len(full_reminder),
                "<memory>" in full_reminder,
                messages[first_idx].id,
            )
            reminder_msg, user_msg = self._make_reminder_and_user_messages(messages[first_idx], full_reminder)
            return {"messages": [reminder_msg, user_msg]}

        if last_date == current_date:
            # ── 同一天：无事可做 ──────────────────────────────────────────
            return None

        # ── 跨午夜：作为独立 HumanMessage 注入日期更新提醒 ──
        last_human_idx = next((i for i in reversed(range(len(messages))) if _is_user_injection_target(messages[i])), None)
        if last_human_idx is None:
            return None

        reminder_msg, user_msg = self._make_reminder_and_user_messages(messages[last_human_idx], self._build_date_update_reminder())
        logger.info("DynamicContextMiddleware: midnight crossing detected — injected date update before current turn")
        return {"messages": [reminder_msg, user_msg]}

    def before_agent(self, state, runtime):
        return self._inject(state)

    async def abefore_agent(self, state, runtime):
        # _inject() 做同步文件 IO（memory JSON 加载）+ 可能阻塞的网络调用（首次 tiktoken
        # encoding 下载）。卸载到线程避免事件循环被阻塞——阻塞调用会饿死所有并发 HTTP 处理器
        # （鉴权、SSE 心跳等）。见 issue #3402。
        #
        # 有界超时：若启动预热静默失败（如部署时网络抖动），首个请求的冷 tiktoken 下载可
        # 阻塞数十分钟（OS TCP 超时）。给注入限时，让请求优雅降级（无记忆上下文）而非挂起。
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._inject, state),
                timeout=_INJECT_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            logger.warning(
                "DynamicContextMiddleware: injection timed out (%.1fs); skipping memory/date injection for this turn",
                _INJECT_TIMEOUT_SECONDS,
            )
            return None
