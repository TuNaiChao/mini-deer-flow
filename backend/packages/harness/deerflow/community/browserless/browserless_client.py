"""Browserless 无头 Chrome 客户端（async）。

对齐 deer ``community/browserless/browserless_client.py``：调自托管 Browserless（headless Chrome）
渲染页面拿 HTML（支持 waitForEvent/waitForSelector/资源拦截）。``base_url``/``token``/``timeout_s``
从 config.yaml 的 web_fetch 段配。

失败归一成 ``"Error: ..."`` 前缀字符串（与 deer 一致，调用方据此判断）。
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class BrowserlessClient:
    """Browserless headless Chrome API 客户端。"""

    def __init__(self, base_url: str, token: str = "", timeout_s: float = 30) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout_s = timeout_s

    async def fetch_html(
        self,
        url: str,
        wait_for_event: str = "",
        wait_for_timeout_ms: int = 0,
        wait_for_selector: str = "",
        wait_for_selector_timeout_ms: int = 5000,
        reject_resource_types: list[str] | None = None,
        reject_request_pattern: list[str] | None = None,
    ) -> str:
        """渲染 ``url`` 拿 HTML。失败返回 ``"Error: ..."``。"""
        payload: dict[str, Any] = {"url": url}
        if self.token:
            payload["token"] = self.token
        if wait_for_event:
            payload["waitForEvent"] = wait_for_event
        if wait_for_timeout_ms > 0:
            payload["waitForTimeout"] = wait_for_timeout_ms
        if wait_for_selector:
            payload["waitForSelector"] = {"selector": wait_for_selector, "timeout": wait_for_selector_timeout_ms}
        if reject_resource_types:
            payload["rejectResourceTypes"] = reject_resource_types
        if reject_request_pattern:
            payload["rejectRequestPattern"] = reject_request_pattern

        logger.debug("Fetching URL via Browserless: %s", url)
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                resp = await client.post(
                    f"{self.base_url}/content",
                    json=payload,
                    headers={"Content-Type": "application/json", "Cache-Control": "no-cache"},
                )
                code = resp.status_code
                if code != 200:
                    return f"Error: Browserless HTTP {code}: {resp.text[:200]}"

                html = resp.text
                if not html or not html.strip():
                    return "Error: Browserless returned empty response"
                return html
        except httpx.TimeoutException:
            return f"Error: Browserless request timed out after {self.timeout_s}s"
        except httpx.RequestError as e:
            logger.error("Browserless request failed: %s", e)
            return f"Error: Browserless request failed: {e!s}"
        except Exception as e:  # noqa: BLE001
            logger.error("Browserless fetch failed: %s", e)
            return f"Error: Browserless fetch failed: {e!s}"
