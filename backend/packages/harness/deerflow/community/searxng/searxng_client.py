"""SearXNG 元搜索引擎客户端（async）。

对齐 deer ``community/searxng/searxng_client.py``：SearXNG 是可自托管的元搜索聚合
（聚合 Google/Bing/DuckDuckGo…）。``base_url`` 指向自建实例（默认 localhost:8088）。
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class SearxngClient:
    """SearXNG 元搜索 API 客户端。"""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    async def search(
        self,
        query: str,
        max_results: int = 5,
        categories: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """搜索并返回原始结果 list（调用方负责归一）。请求失败抛异常（由工具层捕获归一成 Error）。"""
        params: dict[str, Any] = {
            "q": query,
            "format": "json",
            "language": "auto",
            "pageno": 1,
        }
        if max_results:
            params["limit"] = max_results
        if categories:
            params["categories"] = ",".join(categories)

        logger.debug("Searching SearXNG at %s with query: %s", self.base_url, query)
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    f"{self.base_url}/search",
                    params=params,
                    headers={
                        "User-Agent": "Mozilla/5.0 (compatible; DeerFlow/1.0)",
                        "Accept": "application/json",
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                results = data.get("results", [])
                return results[:max_results] if max_results else results
        except httpx.HTTPStatusError as e:
            logger.error("SearXNG search returned error status: %s", e)
            raise
        except httpx.RequestError as e:
            logger.error("SearXNG search request failed: %s", e)
            raise
        except Exception as e:  # noqa: BLE001
            logger.error("An unexpected error occurred during SearXNG search: %s", e)
            raise
