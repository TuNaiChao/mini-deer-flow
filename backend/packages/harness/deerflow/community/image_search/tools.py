"""DuckDuckGo 图片搜索工具（无需 API key）。

对齐 deer ``community/image_search/tools.py``：用 ``ddgs`` 库搜图，支持
size/color/type/layout/license 过滤。返回 ``thumbnail_url`` 供 image generation 当参考图。
"""

from __future__ import annotations

import json
import logging

from langchain.tools import tool

from deerflow.community._common import get_tool_extras

logger = logging.getLogger(__name__)


def _search_images(
    query: str,
    max_results: int = 5,
    region: str = "wt-wt",
    safesearch: str = "moderate",
    size: str | None = None,
    color: str | None = None,
    type_image: str | None = None,
    layout: str | None = None,
    license_image: str | None = None,
) -> list[dict]:
    """用 DuckDuckGo 搜图。缺 ddgs 库 → 记可操作提示 + 返回 []。"""
    try:
        from ddgs import DDGS
    except ImportError:
        logger.error("ddgs library not installed. Run: pip install ddgs")
        return []

    ddgs = DDGS(timeout=30)
    try:
        kwargs: dict = {"region": region, "safesearch": safesearch, "max_results": max_results}
        if size:
            kwargs["size"] = size
        if color:
            kwargs["color"] = color
        if type_image:
            kwargs["type_image"] = type_image
        if layout:
            kwargs["layout"] = layout
        if license_image:
            kwargs["license_image"] = license_image

        results = ddgs.images(query, **kwargs)
        return list(results) if results else []
    except Exception as e:  # noqa: BLE001
        logger.error("Failed to search images: %s", e)
        return []


@tool("image_search", parse_docstring=True)
def image_search_tool(
    query: str,
    max_results: int = 5,
    size: str | None = None,
    type_image: str | None = None,
    layout: str | None = None,
) -> str:
    """Search for images online. Use this tool BEFORE image generation to find reference images for characters, portraits, objects, scenes, or any content requiring visual accuracy.

    **When to use:**
    - Before generating character/portrait images: search for similar poses, expressions, styles
    - Before generating specific objects/products: search for accurate visual references
    - Before generating scenes/locations: search for architectural or environmental references
    - Before generating fashion/clothing: search for style and detail references

    The returned image URLs can be used as reference images in image generation to significantly improve quality.

    Args:
        query: Search keywords describing the images you want to find. Be specific for better results (e.g., "Japanese woman street photography 1990s" instead of just "woman").
        max_results: Maximum number of images to return. Default is 5.
        size: Image size filter. Options: "Small", "Medium", "Large", "Wallpaper". Use "Large" for reference images.
        type_image: Image type filter. Options: "photo", "clipart", "gif", "transparent", "line". Use "photo" for realistic references.
        layout: Layout filter. Options: "Square", "Tall", "Wide". Choose based on your generation needs.
    """
    max_results = get_tool_extras("image_search").get("max_results", max_results)

    results = _search_images(
        query=query,
        max_results=max_results,
        size=size,
        type_image=type_image,
        layout=layout,
    )

    if not results:
        return json.dumps({"error": "No images found", "query": query}, ensure_ascii=False)

    normalized_results = [
        {
            "title": r.get("title", ""),
            "image_url": r.get("thumbnail", ""),
            "thumbnail_url": r.get("thumbnail", ""),
        }
        for r in results
    ]

    output = {
        "query": query,
        "total_results": len(normalized_results),
        "results": normalized_results,
        "usage_hint": "Use the 'image_url' values as reference images in image generation. Download them first if needed.",
    }
    return json.dumps(output, indent=2, ensure_ascii=False)
