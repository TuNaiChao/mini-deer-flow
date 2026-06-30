"""记忆更新与注入的提示词模板（M13 memory）。

对齐 deer ``agents/memory/prompt.py``：

- ``MEMORY_UPDATE_PROMPT`` / ``FACT_EXTRACTION_PROMPT``：喂给 LLM 抽取记忆的提示词。
- ``format_conversation_for_update``：把对话消息格式化成 prompt 输入（剥上传块、截长消息）。
- ``format_memory_for_injection``：把记忆数据格式化成系统提示注入串，按 token 预算截断。
- ``_count_tokens``：tiktoken 计数 + **冷却降级**（首次失败缓存 600s，期间走 CJK 感知字符估算；
  可用 ``memory.token_counting: char`` 完全跳过 tiktoken，见 issue #3402/#3429）。
"""

from __future__ import annotations

import logging
import math
import re
import threading
import time
from typing import Any, cast

logger = logging.getLogger(__name__)

try:
    import tiktoken

    TIKTOKEN_AVAILABLE = True
except ImportError:
    TIKTOKEN_AVAILABLE = False

# 更新记忆的提示词模板
MEMORY_UPDATE_PROMPT = """You are a memory management system. Your task is to analyze a conversation and update the user's memory profile.

Current Memory State:
<current_memory>
{current_memory}
</current_memory>

New Conversation to Process:
<conversation>
{conversation}
</conversation>

Instructions:
1. Analyze the conversation for important information about the user
2. Extract relevant facts, preferences, and context with specific details (numbers, names, technologies)
3. Update the memory sections as needed following the detailed length guidelines below

Before extracting facts, perform a structured reflection on the conversation:
1. Error/Retry Detection: Did the agent encounter errors, require retries, or produce incorrect results?
   If yes, record the root cause and correct approach as a high-confidence fact with category "correction".
2. User Correction Detection: Did the user correct the agent's direction, understanding, or output?
   If yes, record the correct interpretation or approach as a high-confidence fact with category "correction".
   Include what went wrong in "sourceError" only when category is "correction" and the mistake is explicit in the conversation.
3. Project Constraint Discovery: Were any project-specific constraints discovered during the conversation?
   If yes, record them as facts with the most appropriate category and confidence.

{correction_hint}

Memory Section Guidelines:

**User Context** (Current state - concise summaries):
- workContext: Professional role, company, key projects, main technologies (2-3 sentences)
  Example: Core contributor, project names with metrics (16k+ stars), technical stack
- personalContext: Languages, communication preferences, key interests (1-2 sentences)
  Example: Bilingual capabilities, specific interest areas, expertise domains
- topOfMind: Multiple ongoing focus areas and priorities (3-5 sentences, detailed paragraph)
  Example: Primary project work, parallel technical investigations, ongoing learning/tracking
  Include: Active implementation work, troubleshooting issues, market/research interests
  Note: This captures SEVERAL concurrent focus areas, not just one task

**History** (Temporal context - rich paragraphs):
- recentMonths: Detailed summary of recent activities (4-6 sentences or 1-2 paragraphs)
  Timeline: Last 1-3 months of interactions
  Include: Technologies explored, projects worked on, problems solved, interests demonstrated
- earlierContext: Important historical patterns (3-5 sentences or 1 paragraph)
  Timeline: 3-12 months ago
  Include: Past projects, learning journeys, established patterns
- longTermBackground: Persistent background and foundational context (2-4 sentences)
  Timeline: Overall/foundational information
  Include: Core expertise, longstanding interests, fundamental working style

**Facts Extraction**:
- Extract specific, quantifiable details (e.g., "16k+ GitHub stars", "200+ datasets")
- Include proper nouns (company names, project names, technology names)
- Preserve technical terminology and version numbers
- Categories:
  * preference: Tools, styles, approaches user prefers/dislikes
  * knowledge: Specific expertise, technologies mastered, domain knowledge
  * context: Background facts (job title, projects, locations, languages)
  * behavior: Working patterns, communication habits, problem-solving approaches
  * goal: Stated objectives, learning targets, project ambitions
  * correction: Explicit agent mistakes or user corrections, including the correct approach
- Confidence levels:
  * 0.9-1.0: Explicitly stated facts ("I work on X", "My role is Y")
  * 0.7-0.8: Strongly implied from actions/discussions
  * 0.5-0.6: Inferred patterns (use sparingly, only for clear patterns)

**What Goes Where**:
- workContext: Current job, active projects, primary tech stack
- personalContext: Languages, personality, interests outside direct work tasks
- topOfMind: Multiple ongoing priorities and focus areas user cares about recently (gets updated most frequently)
  Should capture 3-5 concurrent themes: main work, side explorations, learning/tracking interests
- recentMonths: Detailed account of recent technical explorations and work
- earlierContext: Patterns from slightly older interactions still relevant
- longTermBackground: Unchanging foundational facts about the user

**Multilingual Content**:
- Preserve original language for proper nouns and company names
- Keep technical terms in their original form (DeepSeek, LangGraph, etc.)
- Note language capabilities in personalContext

Output Format (JSON):
{{
  "user": {{
    "workContext": {{ "summary": "...", "shouldUpdate": true/false }},
    "personalContext": {{ "summary": "...", "shouldUpdate": true/false }},
    "topOfMind": {{ "summary": "...", "shouldUpdate": true/false }}
  }},
  "history": {{
    "recentMonths": {{ "summary": "...", "shouldUpdate": true/false }},
    "earlierContext": {{ "summary": "...", "shouldUpdate": true/false }},
    "longTermBackground": {{ "summary": "...", "shouldUpdate": true/false }}
  }},
  "newFacts": [
    {{ "content": "...", "category": "preference|knowledge|context|behavior|goal|correction", "confidence": 0.0-1.0 }}
  ],
  "factsToRemove": ["fact_id_1", "fact_id_2"]
}}

Important Rules:
- Only set shouldUpdate=true if there's meaningful new information
- Follow length guidelines: workContext/personalContext are concise (1-3 sentences), topOfMind and history sections are detailed (paragraphs)
- Include specific metrics, version numbers, and proper nouns in facts
- Only add facts that are clearly stated (0.9+) or strongly implied (0.7+)
- Use category "correction" for explicit agent mistakes or user corrections; assign confidence >= 0.95 when the correction is explicit
- Include "sourceError" only for explicit correction facts when the prior mistake or wrong approach is clearly stated; omit it otherwise
- Remove facts that are contradicted by new information
- When updating topOfMind, integrate new focus areas while removing completed/abandoned ones
  Keep 3-5 concurrent focus themes that are still active and relevant
- For history sections, integrate new information chronologically into appropriate time period
- Preserve technical accuracy - keep exact names of technologies, companies, projects
- Focus on information useful for future interactions and personalization
- IMPORTANT: Do NOT record file upload events in memory. Uploaded files are
  session-specific and ephemeral — they will not be accessible in future sessions.
  Recording upload events causes confusion in subsequent conversations.

Return ONLY valid JSON, no explanation or markdown."""


