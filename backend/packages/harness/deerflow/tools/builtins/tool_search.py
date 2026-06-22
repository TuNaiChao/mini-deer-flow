"""tool_search——运行时延迟工具发现。

对齐 deer ``tools/builtins/tool_search.py``。MCP 工具体量大，默认全绑会撑爆模型上下文 +
让工具 schema 模糊。所以：MCP 工具默认**延迟**（agent 只看到名字列在 ``<available-deferred-tools>``
里），agent 用 ``tool_search`` 按需取出完整 schema，取出后才可调用。

包含：
- ``DeferredToolCatalog``：不可变、可搜索的延迟工具目录（纯搜索，无副作用）；
- ``build_tool_search_tool``：基于目录闭包构造 ``tool_search`` 工具，把「提升」记录到图状态（``Command``）；
- ``build_deferred_tool_setup`` / ``assemble_deferred_tools``：从**策略过滤后**的工具列表装配目录 + 工具
  （必须在 skill/agent 工具策略过滤之后调，fail-closed）；
- ``get_deferred_tools_prompt_section``：渲染 ``<available-deferred-tools>`` 段。

源无关：一个工具是「延迟」当且仅当它带 ``deerflow_mcp`` 元数据标记（``mcp_metadata.is_mcp_tool``）。
延迟集合在构建时闭包持有，提升记录在 per-thread 图状态——无 ContextVar。
"""

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from functools import cached_property
from typing import Annotated

from langchain.tools import BaseTool
from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langchain_core.utils.function_calling import convert_to_openai_function
from langgraph.types import Command

from deerflow.tools.mcp_metadata import is_mcp_tool

logger = logging.getLogger(__name__)

MAX_RESULTS = 5  # 每次搜索最多返回的工具数


def _compile_catalog_regex(pattern: str) -> re.Pattern[str]:
    """大小写不敏感编译 ``pattern``，非法正则降级为字面匹配。

    搜索查询来自模型，一个非法正则（如未闭合括号）必须降级为字面子串匹配而非抛错。
    """
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error:
        return re.compile(re.escape(pattern), re.IGNORECASE)


# ── 目录 ──


# 注意：frozen=True 但**不**加 slots=True，保留 __dict__，下面的 @cached_property 才能缓存
# （它们写 instance.__dict__，绕过 frozen 的 __setattr__）。加了 slots=True 会让 hash/names 运行时崩。
@dataclass(frozen=True)
class DeferredToolCatalog:
    """不可变的延迟工具目录。纯搜索，无修改。"""

    tools: tuple[BaseTool, ...]

    @cached_property
    def names(self) -> frozenset[str]:
        return frozenset(t.name for t in self.tools)

    @cached_property
    def hash(self) -> str:
        """目录内容的稳定哈希（前 16 位），用于把 per-thread 提升记录 scope 到本目录。

        防止「构建时目录 A 的提升」被「下次构建目录 B」误用——hash 变了就当未提升。
        """
        canon = [{"name": t.name, "schema": convert_to_openai_function(t)} for t in sorted(self.tools, key=lambda t: t.name)]
        blob = json.dumps(canon, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]

    def search(self, query: str) -> list[BaseTool]:
        """按查询返回最多 MAX_RESULTS 个匹配工具。支持三种查询形式：

        - ``select:Read,Edit``——按精确名字取；
        - ``+slack send``——名字必须含 ``slack``，再按剩余词排序；
        - ``notebook jupyter``——关键词正则搜索（名字命中得分更高）。
        """
        query = query.strip()
        if not query:
            return []

        if query.startswith("select:"):
            wanted = {n.strip() for n in query[7:].split(",")}
            return [t for t in self.tools if t.name in wanted][:MAX_RESULTS]

        if query.startswith("+"):
            parts = query[1:].split(None, 1)
            if not parts:
                return []  # 裸 "+" 无必含词 → 无匹配
            required = parts[0].lower()
            candidates = [t for t in self.tools if required in t.name.lower()]
            if len(parts) > 1:
                candidates.sort(key=lambda t: _catalog_regex_score(parts[1], t), reverse=True)
            return candidates[:MAX_RESULTS]

        regex = _compile_catalog_regex(query)
        scored: list[tuple[int, BaseTool]] = []
        for t in self.tools:
            searchable = f"{t.name} {t.description or ''}"
            if regex.search(searchable):
                scored.append((2 if regex.search(t.name) else 1, t))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [t for _, t in scored][:MAX_RESULTS]


def _catalog_regex_score(pattern: str, t: BaseTool) -> int:
    regex = _compile_catalog_regex(pattern)
    return len(regex.findall(f"{t.name} {t.description or ''}"))


# ── 装配 / 工具 ──


