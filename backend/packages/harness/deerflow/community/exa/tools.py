"""Exa 搜索 + 抓取 provider（需 ``exa_py`` SDK + api_key）。

对齐 deer ``community/exa/tools.py``。**软加载占位**：``exa_py`` 缺包时返可操作安装提示。
"""

from __future__ import annotations

import json
import logging

from langchain.tools import tool

from deerflow.community._common import get_tool_extras, normalize_search_result, truncate_content

logger = logging.getLogger(__name__)

_INSTALL_HINT = "exa_py is not installed. Run: pip install exa_py"


def _get_exa_client(tool_name: str = "web_search"):
    """构造 Exa client。缺包抛 ImportError（由工具层捕获转成 Error）。"""
    from exa_py import Exa

    api_key = get_tool_extras(tool_name).get("api_key")
    return Exa(api_key=api_key)


@tool("web_search", parse_docstring=True)
def web_search_tool(query: str) -> str:
    """Search the web.

    Args:
        query: The query to search for.
    """
    try:
        extras = get_tool_extras("web_search")
        max_results = extras.get("max_results", 5)
        search_type = extras.get("search_type", "auto")
        contents_max_characters = extras.get("contents_max_characters", 1000)

        client = _get_exa_client()
        res = client.search(
            query,
            type=search_type,
            num_results=max_results,
            contents={"highlights": {"max_characters": contents_max_characters}},
        )
    except ImportError:
        return f"Error: {_INSTALL_HINT}"
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"

    normalized_results = [
        normalize_search_result(
            title=getattr(result, "title", "") or "",
            url=getattr(result, "url", "") or "",
            snippet="\n".join(result.highlights) if getattr(result, "highlights", None) else "",
            content_key="snippet",
        )
        for result in res.results
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
        client = _get_exa_client("web_fetch")
        res = client.get_contents([url], text={"max_characters": 4096})
    except ImportError:
        return f"Error: {_INSTALL_HINT}"
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"

    if res.results:
        result = res.results[0]
        title = getattr(result, "title", None) or "Untitled"
        text = getattr(result, "text", "") or ""
        return f"# {title}\n\n{truncate_content(text)}"
    return "Error: No results found"
