"""Brave Search 网页搜索 provider（REST API，需 API key）。

对齐 deer ``community/brave/tools.py``：直接调 Brave Search 官方 REST API（``count`` 上限 20），
``BRAVE_SEARCH_API_KEY`` 来自环境变量或 config.yaml 的 web_search.api_key。
与 ddg 的 ``backend: brave``（经 DDGS 聚合抓取）不同——本 provider 调官方 API，给结构化结果 + 配额。
"""

from __future__ import annotations

import json
import logging
import os

import httpx
from langchain.tools import tool

from deerflow.community._common import get_tool_extras, normalize_search_result

logger = logging.getLogger(__name__)

_BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
_DEFAULT_MAX_RESULTS = 5
# Brave Search API 的 count 参数单次最多 20。
_BRAVE_MAX_COUNT = 20
_api_key_warned = False


def _get_api_key() -> str | None:
    extras = get_tool_extras("web_search")
    api_key = extras.get("api_key")
    if isinstance(api_key, str) and api_key.strip():
        return api_key
    return os.getenv("BRAVE_SEARCH_API_KEY")


def _coerce_max_results(value: object, *, default: int = _DEFAULT_MAX_RESULTS) -> int:
    try:
        coerced = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        logger.warning("Invalid Brave Search max_results=%r; using default %s", value, default)
        coerced = default
    return max(1, min(coerced, _BRAVE_MAX_COUNT))


@tool("web_search", parse_docstring=True)
def web_search_tool(query: str, max_results: int = 5) -> str:
    """Search the web for information using Brave Search.

    Args:
        query: Search keywords describing what you want to find. Be specific for better results.
        max_results: Maximum number of search results to return. Default is 5.
    """
    global _api_key_warned

    max_results = get_tool_extras("web_search").get("max_results", max_results)
    count = _coerce_max_results(max_results)

    api_key = _get_api_key()
    if not api_key:
        if not _api_key_warned:
            _api_key_warned = True
            logger.warning("Brave Search API key is not set. Set BRAVE_SEARCH_API_KEY in your environment or provide api_key in config.yaml. Sign up at https://brave.com/search/api/")
        return json.dumps({"error": "BRAVE_SEARCH_API_KEY is not configured", "query": query}, ensure_ascii=False)

    headers = {"X-Subscription-Token": api_key, "Accept": "application/json"}
    params = {"q": query, "count": count, "text_decorations": False}

    try:
        with httpx.Client(timeout=30) as client:
            response = client.get(_BRAVE_ENDPOINT, headers=headers, params=params)
        response.raise_for_status()
        data = response.json()
    except httpx.HTTPStatusError as e:
        logger.error("Brave Search API returned HTTP %s: %s", e.response.status_code, e.response.text)
        return json.dumps(
            {"error": f"Brave Search API error: HTTP {e.response.status_code}", "query": query},
            ensure_ascii=False,
        )
    except Exception as e:  # noqa: BLE001
        logger.error("Brave search failed: %s: %s", type(e).__name__, e)
        return json.dumps({"error": str(e), "query": query}, ensure_ascii=False)

    web_results = (data.get("web") or {}).get("results", [])
    if not web_results:
        return json.dumps({"error": "No results found", "query": query}, ensure_ascii=False)

    normalized_results = [normalize_search_result(title=r.get("title", ""), url=r.get("url", ""), snippet=r.get("description", "")) for r in web_results]

    output = {"query": query, "total_results": len(normalized_results), "results": normalized_results}
    return json.dumps(output, indent=2, ensure_ascii=False)
