"""Tavily 搜索 + 抓取 provider（需 API key）。

对齐 deer ``community/tavily/tools.py``：``tavily-python`` 提供 ``web_search``（搜索）和
``web_fetch``（extract 抓取，4KB 截断）。需 ``api_key``（https://tavily.com）。

mini 适配：SDK 软加载（缺包 → 工具调用时返可操作错误，红线 #24）；config 经 ``_common`` 读。
``@tool`` 装饰的函数体里 import SDK，确保模块本身不因缺包 import 崩——这样 ``resolve_variable``
加载工具时永远成功，真正调用才检测 SDK。
"""

from __future__ import annotations

import json
import logging

from langchain.tools import tool

from deerflow.community._common import get_tool_extras, normalize_search_result, truncate_content

logger = logging.getLogger(__name__)

_DEFAULT_MAX_RESULTS = 5


def _get_tavily_client():  # type: ignore[no-untyped-def]
    """构造 TavilyClient。api_key 来自 config.yaml 的 web_search.api_key。缺包抛 ImportError。"""
    from tavily import TavilyClient

    api_key = get_tool_extras("web_search").get("api_key")
    return TavilyClient(api_key=api_key)


@tool("web_search", parse_docstring=True)
def web_search_tool(query: str) -> str:
    """Search the web.

    Args:
        query: The query to search for.
    """
    max_results = get_tool_extras("web_search").get("max_results", _DEFAULT_MAX_RESULTS)
    try:
        client = _get_tavily_client()
    except ImportError:
        return "Error: tavily-python is not installed. Run: pip install tavily-python"
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"

    try:
        res = client.search(query, max_results=max_results)
        normalized_results = [
            normalize_search_result(
                title=result.get("title", ""),
                url=result.get("url", ""),
                snippet=result.get("content", ""),
                content_key="snippet",
            )
            for result in res.get("results", [])
        ]
        return json.dumps(normalized_results, indent=2, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001
        logger.error("Tavily search failed: %s", e)
        return f"Error: {e}"


@tool("web_fetch", parse_docstring=True)
def web_fetch_tool(url: str) -> str:
    """Fetch the contents of a web page at a given URL.
    Only fetch EXACT URLs that have been provided directly by the user or have been returned in results from the web_search and web_fetch tools.
    This tool can NOT access content that requires authentication, such as private Google Docs or pages behind login walls.
    Do NOT add www. to URLs that do NOT have them.
    URLs must include the schema: https://example.com is a valid URL while example.com is an invalid URL.

    Args:
        url: The URL to fetch the contents of.
    """
    try:
        client = _get_tavily_client()
    except ImportError:
        return "Error: tavily-python is not installed. Run: pip install tavily-python"
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"

    try:
        res = client.extract([url])
        if res.get("failed_results"):
            return f"Error: {res['failed_results'][0].get('error', 'extract failed')}"
        if res.get("results"):
            result = res["results"][0]
            title = result.get("title", "Untitled")
            content = truncate_content(result.get("raw_content", ""))
            return f"# {title}\n\n{content}"
        return "Error: No results found"
    except Exception as e:  # noqa: BLE001
        logger.error("Tavily fetch failed: %s", e)
        return f"Error: {e}"
