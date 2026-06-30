"""GroundRoute community 搜索 + 抓取 provider（纯 httpx，无 SDK）。

GroundRoute（https://groundroute.ai）是一个 meta 搜索层：一个 API 在六个搜索引擎
（Serper / Brave / Exa / Tavily / Firecrawl / Perplexity）前面。它把每个 query 路由到
「达到质量线的最便宜引擎」并缓存重复 query——高量研究任务在一个引擎挂掉时仍能继续，
且花费不超过直连单引擎。

本模块自包含（仅 httpx，无 GroundRoute SDK）。``/v1/search`` 的请求/响应映射对齐
GroundRoute MCP server 与已验证的 Langflow 组件：
  results[] = {url, title, snippet, content, source_engine, published_at}

``web_search`` 返回归一化的 ``{title, url, snippet, source_engine}`` JSON 列表；
``web_fetch`` 用 GroundRoute ``mode=page`` 读一个 URL，返回抽出的文本。

config 访问走 mini ``_common.get_tool_extras``。对齐 deer ``community/groundroute/tools.py``。
"""

from __future__ import annotations

import json
import logging
import os

import httpx
from langchain.tools import tool

from deerflow.community._common import coerce_int, get_tool_extras, truncate_content

logger = logging.getLogger(__name__)

_GROUNDROUTE_ENDPOINT = "https://api.groundroute.ai/v1/search"
_DEFAULT_MAX_RESULTS = 5
# GroundROUTE 服务端把 max_results 钳到 1-50；这里也钳，与之一致。
_MAX_RESULTS_CAP = 50
_TIMEOUT_S = 30.0
# Warn at most once per tool（"web_search" / "web_fetch"）缺 key 的告警。
_api_key_warned: set[str] = set()


def _get_api_key(tool_name: str) -> str | None:
    """从给定 tool 的 config 块、再从环境变量解析 GroundRoute key。

    ``tool_name`` 是要读的 config 段（web_search vs web_fetch）——这样「fetch 用 GroundRoute、
    search 用别的引擎」的流程也能读对 key。对齐 serper/exa/firecrawl（都吃 tool name）。
    """
    api_key = get_tool_extras(tool_name).get("api_key")
    if isinstance(api_key, str) and api_key.strip():
        return api_key.strip()
    env_key = os.getenv("GROUNDROUTE_API_KEY")
    if isinstance(env_key, str) and env_key.strip():
        return env_key.strip()
    return None


def _coerce_max_results(value: object, *, default: int = _DEFAULT_MAX_RESULTS) -> int:
    coerced = coerce_int(value, default)
    return max(1, min(coerced, _MAX_RESULTS_CAP))


def _missing_key_error(tool_name: str, **context: str) -> str:
    if tool_name not in _api_key_warned:
        _api_key_warned.add(tool_name)
        logger.warning(
            "GroundRoute API key is not set for '%s'. Set GROUNDROUTE_API_KEY in your environment or provide api_key in config.yaml. Get a free key at https://groundroute.ai/keys",
            tool_name,
        )
    return json.dumps({"error": "GROUNDROUTE_API_KEY is not configured", **context}, ensure_ascii=False)


def _post_search(api_key: str, body: dict) -> dict:
    with httpx.Client(timeout=_TIMEOUT_S) as client:
        response = client.post(
            _GROUNDROUTE_ENDPOINT,
            json=body,
            headers={"Authorization": f"Bearer {api_key}"},
        )
    response.raise_for_status()
    return response.json()


@tool("web_search", parse_docstring=True)
def web_search_tool(query: str, max_results: int | None = None) -> str:
    """Search the web for information using GroundRoute.

    GroundRoute routes the query across six search engines and returns the result
    set from the engine it selected, with failover if one engine is unavailable.

    Args:
        query: Search keywords describing what you want to find. Be specific for better results.
        max_results: Maximum number of search results to return. If omitted, uses the configured value (default 5). Clamped to 1-50.
    """
    # 优先用调用方传的 max_results；只在没传时回退到 config。
    if max_results is None:
        max_results = get_tool_extras("web_search").get("max_results")
    count = _DEFAULT_MAX_RESULTS if max_results is None else _coerce_max_results(max_results)

    api_key = _get_api_key("web_search")
    if not api_key:
        return _missing_key_error("web_search", query=query)

    try:
        data = _post_search(api_key, {"query": query, "max_results": count})
    except httpx.HTTPStatusError as e:
        logger.error("GroundRoute API returned HTTP %s: %s", e.response.status_code, e.response.text)
        return json.dumps(
            {"error": f"GroundRoute API error: HTTP {e.response.status_code}", "query": query},
            ensure_ascii=False,
        )
    except Exception as e:  # noqa: BLE001
        logger.error("GroundRoute search failed: %s: %s", type(e).__name__, e)
        return json.dumps({"error": str(e), "query": query}, ensure_ascii=False)

    results = data.get("results") or []
    if not results:
        return json.dumps({"error": "No results found", "query": query}, ensure_ascii=False)

    normalized_results = [
        {
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "snippet": r.get("snippet", ""),
            "source_engine": r.get("source_engine", ""),
        }
        for r in results
    ]
    return json.dumps(normalized_results, indent=2, ensure_ascii=False)


@tool("web_fetch", parse_docstring=True)
def web_fetch_tool(url: str) -> str:
    """Fetch the contents of a web page at a given URL via GroundRoute.
    Only fetch EXACT URLs that have been provided directly by the user or have been returned in results from the web_search and web_fetch tools.
    This tool can NOT access content that requires authentication, such as private Google Docs or pages behind login walls.
    Do NOT add www. to URLs that do NOT have them.
    URLs must include the schema: https://example.com is a valid URL while example.com is an invalid URL.

    Args:
        url: The URL to fetch the contents of.
    """
    api_key = _get_api_key("web_fetch")
    if not api_key:
        return _missing_key_error("web_fetch", url=url)

    try:
        data = _post_search(api_key, {"query": url, "mode": "page", "max_results": 1})
    except httpx.HTTPStatusError as e:
        logger.error("GroundRoute fetch returned HTTP %s: %s", e.response.status_code, e.response.text)
        return f"Error: GroundRoute API error: HTTP {e.response.status_code}"
    except Exception as e:  # noqa: BLE001
        logger.error("GroundRoute fetch failed: %s: %s", type(e).__name__, e)
        return f"Error: {e}"

    results = data.get("results") or []
    if not results:
        return "Error: No results found"

    result = results[0]
    content = result.get("content") or result.get("snippet") or ""
    title = result.get("title", "")
    return f"# {title}\n\n{truncate_content(content)}"
