"""Firecrawl 搜索 + 抓取 provider（需 ``firecrawl`` SDK + api_key，软加载占位）。"""

from deerflow.community.firecrawl.tools import web_fetch_tool, web_search_tool

__all__ = ["web_search_tool", "web_fetch_tool"]
