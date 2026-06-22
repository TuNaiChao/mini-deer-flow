"""SearXNG 网页搜索 provider（async，自托管）。

对齐 deer ``community/searxng/tools.py``：经 ``SearxngClient`` 搜，结果归一成
``{title,url,snippet}``。``base_url`` / ``max_results`` 从 config.yaml 的 web_search 段配。
"""

from __future__ import annotations

import json
import logging

from langchain.tools import tool

from deerflow.community._common import coerce_int, get_tool_extras, normalize_search_result
from deerflow.community.searxng.searxng_client import SearxngClient

logger = logging.getLogger(__name__)


def _get_searxng_client() -> SearxngClient:
    extras = get_tool_extras("web_search")
    base_url = extras.get("base_url", "http://localhost:8088")
    return SearxngClient(base_url=base_url)


@tool("web_search", parse_docstring=True)
async def web_search_tool(query: str) -> str:
    """Search the web using SearXNG.

    Args:
        query: The query to search for.
    """
    try:
        max_results = coerce_int(get_tool_extras("web_search").get("max_results"), 5)

        client = _get_searxng_client()
        results = await client.search(query, max_results=max_results)

        normalized = [
            normalize_search_result(
                title=r.get("title", ""),
                url=r.get("url", ""),
                snippet=r.get("content", ""),
                content_key="snippet",
            )
            for r in results
        ]
        return json.dumps(normalized, indent=2, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001
        logger.error("Error in searxng web_search_tool: %s", e)
        return json.dumps({"error": str(e), "query": query}, ensure_ascii=False)
