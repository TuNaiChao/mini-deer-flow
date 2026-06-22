"""子代理执行引擎。

设计要点（⚠️ 红线 #34：**单 scheduler pool + 持久化隔离事件循环**，非双线程池）：

- ``_scheduler_pool = ThreadPoolExecutor(max_workers=3)``：唯一的线程池，负责后台
  任务的调度与编排（``execute_async`` 提交到这里）。
- ``_isolated_subagent_loop``：一个 **daemon 线程上的持久 ``asyncio`` 事件循环**，常驻
  进程。当父 agent 自己已在一个事件循环里跑时，子代理协程提交到这个隔离循环执行，
  而不是每次 ``asyncio.run`` 起一个新循环再关掉——复用循环 = 复用共享 async client
  （httpx 等），避免把 client 绑死到一个随即关闭的短命循环上。
- ``MAX_CONCURRENT_SUBAGENTS = 3``：并发上限。由 ``SubagentLimitMiddleware``（M16 第 19
  步）在模型响应后**截断**多余的 ``task`` 调用保证；本执行器不再自建第二线程池。
- 子代理图 ``checkpointer=False``：子代理是一次性的，从不 resume，不继承父 run 的
  checkpointer。
- 协作取消：在 ``astream`` 迭代**边界**检查 ``cancel_event``——单个迭代内的长工具调用
  不会被强中断，要到下一个 chunk 才能停（``Future.cancel()`` 杀不掉已在跑的子代理线程）。

**Phase 2 骨架说明**：本模块的「执行机制」（状态/结果/线程池/隔离循环/取消/超时/后台任务
存储）完整可用。``_create_agent`` 真正构造 agent 依赖 Phase 7 的
``build_subagent_runtime_middlewares``、Phase 5 的 ``tool_search`` 延迟装配、Phase 4 的
skills——这些用**延迟导入 + 缺包降级**处理：模块当前缺失时优雅退化（无中间件/无延迟/
无技能），等对应 Phase 落地后自动生效，无需改本文件。
"""

import asyncio
import atexit
import logging
import threading
import uuid
from collections.abc import Callable, Coroutine
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from contextvars import Context, copy_context
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from langchain.agents import create_agent
from langchain.tools import BaseTool
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from deerflow.agents.thread_state import ThreadState
from deerflow.config import get_app_config
from deerflow.config.app_config import AppConfig
from deerflow.models import create_chat_model
from deerflow.subagents.config import SubagentConfig, resolve_subagent_model_name
from deerflow.subagents.token_collector import SubagentTokenCollector

# 注：tool_search 的 ``DeferredToolSetup`` 仅作类型注解出现在 deer 源里；mini 的
# ``_build_initial_state`` 返回 ``tuple[..., Any]``，运行时不依赖该类型，故不强 import
# （强 import 会触发 tools/builtins/__init__ -> task_tool -> `from deerflow.subagents import ...`
# 的循环，且 M15 未落地）。

logger = logging.getLogger(__name__)


# 进程重启（reload）时先把上一份隔离循环关掉，避免泄漏。
_previous_shutdown_isolated_subagent_loop = globals().get("_shutdown_isolated_subagent_loop")
if callable(_previous_shutdown_isolated_subagent_loop):
    atexit.unregister(_previous_shutdown_isolated_subagent_loop)
    _previous_shutdown_isolated_subagent_loop()


class SubagentStatus(Enum):
    """子代理执行状态。"""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"

    @property
    def is_terminal(self) -> bool:
        return self in {
            type(self).COMPLETED,
            type(self).FAILED,
            type(self).CANCELLED,
            type(self).TIMED_OUT,
        }