@dataclass(frozen=True)
class DeferredToolSetup:
    """一次 agent 构建的延迟工具装配结果。三字段成组移动，调用方按 ``tool_search_tool`` 分支：

    - **空** ``(None, frozenset(), None)``：延迟未启用，或没有 MCP 工具通过策略过滤。什么都不延迟，
      工具原样绑定。
    - **有值**：``tool_search_tool`` 追加进 agent 工具集，``deferred_names`` 暂不暴露给模型直到被提升，
      ``catalog_hash`` 把提升记录 scope 到本目录。

    不变量：``tool_search_tool is None`` ⟺ ``deferred_names`` 为空 ⟺ ``catalog_hash is None``。
    """

    tool_search_tool: BaseTool | None
    deferred_names: frozenset[str]
    catalog_hash: str | None


def build_tool_search_tool(catalog: DeferredToolCatalog) -> BaseTool:
    catalog_hash = catalog.hash

    @tool
    def tool_search(query: str, tool_call_id: Annotated[str, InjectedToolCallId]) -> Command:
        """Fetches full schema definitions for deferred tools so they can be called.

        Deferred tools appear by name in <available-deferred-tools> in the system
        prompt. Until fetched, only the name is known. This tool matches a query
        against the deferred tools and returns the matched tools complete schemas;
        once returned, a tool becomes callable.

        Query forms:
          - "select:Read,Edit" -- fetch these exact tools by name
          - "notebook jupyter" -- keyword search, up to max_results best matches
          - "+slack send" -- require "slack" in the name, rank by remaining terms
        """
        matched = catalog.search(query)[:MAX_RESULTS]
        if not matched:
            content, names = f"No tools found matching: {query}", []
        else:
            content = json.dumps([convert_to_openai_function(t) for t in matched], indent=2, ensure_ascii=False)
            names = [t.name for t in matched]
        return Command(
            update={
                "promoted": {"catalog_hash": catalog_hash, "names": names},
                "messages": [ToolMessage(content=content, tool_call_id=tool_call_id, name="tool_search")],
            },
        )

    return tool_search


def build_deferred_tool_setup(filtered_tools: list[BaseTool], *, enabled: bool) -> DeferredToolSetup:
    """从**策略过滤后**的工具列表构建延迟工具装配。

    必须在 skill/agent 工具策略过滤**之后**调，这样目录绝不会暴露当前 agent 无权使用的工具。

    返回空 setup（见 :class:`DeferredToolSetup`）有两种情况：延迟未启用；或启用了但没有 MCP 工具
    通过过滤。
    """
    if not enabled:
        # 延迟未启用：什么都不延迟，模型照旧绑定每个工具。
        return DeferredToolSetup(None, frozenset(), None)
    deferred = [t for t in filtered_tools if is_mcp_tool(t)]
    if not deferred:
        # 启用了但没有可延迟的 MCP 工具：同样空结果，但原因不同。
        return DeferredToolSetup(None, frozenset(), None)
    catalog = DeferredToolCatalog(tuple(deferred))
    return DeferredToolSetup(build_tool_search_tool(catalog), catalog.names, catalog.hash)


def assemble_deferred_tools(filtered_tools: list[BaseTool], *, enabled: bool) -> tuple[list[BaseTool], DeferredToolSetup]:
    """从**策略过滤后**的列表构建最终工具列表 + 延迟装配。

    在工具策略过滤**之后**调，目录绝不暴露 agent 无权用的工具。**fail-closed**：若 tool_search 启用、
    有 MCP 工具通过过滤但没恢复出延迟集合，**抛错**而非静默把它们的完整 schema 绑给模型。

    每个 agent 构建路径（lead / 内嵌 client / 子代理）共用，从一处拿到同样的 fail-closed 保证。
    """
    deferred_setup = build_deferred_tool_setup(filtered_tools, enabled=enabled)
    if enabled and not deferred_setup.deferred_names and any(is_mcp_tool(t) for t in filtered_tools):
        raise RuntimeError("tool_search enabled and MCP tools survived policy filtering, but no deferred set was recovered - refusing to bind MCP schemas (fail-closed).")
    final_tools = list(filtered_tools)
    if deferred_setup.tool_search_tool:
        final_tools.append(deferred_setup.tool_search_tool)
    return final_tools, deferred_setup


# ── Prompt 渲染 ──


def get_deferred_tools_prompt_section(*, deferred_names: frozenset[str] = frozenset()) -> str:
    """从显式的延迟名字集生成 ``<available-deferred-tools>`` 段。

    只列名字，让 agent 知道有哪些工具存在、可用 tool_search 加载。无延迟工具时返回空串。
    名字集在 agent 构建时（工具策略过滤后）算好传入。

    放在这里（紧邻产出 ``deferred_names`` 的装配），让每个 agent 构建路径（lead / 内嵌 client / 子代理）
    用同样方式渲染，无需回耦合 ``lead_agent.prompt``。
    """
    if not deferred_names:
        return ""
    names = "\n".join(sorted(deferred_names))
    return f"<available-deferred-tools>\n{names}\n</available-deferred-tools>"
