"""DuckDuckGo 网页搜索工具（无需 API key）。

对齐 deer ``community/ddg_search/tools.py``：用 ``ddgs`` 库做文本搜索，**不需要 API key**
——这是它相对 tavily/brave/serper 的最大优势（开箱即用）。支持多 backend（auto/duckduckgo/
wikipedia/…）、region、safesearch，并对 CJK 等非拉丁查询自动推断 Wikipedia region。

mini 适配：``config.model_extra.get(...)`` → ``_common.get_tool_extras(...).get(...)``；
``_common.normalize_search_result`` 归一结果。
"""

from __future__ import annotations

import json
import logging

from langchain.tools import tool

from deerflow.community._common import get_tool_extras, normalize_search_result

logger = logging.getLogger(__name__)

DEFAULT_BACKEND = "auto"
DEFAULT_REGION = "wt-wt"  # DDGS 全球 region
DEFAULT_SAFESEARCH = "moderate"
DEFAULT_WIKIPEDIA_REGION = "us-en"

# backend 里出现这些值时，region 的第二段会被当成 Wikipedia 子域名语言。
WIKIPEDIA_BACKENDS = {"auto", "all", "wikipedia"}
WIKIPEDIA_LANGUAGE_ALIASES = {
    "jp": "ja",
    "kr": "ko",
    "tzh": "zh",
    "wt": "en",
}


def _normalize_backend(backend: str | list[str] | tuple[str, ...] | None) -> str:
    if backend is None:
        return DEFAULT_BACKEND
    if isinstance(backend, (list, tuple)):
        return ",".join(str(part).strip() for part in backend if str(part).strip()) or DEFAULT_BACKEND
    return str(backend).strip() or DEFAULT_BACKEND


def _normalize_setting(value: str | None, default: str) -> str:
    return str(value).strip() if value else default


def _backend_includes_wikipedia(backend: str | list[str] | tuple[str, ...] | None) -> bool:
    backend_str = _normalize_backend(backend)
    return any(part.strip().lower() in WIKIPEDIA_BACKENDS for part in backend_str.split(","))


def _contains_codepoint(text: str, ranges: tuple[tuple[int, int], ...]) -> bool:
    return any(start <= ord(char) <= end for char in text for start, end in ranges)


def _infer_wikipedia_region(query: str) -> str:
    """当 DDGS 用全球 region（wt-wt）时，按查询文字推断一个合法的 Wikipedia 语言 region。

    DDGS 的 wikipedia engine 把 region 第二段当 Wikipedia 子域名，wt-wt 会变成
    wt.wikipedia.org（无效）。按 CJK / 西里尔 / 希腊 / 希伯来 / 阿拉伯字符块选合理的 region。
    """
    if _contains_codepoint(query, ((0x3040, 0x30FF), (0x31F0, 0x31FF))):
        return "jp-ja"  # 平假名/片假名 → 日本
    if _contains_codepoint(query, ((0xAC00, 0xD7AF), (0x1100, 0x11FF), (0x3130, 0x318F))):
        return "kr-ko"  # 韩文
    if _contains_codepoint(query, ((0x3400, 0x9FFF),)):
        return "cn-zh"  # CJK 统一表意文字 → 中国
    if _contains_codepoint(query, ((0x0400, 0x04FF),)):
        return "ru-ru"  # 西里尔 → 俄语
    if _contains_codepoint(query, ((0x0370, 0x03FF),)):
        return "gr-el"  # 希腊
    if _contains_codepoint(query, ((0x0590, 0x05FF),)):
        return "il-he"  # 希伯来
    if _contains_codepoint(query, ((0x0600, 0x06FF),)):
        return "xa-ar"  # 阿拉伯
    return DEFAULT_WIKIPEDIA_REGION


def _resolve_ddgs_region(query: str, region: str | None, backend: str | list[str] | tuple[str, ...] | None) -> str:
    """计算真正传给 DDGS 的 region（处理 Wikipedia region 的语言子域名问题）。"""
    normalized_region = _normalize_setting(region, DEFAULT_REGION).lower()
    if not _backend_includes_wikipedia(backend):
        return normalized_region

    if normalized_region == DEFAULT_REGION:
        return _infer_wikipedia_region(query)

    if "-" not in normalized_region:
        return DEFAULT_WIKIPEDIA_REGION

    country, language = normalized_region.split("-", 1)
    return f"{country}-{WIKIPEDIA_LANGUAGE_ALIASES.get(language, language)}"


def _search_text(
    query: str,
    max_results: int = 5,
    region: str | None = DEFAULT_REGION,
    safesearch: str | None = DEFAULT_SAFESEARCH,
    backend: str | list[str] | tuple[str, ...] | None = DEFAULT_BACKEND,
) -> list[dict]:
    """用 DuckDuckGo 执行文本搜索。缺 ddgs 库 → 记可操作提示 + 返回 []。"""
    try:
        from ddgs import DDGS
    except ImportError:
        logger.error("ddgs library not installed. Run: pip install ddgs")
        return []

    ddgs = DDGS(timeout=30)

    try:
        backend = _normalize_backend(backend)
        safesearch = _normalize_setting(safesearch, DEFAULT_SAFESEARCH)
        effective_region = _resolve_ddgs_region(query, region, backend)
        results = ddgs.text(
            query,
            region=effective_region,
            safesearch=safesearch,
            max_results=max_results,
            backend=backend,
        )
        return list(results) if results else []
    except Exception as e:  # noqa: BLE001 — 搜索失败不拖垮 agent，返回空
        logger.error("Failed to search web: %s", e)
        return []


@tool("web_search", parse_docstring=True)
def web_search_tool(
    query: str,
    max_results: int = 5,
) -> str:
    """Search the web for information. Use this tool to find current information, news, articles, and facts from the internet.

    Args:
        query: Search keywords describing what you want to find. Be specific for better results.
        max_results: Maximum number of results to return. Default is 5.
    """
    extras = get_tool_extras("web_search")
    region = extras.get("region", DEFAULT_REGION)
    safesearch = extras.get("safesearch", DEFAULT_SAFESEARCH)
    backend = extras.get("backend", DEFAULT_BACKEND)
    max_results = extras.get("max_results", max_results)

    results = _search_text(
        query=query,
        max_results=max_results,
        region=region,
        safesearch=safesearch,
        backend=backend,
    )

    if not results:
        return json.dumps({"error": "No results found", "query": query}, ensure_ascii=False)

    normalized_results = [
        normalize_search_result(
            title=r.get("title", ""),
            url=r.get("href", r.get("link", "")),
            snippet=r.get("body", r.get("snippet", "")),
        )
        for r in results
    ]

    output = {
        "query": query,
        "total_results": len(normalized_results),
        "results": normalized_results,
    }
    return json.dumps(output, indent=2, ensure_ascii=False)