@dataclass
class SubagentResult:
    """子代理执行结果。

    Attributes:
        task_id: 本次执行的唯一 id。
        trace_id: 分布式追踪 id（串起父与子代理日志）。
        status: 当前状态。
        result: 最终结果消息（完成时）。
        error: 错误消息（失败时）。
        started_at: 开始时间。
        completed_at: 完成时间。
        ai_messages: 执行中产生的完整 AI 消息（dict 列表）。
    """

    task_id: str
    trace_id: str
    status: SubagentStatus
    result: str | None = None
    error: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    ai_messages: list[dict[str, Any]] | None = None
    token_usage_records: list[dict[str, int | str]] = field(default_factory=list)
    usage_reported: bool = False
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    _state_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self):
        """初始化可变默认。"""
        if self.ai_messages is None:
            self.ai_messages = []

    def try_set_terminal(
        self,
        status: SubagentStatus,
        *,
        result: str | None = None,
        error: str | None = None,
        completed_at: datetime | None = None,
        ai_messages: list[dict[str, Any]] | None = None,
        token_usage_records: list[dict[str, int | str]] | None = None,
    ) -> bool:
        """恰好一次地设终态。

        后台超时/取消与执行 worker 会在同一个 result holder 上竞争。第一个终态转换赢；
        迟到的终态写入不得改状态或负载字段。
        """
        if not status.is_terminal:
            raise ValueError(f"Status {status} is not terminal")

        with self._state_lock:
            if self.status.is_terminal:
                return False

            if result is not None:
                self.result = result
            if error is not None:
                self.error = error
            if ai_messages is not None:
                self.ai_messages = ai_messages
            if token_usage_records is not None:
                self.token_usage_records = token_usage_records
            self.completed_at = completed_at or datetime.now()
            self.status = status
            return True


# 后台任务结果的全局存储
_background_tasks: dict[str, SubagentResult] = {}
_background_tasks_lock = threading.Lock()

# 后台任务调度与编排的唯一线程池（红线 #34：单 scheduler pool，非双池）
_scheduler_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="subagent-scheduler-")

# 当父 agent 已在事件循环里时，子代理协程跑在这个持久隔离循环上。复用一个长命循环
# 避免每次执行起一个新循环再关掉绑在它上面的 async client（红线 #34）。
_isolated_subagent_loop: asyncio.AbstractEventLoop | None = None
_isolated_subagent_loop_thread: threading.Thread | None = None
_isolated_subagent_loop_started: threading.Event | None = None
_isolated_subagent_loop_lock = threading.Lock()


def _run_isolated_subagent_loop(
    loop: asyncio.AbstractEventLoop,
    started_event: threading.Event,
) -> None:
    """在专用 daemon 线程里跑持久隔离子代理循环。"""
    asyncio.set_event_loop(loop)
    loop.call_soon(started_event.set)
    try:
        loop.run_forever()
    finally:
        started_event.clear()


def _shutdown_isolated_subagent_loop() -> None:
    """停止并关闭持久隔离子代理循环。"""
    global _isolated_subagent_loop, _isolated_subagent_loop_thread, _isolated_subagent_loop_started

    with _isolated_subagent_loop_lock:
        loop = _isolated_subagent_loop
        thread = _isolated_subagent_loop_thread
        _isolated_subagent_loop = None
        _isolated_subagent_loop_thread = None
        _isolated_subagent_loop_started = None

    if loop is None:
        return

    if loop.is_running():
        loop.call_soon_threadsafe(loop.stop)

    if thread is not None and thread.is_alive() and thread is not threading.current_thread():
        thread.join(timeout=1)

    thread_stopped = thread is None or not thread.is_alive()
    loop_stopped = not loop.is_running()

    if not loop.is_closed():
        if thread_stopped and loop_stopped:
            loop.close()
        else:
            logger.warning(
                "Skipping close of isolated subagent loop because shutdown did not complete within timeout (thread_alive=%s, loop_running=%s)",
                thread is not None and thread.is_alive(),
                loop.is_running(),
            )


atexit.register(_shutdown_isolated_subagent_loop)


def _get_isolated_subagent_loop() -> asyncio.AbstractEventLoop:
    """返回隔离子代理执行用的持久事件循环（首次用时惰性起一个 daemon 线程）。"""
    global _isolated_subagent_loop, _isolated_subagent_loop_thread, _isolated_subagent_loop_started
    with _isolated_subagent_loop_lock:
        thread_is_alive = _isolated_subagent_loop_thread is not None and _isolated_subagent_loop_thread.is_alive()
        loop_is_usable = _isolated_subagent_loop is not None and not _isolated_subagent_loop.is_closed() and _isolated_subagent_loop.is_running() and thread_is_alive

        if not loop_is_usable:
            loop = asyncio.new_event_loop()
            started_event = threading.Event()
            thread = threading.Thread(
                target=_run_isolated_subagent_loop,
                args=(loop, started_event),
                name="subagent-persistent-loop",
                daemon=True,
            )
            thread.start()
            if not started_event.wait(timeout=5):
                loop.call_soon_threadsafe(loop.stop)
                thread.join(timeout=1)
                loop.close()
                raise RuntimeError("Timed out starting isolated subagent event loop")
            _isolated_subagent_loop = loop
            _isolated_subagent_loop_thread = thread
            _isolated_subagent_loop_started = started_event

        if _isolated_subagent_loop is None:
            raise RuntimeError("Isolated subagent event loop is not initialized")
        return _isolated_subagent_loop


