"""BytePlus InfoQuest 搜索/抓取/图搜 provider（需 INFOQUEST_API_KEY，软加载占位）。

对齐 deer ``community/infoquest/tools.py``。``web_fetch`` 经 readability 提取 + 4KB 截断。
配置（``search_time_range`` / ``fetch_timeout`` / ``image_size`` 等）从 config.yaml 的
web_search / web_fetch / image_search 段读。
"""

from __future__ import annotations

from langchain.tools import tool

from deerflow.community._common import get_tool_extras, truncate_content
from deerflow.community.infoquest.infoquest_client import InfoQuestClient
from deerflow.utils.readability import ReadabilityExtractor

readability_extractor = ReadabilityExtractor()


def _get_infoquest_client() -> InfoQuestClient:
    search_extras = get_tool_extras("web_search")
    fetch_extras = get_tool_extras("web_fetch")
    image_extras = get_tool_extras("image_search")
    return InfoQuestClient(
        search_time_range=search_extras.get("search_time_range", -1),
        fetch_time=fetch_extras.get("fetch_time", -1),
        fetch_timeout=fetch_extras.get("timeout", -1),
        fetch_navigation_timeout=fetch_extras.get("navigation_timeout", -1),
        image_search_time_range=image_extras.get("image_search_time_range", -1),
        image_size=image_extras.get("image_size", "i"),
    )


@tool("web_search", parse_docstring=True)
def web_search_tool(query: str) -> str:
    """Search the web.

    Args:
        query: The query to search for.
    """
    return _get_infoquest_client().web_search(query)


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
    client = _get_infoquest_client()
    result = client.fetch(url)
    if result.startswith("Error:"):
        return result
    article = readability_extractor.extract_article(result)
    return truncate_content(article.to_markdown())


@tool("image_search", parse_docstring=True)
def image_search_tool(query: str) -> str:
    """Search for images online. Use this tool BEFORE image generation to find reference images for characters, portraits, objects, scenes, or any content requiring visual accuracy.

    The returned image URLs can be used as reference images in image generation to significantly improve quality.

    Args:
        query: The query to search for images.
    """
    return _get_infoquest_client().image_search(query)
