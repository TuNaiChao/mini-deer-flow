"""Firecrawl 搜索 + 抓取 provider（需 ``firecrawl`` SDK + api_key）。

对齐 deer ``community/firecrawl/tools.py``。**软加载占位**：``firecrawl`` SDK 缺包时工具调用
返可操作安装提示（红线 #24），模块本身不 import 崩——这样 ``tools[].use:`` 路径永远能 resolve。
装上 ``firecrawl-py`` + 配 ``api_key`` 后自动走真实逻辑。
"""

from __future__ import annotations

import json
import logging

from langchain.tools import tool

from deerflow.community._common import get_tool_extras, normalize_search_result, truncate_content

logger = logging.getLogger(__name__)

_INSTALL_HINT = "firecrawl-py is not installed. Run: pip install firecrawl-py"


def _get_firecrawl_client(tool_name: str = "web_search"):
    """构造 FirecrawlApp。缺包抛 ImportError（由工具层捕获转成 Error）。"""
    from firecrawl import FirecrawlApp

    api_key = get_tool_extras(tool_name).get("api_key")
    return FirecrawlApp(api_key=api_key)


@tool("web_search", parse_docstring=True)
def web_search_tool(query: str) -> str:
    """Search the web.

    Args:
        query: The query to search for.
    """
    try:
        max_results = get_tool_extras("web_search").get("max_results", 5)
        client = _get_firecrawl_client("web_search")
        result = client.search(query, limit=max_results)
    except ImportError:
        return f"Error: {_INSTALL_HINT}"
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"

    web_results = getattr(result, "web", None) or []
    normalized_results = [
        normalize_search_result(
            title=getattr(item, "title", "") or "",
            url=getattr(item, "url", "") or "",
            snippet=getattr(item, "description", "") or "",
            content_key="snippet",
        )
        for item in web_results
    ]
    return json.dumps(normalized_results, indent=2, ensure_ascii=False)


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
        client = _get_firecrawl_client("web_fetch")
        result = client.scrape(url, formats=["markdown"])
    except ImportError:
        return f"Error: {_INSTALL_HINT}"
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"

    markdown_content = getattr(result, "markdown", None) or ""
    metadata = getattr(result, "metadata", None)
    title = (metadata.title if metadata and metadata.title else None) or "Untitled"
    if not markdown_content:
        return "Error: No content found"
    return f"# {title}\n\n{truncate_content(markdown_content)}"