def _submit_to_isolated_loop_in_context(
    context: Context,
    coro_factory: Callable[[], Coroutine[Any, Any, SubagentResult]],
) -> Future[SubagentResult]:
    """把协程提交到隔离循环，同时保留 ContextVar 状态。"""
    return context.run(
        lambda: asyncio.run_coroutine_threadsafe(
            coro_factory(),
            _get_isolated_subagent_loop(),
        )
    )


def _filter_tools(
    all_tools: list[BaseTool],
    allowed: list[str] | None,
    disallowed: list[str] | None,
) -> list[BaseTool]:
    """按子代理配置过滤工具。

    Args:
        all_tools: 全部可用工具。
        allowed: 工具名白名单。给出则只留这些。
        disallowed: 工具名黑名单。总是排除。

    Returns:
        过滤后的工具列表。
    """
    filtered = all_tools

    # 白名单
    if allowed is not None:
        allowed_set = set(allowed)
        filtered = [t for t in filtered if t.name in allowed_set]

    # 黑名单
    if disallowed is not None:
        disallowed_set = set(disallowed)
        filtered = [t for t in filtered if t.name not in disallowed_set]

    return filtered


def _resolve_subagent_runtime_middlewares(
    app_config: AppConfig | None,
    model_name: str | None,
    *,
    lazy_init: bool,
    deferred_setup: Any = None,
) -> list:
    """组装子代理运行时中间件（延迟导入 M16 的 helper，缺包降级到最小集）。

    deer 的 ``build_subagent_runtime_middlewares`` 在 M16 落地；落地前用最小集
    ``[ToolErrorHandlingMiddleware()]``（mini 已有该类）兜底，让 Phase 2 的骨架可跑、
    可测。M16 落地后本函数自动切到真实 helper，无需改调用方。
    """
    try:
        from deerflow.agents.middlewares.tool_error_handling_middleware import build_subagent_runtime_middlewares

        return build_subagent_runtime_middlewares(
            app_config=app_config,
            model_name=model_name,
            lazy_init=lazy_init,
            deferred_setup=deferred_setup,
        )
    except ImportError:
        # M16 未落地：最小中间件集（ToolErrorHandlingMiddleware 已存在）。
        from deerflow.agents.middlewares.tool_error_handling_middleware import ToolErrorHandlingMiddleware

        return [ToolErrorHandlingMiddleware()]


