"""Jina Reader API 客户端。

对齐 deer ``community/jina_ai/jina_client.py``：调 ``https://r.jina.ai/`` 把网页转成
可读文本（HTML/markdown）。``JINA_API_KEY`` 可选（无 key 走限流免费额度，记一次 warning）。

mini 适配：HTTP 走 ``_common.post_json``（抽出的共享 async httpx 封装），异常归一成
``"Error: ..."`` 前缀字符串（与 deer JinaClient 一致）。
"""

from __future__ import annotations

import logging
import os

from deerflow.community._common import post_json

logger = logging.getLogger(__name__)

JINA_READER_ENDPOINT = "https://r.jina.ai/"

_api_key_warned = False


class JinaClient:
    """调 Jina Reader API 抓单个 URL。"""

    async def crawl(
        self,
        url: str,
        return_format: str = "html",
        timeout: int = 10,
        proxy: str | None = None,
        trust_env: bool = True,
    ) -> str:
        """抓 ``url``，返回 Jina 给的文本（失败返回 ``"Error: ..."`` 字符串）。"""
        global _api_key_warned
        headers = {
            "Content-Type": "application/json",
            "X-Return-Format": return_format,
            "X-Timeout": str(timeout),
        }
        if os.getenv("JINA_API_KEY"):
            headers["Authorization"] = f"Bearer {os.getenv('JINA_API_KEY')}"
        elif not _api_key_warned:
            _api_key_warned = True
            logger.warning("Jina API key is not set. Provide your own key to access a higher rate limit. See https://jina.ai/reader for more information.")

        return await post_json(
            JINA_READER_ENDPOINT,
            headers=headers,
            json_body={"url": url},
            timeout=timeout,
            proxy=proxy,
            trust_env=trust_env,
        )