# 从单条消息抽取事实的提示词模板
FACT_EXTRACTION_PROMPT = """Extract factual information about the user from this message.

Message:
{message}

Extract facts in this JSON format:
{{
  "facts": [
    {{ "content": "...", "category": "preference|knowledge|context|behavior|goal|correction", "confidence": 0.0-1.0 }}
  ]
}}

Categories:
- preference: User preferences (likes/dislikes, styles, tools)
- knowledge: User's expertise or knowledge areas
- context: Background context (location, job, projects)
- behavior: Behavioral patterns
- goal: User's goals or objectives
- correction: Explicit corrections or mistakes to avoid repeating

Rules:
- Only extract clear, specific facts
- Confidence should reflect certainty (explicit statement = 0.9+, implied = 0.6-0.8)
- Skip vague or temporary information

Return ONLY valid JSON."""


# 模块级 tiktoken encoding 缓存。首次用时懒加载；后续是 dict 查找（无网络 IO）。
#
# 加载**失败**缓存为 ``(None, monotonic_timestamp)`` 元组——网络受限环境不会每次调用都
# 重试阻塞的 BPE 下载。``_TIKTOKEN_RETRY_COOLDOWN_S`` 后失败过期，让瞬时网络中断可自愈回
# 精确 tiktoken 计数而无需重启进程。进行中的加载缓存为 ``_TIKTOKEN_ENCODING_LOADING``，
# 让并发调用方立即回退而非再起阻塞的 ``tiktoken.get_encoding`` 线程。用
# ``memory.token_counting: char`` 完全跳过 tiktoken。
_TIKTOKEN_ENCODING_MISSING = object()
_TIKTOKEN_ENCODING_LOADING = object()
# tiktoken 加载失败后重试前的冷却（内部调参常量，非用户配置；只影响默认 tiktoken 模式
# 在瞬时网络中断后多快自愈。想彻底避开 tiktoken 网络依赖应设 ``memory.token_counting: char``）。
_TIKTOKEN_RETRY_COOLDOWN_S = 600.0
_tiktoken_encoding_cache: dict[str, Any] = {}
_tiktoken_encoding_cache_lock = threading.Lock()


