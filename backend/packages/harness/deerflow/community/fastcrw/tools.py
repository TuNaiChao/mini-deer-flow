"""fastCRW 搜索 + 抓取 provider（Firecrawl 兼容 API，复用 ``firecrawl`` SDK）。

fastCRW 是一个 Firecrawl 兼容的 web 数据引擎（单个 Rust 二进制；可自托管或云）。
因为 REST API 与 Firecrawl 兼容，本 provider 复用 ``FirecrawlApp`` 客户端、只换 base URL。
云默认指向托管服务；自托管时在 tool config 里覆盖 ``base_url``（或设 ``CRW_API_URL``）。

**软加载占位**（红线 #24）：``firecrawl`` SDK 缺包时工具调用返可操作安装提示，模块本身不崩。
对齐 deer ``community/fastcrw/tools.py``，config 访问走 mini ``_common.get_tool_extras``。
"""

from __future__ import annotations

import json
import logging
import os

from langchain.tools import tool

from deerflow.community._common import get_tool_extras, normalize_search_result, truncate_content

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://fastcrw.com/api"
_INSTALL_HINT = "firecrawl-py is not installed. Run: pip install firecrawl-py"


def _get_fastcrw_client(tool_name: str = "web_search"):
    """构造 FirecrawlApp（指向 fastCRW base URL）。缺包抛 ImportError（由工具层捕获转 Error）。"""
    from firecrawl import FirecrawlApp

    extras = get_tool_extras(tool_name)
    api_key = extras.get("api_key") or os.getenv("CRW_API_KEY")
    base_url = extras.get("base_url") or os.getenv("CRW_API_URL", DEFAULT_BASE_URL)
    return FirecrawlApp(api_key=api_key, api_url=base_url)  # type: ignore[arg-type]


@tool("web_search", parse_docstring=True)
def web_search_tool(query: str) -> str:
    """Search the web.

    Args:
        query: The query to search for.
    """
    try:
        max_results = get_tool_extras("web_search").get("max_results", 5)
        client = _get_fastcrw_client("web_search")
        result = client.search(query, limit=max_results)
    except ImportError:
        return f"Error: {_INSTALL_HINT}"
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"

    # result.web 是 SearchResultWeb 对象列表
    web_results = getattr(result, "web", None) or []
    normalized_results = [
        normalize_search_result(
            title=getattr(item, "title", "") or "",
            url=getattr(item, "url", "") or "",
            snippet=getattr(item, "description", "") or "",
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
        client = _get_fastcrw_client("web_fetch")
        result = client.scrape(url, formats=["markdown"])
    except ImportError:
        return f"Error: {_INSTALL_HINT}"
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"

    markdown_content = getattr(result, "markdown", None) or ""
    metadata = getattr(result, "metadata", None)
    title = (metadata.title if metadata and metadata.title else "Untitled") or "Untitled"

    if not markdown_content:
        return "Error: No content found"

    return f"# {title}\n\n{truncate_content(markdown_content)}"
