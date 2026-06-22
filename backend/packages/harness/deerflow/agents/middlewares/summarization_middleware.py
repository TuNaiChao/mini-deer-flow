"""``DeerFlowSummarizationMiddleware``：摘要前钩子派发 + 技能抢救（M16）。

扩展 LangChain ``SummarizationMiddleware``，加两个 deer 专属能力：

1. **``before_summarization`` 钩子**：摘要删消息前派发 :class:`SummarizationEvent`，
   让记忆（M13 ``memory_flush_hook``）抢拍细节进长期记忆——否则被压缩的消息永久丢失。

2. **技能文件抢救**：摘要分区后，把最近 N 个「加载过技能文件」的 read_file 工具调用 +
   其结果**保留**在上下文里（不进摘要），避免技能正文被压掉后模型又得重读（费 token）。

摘要 LLM 调用贴 ``TAG_NOSTREAM``——中间件钩子内的 token 流会被 messages-tuple 流回调
捕获、当成幽灵 AI 消息广播给前端；nostream 标记让流处理器跳过它。保留 ``self.model``
不打 tag（父类的 profile / ls_params 检查需原模型）。
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any, Protocol, override, runtime_checkable

from langchain.agents import AgentState
from langchain.agents.middleware import SummarizationMiddleware
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, RemoveMessage, ToolMessage, get_buffer_string
from langgraph.config import get_config
from langgraph.constants import TAG_NOSTREAM
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.runtime import Runtime

from deerflow.agents.middlewares.dynamic_context_middleware import is_dynamic_context_reminder
from deerflow.agents.middlewares.tool_call_metadata import clone_ai_message_with_tool_calls

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SummarizationEvent:
    """摘要前发出的事件：即将被摘要的消息 + 保留的消息 + 上下文。"""

    messages_to_summarize: tuple[AnyMessage, ...]
    preserved_messages: tuple[AnyMessage, ...]
    thread_id: str | None
    agent_name: str | None
    runtime: Runtime


@runtime_checkable
class BeforeSummarizationHook(Protocol):
    """摘要删消息前调用的钩子。"""

    def __call__(self, event: SummarizationEvent) -> None: ...


def _resolve_thread_id(runtime: Runtime) -> str | None:
    thread_id = runtime.context.get("thread_id") if runtime.context else None
    if thread_id is None:
        try:
            config_data = get_config()
        except RuntimeError:
            return None
        thread_id = config_data.get("configurable", {}).get("thread_id")
    return thread_id


def _resolve_agent_name(runtime: Runtime) -> str | None:
    agent_name = runtime.context.get("agent_name") if runtime.context else None
    if agent_name is None:
        try:
            config_data = get_config()
        except RuntimeError:
            return None
        agent_name = config_data.get("configurable", {}).get("agent_name")
    return agent_name


def _tool_call_path(tool_call: dict[str, Any]) -> str | None:
    """从 read_file 类工具调用里尽力抽文件路径参数。"""
    args = tool_call.get("args") or {}
    if not isinstance(args, dict):
        return None
    for key in ("path", "file_path", "filepath"):
        value = args.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _clone_ai_message(
    message: AIMessage,
    tool_calls: list[dict[str, Any]],
    *,
    content: Any | None = None,
) -> AIMessage:
    return clone_ai_message_with_tool_calls(message, tool_calls, content=content)


@dataclass
class _SkillBundle:
    """一条 AIMessage 关联的技能工具调用 + 结果。"""

    ai_index: int
    skill_tool_indices: tuple[int, ...]
    skill_tool_call_ids: frozenset[str]
    skill_tool_tokens: int
    skill_key: str


class DeerFlowSummarizationMiddleware(SummarizationMiddleware):
    """带摘要前钩子派发 + 技能抢救的摘要中间件。"""

    def __init__(
        self,
        *args,
        skills_container_path: str | None = None,
        skill_file_read_tool_names: Collection[str] | None = None,
        before_summarization: list[BeforeSummarizationHook] | None = None,
        preserve_recent_skill_count: int = 5,
        preserve_recent_skill_tokens: int = 25_000,
        preserve_recent_skill_tokens_per_skill: int = 5_000,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._skills_container_path = skills_container_path or "/mnt/skills"
        self._skill_file_read_tool_names = frozenset(skill_file_read_tool_names or {"read_file", "read", "view", "cat"})
        self._before_summarization_hooks = before_summarization or []
        self._preserve_recent_skill_count = max(0, preserve_recent_skill_count)
        self._preserve_recent_skill_tokens = max(0, preserve_recent_skill_tokens)
        self._preserve_recent_skill_tokens_per_skill = max(0, preserve_recent_skill_tokens_per_skill)
        # 摘要 LLM 跑在中间件钩子内，其 token 流会被 messages-tuple 流回调捕获并当成幽灵
        # AI 消息广播给前端。给专用模型副本贴 TAG_NOSTREAM 让流处理器跳过。
        # self.model 不打 tag（父类 profile / ls_params 检查需原模型）。
        # 保留模型上已有 tag（如 lead_agent 设的 "middleware:summarize"）：with_config 浅合并
        # 会整体覆盖 tag 列表，故先取出再追加。
        existing_tags = list((getattr(self.model, "config", None) or {}).get("tags") or [])
        merged_tags = [*existing_tags, TAG_NOSTREAM] if TAG_NOSTREAM not in existing_tags else existing_tags
        self._summary_model = self.model.with_config(tags=merged_tags)

    @override
    def _create_summary(self, messages_to_summarize: list[AnyMessage]) -> str:
        return self._summarize_with(messages_to_summarize)

    @override
    async def _acreate_summary(self, messages_to_summarize: list[AnyMessage]) -> str:
        return await self._asummarize_with(messages_to_summarize)

    def _summarize_with(self, messages_to_summarize: list[AnyMessage]) -> str:
        """镜像父类 ``_create_summary`` 但调 nostream 模型。

        不在实例级换 ``self.model``：agent / 中间件被缓存跨并发 run 复用，临时换会让
        RunnableBinding 在 ``await`` 期间泄漏给其它协程，破坏父类检查原模型的逻辑。
        """
        if not messages_to_summarize:
            return "No previous conversation history."
        prompt = self._build_summary_prompt(messages_to_summarize)
        if prompt is None:
            return "Previous conversation was too long to summarize."
        try:
            response = self._summary_model.invoke(prompt, config={"metadata": {"lc_source": "summarization"}})
            return response.text.strip()
        except Exception as e:
            return f"Error generating summary: {e!s}"

    async def _asummarize_with(self, messages_to_summarize: list[AnyMessage]) -> str:
        if not messages_to_summarize:
            return "No previous conversation history."
        prompt = self._build_summary_prompt(messages_to_summarize)
        if prompt is None:
            return "Previous conversation was too long to summarize."
        try:
            response = await self._summary_model.ainvoke(prompt, config={"metadata": {"lc_source": "summarization"}})
            return response.text.strip()
        except Exception as e:
            return f"Error generating summary: {e!s}"

    def _build_summary_prompt(self, messages_to_summarize: list[AnyMessage]) -> str | None:
        trimmed_messages = self._trim_messages_for_summary(messages_to_summarize)
        if not trimmed_messages:
            return None
        # 格式化避免 str() message 对象时的元数据 token 膨胀。
        formatted_messages = get_buffer_string(trimmed_messages)
        return self.summary_prompt.format(messages=formatted_messages).rstrip()

    def before_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self._maybe_summarize(state, runtime)

    async def abefore_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        return await self._amaybe_summarize(state, runtime)

    def _maybe_summarize(self, state: AgentState, runtime: Runtime) -> dict | None:
        messages = state["messages"]
        self._ensure_message_ids(messages)

        total_tokens = self.token_counter(messages)
        if not self._should_summarize(messages, total_tokens):
            return None

        cutoff_index = self._determine_cutoff_index(messages)
        if cutoff_index <= 0:
            return None

        messages_to_summarize, preserved_messages = self._partition_with_skill_rescue(messages, cutoff_index)
        messages_to_summarize, preserved_messages = self._preserve_dynamic_context_reminders(messages_to_summarize, preserved_messages)
        self._fire_hooks(messages_to_summarize, preserved_messages, runtime)
        summary = self._create_summary(messages_to_summarize)
        new_messages = self._build_new_messages(summary)

        return {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *new_messages, *preserved_messages]}

    async def _amaybe_summarize(self, state: AgentState, runtime: Runtime) -> dict | None:
        messages = state["messages"]
        self._ensure_message_ids(messages)

        total_tokens = self.token_counter(messages)
        if not self._should_summarize(messages, total_tokens):
            return None

        cutoff_index = self._determine_cutoff_index(messages)
        if cutoff_index <= 0:
            return None

        messages_to_summarize, preserved_messages = self._partition_with_skill_rescue(messages, cutoff_index)
        messages_to_summarize, preserved_messages = self._preserve_dynamic_context_reminders(messages_to_summarize, preserved_messages)
        self._fire_hooks(messages_to_summarize, preserved_messages, runtime)
        summary = await self._acreate_summary(messages_to_summarize)
        new_messages = self._build_new_messages(summary)

        return {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *new_messages, *preserved_messages]}

    @override
    def _build_new_messages(self, summary: str) -> list[HumanMessage]:
        """覆写：用特殊 name='summary' 的 HumanMessage。

        前端据此隐藏显示，但仍作为模型上下文（对齐 deer）。
        """
        return [HumanMessage(content=f"Here is a summary of the conversation to date:\n\n{summary}", name="summary")]

    def _preserve_dynamic_context_reminders(
        self,
        messages_to_summarize: list[AnyMessage],
        preserved_messages: list[AnyMessage],
    ) -> tuple[list[AnyMessage], list[AnyMessage]]:
        """把隐藏的动态上下文 reminder 排除在摘要压缩外。

        这些 reminder 带当前日期 + 记忆。若被摘要删掉，DynamicContextMiddleware 会把摘要
        HumanMessage 误当首条用户消息，把 reminder 注到错误位置。
        """
        reminders = [msg for msg in messages_to_summarize if is_dynamic_context_reminder(msg)]
        if not reminders:
            return messages_to_summarize, preserved_messages

        remaining = [msg for msg in messages_to_summarize if not is_dynamic_context_reminder(msg)]
        return remaining, reminders + preserved_messages

    def _partition_with_skill_rescue(
        self,
        messages: list[AnyMessage],
        cutoff_index: int,
    ) -> tuple[list[AnyMessage], list[AnyMessage]]:
        """像父类一样分区，然后抢救最近加载的技能 bundle。"""
        to_summarize, preserved = self._partition_messages(messages, cutoff_index)

        if self._preserve_recent_skill_count == 0 or self._preserve_recent_skill_tokens == 0 or not to_summarize:
            return to_summarize, preserved

        try:
            bundles = self._find_skill_bundles(to_summarize, self._skills_container_path)
        except Exception:
            logger.exception("Skill-preserving summarization rescue failed; falling back to default partition")
            return to_summarize, preserved

        if not bundles:
            return to_summarize, preserved

        rescue_bundles = self._select_bundles_to_rescue(bundles)
        if not rescue_bundles:
            return to_summarize, preserved

        bundles_by_ai_index = {bundle.ai_index: bundle for bundle in rescue_bundles}
        rescue_tool_indices = {idx for bundle in rescue_bundles for idx in bundle.skill_tool_indices}
        rescued: list[AnyMessage] = []
        remaining: list[AnyMessage] = []
        for i, msg in enumerate(to_summarize):
            bundle = bundles_by_ai_index.get(i)
            if bundle is not None and isinstance(msg, AIMessage):
                rescued_tool_calls = [tc for tc in msg.tool_calls if tc.get("id") in bundle.skill_tool_call_ids]
                remaining_tool_calls = [tc for tc in msg.tool_calls if tc.get("id") not in bundle.skill_tool_call_ids]

                if rescued_tool_calls:
                    rescued.append(_clone_ai_message(msg, rescued_tool_calls, content=""))
                if remaining_tool_calls or msg.content:
                    remaining.append(_clone_ai_message(msg, remaining_tool_calls))
                continue

            if i in rescue_tool_indices:
                rescued.append(msg)
                continue

            remaining.append(msg)

        return remaining, rescued + preserved

    def _find_skill_bundles(self, messages: list[AnyMessage], skills_root: str) -> list[_SkillBundle]:
        """定位「AIMessage + 配对 ToolMessage 加载技能文件」的分组。"""
        bundles: list[_SkillBundle] = []
        n = len(messages)
        i = 0
        while i < n:
            msg = messages[i]
            if not (isinstance(msg, AIMessage) and msg.tool_calls):
                i += 1
                continue

            tool_calls = list(msg.tool_calls)
            skill_paths_by_id: dict[str, str] = {}
            for tc in tool_calls:
                if self._is_skill_tool_call(tc, skills_root):
                    tc_id = tc.get("id")
                    path = _tool_call_path(tc)
                    if tc_id and path:
                        skill_paths_by_id[tc_id] = path

            if not skill_paths_by_id:
                i += 1
                continue

            skill_tool_tokens = 0
            skill_key_parts: list[str] = []
            skill_tool_indices: list[int] = []
            matched_skill_call_ids: set[str] = set()

            j = i + 1
            while j < n and isinstance(messages[j], ToolMessage):
                j += 1

            for k in range(i + 1, j):
                tool_msg = messages[k]
                if isinstance(tool_msg, ToolMessage) and tool_msg.tool_call_id in skill_paths_by_id:
                    skill_tool_tokens += self.token_counter([tool_msg])
                    skill_key_parts.append(skill_paths_by_id[tool_msg.tool_call_id])
                    skill_tool_indices.append(k)
                    matched_skill_call_ids.add(tool_msg.tool_call_id)

            if not skill_tool_indices:
                i = j
                continue

            bundles.append(
                _SkillBundle(
                    ai_index=i,
                    skill_tool_indices=tuple(skill_tool_indices),
                    skill_tool_call_ids=frozenset(matched_skill_call_ids),
                    skill_tool_tokens=skill_tool_tokens,
                    skill_key="|".join(sorted(skill_key_parts)),
                )
            )
            i = j

        return bundles

    def _select_bundles_to_rescue(self, bundles: list[_SkillBundle]) -> list[_SkillBundle]:
        """按 count / token 预算从新到旧挑选保留的 bundle。"""
        selected: list[_SkillBundle] = []
        if not bundles:
            return selected

        seen_skill_keys: set[str] = set()
        total_tokens = 0
        kept = 0

        for bundle in reversed(bundles):
            if kept >= self._preserve_recent_skill_count:
                break
            if bundle.skill_key in seen_skill_keys:
                continue
            if bundle.skill_tool_tokens > self._preserve_recent_skill_tokens_per_skill:
                continue
            if total_tokens + bundle.skill_tool_tokens > self._preserve_recent_skill_tokens:
                continue

            selected.append(bundle)
            total_tokens += bundle.skill_tool_tokens
            kept += 1
            seen_skill_keys.add(bundle.skill_key)

        selected.reverse()
        return selected

    def _is_skill_tool_call(self, tool_call: dict[str, Any], skills_root: str) -> bool:
        name = tool_call.get("name") or ""
        if name not in self._skill_file_read_tool_names:
            return False
        path = _tool_call_path(tool_call)
        if not path:
            return False
        normalized_root = skills_root.rstrip("/")
        return path == normalized_root or path.startswith(normalized_root + "/")

    def _fire_hooks(
        self,
        messages_to_summarize: list[AnyMessage],
        preserved_messages: list[AnyMessage],
        runtime: Runtime,
    ) -> None:
        if not self._before_summarization_hooks:
            return

        event = SummarizationEvent(
            messages_to_summarize=tuple(messages_to_summarize),
            preserved_messages=tuple(preserved_messages),
            thread_id=_resolve_thread_id(runtime),
            agent_name=_resolve_agent_name(runtime),
            runtime=runtime,
        )

        for hook in self._before_summarization_hooks:
            try:
                hook(event)
            except Exception:
                hook_name = getattr(hook, "__name__", None) or type(hook).__name__
                logger.exception("before_summarization hook %s failed", hook_name)


def _create_summarization_middleware(*, app_config) -> DeerFlowSummarizationMiddleware | None:
    """从 config 构造摘要中间件；禁用返回 None。

    把 mini 的 :class:`SummarizationConfig`（trigger/keep 是 ``ContextSize``）转成
    LangChain ``SummarizationMiddleware`` 要的 tuple；模型贴 ``middleware:summarize`` tag
    供 RunJournal 归因 + ``attach_tracing=False``（图根已挂回调，模型级再挂重复 span）。
    memory.enabled 时接 M13 的 ``memory_flush_hook``。
    """
    from deerflow.agents.memory.summarization_hook import memory_flush_hook
    from deerflow.models import create_chat_model

    config = app_config.summarization
    if not config.enabled:
        return None

    # trigger: ContextSize | list[ContextSize] | None → tuple / list[tuple]
    trigger = None
    if config.trigger is not None:
        if isinstance(config.trigger, list):
            trigger = [t.to_tuple() for t in config.trigger]
        else:
            trigger = config.trigger.to_tuple()

    keep = config.keep.to_tuple()

    if config.model_name:
        model = create_chat_model(name=config.model_name, thinking_enabled=False, app_config=app_config, attach_tracing=False)
    else:
        model = create_chat_model(thinking_enabled=False, app_config=app_config, attach_tracing=False)
    model = model.with_config(tags=["middleware:summarize"])

    kwargs: dict[str, Any] = {"model": model, "trigger": trigger, "keep": keep}
    if config.trim_tokens_to_summarize is not None:
        kwargs["trim_tokens_to_summarize"] = config.trim_tokens_to_summarize
    if config.summary_prompt is not None:
        kwargs["summary_prompt"] = config.summary_prompt

    hooks: list[BeforeSummarizationHook] = []
    if app_config.memory.enabled:
        hooks.append(memory_flush_hook)

    skills_container_path = getattr(app_config.skills, "container_path", None) or "/mnt/skills"

    return DeerFlowSummarizationMiddleware(
        **kwargs,
        skills_container_path=skills_container_path,
        before_summarization=hooks,
    )