def _get_tiktoken_encoding(encoding_name: str = "cl100k_base") -> "tiktoken.Encoding | None":
    """返回缓存的 tiktoken encoding，失败 / 不可用时返回 ``None``。

    首次调用某 encoding 时 tiktoken 可能要从 ``openaipublic.blob.core.windows.net`` 下载
    BPE 数据。网络受限环境（如 GFW 后）此下载可阻塞数十分钟直到 OS TCP 超时。故调用方须
    预期它可能阻塞，应在线程外跑（如 ``asyncio.to_thread``）。

    失败被记住（带时间戳），后续调用立即回退字符估算而非重触发阻塞下载。失败在
    ``_TIKTOKEN_RETRY_COOLDOWN_S`` 后过期让瞬时中断自愈。进行中的加载也被记住，避免超时
    的调用方留下空窗让后续请求再起阻塞的 ``get_encoding``。
    """
    if not TIKTOKEN_AVAILABLE:
        return None

    with _tiktoken_encoding_cache_lock:
        cached = _tiktoken_encoding_cache.get(encoding_name, _TIKTOKEN_ENCODING_MISSING)
        if cached is _TIKTOKEN_ENCODING_LOADING:
            return None
        if isinstance(cached, tuple):
            # 缓存的失败：(None, failed_at)。仅冷却后重试。
            _, failed_at = cached
            if time.monotonic() - failed_at < _TIKTOKEN_RETRY_COOLDOWN_S:
                return None
            cached = _TIKTOKEN_ENCODING_MISSING
        if cached is not _TIKTOKEN_ENCODING_MISSING:
            return cast("tiktoken.Encoding", cached)
        _tiktoken_encoding_cache[encoding_name] = _TIKTOKEN_ENCODING_LOADING

    try:
        encoding = tiktoken.get_encoding(encoding_name)
    except Exception:
        logger.warning("Failed to load tiktoken encoding %r; falling back to char-based estimation", encoding_name, exc_info=True)
        with _tiktoken_encoding_cache_lock:
            _tiktoken_encoding_cache[encoding_name] = (None, time.monotonic())
        return None

    with _tiktoken_encoding_cache_lock:
        _tiktoken_encoding_cache[encoding_name] = encoding
    return encoding


def _char_based_token_estimate(text: str) -> int:
    """无网络的 token 估算，考虑 CJK 密度。

    朴素的 ``len(text) // 4`` 对英文/代码合理（~4 字符/token），但显著低估中日韩文本
    （比例更接近 1.5-2 字符/token）。CJK 字符单独计（~2 字符/token）避免 CJK 重的记忆内容
    超量注入预算。
    """
    cjk = sum(
        1
        for ch in text
        if "一" <= ch <= "鿿"  # CJK Unified Ideographs
        or "぀" <= ch <= "ヿ"  # Hiragana + Katakana
        or "가" <= ch <= "힣"  # Hangul syllables
    )
    return (len(text) - cjk) // 4 + cjk // 2


