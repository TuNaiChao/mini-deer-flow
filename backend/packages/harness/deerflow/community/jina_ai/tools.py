"""Jina Reader 抓取 provider（async）。

对齐 deer ``community/jina_ai/tools.py``：用 ``JinaClient`` 抓 HTML，再经
``utils/readability.py`` 的 ``ReadabilityExtractor`` 提取标题 + 正文 → markdown，
最后 4KB 截断。``timeout`` / ``proxy`` / ``trust_env`` 可从 config.yaml 的 web_fetch 段配。

mini 适配：参数强转经 ``_common``（``coerce_bool``/``coerce_timeout``/``coerce_proxy``）；
readability 软加载（缺 readabilipy/markdownify 走纯 Python 兜底）；CPU 解析卸线程（``to_thread``）。
"""

from __future__ import annotations

import asyncio
import logging

from langchain.tools import tool

from deerflow.community._common import coerce_bool, coerce_proxy, coerce_timeout, get_tool_extras, truncate_content
from deerflow.community.jina_ai.jina_client import JinaClient
from deerflow.utils.readability import ReadabilityExtractor

logger = logging.getLogger(__name__)

_readability_extractor = ReadabilityExtractor()


@tool("web_fetch", parse_docstring=True)
async def web_fetch_tool(url: str) -> str:
    """Fetch the contents of a web page at a given URL.
    Only fetch EXACT URLs that have been provided directly by the user or have been returned in results from the web_search and web_fetch tools.
    This tool can NOT access content that requires authentication, such as private Google Docs or pages behind login walls.
    Do NOT add www. to URLs that do NOT have them.
    URLs must include the schema: https://example.com is a valid URL while example.com is an invalid URL.

    Args:
        url: The URL to fetch the contents of.
    """
    extras = get_tool_extras("web_fetch")
    timeout = coerce_timeout(extras.get("timeout"), 10)
    proxy = coerce_proxy(extras.get("proxy"))
    trust_env = coerce_bool(extras.get("trust_env"), True)

    jina_client = JinaClient()
    html_content = await jina_client.crawl(url, return_format="html", timeout=timeout, proxy=proxy, trust_env=trust_env)

    if isinstance(html_content, str) and html_content.startswith("Error:"):
        return html_content

    # readability 提取是 CPU 密集（可能跑 readabilipy 子进程）→ 卸线程防阻塞事件循环。
    article = await asyncio.to_thread(_readability_extractor.extract_article, html_content)
    return truncate_content(article.to_markdown())
