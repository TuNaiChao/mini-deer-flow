"""community provider 的共享层。

把 deer 散落在各 provider 里重复的几件事抽到这里（outline M21 明确要求建 ``_common``）：

1. **结果归一化** —— 所有搜索 provider 统一吐 ``{"title","url","snippet"}``；
2. **4KB 截断** —— 所有抓取 provider 统一截到 ``MAX_FETCH_CHARS``（防 prompt 爆炸）；
3. **async httpx 封装** —— ``post_json`` 把 JinaClient 那套「timeout/proxy/trust_env +
   异常→``"Error: ..."`` 前缀字符串」的套路抽出来，供 jina_ai（及任何想异步抓的 provider）复用；
4. **通用参数强转** —— ``coerce_bool`` / ``coerce_int`` / ``coerce_timeout`` / ``coerce_proxy``
   把 config.yaml 里可能是字符串的参数（``timeout: "10"``、``trust_env: "yes"``）安全转成期望类型；
5. **配置访问** —— ``get_tool_extras(name)`` 读 ``AppConfig.get_tool_config(name)`` 返回的 dict，
   None 安全归一成空 dict（调用方直接 ``.get(key, default)``）。

这些函数**都是纯 / 弱依赖**：``httpx`` 是核心依赖但仍按需 import（``post_json`` 内部 import），
缺包不影响其它 helper。provider 各自的 SDK（``ddgs`` / ``tavily`` / ``firecrawl`` …）由各 provider
自己 try/except 软加载，不在这里集中管理（红线 #24：每个 SDK 独立软加载 + 可操作安装提示）。
"""

from __future__ import annotations

import logging
from typing import Any

from deerflow.config import get_app_config

logger = logging.getLogger(__name__)

#: 抓取工具统一的内容截断上限（字符）。deer 各 fetch 工具都用 4096；这里单源定义。
MAX_FETCH_CHARS = 4096


# ---------------------------------------------------------------------------
# 结果归一化 + 截断
# ---------------------------------------------------------------------------


def _as_str(value: Any) -> str:
    """把任意值安全转成字符串（None / 非字符串 → 空串或字符串化）。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def normalize_search_result(
    title: Any,
    url: Any,
    snippet: Any,
    *,
    content_key: str = "content",
) -> dict[str, str]:
    """把一条原始搜索结果归一成统一 dict。

    deer 的搜索 provider 字段名五花八门（``href``/``link``/``url``、``body``/``snippet``/
    ``content``/``description``）；归一后所有 provider 都吐 ``{"title","url","snippet"}``，
    方便 agent 解析 + 跨 provider 切换。``content_key`` 控制归一后的「正文」字段名：
    ddg/brave/serper 习惯叫 ``content``，tavily/exa 习惯叫 ``snippet``——保留调用方选择权，
    默认 ``content``（与 ddg/brave/serper 一致，是搜索结果给 agent 看时最常用的名）。

    None / 缺字段 → 空串，不会 KeyError。
    """
    return {content_key: _as_str(snippet), "title": _as_str(title), "url": _as_str(url)}


def truncate_content(text: Any, limit: int = MAX_FETCH_CHARS) -> str:
    """把抓取到的内容截到 ``limit`` 字符（防爆 prompt）。None 安全。"""
    if not text:
        return ""
    s = text if isinstance(text, str) else str(text)
    return s[:limit]


# ---------------------------------------------------------------------------
# 通用参数强转（config.yaml 值可能是字符串）
# ---------------------------------------------------------------------------


def coerce_bool(value: Any, default: bool) -> bool:
    """把 ``"yes"``/``"true"``/``"1"``/``"on"`` 等字符串安全转 bool。

    非法 / None → ``default``。``bool`` 原样返回（注意先排除 bool 再判 int，避免 True 被当 int）。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


def coerce_int(value: Any, default: int) -> int:
    """把字符串 / float 安全转 int；非法 / None → default（bool 不当 int）。"""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except (ValueError, TypeError):
            return default
    return default


def coerce_timeout(value: Any, default: int) -> int:
    """``coerce_int`` 的语义别名——专门给超时字段用（配置里常写成 ``timeout: "10"``）。"""
    return coerce_int(value, default)


def coerce_proxy(value: Any) -> str | None:
    """代理 URL 强转：非字符串 / 空白 → None（表示「不走代理」）。"""
    if not isinstance(value, str):
        return None
    proxy = value.strip()
    return proxy or None


# ---------------------------------------------------------------------------
# 配置访问
# ---------------------------------------------------------------------------


def get_tool_extras(name: str) -> dict[str, Any]:
    """读 ``config.yaml`` 的 ``tools[].name == name`` 条目，返回其 dict（含额外字段）。

    没配该工具 → 返回空 dict（不是 None），调用方直接 ``extras.get("api_key", default)``
    不用先判空。deer 等价：``config.model_extra``。
    """
    config = get_app_config().get_tool_config(name)
    if config is None:
        return {}
    return config


# ---------------------------------------------------------------------------
# async httpx 封装
# ---------------------------------------------------------------------------


async def post_json(
    url: str,
    *,
    headers: dict[str, str],
    json_body: dict[str, Any],
    timeout: int = 10,
    proxy: str | None = None,
    trust_env: bool = True,
) -> str:
    """异步 POST JSON 并返回响应文本（失败返回 ``"Error: ..."`` 字符串）。

    抽自 JinaClient 的套路：``trust_env`` 控制是否读 ``HTTP_PROXY`` 等环境变量代理；
    ``proxy`` 显式代理优先；任何异常（含非 200、空响应、网络错误）都归一成
    ``"Error: <可读消息>"`` 前缀字符串——调用方据此判断成败（与 deer JinaClient 一致）。

    ``httpx`` 按需 import：缺包时返回 ``"Error: ..."`` 而不是 import 崩（红线 #24）。
    """
    try:
        import httpx
    except ImportError:
        msg = "httpx is not installed. Run: pip install httpx"
        logger.error(msg)
        return f"Error: {msg}"

    try:
        client_kwargs: dict[str, Any] = {"trust_env": trust_env}
        if proxy:
            client_kwargs["proxy"] = proxy
        async with httpx.AsyncClient(**client_kwargs) as client:
            response = await client.post(url, headers=headers, json=json_body, timeout=timeout)

        if response.status_code != 200:
            error_message = f"Request to {url} returned status {response.status_code}: {response.text}"
            logger.error(error_message)
            return f"Error: {error_message}"

        if not response.text or not response.text.strip():
            error_message = f"Request to {url} returned empty response"
            logger.error(error_message)
            return f"Error: {error_message}"

        return response.text
    except Exception as e:  # noqa: BLE001 — 归一成 Error 字符串，与 deer 一致
        error_message = f"Request to {url} failed: {type(e).__name__}: {e}"
        logger.warning(error_message)
        return f"Error: {error_message}"