def _count_tokens(text: str, encoding_name: str = "cl100k_base", *, use_tiktoken: bool = True) -> int:
    """用 tiktoken 计数文本 token。

    Args:
        text: 待计数文本。
        encoding_name: 用的 encoding（默认 cl100k_base，GPT-4/3.5）。
        use_tiktoken: ``False`` 时完全跳过 tiktoken，用无网络字符估算（见
            ``memory.token_counting`` 配置）。
    """
    if not use_tiktoken:
        return _char_based_token_estimate(text)

    encoding = _get_tiktoken_encoding(encoding_name)
    if encoding is None:
        # tiktoken 不可用或 encoding 加载失败时回退 CJK 感知字符估算。
        return _char_based_token_estimate(text)

    try:
        return len(encoding.encode(text))
    except Exception:
        return _char_based_token_estimate(text)


def warm_tiktoken_cache() -> bool:
    """预热 tiktoken encoding 缓存（启动时在线程外调，首个请求不阻塞在 BPE 下载）。"""
    return _get_tiktoken_encoding("cl100k_base") is not None


def _coerce_confidence(value: Any, default: float = 0.0) -> float:
    """把置信度值强转成 [0, 1] 的 float。非有限值（NaN/inf）回退默认再 clamp。"""
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return max(0.0, min(1.0, default))
    if not math.isfinite(confidence):
        return max(0.0, min(1.0, default))
    return max(0.0, min(1.0, confidence))


def _format_fact_line(fact: dict[str, Any]) -> str | None:
    """构造单条格式化 fact 行；无效 fact 返回 ``None``。

    抽成共享 helper，让 guaranteed 注入路径与常规注入路径产出完全一致的行格式。
    """
    content_value = fact.get("content")
    if not isinstance(content_value, str):
        return None
    content = content_value.strip()
    if not content:
        return None
    category = str(fact.get("category", "context")).strip() or "context"
    confidence = _coerce_confidence(fact.get("confidence"), default=0.0)
    source_error = fact.get("sourceError")
    if category == "correction" and isinstance(source_error, str) and source_error.strip():
        return f"- [{category} | {confidence:.2f}] {content} (avoid: {source_error.strip()})"
    return f"- [{category} | {confidence:.2f}] {content}"


def _select_fact_lines(
    ranked_facts: list[dict[str, Any]],
    *,
    token_budget: int,
    use_tiktoken: bool,
) -> tuple[list[str], int]:
    """在「仅行」token 预算内贪心选格式化 fact 行。

    本函数故意**与 header 无关**：只算 fact 行本身（含行间 ``\\n`` 分隔符）。调用方负责
    在调用前为 ``"Facts:\\n"`` header 与任何跨段 ``"\\n\\n"`` 分隔符预留 token，把剩余容量
    作为 *token_budget* 传入。

    在第一条会超预算的 fact 处停下，严格保留调用方的预排序（通常按置信度降序）：更短、
    更低秩的 fact 绝不会溜到一个被跳过的更高秩 fact 前面。

    Returns:
        ``(selected_lines, consumed_tokens)``——*consumed_tokens* 是返回行的精确 token 成本
        （含行间 ``\\n``，但不含前导 header）。
    """
    lines: list[str] = []
    consumed = 0
    for fact in ranked_facts:
        formatted = _format_fact_line(fact)
        if formatted is None:
            continue
        line_text = ("\n" + formatted) if lines else formatted
        line_tokens = _count_tokens(line_text, use_tiktoken=use_tiktoken)
        if consumed + line_tokens > token_budget:
            break
        lines.append(formatted)
        consumed += line_tokens
    return lines, consumed


def _fallback_format_facts(
    valid_facts: list[dict[str, Any]],
    *,
    preceding_section_cost: int,
    max_tokens: int,
    use_tiktoken: bool,
) -> tuple[str, list[str]] | tuple[None, None]:
    """主路径抛异常时用的「仅按置信度排序」回退。

    返回 ``(section_text, fact_lines)``：``section_text`` 是格式化好的 ``"Facts:\\n..."`` 段
    （不含前导跨段分隔符——那归调用方），``fact_lines`` 是组成 facts 块的各行。无 fact 存活时
    两者都为 ``None``。

    单独返回行让调用方能在结构感知截断里追踪它们，使回退 fact 享受与主路径 fact 相同的
    「受保护后缀」待遇。

    *valid_facts* 是主路径已建好的、过滤好的 fact 列表（回退不重做校验）；
    *preceding_section_cost* 是 user-context / history 段已耗 token（用来推剩余预算）。
    """
    ranked = sorted(valid_facts, key=lambda f: _coerce_confidence(f.get("confidence"), default=0.0), reverse=True)

    header = "Facts:\n"
    overhead = _count_tokens(header, use_tiktoken=use_tiktoken)
    line_budget = max_tokens - preceding_section_cost - overhead
    if line_budget <= 0:
        return None, None

    lines, _ = _select_fact_lines(ranked, token_budget=line_budget, use_tiktoken=use_tiktoken)
    if not lines:
        return None, None
    return header + "\n".join(lines), lines


