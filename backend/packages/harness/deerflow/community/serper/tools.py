"""Serper 网页搜索 + 图片搜索 provider（Google Search API，需 API key）。

对齐 deer ``community/serper/tools.py``：调 Serper（https://serper.dev）的 JSON API，
拿实时 Google 搜索 + Google Images 结果。``SERPER_API_KEY`` 来自环境变量或 config.yaml。

**#3575**：``image_search`` 工具查 Google Images，返回的图片 URL 经 ``_safe_public_url``
过一遍 SSRF 守卫（拒非 http(s) scheme、localhost、私有/非全局 IP——含十进制/十六进制/八进制
混淆字面量）。``web_search`` 的结果链接是给模型读的引用、不由本工具下载，故原样返回不走守卫。

config 访问走 mini ``_common.get_tool_extras``。
"""

from __future__ import annotations

import json
import logging
import os
from ipaddress import IPv4Address, ip_address
from urllib.parse import urlparse

import httpx
from langchain.tools import tool

from deerflow.community._common import get_tool_extras

logger = logging.getLogger(__name__)

_SERPER_SEARCH_ENDPOINT = "https://google.serper.dev/search"
_SERPER_IMAGES_ENDPOINT = "https://google.serper.dev/images"
_SERPER_MAX_RESULTS = 10
_api_key_warned: set[str] = set()


def _get_api_key(tool_name: str) -> str | None:
    api_key = get_tool_extras(tool_name).get("api_key")
    if isinstance(api_key, str) and api_key.strip():
        return api_key.strip()
    env_key = os.getenv("SERPER_API_KEY")
    if isinstance(env_key, str) and env_key.strip():
        return env_key.strip()
    return None


def _coerce_max_results(value: object, default: int = 5, max_allowed: int = _SERPER_MAX_RESULTS) -> int:
    """把 config / 参数强转成有界的正结果数。"""
    try:
        count = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    if count <= 0:
        return default
    return min(count, max_allowed)


def _missing_key_error(query: str, tool_name: str) -> str:
    if tool_name not in _api_key_warned:
        _api_key_warned.add(tool_name)
        logger.warning(
            "Serper API key is not set for '%s'. Set SERPER_API_KEY in your environment or provide api_key in config.yaml. Sign up at https://serper.dev",
            tool_name,
        )
    return json.dumps(
        {"error": "SERPER_API_KEY is not configured", "query": query},
        ensure_ascii=False,
    )


def _unexpected_format_error(query: str) -> str:
    return json.dumps(
        {"error": "Serper returned an unexpected response format", "query": query},
        ensure_ascii=False,
    )


def _response_items(data: dict, field: str, query: str) -> tuple[list[dict] | None, str | None]:
    items = data.get(field)
    # 把缺失 / null 字段当「无结果」（有些 API 返 ``{"organic": null}`` 表态），不当畸形 payload。
    if items is None:
        return [], None
    if not isinstance(items, list):
        logger.error("Serper returned unexpected '%s' payload type: %s", field, type(items).__name__)
        return None, _unexpected_format_error(query)
    return [item for item in items if isinstance(item, dict)], None


def _clean_query(query: str) -> str:
    """把原始 query 归一成实际发给 Serper 的值。"""
    query = query.strip()
    if len(query) > 500:
        query = query[:500]
    return query


