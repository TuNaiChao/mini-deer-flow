"""ISO 8601 时间戳工具。

DeerFlow 把线程 / 运行的时间戳统一存成 ISO 8601 UTC 字符串，以对齐 LangGraph
Platform 的 schema（``langgraph_sdk.schema.Thread`` 的 ``created_at`` /
``updated_at`` 是 ``datetime``，JSON 序列化为 ISO 8601）。所有时间戳生成都应
走 :func:`now_iso`，保证各端点、嵌入式 RunManager、checkpointer 写入的元数据
格式一致。

:func:`coerce_iso` 为向前兼容提供读路径：历史记录可能存的是
``str(time.time())`` 这种 unix 秒浮点字符串，这里把它归一成 ISO，无需一次性迁移。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

__all__ = ["coerce_iso", "now_iso"]

_UNIX_TIMESTAMP_PATTERN = re.compile(r"^\d{10}(?:\.\d+)?$")
"""匹配历史 ``str(time.time())`` 写入的 unix 时间戳字符串形态
（10 位秒 + 可选小数部分）。用 10 位锚点避免误把 ``"2026"`` 这种年份重写，
且在 2286 年之前都有效。"""


def now_iso() -> str:
    """返回当前 UTC 时间的 ISO 8601 字符串。

    例：``"2026-04-27T03:19:46.511479+00:00"``。
    """
    return datetime.now(UTC).isoformat()


def coerce_iso(value: object) -> str:
    """尽力把存储的时间戳归一成 ISO 8601 字符串。

    把旧版 DeerFlow 写入的 unix 时间戳（浮点 / 字符串）翻译成 ISO，无需一次性迁移；
    ISO 字符串原样返回；``datetime`` 实例归一到 UTC（无时区视为 UTC）后用
    ``isoformat()`` 输出，保证线格式始终用 ``T`` 分隔符；空值变 ``""``；
    无法识别的值最后兜底成 ``str(value)``。
    """
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        # bool 是 int 的子类——当成垃圾值处理，而不是 0/1。
        return str(value)
    if isinstance(value, datetime):
        # datetime 必须在 int/float 判断之前处理；str(datetime) 会产生
        # "YYYY-MM-DD HH:MM:SS+00:00"（空格分隔），破坏严格的 ISO 8601 消费者。
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        else:
            value = value.astimezone(UTC)
        return value.isoformat()
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), UTC).isoformat()
        except (ValueError, OverflowError, OSError):
            return str(value)
    if isinstance(value, str):
        if _UNIX_TIMESTAMP_PATTERN.match(value):
            try:
                return datetime.fromtimestamp(float(value), UTC).isoformat()
            except (ValueError, OverflowError, OSError):
                return value
        return value
    return str(value)