def format_memory_for_injection(
    memory_data: dict[str, Any],
    max_tokens: int = 2000,
    *,
    use_tiktoken: bool = True,
    guaranteed_categories: list[str] | None = None,
    guaranteed_token_budget: int = 500,
) -> str:
    """把记忆数据格式化成系统提示注入串。

    按 token 预算（tiktoken 精确计 / char 估算）截断：先放 user/history 段，facts 按 guaranteed
    → 常规两阶段选（#3592）。超预算时用**结构感知**截断——Facts 块作为受保护后缀，只截前面的段。

    Args:
        memory_data: 记忆数据字典。
        max_tokens: 最大 token 数。
        use_tiktoken: ``False`` 时全用无网络字符估算（见 ``memory.token_counting``）。
        guaranteed_categories: 无论常规预算多紧都必须注入的 fact 类别。这些 fact 从独立的
            *guaranteed_token_budget* 分配。``None`` / 空时所有 fact 竞争同一预算（旧行为）。
        guaranteed_token_budget: guaranteed 段的 token 上限。常见情况下 guaranteed 行在
            *max_tokens* 内挤占常规行（总输出 ≤ max_tokens）；仅当 guaranteed 行单独顶过
            *max_tokens* 时预算才叠加——此时安全截断上限抬到 ``max_tokens + guaranteed_actual_usage``
            以保护它们。*guaranteed_categories* 为 ``None`` / 空时忽略。
    """
    if not memory_data:
        return ""

    # 显式拒裸字符串：迭代 ``str`` 会产出单字符 frozenset，静默关掉 guarantee 且无告警。
    # config 层调用方走 Pydantic（强制 ``list[str]``），这里只守公共 helper 入口。
    if isinstance(guaranteed_categories, str):
        raise TypeError("guaranteed_categories must be an iterable of strings, not a bare str")
    effective_guaranteed: frozenset[str] = frozenset(c.strip() for c in guaranteed_categories if isinstance(c, str) and c.strip()) if guaranteed_categories else frozenset()

    sections: list[str] = []

    # 格式化 user context
    user_data = memory_data.get("user", {})
    if user_data:
        user_sections = []

        work_ctx = user_data.get("workContext", {})
        if work_ctx.get("summary"):
            user_sections.append(f"Work: {work_ctx['summary']}")

        personal_ctx = user_data.get("personalContext", {})
        if personal_ctx.get("summary"):
            user_sections.append(f"Personal: {personal_ctx['summary']}")

        top_of_mind = user_data.get("topOfMind", {})
        if top_of_mind.get("summary"):
            user_sections.append(f"Current Focus: {top_of_mind['summary']}")

        if user_sections:
            sections.append("User Context:\n" + "\n".join(f"- {s}" for s in user_sections))

    # 格式化 history
    history_data = memory_data.get("history", {})
    if history_data:
        history_sections = []

        recent = history_data.get("recentMonths", {})
        if recent.get("summary"):
            history_sections.append(f"Recent: {recent['summary']}")

        earlier = history_data.get("earlierContext", {})
        if earlier.get("summary"):
            history_sections.append(f"Earlier: {earlier['summary']}")

        background = history_data.get("longTermBackground", {})
        if background.get("summary"):
            history_sections.append(f"Background: {background['summary']}")

        if history_sections:
            sections.append("History:\n" + "\n".join(f"- {s}" for s in history_sections))

    # ── Facts（#3592 guaranteed 注入）────────────────────────────────────────
    #
    # • 最多发一个 ``"Facts:\\n"`` header。
    # • guaranteed 类别 fact 先从独立的 *guaranteed_token_budget* 选出、放在 Facts 块最前，
    #   故不会被常规 fact 挤掉。常见情况总输出仍落在 max_tokens 内（guaranteed 行挤占常规行）；
    #   仅当 guaranteed 行单独顶过 max_tokens 时预算才叠加，安全截断上限相应抬高。
    # • 常规 fact 只从 *max_tokens* 取。
    # • 所有 token 计数（header / 分隔符 / 行）都在调用方做；``_select_fact_lines`` 与 header 无关。
    # • 主路径抛任何异常时，``_fallback_format_facts`` 做一遍仅按置信度排序的回退。
    facts_data = memory_data.get("facts", [])
    guaranteed_line_tokens = 0  # 后面用来算有效截断上限
    # 在 try/if 外初始化 facts 块标记，让底部结构感知截断无论有没有 facts、走主路径还是回退，
    # 都能安全引用（无 facts 时 all_fact_lines 为空、facts_block 为 ""）。
    facts_header = "Facts:\n"
    all_fact_lines: list[str] = []
    if isinstance(facts_data, list) and facts_data:
        # 进 try 前先过滤好 valid facts，让 except 路径直接把同一列表喂给回退、不重做校验。
        valid_facts = [f for f in facts_data if isinstance(f, dict) and isinstance(f.get("content"), str) and f.get("content", "").strip()]

        # 已有段的 token 成本（user context / history）。
        base_text = "\n\n".join(sections)
        base_tokens = _count_tokens(base_text, use_tiktoken=use_tiktoken) if base_text else 0

        try:
            # 用**原始** category 字段（不 ``or "context"`` 默认），免得无类别的 legacy fact
            # 被静默提升进「operator 配了 guaranteed_categories=["context"]」的 guaranteed 池。
            # 缺类别 fact 一律落常规路径。
            def _confidence_key(fact: dict[str, Any]) -> float:
                return _coerce_confidence(fact.get("confidence"), default=0.0)

            if effective_guaranteed:

                def _category_match(fact: dict[str, Any]) -> bool:
                    raw = fact.get("category")
                    if not isinstance(raw, str):
                        return False
                    cat = raw.strip()
                    return bool(cat) and cat in effective_guaranteed

                guaranteed = sorted(
                    [f for f in valid_facts if _category_match(f)],
                    key=_confidence_key,
                    reverse=True,
                )
                regular = sorted(
                    [f for f in valid_facts if not _category_match(f)],
                    key=_confidence_key,
                    reverse=True,
                )
            else:
                guaranteed = []
                regular = sorted(valid_facts, key=_confidence_key, reverse=True)

            # ── 阶段 1：选 guaranteed 行 ──────────────────────────────────
            header_cost = _count_tokens(facts_header, use_tiktoken=use_tiktoken)

            guaranteed_lines: list[str] = []
            if guaranteed:
                guaranteed_lines, guaranteed_line_tokens = _select_fact_lines(
                    guaranteed,
                    token_budget=guaranteed_token_budget,
                    use_tiktoken=use_tiktoken,
                )

            # ── 阶段 2：选常规行 ──────────────────────────────────────────
            # 常规 fact 竞争 *max_tokens*（主预算）。减去已计入的：
            #   base 段 + 跨段分隔符 + header + guaranteed 行 + 两组之间的 ``\n``（两组都有时）。
            regular_lines: list[str] = []
            if regular:
                inter_group_newline_tokens = _count_tokens("\n", use_tiktoken=use_tiktoken) if guaranteed_lines else 0
                used_before_regular = base_tokens + header_cost + guaranteed_line_tokens + inter_group_newline_tokens
                regular_line_budget = max_tokens - used_before_regular
                if regular_line_budget > 0:
                    regular_lines, _ = _select_fact_lines(
                        regular,
                        token_budget=regular_line_budget,
                        use_tiktoken=use_tiktoken,
                    )

            # ── 发一个 "Facts:" 段 ────────────────────────────────────────
            # 不嵌前导跨段分隔符；最终 ``"\n\n".join(sections)`` 是段间间距的唯一真相，
            # 防止以前的双 ``\n\n`` bug。
            all_fact_lines = guaranteed_lines + regular_lines
            if all_fact_lines:
                section_text = facts_header + "\n".join(all_fact_lines)
                sections.append(section_text)

        except Exception:
            # ── 回退：仅按置信度排序、单一预算 ────────────────────────────
            # 分区 / guaranteed 路径的任何意外错误都不能让记忆注入整个失败。回退到原始的
            # 单遍置信度排序。复用预过滤的 ``valid_facts``，不在热回退路径上重做校验。
            logger.warning(
                "Memory injection: guaranteed-category path failed, falling back to confidence-only ranking",
                exc_info=True,
            )
            fallback, fallback_lines = _fallback_format_facts(
                valid_facts,
                preceding_section_cost=base_tokens,
                max_tokens=max_tokens,
                use_tiktoken=use_tiktoken,
            )
            if fallback:
                sections.append(fallback)
                # 把回退的行暴露给 ``all_fact_lines``，让下面的结构感知截断也把回退 fact 当
                # 受保护后缀。否则一个大 user-context 前缀可能经原始前缀截断静默裁掉回退 fact。
                all_fact_lines = fallback_lines

    if not sections:
        return ""

    result = "\n\n".join(sections)

    token_count = _count_tokens(result, use_tiktoken=use_tiktoken)
    effective_limit = max_tokens + guaranteed_line_tokens
    if token_count > effective_limit:
        # 结构感知截断：``Facts:\n...`` 块当作**受保护后缀**，guaranteed 类别 fact——本 PR
        # 就是为保住它们——绝不能被溢出时的前缀截断静默丢弃。只有前面的（user-context / history）
        # 段可截；若它们单独超出「为 Facts 块预留后剩余的预算」，从尾部裁。*guaranteed_line_tokens*
        # 为 0（未配 guaranteed 或无 fact 存活）时，等式退化为对 ``max_tokens`` 的原始前缀截断，
        # 向后兼容。
        facts_block = (facts_header + "\n".join(all_fact_lines)) if all_fact_lines else ""
        facts_block_tokens = _count_tokens(facts_block, use_tiktoken=use_tiktoken)
        separator_tokens = _count_tokens("\n\n", use_tiktoken=use_tiktoken)
        budget_for_non_facts = max(
            0,
            effective_limit - facts_block_tokens - (separator_tokens if facts_block else 0),
        )

        # 从 *sections*（去掉尾部的 Facts 块）构建前面的（非 facts）部分。
        preceding_sections = sections[:-1] if all_fact_lines else sections
        preceding = "\n\n".join(preceding_sections)

        if preceding:
            preceding_tokens = _count_tokens(preceding, use_tiktoken=use_tiktoken)
            if preceding_tokens > budget_for_non_facts:
                char_per_token = len(preceding) / max(preceding_tokens, 1)
                target_chars = int(budget_for_non_facts * char_per_token * 0.95)
                preceding = preceding[:target_chars].rstrip() + "\n..."
            result = (preceding + "\n\n" + facts_block) if facts_block else preceding
        else:
            result = facts_block

    return result