def _decode_ipv4(host: str) -> IPv4Address | None:
    """解码 ``ip_address`` 拒绝的混淆 IPv4 字面量。

    镜像很多 HTTP client 用的宽容 ``inet_aton`` 解析，让整数（``2130706433``）、十六进制
    （``0x7f000001``）、八进制（``0177.0.0.1``）编码的地址也被识别。host 解码成 IPv4 时返回
    ``IPv4Address``，否则返回 ``None``（如 ``cafe.com`` 这种真域名解不了，留给调用方当 host 处理）。
    """
    parts = host.split(".")
    if not 1 <= len(parts) <= 4:
        return None

    values: list[int] = []
    for part in parts:
        if not part:
            return None
        try:
            if part.startswith(("0x", "0X")):
                values.append(int(part, 16))
            elif part.startswith("0") and len(part) > 1:
                values.append(int(part, 8))
            else:
                values.append(int(part, 10))
        except ValueError:
            return None

    *leading, last = values
    for value in leading:
        if not 0 <= value <= 0xFF:
            return None
    max_last = (1 << (8 * (4 - len(leading)))) - 1
    if not 0 <= last <= max_last:
        return None

    result = 0
    for value in leading:
        result = (result << 8) | value
    result = (result << (8 * (4 - len(leading)))) | last
    return ip_address(result)


def _is_url_present(value: object) -> bool:
    """``value`` 是非空 URL 字符串时返回 ``True``。

    用来区分一个字段是「缺失」（可跨字段回退）还是「**存在但被 SSRF 守卫滤掉**」
    （必须留空，不能塌缩到另一半）。
    """
    return isinstance(value, str) and bool(value.strip())


def _safe_public_url(value: object) -> str:
    """仅当 ``value`` 是安全的、公网 http(s) URL 时返回它，否则返回 ""。

    best-effort SSRF 守卫：拒绝非 http(s) scheme、``localhost``、私有/非全局 IP 字面量
    （含十进制/十六进制/八进制混淆编码）。只检视 URL 字符串，抓不住「公网域名解析到内网 IP」
    （如 DNS rebinding）；真正下载这些 URL 的消费方必须在 fetch 时对解析后的 IP 再验一次。
    """
    if not isinstance(value, str):
        return ""
    url = value.strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or not parsed.hostname:
        return ""

    # 去掉单个结尾点（FQDN 根标签）。``localhost.`` 和 ``127.0.0.1.`` 在常见 resolver 上解析到
    # loopback，否则会溜过下面的 localhost/IP 检查。
    host = parsed.hostname.lower().rstrip(".")
    if not host:
        return ""
    if host == "localhost" or host.endswith(".localhost"):
        return ""

    try:
        ip = ip_address(host)
    except ValueError:
        ip = _decode_ipv4(host)
        if ip is None:
            return url
    return url if ip.is_global else ""


