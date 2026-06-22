"""
提示词模板系统

支持占位符替换的模板引擎，将配置和运行时信息注入系统提示词。
"""

import asyncio
import logging
import threading
from functools import lru_cache

from deerflow.config.app_config import AppConfig

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# enabled skills 缓存（M14 skills）——后台线程刷新 + 非阻塞读
# ---------------------------------------------------------------------------
# load_skills 扫盘是 IO，不能阻塞请求路径。进程级缓存 + daemon 线程后台刷新：
# miss 时立即返回 [] 并触发后台刷新，下次调用看到预热结果。按 AppConfig 身份隔离
# （请求级配置注入仍能从匹配 config 解析技能路径，不必每次 agent 工厂调用重扫）。
_ENABLED_SKILLS_REFRESH_WAIT_TIMEOUT_SECONDS = 5.0
_enabled_skills_lock = threading.Lock()
_enabled_skills_cache: list | None = None  # list[Skill] | None
_enabled_skills_by_config_cache: dict[int, tuple[object, list]] = {}
_enabled_skills_refresh_active = False
_enabled_skills_refresh_version = 0
_enabled_skills_refresh_event = threading.Event()


def _load_enabled_skills_sync():
    from deerflow.skills.storage import get_or_new_skill_storage

    return list(get_or_new_skill_storage().load_skills(enabled_only=True))


def _start_enabled_skills_refresh_thread() -> None:
    threading.Thread(
        target=_refresh_enabled_skills_cache_worker,
        name="deerflow-enabled-skills-loader",
        daemon=True,
    ).start()


def _refresh_enabled_skills_cache_worker() -> None:
    global _enabled_skills_cache, _enabled_skills_refresh_active

    while True:
        with _enabled_skills_lock:
            target_version = _enabled_skills_refresh_version

        try:
            skills = _load_enabled_skills_sync()
        except Exception:
            logger.exception("Failed to load enabled skills for prompt injection")
            skills = []

        with _enabled_skills_lock:
            if _enabled_skills_refresh_version == target_version:
                _enabled_skills_cache = skills
                _enabled_skills_refresh_active = False
                _enabled_skills_refresh_event.set()
                return

            # 加载期间又来了一次更新的失效。保持 worker 存活再循环，让缓存总收敛到最新版本。
            _enabled_skills_cache = None


def _ensure_enabled_skills_cache() -> threading.Event:
    global _enabled_skills_refresh_active

    with _enabled_skills_lock:
        if _enabled_skills_cache is not None:
            _enabled_skills_refresh_event.set()
            return _enabled_skills_refresh_event
        if _enabled_skills_refresh_active:
            return _enabled_skills_refresh_event
        _enabled_skills_refresh_active = True
        _enabled_skills_refresh_event.clear()

    _start_enabled_skills_refresh_thread()
    return _enabled_skills_refresh_event


def _invalidate_enabled_skills_cache() -> threading.Event:
    global _enabled_skills_cache, _enabled_skills_refresh_active, _enabled_skills_refresh_version

    _get_cached_skills_prompt_section.cache_clear()
    with _enabled_skills_lock:
        _enabled_skills_cache = None
        _enabled_skills_by_config_cache.clear()
        _enabled_skills_refresh_version += 1
        _enabled_skills_refresh_event.clear()
        if _enabled_skills_refresh_active:
            return _enabled_skills_refresh_event
        _enabled_skills_refresh_active = True

    _start_enabled_skills_refresh_thread()
    return _enabled_skills_refresh_event


def prime_enabled_skills_cache() -> None:
    """启动时预热（非阻塞触发后台刷新）。"""
    _ensure_enabled_skills_cache()


def warm_enabled_skills_cache(timeout_seconds: float = _ENABLED_SKILLS_REFRESH_WAIT_TIMEOUT_SECONDS) -> bool:
    """启动时同步预热（阻塞等至多 timeout）。"""
    if _ensure_enabled_skills_cache().wait(timeout=timeout_seconds):
        return True

    logger.warning("Timed out waiting %.1fs for enabled skills cache warm-up", timeout_seconds)
    return False


def get_cached_enabled_skills() -> list:
    """返回缓存的 enabled skills；miss 时返回 [] 并触发后台刷新（请求路径安全，不阻塞）。"""
    with _enabled_skills_lock:
        cached = _enabled_skills_cache

    if cached is not None:
        return list(cached)

    _ensure_enabled_skills_cache()
    return []