def format_conversation_for_update(messages: list[Any]) -> str:
    """把对话消息格式化成记忆更新 prompt 的输入。

    human 消息剥 ``<uploaded_files>`` 块（防把临时文件路径写进长期记忆）；剥光后为空则跳过整轮。
    超长消息截断到 1000 字符。
    """
    lines = []
    for msg in messages:
        role = getattr(msg, "type", "unknown")
        content = getattr(msg, "content", str(msg))

        # content 可能是 list（多模态）
        if isinstance(content, list):
            text_parts = []
            for p in content:
                if isinstance(p, str):
                    text_parts.append(p)
                elif isinstance(p, dict):
                    text_val = p.get("text")
                    if isinstance(text_val, str):
                        text_parts.append(text_val)
            content = " ".join(text_parts) if text_parts else str(content)

        # human 消息剥 uploaded_files 标签，防把临时文件路径信息写进长期记忆。剥光后整轮跳过。
        if role == "human":
            content = re.sub(r"<uploaded_files>[\s\S]*?</uploaded_files>\n*", "", str(content)).strip()
            if not content:
                continue

        # 截断超长消息
        if len(str(content)) > 1000:
            content = str(content)[:1000] + "..."

        if role == "human":
            lines.append(f"User: {content}")
        elif role == "ai":
            lines.append(f"Assistant: {content}")

    return "\n\n".join(lines)
