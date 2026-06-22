"""BytePlus InfoQuest 搜索/抓取/图搜客户端（compact，软加载占位）。

对齐 deer ``community/infoquest/infoquest_client.py``（deer 版 ~400 行含大量 debug 日志），
mini 做精简移植：保留核心 ``web_search`` / ``fetch`` / ``image_search`` 三方法的请求与结果解析
逻辑，去掉冗长日志。``INFOQUEST_API_KEY`` 来自环境变量；``requests`` 是核心依赖但仍 try/except
软加载，保证缺包时模块 import 不崩。
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_SEARCH_ENDPOINT = "https://search.infoquest.bytepluses.com"
_READER_ENDPOINT = "https://reader.infoquest.bytepluses.com"


def _requests_module():
    """软加载 requests（核心依赖，但仍防御性 try/except）。"""
    import requests

    return requests


class InfoQuestClient:
    """BytePlus InfoQuest API 客户端（compact 版）。"""

    def __init__(
        self,
        fetch_time: int = -1,
        fetch_timeout: int = -1,
        fetch_navigation_timeout: int = -1,
        search_time_range: int = -1,
        image_search_time_range: int = -1,
        image_size: str = "i",
    ) -> None:
        self.fetch_time = fetch_time
        self.fetch_timeout = fetch_timeout
        self.fetch_navigation_timeout = fetch_navigation_timeout
        self.search_time_range = search_time_range
        self.image_search_time_range = image_search_time_range
        self.image_size = image_size
        self.api_key_set = bool(os.getenv("INFOQUEST_API_KEY"))
        if not self.api_key_set:
            logger.warning("InfoQuest API key is not set. Provide INFOQUEST_API_KEY for authentication.")

    def _prepare_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if os.getenv("INFOQUEST_API_KEY"):
            headers["Authorization"] = f"Bearer {os.getenv('INFOQUEST_API_KEY')}"
        return headers

    def fetch(self, url: str, return_format: str = "html") -> str:
        """抓单 URL，返回 reader_result / content / 原文（失败 ``Error: ...``）。"""
        if not self.api_key_set:
            return "Error: INFOQUEST_API_KEY is not configured"
        normalized_format = "HTML" if return_format and return_format.lower() == "html" else return_format
        data: dict[str, Any] = {"url": url, "format": normalized_format}
        if self.fetch_time > 0:
            data["fetch_time"] = self.fetch_time
        if self.fetch_timeout > 0:
            data["timeout"] = self.fetch_timeout
        if self.fetch_navigation_timeout > 0:
            data["navi_timeout"] = self.fetch_navigation_timeout

        try:
            requests = _requests_module()
            response = requests.post(_READER_ENDPOINT, headers=self._prepare_headers(), json=data)
            if response.status_code != 200:
                return f"Error: fetch API returned status {response.status_code}: {response.text}"
            if not response.text or not response.text.strip():
                return "Error: no result found"
            try:
                response_data = json.loads(response.text)
            except json.JSONDecodeError:
                return response.text
            if "reader_result" in response_data:
                return response_data["reader_result"]
            if "content" in response_data:
                return response_data["content"]
            return response.text
        except Exception as e:  # noqa: BLE001
            return f"Error: fetch API failed: {e}"

    @staticmethod
    def _clean_search_results(raw_results: list) -> list[dict]:
        seen_urls: set[str] = set()
        cleaned: list[dict] = []
        for content_list in raw_results:
            results = content_list.get("content", {}).get("results", {})
            for result in results.get("organic", []):
                url = result.get("url")
                if isinstance(url, str) and url and url not in seen_urls:
                    seen_urls.add(url)
                    clean = {"type": "page"}
                    if "title" in result:
                        clean["title"] = result["title"]
                    if "desc" in result:
                        clean["snippet"] = result["desc"]
                    clean["url"] = url
                    cleaned.append(clean)
            top_stories = results.get("top_stories") or {}
            for obj in top_stories.get("items", []):
                url = obj.get("url")
                title = obj.get("title")
                if title and isinstance(url, str) and url and url not in seen_urls:
                    seen_urls.add(url)
                    cleaned.append({"type": "news", "title": title, "url": url, **({"time_frame": obj["time_frame"]} if "time_frame" in obj else {}), **({"source": obj["source"]} if "source" in obj else {})})
        return cleaned

    @staticmethod
    def _clean_image_results(raw_results: list) -> list[dict]:
        seen_urls: set[str] = set()
        cleaned: list[dict] = []
        for content_list in raw_results:
            for result in content_list.get("content", {}).get("results", {}).get("images_results", []):
                original = result.get("original")
                if isinstance(original, str) and original and original not in seen_urls:
                    seen_urls.add(original)
                    clean: dict[str, Any] = {"image_url": original}
                    if "title" in result:
                        clean["title"] = result["title"]
                    cleaned.append(clean)
        return cleaned

    def web_search(self, query: str, site: str = "") -> str:
        if not self.api_key_set:
            return "Error: INFOQUEST_API_KEY is not configured"
        params: dict[str, Any] = {"format": "JSON", "query": query}
        if self.search_time_range > 0:
            params["time_range"] = self.search_time_range
        if site:
            params["site"] = site
        try:
            requests = _requests_module()
            response = requests.post(_SEARCH_ENDPOINT, headers=self._prepare_headers(), json=params)
            response.raise_for_status()
            raw = response.json()
        except Exception as e:  # noqa: BLE001
            return f"Error: {e}"
        if "search_result" in raw:
            cleaned = self._clean_search_results(raw["search_result"].get("results", []))
            return json.dumps(cleaned, indent=2, ensure_ascii=False)
        return json.dumps(raw, indent=2, ensure_ascii=False)

    def image_search(self, query: str, site: str = "") -> str:
        if not self.api_key_set:
            return "Error: INFOQUEST_API_KEY is not configured"
        params: dict[str, Any] = {"format": "JSON", "query": query, "search_type": "Images"}
        if 1 <= self.image_search_time_range <= 365:
            params["time_range"] = self.image_search_time_range
        if site:
            params["site"] = site
        if self.image_size in {"l", "m", "i"}:
            params["image_size"] = self.image_size
        try:
            requests = _requests_module()
            response = requests.post(_SEARCH_ENDPOINT, headers=self._prepare_headers(), json=params)
            response.raise_for_status()
            raw = response.json()
        except Exception as e:  # noqa: BLE001
            return f"Error: {e}"
        if "search_result" in raw:
            cleaned = self._clean_image_results(raw["search_result"].get("results", []))
            return json.dumps(cleaned, indent=2, ensure_ascii=False)
        return json.dumps(raw, indent=2, ensure_ascii=False)