def get_enabled_skills_for_config(app_config: AppConfig | None = None) -> list:
    """用调用方的 config 取 enabled skills。

    给了具体 ``app_config`` 时按其对象身份缓存，让请求级配置注入仍能从匹配 config 解析技能路径，
    而不必每次 agent 工厂调用重扫存储。
    """
    if app_config is None:
        return get_cached_enabled_skills()

    cache_key = id(app_config)
    with _enabled_skills_lock:
        cached = _enabled_skills_by_config_cache.get(cache_key)
        if cached is not None:
            cached_config, cached_skills = cached
            if cached_config is app_config:
                return list(cached_skills)

    from deerflow.skills.storage import get_or_new_skill_storage

    skills = list(get_or_new_skill_storage(app_config=app_config).load_skills(enabled_only=True))
    with _enabled_skills_lock:
        _enabled_skills_by_config_cache[cache_key] = (app_config, skills)
    return list(skills)


def _skill_mutability_label(category) -> str:
    from deerflow.skills.types import SkillCategory

    return "[custom, editable]" if category == SkillCategory.CUSTOM else "[built-in]"


def clear_skills_system_prompt_cache() -> None:
    """技能变更后失效缓存（Gateway 写技能后调）。"""
    _invalidate_enabled_skills_cache()


async def refresh_skills_system_prompt_cache_async() -> None:
    """异步等缓存失效完成。"""
    await asyncio.to_thread(_invalidate_enabled_skills_cache().wait)


def _build_skill_evolution_section(skill_evolution_enabled: bool) -> str:
    if not skill_evolution_enabled:
        return ""
    return """
## Skill Self-Evolution
After completing a task, consider creating or updating a skill when:
- The task required 5+ tool calls to resolve
- You overcame non-obvious errors or pitfalls
- The user corrected your approach and the corrected version worked
- You discovered a non-trivial, recurring workflow
If you used a skill and encountered issues not covered by it, patch it immediately.
Prefer patch over edit. Before creating a new skill, confirm with the user first.
Skip simple one-off tasks.
"""


@lru_cache(maxsize=32)
def _get_cached_skills_prompt_section(
    skill_signature: tuple,
    available_skills_key: tuple | None,
    container_base_path: str,
    skill_evolution_section: str,
) -> str:
    """lru_cache 渲染技能提示段（签名哈希做 key，内容不变命中缓存）。"""
    filtered = [(name, description, category, location) for name, description, category, location in skill_signature if available_skills_key is None or name in available_skills_key]
    skills_list = ""
    if filtered:
        skill_items = "\n".join(
            f"    <skill>\n        <name>{name}</name>\n        <description>{description} {_skill_mutability_label(category)}</description>\n        <location>{location}</location>\n    </skill>"
            for name, description, category, location in filtered
        )
        skills_list = f"<available_skills>\n{skill_items}\n</available_skills>"
    return f"""<skill_system>
You have access to skills that provide optimized workflows for specific tasks. Each skill contains best practices, frameworks, and references to additional resources.

**Progressive Loading Pattern:**
1. When a user query matches a skill's use case, immediately call `read_file` on the skill's main file using the path attribute provided in the skill tag below
2. Read and understand the skill's workflow and instructions
3. The skill file contains references to external resources under the same folder
4. Load referenced resources only when needed during execution
5. Follow the skill's instructions precisely

**Explicit Slash Skill Activation:**
- If the user starts a request with `/<skill-name>`, that skill was explicitly requested for the current turn.
- Follow the activated skill before choosing a general workflow.
- The runtime injects the activated skill content for explicit slash activations; do not call `read_file` for that SKILL.md again unless the injected skill references supporting resources you need.

**Skills are located at:** {container_base_path}
{skill_evolution_section}
{skills_list}

</skill_system>"""