def _serper_post(endpoint: str, api_key: str, query: str, max_results: int) -> tuple[dict | None, str | None]:
    """发一个 POST 到 Serper 端点。

    ``query`` 应已用 :func:`_clean_query` 归一。

    返回 ``(data, error_json)``：成功时 ``data`` 是解析后的 JSON、``error_json`` 为 None；
    失败时 ``data`` 为 None、``error_json`` 是序列化好的结构化错误（可直接 return）。
    """
    headers = {"X-API-KEY": api_key, "Content-Type": "application/json"}
    payload = {"q": query, "num": max_results}

    try:
        with httpx.Client(timeout=30) as client:
            response = client.post(endpoint, headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            logger.error("Serper returned an unexpected payload type: %s", type(data).__name__)
            return None, _unexpected_format_error(query)
        return data, None
    except httpx.HTTPStatusError as e:
        resp_text = (e.response.text or "")[:500]
        logger.error("Serper API returned HTTP %s: %s", e.response.status_code, resp_text)
        return None, json.dumps(
            {"error": f"Serper API error: HTTP {e.response.status_code}", "query": query},
            ensure_ascii=False,
        )
    except Exception as e:  # noqa: BLE001
        logger.error("Serper request failed: %s: %s", type(e).__name__, str(e)[:500])
        return None, json.dumps({"error": str(e)[:500], "query": query}, ensure_ascii=False)


@tool("web_search", parse_docstring=True)
def web_search_tool(query: str, max_results: int = 5) -> str:
    """Search the web for information using Google Search via Serper.

    Args:
        query: Search keywords describing what you want to find. Be specific for better results.
        max_results: Maximum number of search results to return. Default is 5, capped at 10.
    """
    extras = get_tool_extras("web_search")
    if "max_results" in extras:
        max_results = extras.get("max_results", max_results)
    max_results = _coerce_max_results(max_results)
    query = _clean_query(query)

    api_key = _get_api_key("web_search")
    if not api_key:
        return _missing_key_error(query, "web_search")

    data, error_json = _serper_post(_SERPER_SEARCH_ENDPOINT, api_key, query, max_results)
    if error_json is not None:
        return error_json

    organic, error_json = _response_items(data, "organic", query)
    if error_json is not None:
        return error_json
    if not organic:
        return json.dumps({"error": "No results found", "query": query}, ensure_ascii=False)

    # 搜索结果链接原样返回（不走 _safe_public_url）：它们是给模型读的引用，不由本工具下载，
    # 与 image_search 的图片 URL 不同。
    normalized_results = [
        {
            "title": r.get("title", ""),
            "url": r.get("link", ""),
            "content": r.get("snippet", ""),
        }
        for r in organic[:max_results]
    ]

    output = {
        "query": query,
        "total_results": len(normalized_results),
        "results": normalized_results,
    }
    return json.dumps(output, indent=2, ensure_ascii=False)


@tool("image_search", parse_docstring=True)
def image_search_tool(query: str, max_results: int = 5) -> str:
    """Search for images online using Google Images via Serper. Use this tool BEFORE image generation to find reference images for characters, portraits, objects, scenes, or any content requiring visual accuracy.

    The returned image URLs can be used as reference images in image generation to significantly improve quality.

    Args:
        query: Search keywords describing the images you want to find. Be specific for better results (e.g., "Japanese woman street photography 1990s" instead of just "woman").
        max_results: Maximum number of images to return. Default is 5, capped at 10.
    """
    extras = get_tool_extras("image_search")
    if "max_results" in extras:
        max_results = extras.get("max_results", max_results)
    max_results = _coerce_max_results(max_results)
    query = _clean_query(query)

    api_key = _get_api_key("image_search")
    if not api_key:
        return _missing_key_error(query, "image_search")

    data, error_json = _serper_post(_SERPER_IMAGES_ENDPOINT, api_key, query, max_results)
    if error_json is not None:
        return error_json

    images, error_json = _response_items(data, "images", query)
    if error_json is not None:
        return error_json
    if not images:
        return json.dumps({"error": "No images found", "query": query}, ensure_ascii=False)

    normalized_results = []
    for r in images:
        raw_image = r.get("imageUrl")
        raw_thumb = r.get("thumbnailUrl")
        # （非平凡）SSRF 守卫每个字段只算一次，不重算。
        safe_image = _safe_public_url(raw_image)
        safe_thumb = _safe_public_url(raw_thumb)
        # 只在另一半**缺失**时才跨字段回退。存在但被 SSRF 滤掉的字段留空，不塌缩到另一半——
        # 免得被丢的高清 URL 静默冒充预览图（反之亦然），保住调用方依赖的高清/预览契约。
        image_url = safe_image or (safe_thumb if not _is_url_present(raw_image) else "")
        thumbnail_url = safe_thumb or (safe_image if not _is_url_present(raw_thumb) else "")
        if not image_url and not thumbnail_url:
            continue
        normalized_results.append(
            {
                "title": r.get("title", ""),
                "image_url": image_url,
                "thumbnail_url": thumbnail_url,
            }
        )
        if len(normalized_results) >= max_results:
            break

    if not normalized_results:
        return json.dumps({"error": "No safe image URLs found", "query": query}, ensure_ascii=False)

    output = {
        "query": query,
        "total_results": len(normalized_results),
        "results": normalized_results,
        "usage_hint": "Use the 'image_url' values as reference images in image generation. Download them first if needed.",
    }
    return json.dumps(output, indent=2, ensure_ascii=False)
