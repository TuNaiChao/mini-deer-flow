"""Browserless 抓取 provider（async，headless Chrome 渲染）。

对齐 deer ``community/browserless/tools.py``：经 ``BrowserlessClient`` 渲染拿 HTML（JS 执行后的），
再经 ``readability`` 提取 → markdown → 4KB 截断。适合抓 SPA / 需要等元素的页面。
``base_url``/``token``/``timeout_s``/``wait_for_*`` 从 config.yaml 的 web_fetch 段配。
"""

from __future__ import annotations

import asyncio
import logging

from langchain.tools import tool

from deerflow.community._common import coerce_int, get_tool_extras, truncate_content
from deerflow.community.browserless.browserless_client import BrowserlessClient
from deerflow.utils.readability import ReadabilityExtractor

logger = logging.getLogger(__name__)

# readability 提取是 CPU 密集 → 一律经 to_thread 卸线程。
_readability_extractor = ReadabilityExtractor()


def _get_browserless_client() -> BrowserlessClient:
    extras = get_tool_extras("web_fetch")
    base_url = extras.get("base_url", "http://localhost:3032")
    token = extras.get("token", "")
    timeout_s = float(extras.get("timeout_s", 30))
    return BrowserlessClient(base_url=base_url, token=token, timeout_s=timeout_s)


@tool("web_fetch", parse_docstring=True)
async def web_fetch_tool(url: str) -> str:
    """Fetch the contents of a web page at a given URL using Browserless (headless Chrome).
    Only fetch EXACT URLs that have been provided directly by the user or have been returned in results from the web_search and web_fetch tools.
    This tool can NOT access content that requires authentication, such as private Google Docs or pages behind login walls.
    Do NOT add www. to URLs that do NOT have them.
    URLs must include the schema: https://example.com is a valid URL while example.com is an invalid URL.

    Args:
        url: The URL to fetch the contents of.
    """
    try:
        extras = get_tool_extras("web_fetch")
        wait_for_event = extras.get("wait_for_event", "")
        wait_for_timeout_ms = coerce_int(extras.get("wait_for_timeout_ms"), 0)
        wait_for_selector = extras.get("wait_for_selector", "")

        client = _get_browserless_client()
        html = await client.fetch_html(
            url=url,
            wait_for_event=wait_for_event,
            wait_for_timeout_ms=wait_for_timeout_ms,
            wait_for_selector=wait_for_selector,
        )

        if html.startswith("Error:"):
            return html

        article = await asyncio.to_thread(_readability_extractor.extract_article, html)
        return truncate_content(article.to_markdown())
    except Exception as e:  # noqa: BLE001
        logger.error("Error in browserless web_fetch_tool: %s", e)
        return f"Error: {e}"