def get_skills_prompt_section(available_skills: set[str] | None = None, *, app_config: AppConfig | None = None) -> str:
    """生成技能提示段（含可用技能列表）。

    无技能且未开自演化 → ``""``。白名单不命中任何技能 → ``""``。
    """
    skills = get_enabled_skills_for_config(app_config)

    if app_config is None:
        try:
            from deerflow.config import get_app_config

            config = get_app_config()
            container_base_path = config.skills.container_path
            skill_evolution_enabled = config.skill_evolution.enabled
        except Exception:
            container_base_path = "/mnt/skills"
            skill_evolution_enabled = False
    else:
        config = app_config
        container_base_path = config.skills.container_path
        skill_evolution_enabled = config.skill_evolution.enabled

    if not skills and not skill_evolution_enabled:
        return ""

    if available_skills is not None and not any(skill.name in available_skills for skill in skills):
        return ""

    skill_signature = tuple((skill.name, skill.description, str(skill.category), skill.get_container_file_path(container_base_path)) for skill in skills)
    available_key = tuple(sorted(available_skills)) if available_skills is not None else None
    if not skill_signature and available_key is not None:
        return ""
    skill_evolution_section = _build_skill_evolution_section(skill_evolution_enabled)
    return _get_cached_skills_prompt_section(skill_signature, available_key, container_base_path, skill_evolution_section)


# 基础系统提示词模板
SYSTEM_PROMPT_TEMPLATE = """你是一个有用的 AI 助手，名叫 DeerFlow。

你的职责是：
1. 理解用户的问题和需求
2. 使用可用的工具来帮助用户
3. 提供准确、有帮助的回答

{skills_section}

请遵循以下原则：
- 用中文回答用户的问题（除非用户使用其他语言）
- 保持简洁明了，但确保回答完整
- 如果需要更多信息，请主动询问
- 使用工具时，请确保参数正确
"""

# 技能提示词段落模板
SKILLS_SECTION_TEMPLATE = """
## 可用技能

你可以使用以下技能来帮助完成任务：

{skill_list}

使用技能时，请输入 /技能名称 后跟你需要完成的任务。
"""


def apply_prompt_template(
    *,
    available_skills: set[str] | None = None,
    **kwargs,
) -> str:
    """
    生成系统提示词

    Args:
        available_skills: 当前可用的技能名称集合
        **kwargs: 其他模板变量

    Returns:
        填充后的系统提示词字符串
    """
    # 构建技能段落
    if available_skills:
        skill_lines = []
        for skill_name in sorted(available_skills):
            skill_lines.append(f"- **{skill_name}**: /{skill_name} <任务描述>")
        skills_section = SKILLS_SECTION_TEMPLATE.format(skill_list="\n".join(skill_lines))
    else:
        skills_section = ""

    return SYSTEM_PROMPT_TEMPLATE.format(
        skills_section=skills_section,
        **kwargs,
    )


def get_default_system_prompt() -> str:
    """获取默认系统提示词（不含技能）"""
    return apply_prompt_template()


def _get_memory_context(agent_name: str | None = None, *, app_config: AppConfig | None = None) -> str:
    """取注入系统提示的记忆上下文（M13 memory）。

    延迟导入 ``deerflow.agents.memory`` 与 ``runtime.user_context`` 防循环依赖。
    记忆由 ``memory.enabled`` + ``memory.injection_enabled`` 双开关门控；取
    ``get_effective_user_id()`` 的 per-user 记忆，按 ``max_injection_tokens`` 预算截断。

    Args:
        agent_name: 非 None 取 per-agent 记忆；None 取全局记忆。
        app_config: 显式配置；提供时记忆选项从此值读，否则读全局单例。

    Returns:
        包在 XML 标签里的格式化记忆上下文串；禁用或为空返回 ``""``。任何异常吞掉返回 ``""``
        （记忆是 nice-to-have，不能让它挂起 agent 启动）。
    """
    try:
        from deerflow.agents.memory import format_memory_for_injection, get_memory_data
        from deerflow.runtime.user_context import get_effective_user_id

        if app_config is None:
            from deerflow.config.memory_config import get_memory_config

            config = get_memory_config()
        else:
            config = app_config.memory

        if not config.enabled or not config.injection_enabled:
            return ""

        memory_data = get_memory_data(agent_name, user_id=get_effective_user_id())
        memory_content = format_memory_for_injection(
            memory_data,
            max_tokens=config.max_injection_tokens,
            use_tiktoken=(config.token_counting == "tiktoken"),
        )

        if not memory_content.strip():
            return ""

        return f"""<memory>
{memory_content}
</memory>
"""
    except Exception:
        logger.exception("Failed to load memory context")
        return ""