class SubagentExecutor:
    """运行子代理的执行器。"""

    def __init__(
        self,
        config: SubagentConfig,
        tools: list[BaseTool],
        app_config: AppConfig | None = None,
        parent_model: str | None = None,
        sandbox_state: dict | None = None,
        thread_data: dict | None = None,
        thread_id: str | None = None,
        trace_id: str | None = None,
    ):
        """初始化执行器。

        Args:
            config: 子代理配置。
            tools: 全部可用工具（会被过滤）。
            app_config: 解析好的 AppConfig。None 时 ``_create_agent`` 回退 ``get_app_config()``
                （与 lead-agent factory 一致）。
            parent_model: 父 agent 的模型名（继承用）。
            sandbox_state: 父 agent 的沙箱状态（mini 用 dict）。
            thread_data: 父 agent 的线程数据（mini 用 dict）。
            thread_id: 线程 id（沙箱操作用）。
            trace_id: 父的追踪 id。
        """
        self.config = config
        self.app_config = app_config
        self.parent_model = parent_model
        # 仅当不需要加载 config.yaml 时才预解析模型名；否则推迟到 _create_agent
        # （它本就会加载 app_config），让单测在无 config 文件时也能构造执行器。
        if config.model != "inherit" or parent_model is not None or app_config is not None:
            self.model_name: str | None = resolve_subagent_model_name(config, parent_model, app_config=app_config)
        else:
            self.model_name = None
        self.sandbox_state = sandbox_state
        self.thread_data = thread_data
        self.thread_id = thread_id
        # 顶层调用未提供时生成 trace_id
        self.trace_id = trace_id or str(uuid.uuid4())[:8]

        self._base_tools = _filter_tools(
            tools,
            config.tools,
            config.disallowed_tools,
        )
        self.tools = self._base_tools

        logger.info(f"[trace={self.trace_id}] SubagentExecutor initialized: {config.name} with {len(self.tools)} tools")

    def _create_agent(self, tools: list[BaseTool] | None = None, *, deferred_setup: Any = None):
        """创建 agent 实例。

        ``deferred_setup``（在 ``_build_initial_state`` 组装）携带延迟 MCP 工具名 + catalog
        hash，让子代理拿到与 lead agent 相同的 ``DeferredToolFilterMiddleware``。None 为 no-op。
        """
        app_config = self.app_config or get_app_config()
        if self.model_name is None:
            self.model_name = resolve_subagent_model_name(self.config, self.parent_model, app_config=app_config)
        model = create_chat_model(name=self.model_name, thinking_enabled=False, app_config=app_config)

        # 复用与 lead agent 共享的中间件组装（延迟导入 M16，缺包降级）。
        middlewares = _resolve_subagent_runtime_middlewares(
            app_config=app_config,
            model_name=self.model_name,
            lazy_init=True,
            deferred_setup=deferred_setup,
        )

        # system_prompt 放进初始状态消息（见 _build_initial_state），避免多条 SystemMessage
        # （有些 LLM API 不支持「System message must be at the beginning」）。
        return create_agent(
            model=model,
            tools=tools if tools is not None else self.tools,
            middleware=middlewares,
            system_prompt=None,
            state_schema=ThreadState,
            checkpointer=False,
        )

    async def _load_skills(self) -> list[Any]:
        """按 config.skills 加载已启用技能元数据（延迟导入 M14，缺包降级到无技能）。

        M14 skills 未落地时返回空列表——子代理仍可跑，只是不带技能注入。
        """
        if self.config.skills is not None and len(self.config.skills) == 0:
            logger.info(f"[trace={self.trace_id}] Subagent {self.config.name} skills=[] — skipping skill loading")
            return []

        try:
            from deerflow.skills.storage import get_or_new_skill_storage
        except ImportError:
            # M14 skills 未落地：降级为无技能。
            logger.debug(f"[trace={self.trace_id}] skills package unavailable; subagent {self.config.name} runs without skills")
            return []

        try:
            storage_kwargs = {"app_config": self.app_config} if self.app_config is not None else {}
            storage = await asyncio.to_thread(get_or_new_skill_storage, **storage_kwargs)
            # 用 asyncio.to_thread 卸载，避免阻塞事件循环（LangGraph ASGI 要求）
            all_skills = await asyncio.to_thread(storage.load_skills, enabled_only=True)
            logger.info(f"[trace={self.trace_id}] Subagent {self.config.name} loaded {len(all_skills)} enabled skills from disk")
        except Exception:
            # 技能是子代理的可选项：存储 / 配置任何加载失败都降级为无技能，不让子代理跑不起来
            # （对齐「缺包降级到无技能」语义；M14 落地前靠 ImportError 短路，现由这里统一兜底）。
            logger.warning(f"[trace={self.trace_id}] Failed to load skills for subagent {self.config.name}; running without skills", exc_info=True)
            return []

        if not all_skills:
            logger.info(f"[trace={self.trace_id}] Subagent {self.config.name} no enabled skills found")
            return []

        # 按 config.skills 白名单过滤
        if self.config.skills is not None:
            allowed = set(self.config.skills)
            return [s for s in all_skills if s.name in allowed]
        return all_skills

    def _apply_skill_allowed_tools(self, skills: list[Any]) -> list[BaseTool]:
        """按技能的 allowed-tools 策略收紧工具（延迟导入 M14，缺包原样返回）。"""
        if not skills:
            return self._base_tools
        try:
            from deerflow.skills.tool_policy import filter_tools_by_skill_allowed_tools

            return filter_tools_by_skill_allowed_tools(self._base_tools, skills)
        except ImportError:
            # M14 未落地：无法按技能策略过滤，原样返回基础工具。
            return self._base_tools

    async def _load_skill_messages(self, skills: list[Any]) -> list[SystemMessage]:
        """把技能内容作为对话项注入（Codex 模式：developer message 而非 system prompt 文本）。

        config.skills 白名单控制加载哪些：None = 全部已启用；[] = 无；列表 = 只这些。

        Returns:
            含技能内容的 SystemMessage 列表。
        """
        if not skills:
            return []

        # 读每个技能的 SKILL.md 内容，造对话项
        messages = []
        for skill in skills:
            try:
                content = await asyncio.to_thread(skill.skill_file.read_text, encoding="utf-8")
                content = content.strip()
                if content:
                    messages.append(SystemMessage(content=f'<skill name="{skill.name}">\n{content}\n</skill>'))
                    logger.info(f"[trace={self.trace_id}] Subagent {self.config.name} loaded skill: {skill.name}")
            except Exception:
                logger.debug(f"[trace={self.trace_id}] Failed to read skill {skill.name}", exc_info=True)

        return messages

    async def _build_initial_state(self, task: str) -> tuple[dict[str, Any], list[BaseTool], Any]:
        """构造 agent 执行的初始状态。

        Args:
            task: 任务描述。

        Returns:
            ``(state, final_tools, deferred_setup)``。``final_tools`` 是策略过滤后的工具列表，
            启用延迟时追加 ``tool_search`` 工具；``deferred_setup`` 给 ``_create_agent`` 用，
            让 agent 构建与注入的 ``<available-deferred-tools>`` 段共享同一 catalog/hash。
        """
        # 延迟导入 tool_search（M15）：见模块顶部 TYPE_CHECKING 注释——导入会触发
        # tools/builtins/__init__，在包自身初始化期间重入本包。M15 未落地则跳过延迟装配。
        deferred_setup: Any = None
        deferred_section = ""
        final_tools: list[BaseTool]

        # 技能作为对话项加载（Codex 模式）
        skills = await self._load_skills()
        filtered_tools = self._apply_skill_allowed_tools(skills)

        try:
            from deerflow.tools.builtins.tool_search import assemble_deferred_tools, get_deferred_tools_prompt_section

            # 在策略过滤**之后**组装 tool_search（fail-closed），镜像 lead 路径，让子代理
            # 也不再绑定完整 MCP schema。生成的 tool_search helper 故意不受子代理的
            # name 级 allow/deny（config.tools/disallowed_tools）约束：它的 catalog 从已
            # 过滤列表构建，永远不可能暴露策略拒绝的工具。与 lead agent 一致。
            enabled = (self.app_config or get_app_config()).tool_search.enabled
            final_tools, deferred_setup = assemble_deferred_tools(filtered_tools, enabled=enabled)
            deferred_section = get_deferred_tools_prompt_section(deferred_names=deferred_setup.deferred_names)
        except ImportError:
            # M15 tool_search 未落地：不做延迟装配，工具即过滤后的列表。
            final_tools = filtered_tools

        skill_messages = await self._load_skill_messages(skills)

        # 把 system_prompt 与技能合成单条 SystemMessage。
        # 有些 LLM API 拒绝多条 SystemMessage（"System message must be at the beginning."）。
        system_parts: list[str] = []
        if self.config.system_prompt:
            system_parts.append(self.config.system_prompt)
        for skill_msg in skill_messages:
            system_parts.append(skill_msg.content)
        # 在提示词里点名延迟 MCP 工具；它们的 schema 在 tool_search 提升前先扣住。
        # 空集合 -> "" -> 不追加。
        if deferred_section:
            system_parts.append(deferred_section)

        messages: list[Any] = []
        if system_parts:
            messages.append(SystemMessage(content="\n\n".join(system_parts)))

        # 然后是真正的任务
        messages.append(HumanMessage(content=task))

        state: dict[str, Any] = {
            "messages": messages,
        }

        # 从父 agent 透传 sandbox 与 thread_data
        if self.sandbox_state is not None:
            state["sandbox"] = self.sandbox_state
        if self.thread_data is not None:
            state["thread_data"] = self.thread_data

        return state, final_tools, deferred_setup

    async def _aexecute(self, task: str, result_holder: SubagentResult | None = None) -> SubagentResult:
        """异步执行一个任务。

        Args:
            task: 子代理的任务描述。
            result_holder: 可选的预创建 result 对象，执行中更新。

        Returns:
            含执行结果的 SubagentResult。
        """
        if result_holder is not None:
            # 用提供的 result holder（带实时更新的异步执行）
            result = result_holder
        else:
            # 同步执行：新建 result
            task_id = str(uuid.uuid4())[:8]
            result = SubagentResult(
                task_id=task_id,
                trace_id=self.trace_id,
                status=SubagentStatus.RUNNING,
                started_at=datetime.now(),
            )
        ai_messages = result.ai_messages
        if ai_messages is None:
            ai_messages = []
            result.ai_messages = ai_messages

        collector: SubagentTokenCollector | None = None
        try:
            state, final_tools, deferred_setup = await self._build_initial_state(task)
            agent = self._create_agent(final_tools, deferred_setup=deferred_setup)

            # 子代理 LLM 调用的 token 收集器
            collector_caller = f"subagent:{self.config.name}"
            collector = SubagentTokenCollector(caller=collector_caller)

            # 带 thread_id（沙箱访问）+ recursion limit 的 config
            run_config: RunnableConfig = {
                "recursion_limit": self.config.max_turns,
                "callbacks": [collector],
                "tags": [collector_caller],
            }
            context: dict[str, Any] = {}
            if self.thread_id:
                run_config["configurable"] = {"thread_id": self.thread_id}
                context["thread_id"] = self.thread_id
            if self.app_config is not None:
                context["app_config"] = self.app_config

            logger.info(f"[trace={self.trace_id}] Subagent {self.config.name} starting async execution with max_turns={self.config.max_turns}")

            # 用 stream 而非 invoke，拿实时更新——能在 AI 消息生成时逐条收集
            final_state = None

            # 前置检查：流式开始前若已取消，立即退出
            if result.cancel_event.is_set():
                logger.info(f"[trace={self.trace_id}] Subagent {self.config.name} cancelled before streaming")
                result.try_set_terminal(
                    SubagentStatus.CANCELLED,
                    error="Cancelled by user",
                    token_usage_records=collector.snapshot_records(),
                )
                return result

            async for chunk in agent.astream(state, config=run_config, context=context, stream_mode="values"):  # type: ignore[arg-type]
                # 协作取消：检查父是否请求了停止。
                # 注意：取消只在 astream 迭代边界检测到，单次迭代内的长工具调用不会被
                # 中断，要到下一个 chunk 才能停。
                if result.cancel_event.is_set():
                    logger.info(f"[trace={self.trace_id}] Subagent {self.config.name} cancelled by parent")
                    result.try_set_terminal(
                        SubagentStatus.CANCELLED,
                        error="Cancelled by user",
                        token_usage_records=collector.snapshot_records(),
                    )
                    return result

                final_state = chunk

                # 从当前 state 抽 AI 消息
                messages = chunk.get("messages", [])
                if messages:
                    last_message = messages[-1]
                    # 检查是否是新 AI 消息
                    if isinstance(last_message, AIMessage):
                        # 转 dict 供序列化
                        message_dict = last_message.model_dump()
                        # 仅在不在列表里时才加（去重）
                        # 有 id 比 id，否则比整 dict
                        message_id = message_dict.get("id")
                        is_duplicate = False
                        if message_id:
                            is_duplicate = any(msg.get("id") == message_id for msg in ai_messages)
                        else:
                            is_duplicate = message_dict in ai_messages

                        if not is_duplicate:
                            ai_messages.append(message_dict)
                            logger.info(f"[trace={self.trace_id}] Subagent {self.config.name} captured AI message #{len(ai_messages)}")

            logger.info(f"[trace={self.trace_id}] Subagent {self.config.name} completed async execution")
            token_usage_records = collector.snapshot_records()
            final_result: str | None = None

            if final_state is None:
                logger.warning(f"[trace={self.trace_id}] Subagent {self.config.name} no final state")
                final_result = "No response generated"
            else:
                # 抽最终消息——找最后一条 AIMessage
                messages = final_state.get("messages", [])
                logger.info(f"[trace={self.trace_id}] Subagent {self.config.name} final messages count: {len(messages)}")

                last_ai_message = None
                for msg in reversed(messages):
                    if isinstance(msg, AIMessage):
                        last_ai_message = msg
                        break

                if last_ai_message is not None:
                    content = last_ai_message.content
                    # 最终结果同时处理 str 与 list 内容类型
                    if isinstance(content, str):
                        final_result = content
                    elif isinstance(content, list):
                        # 从内容块列表抽文本（仅最终结果）。直接拼裸字符串块，但在完整文本块
                        # 之间保留分隔以提升可读性。
                        text_parts = []
                        pending_str_parts = []
                        for block in content:
                            if isinstance(block, str):
                                pending_str_parts.append(block)
                            elif isinstance(block, dict):
                                if pending_str_parts:
                                    text_parts.append("".join(pending_str_parts))
                                    pending_str_parts.clear()
                                text_val = block.get("text")
                                if isinstance(text_val, str):
                                    text_parts.append(text_val)
                        if pending_str_parts:
                            text_parts.append("".join(pending_str_parts))
                        final_result = "\n".join(text_parts) if text_parts else "No text content in response"
                    else:
                        final_result = str(content)
                elif messages:
                    # 兜底：没 AIMessage 就用最后一条消息
                    last_message = messages[-1]
                    logger.warning(f"[trace={self.trace_id}] Subagent {self.config.name} no AIMessage found, using last message: {type(last_message)}")
                    raw_content = last_message.content if hasattr(last_message, "content") else str(last_message)
                    if isinstance(raw_content, str):
                        final_result = raw_content
                    elif isinstance(raw_content, list):
                        parts = []
                        pending_str_parts = []
                        for block in raw_content:
                            if isinstance(block, str):
                                pending_str_parts.append(block)
                            elif isinstance(block, dict):
                                if pending_str_parts:
                                    parts.append("".join(pending_str_parts))
                                    pending_str_parts.clear()
                                text_val = block.get("text")
                                if isinstance(text_val, str):
                                    parts.append(text_val)
                        if pending_str_parts:
                            parts.append("".join(pending_str_parts))
                        final_result = "\n".join(parts) if parts else "No text content in response"
                    else:
                        final_result = str(raw_content)
                else:
                    logger.warning(f"[trace={self.trace_id}] Subagent {self.config.name} no messages in final state")
                    final_result = "No response generated"

            if final_result is None:
                final_result = "No response generated"

            result.try_set_terminal(
                SubagentStatus.COMPLETED,
                result=final_result,
                token_usage_records=token_usage_records,
            )

        except Exception as e:
            logger.exception(f"[trace={self.trace_id}] Subagent {self.config.name} async execution failed")
            result.try_set_terminal(
                SubagentStatus.FAILED,
                error=str(e),
                token_usage_records=collector.snapshot_records() if collector is not None else None,
            )

        return result

    def _execute_in_isolated_loop(self, task: str, result_holder: SubagentResult | None = None) -> SubagentResult:
        """在持久隔离事件循环上执行子代理。

        当调用方已在一个事件循环里跑时，同步 ``execute()`` 走这条路。因为 ``execute()`` 是
        同步 API，这条路会阻塞调用方，而真正的协程跑在长命隔离循环上。复用那个循环让共享
        async client 不被绑到一个随即关闭的短命循环。
        """
        future: Future[SubagentResult] | None = None
        parent_context = copy_context()
        try:
            future = _submit_to_isolated_loop_in_context(
                parent_context,
                lambda: self._aexecute(task, result_holder),
            )
            return future.result(timeout=self.config.timeout_seconds)
        except FuturesTimeoutError:
            if result_holder is not None:
                result_holder.cancel_event.set()
            if future is not None:
                future.cancel()
            raise
        except Exception:
            if future is None:
                logger.debug(
                    f"[trace={self.trace_id}] Failed to submit subagent {self.config.name} to the isolated event loop",
                    exc_info=True,
                )
            else:
                logger.debug(
                    f"[trace={self.trace_id}] Subagent {self.config.name} failed while executing on the isolated event loop",
                    exc_info=True,
                )
            raise

    def execute(self, task: str, result_holder: SubagentResult | None = None) -> SubagentResult:
        """同步执行一个任务（异步执行的同步包装）。

        在新事件循环里跑异步执行，让异步工具（如 MCP 工具）能在线程池里用。

        当调用方已在一个事件循环里（如父 agent 是 async）时，本方法同步等待持久隔离
        循环，避免与 httpx client 等共享 async 原语发生事件循环冲突。

        Args:
            task: 子代理的任务描述。
            result_holder: 可选的预创建 result 对象，执行中更新。

        Returns:
            含执行结果的 SubagentResult。
        """
        try:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

            if loop is not None and loop.is_running():
                logger.debug(f"[trace={self.trace_id}] Subagent {self.config.name} detected running event loop, using isolated loop")
                return self._execute_in_isolated_loop(task, result_holder)

            # 标准路径：没有运行中的事件循环，用 asyncio.run
            return asyncio.run(self._aexecute(task, result_holder))
        except Exception as e:
            logger.exception(f"[trace={self.trace_id}] Subagent {self.config.name} execution failed")
            # 没有 result holder 就建一个带错误的
            if result_holder is not None:
                result = result_holder
            else:
                result = SubagentResult(
                    task_id=str(uuid.uuid4())[:8],
                    trace_id=self.trace_id,
                    status=SubagentStatus.RUNNING,
                )
            result.try_set_terminal(SubagentStatus.FAILED, error=str(e))
            return result

    def execute_async(self, task: str, task_id: str | None = None) -> str:
        """在后台启动一个任务执行。

        Args:
            task: 子代理的任务描述。
            task_id: 可选 task id。不提供则生成随机 UUID。

        Returns:
            后续查状态用的 task id。
        """
        # 用提供的 task_id 或生成新的
        if task_id is None:
            task_id = str(uuid.uuid4())[:8]

        # 建初始 pending result
        result = SubagentResult(
            task_id=task_id,
            trace_id=self.trace_id,
            status=SubagentStatus.PENDING,
        )

        logger.info(f"[trace={self.trace_id}] Subagent {self.config.name} starting async execution, task_id={task_id}, timeout={self.config.timeout_seconds}s")

        with _background_tasks_lock:
            _background_tasks[task_id] = result

        parent_context = copy_context()

        # 提交到 scheduler pool
        def run_task():
            with _background_tasks_lock:
                _background_tasks[task_id].status = SubagentStatus.RUNNING
                _background_tasks[task_id].started_at = datetime.now()
                result_holder = _background_tasks[task_id]

            try:
                # 直接提交到持久隔离循环，让后台路径不经过 execute() 起临时循环
                execution_future = _submit_to_isolated_loop_in_context(
                    parent_context,
                    lambda: self._aexecute(task, result_holder),
                )
                try:
                    # 带超时等执行
                    execution_future.result(timeout=self.config.timeout_seconds)
                except FuturesTimeoutError:
                    logger.error(f"[trace={self.trace_id}] Subagent {self.config.name} execution timed out after {self.config.timeout_seconds}s")
                    # 发协作取消信号并取消 future
                    result_holder.cancel_event.set()
                    result_holder.try_set_terminal(
                        SubagentStatus.TIMED_OUT,
                        error=f"Execution timed out after {self.config.timeout_seconds} seconds",
                    )
                    execution_future.cancel()
            except Exception as e:
                logger.exception(f"[trace={self.trace_id}] Subagent {self.config.name} async execution failed")
                with _background_tasks_lock:
                    task_result = _background_tasks[task_id]
                task_result.try_set_terminal(SubagentStatus.FAILED, error=str(e))

        _scheduler_pool.submit(run_task)
        return task_id


