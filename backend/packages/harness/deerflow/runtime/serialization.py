"""LangChain / LangGraph 对象的规范化序列化。

提供把 LangChain 消息对象、pydantic 模型、LangGraph 状态 dict 转成纯 JSON 可序列化
Python 结构的**单一真相源**。

消费方（未来）：``runtime.runs.worker``（SSE 发布）、消息/事件 REST 端点。

为什么需要单独一层：
- LangChain 消息是 pydantic 对象，不能直接 ``json.dumps``；不同调用方各自 dump
  会产生格式漂移。
- LangGraph 的 channel values 里有 ``__pregel_*`` / ``__interrupt__`` 等内部键，
  不能泄漏给前端（对齐 LangGraph Platform API 的返回）。
- ``ViewImageMiddleware`` 会把完整 base64 图片塞进 ``hide_from_ui`` 的 human 消息，
  历史/回放端点绝不能把这些 base64 发给前端（响应体巨大、无 UI 价值）。
"""

from __future__ import annotations

from typing import Any


def serialize_lc_object(obj: Any) -> Any:
    """递归把一个 LangChain 对象序列化成 JSON 可序列化的 dict / list / 标量。"""
    if obj is None:
        return None
    if isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {k: serialize_lc_object(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [serialize_lc_object(item) for item in obj]
    # pydantic v2
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump()
        except Exception:
            pass
    # pydantic v1 / 旧对象
    if hasattr(obj, "dict"):
        try:
            return obj.dict()
        except Exception:
            pass
    # LangGraph 的 Interrupt 是 __slots__ 类——没有 model_dump/dict/__dict__，
    # 否则会落到下面的 str() 兜底，产出一个畸形 payload。这里把它规范化成
    # {value, id}（对齐 LangGraph Platform API）。
    try:
        from langgraph.types import Interrupt
    except ImportError:
        pass
    else:
        if isinstance(obj, Interrupt):
            return serialize_lc_object({"value": obj.value, "id": getattr(obj, "id", None)})
    # 最后兜底
    try:
        return str(obj)
    except Exception:
        return repr(obj)


def serialize_channel_values(channel_values: dict[str, Any]) -> dict[str, Any]:
    """序列化 channel values，剥掉 LangGraph 的内部键。

    只移除 ``__pregel_*`` 键——``__interrupt__`` **故意保留**，让 LangGraph SDK
    能从 values chunk 里识别中断事件（见 issue #3595）；其值（Interrupt 对象列表）
    由 :func:`serialize_lc_object` 里的 Interrupt 分支规范化成 ``{value, id}``。
    """
    result: dict[str, Any] = {}
    for key, value in channel_values.items():
        if key.startswith("__pregel_"):
            continue
        result[key] = serialize_lc_object(value)
    return result


def strip_data_url_image_blocks(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从 *hide_from_ui* 消息里移除 ``data:`` scheme 的 ``image_url`` 块。

    历史与 run-wait 端点会把 checkpointer 持久化的消息返回给前端。
    ``ViewImageMiddleware`` 在 ``hide_from_ui`` 的 human 消息里存了完整 base64 图片
    payload——这些是内部模型上下文，**不能**发给前端（响应体巨大、无 UI 价值）。

    只剥 ``type == "image_url"`` 且 URL 以 ``data:`` 开头的 content 块。text 块、
    ``https://`` 图片 URL、非 hide_from_ui 的消息都原样保留——保证消息顺序与数量不变。
    """
    result: list[dict[str, Any]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            result.append(msg)
            continue

        # 只动显式标记为 hide_from_ui 的消息。
        additional_kwargs = msg.get("additional_kwargs")
        if not (isinstance(additional_kwargs, dict) and additional_kwargs.get("hide_from_ui") is True):
            result.append(msg)
            continue

        content = msg.get("content")
        if not isinstance(content, list):
            result.append(msg)
            continue

        # 过滤掉 data: scheme 的 image_url 块。
        filtered = [block for block in content if not (isinstance(block, dict) and block.get("type") == "image_url" and isinstance(block.get("image_url"), dict) and str(block["image_url"].get("url", "")).startswith("data:"))]
        result.append({**msg, "content": filtered})
    return result


def serialize_channel_values_for_api(channel_values: dict[str, Any]) -> dict[str, Any]:
    """序列化 channel values，并从消息里剥掉 base64 图片数据。

    组合 :func:`serialize_channel_values` 与 :func:`strip_data_url_image_blocks` 的便利
    封装。所有返回 channel values 给前端的 REST 端点都应用它，确保 ``data:`` scheme
    的 base64 图片 payload 永不发到线上。
    """
    result = serialize_channel_values(channel_values)
    if isinstance(result.get("messages"), list):
        result["messages"] = strip_data_url_image_blocks(result["messages"])
    return result


def serialize_messages_tuple(obj: Any) -> Any:
    """序列化 messages-mode 的 tuple ``(chunk, metadata)``。"""
    if isinstance(obj, tuple) and len(obj) == 2:
        chunk, metadata = obj
        return [serialize_lc_object(chunk), metadata if isinstance(metadata, dict) else {}]
    return serialize_lc_object(obj)


def serialize(obj: Any, *, mode: str = "") -> Any:
    """按 mode 序列化 LangChain 对象。

    * ``messages`` — obj 是 ``(message_chunk, metadata_dict)``。
    * ``values`` — obj 是完整 state dict；剥 ``__pregel_*`` 键。
    * 其它 — 递归 ``model_dump()`` / ``dict()`` 兜底。
    """
    if mode == "messages":
        return serialize_messages_tuple(obj)
    if mode == "values":
        # ``values`` 快照把完整 state 流给前端，所以必须像 REST 端点一样剥掉
        # hide_from_ui 消息里的 base64 图片 payload。
        return serialize_channel_values_for_api(obj) if isinstance(obj, dict) else serialize_lc_object(obj)
    return serialize_lc_object(obj)