MAX_CONCURRENT_SUBAGENTS = 3


def request_cancel_background_task(task_id: str) -> None:
    """请求一个运行中的后台任务停止。

    在任务的 cancel_event 上置位，``_aexecute`` 在 ``agent.astream()`` 迭代时协作检查。
    这让子代理线程（无法用 ``Future.cancel()`` 强杀）能在下一个迭代边界停。

    Args:
        task_id: 要取消的 task id。
    """
    with _background_tasks_lock:
        result = _background_tasks.get(task_id)
        if result is not None:
            result.cancel_event.set()
            logger.info("Requested cancellation for background task %s", task_id)


def get_background_task_result(task_id: str) -> SubagentResult | None:
    """取一个后台任务的结果。

    Args:
        task_id: ``execute_async`` 返回的 task id。

    Returns:
        找到返回 SubagentResult，否则 None。
    """
    with _background_tasks_lock:
        return _background_tasks.get(task_id)


def list_background_tasks() -> list[SubagentResult]:
    """列出全部后台任务。

    Returns:
        全部 SubagentResult 实例列表。
    """
    with _background_tasks_lock:
        return list(_background_tasks.values())


def cleanup_background_task(task_id: str) -> None:
    """从后台任务里移除一个已完成的任务。

    task_tool 在轮询完并返回结果后调用，防已完成的任务堆积泄漏内存。

    只移除**终态**（COMPLETED/FAILED/TIMED_OUT）的任务，避免与后台执行器仍在更新该条目的竞态。

    Args:
        task_id: 要移除的 task id。
    """
    with _background_tasks_lock:
        result = _background_tasks.get(task_id)
        if result is None:
            # 没东西可清；可能已经被移除了
            logger.debug("Requested cleanup for unknown background task %s", task_id)
            return

        # 只清终态任务，避免与后台执行器仍在更新该条目的竞态
        if result.status.is_terminal or result.completed_at is not None:
            del _background_tasks[task_id]
            logger.debug("Cleaned up background task: %s", task_id)
        else:
            logger.debug(
                "Skipping cleanup for non-terminal background task %s (status=%s)",
                task_id,
                result.status.value if hasattr(result.status, "value") else result.status,
            )
