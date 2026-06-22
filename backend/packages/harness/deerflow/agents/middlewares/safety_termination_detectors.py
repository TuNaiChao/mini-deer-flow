"""provider 侧安全终止信号的检测器（M16）。

不同 LLM provider 用不同字段 / 不同值表达「我因安全原因停了这次响应」：
  - OpenAI 系（含 Azure / Moonshot / DeepSeek / vLLM / Qwen 兼容）：``finish_reason='content_filter'``
  - Anthropic：``stop_reason='refusal'``
  - Gemini / Vertex：``finish_reason='SAFETY' / 'BLOCKLIST' / 'RECITATION' ...``

本模块定义策略接口 :class:`SafetyTerminationDetector` + 三个内置检测器。新 provider 实现
该接口、经 ``config.yaml: safety_finish_reason.detectors`` 接入即可。

消费这些检测器的中间件在 :mod:`safety_finish_reason_middleware`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from langchain_core.messages import AIMessage


@dataclass(frozen=True)
class SafetyTermination:
    """检测到的安全终止信号。

    Attributes:
        detector: 产出该结果的检测器名（观测用）。
        reason_field: 承载信号的元数据字段（如 ``finish_reason`` / ``stop_reason``）。
        reason_value: 该字段的实际值（如 ``content_filter`` / ``refusal`` / ``SAFETY``）。
        extras: provider 特定元数据（如 Azure content_filter_results、Gemini safety_ratings）。
    """

    detector: str
    reason_field: str
    reason_value: str
    extras: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class SafetyTerminationDetector(Protocol):
    """provider 安全终止检测的策略接口。"""

    name: str

    def detect(self, message: AIMessage) -> SafetyTermination | None:
        """message 表示 provider 安全终止 → 返回 :class:`SafetyTermination`，否则 None。

        实现须无副作用、容忍缺失 / 类型异常的元数据——检测器跑在每个模型响应上。
        """
        ...


def _get_metadata_value(message: AIMessage, field_name: str) -> str | None:
    """从 response_metadata 或 additional_kwargs 读字符串值。

    LangChain provider adapter 把停止信号塞在哪不一致。现代用 response_metadata，老 /
    passthrough 路径仍用 additional_kwargs。两者都查，只接受字符串值（Pydantic enum /
    dict 忽略，防畸形输入抛异常）。
    """
    for container_name in ("response_metadata", "additional_kwargs"):
        container = getattr(message, container_name, None) or {}
        if not isinstance(container, dict):
            continue
        value = container.get(field_name)
        if isinstance(value, str) and value:
            return value
    return None


class OpenAICompatibleContentFilterDetector:
    """OpenAI 系 content_filter 信号。

    覆盖 OpenAI / Azure / Moonshot/Kimi / DeepSeek / Mistral / vLLM / Qwen(兼容模式) 等遵循
    OpenAI ``finish_reason`` 约定的 adapter。部分国内 provider 自定义网关用 ``sensitive`` /
    ``violation`` 等替代 token，可经 ``finish_reasons`` kwarg 扩展。
    """

    name = "openai_compatible_content_filter"

    def __init__(self, finish_reasons: list[str] | tuple[str, ...] | None = None) -> None:
        configured = finish_reasons if finish_reasons is not None else ("content_filter",)
        self._finish_reasons: frozenset[str] = frozenset(r.lower() for r in configured)

    def detect(self, message: AIMessage) -> SafetyTermination | None:
        value = _get_metadata_value(message, "finish_reason")
        if value is None or value.lower() not in self._finish_reasons:
            return None

        extras: dict[str, Any] = {}
        # Azure 带结构化 content_filter_results；带出来让运维看清滤了什么。
        response_metadata = getattr(message, "response_metadata", None) or {}
        if isinstance(response_metadata, dict):
            filter_results = response_metadata.get("content_filter_results")
            if filter_results:
                extras["content_filter_results"] = filter_results

        return SafetyTermination(detector=self.name, reason_field="finish_reason", reason_value=value, extras=extras)


class AnthropicRefusalDetector:
    """Anthropic ``stop_reason == "refusal"`` 信号。

    Anthropic 用专门的 ``stop_reason`` 而非 ``finish_reason`` 表达安全拒绝。
    """

    name = "anthropic_refusal"

    def __init__(self, stop_reasons: list[str] | tuple[str, ...] | None = None) -> None:
        configured = stop_reasons if stop_reasons is not None else ("refusal",)
        self._stop_reasons: frozenset[str] = frozenset(r.lower() for r in configured)

    def detect(self, message: AIMessage) -> SafetyTermination | None:
        value = _get_metadata_value(message, "stop_reason")
        if value is None or value.lower() not in self._stop_reasons:
            return None
        return SafetyTermination(detector=self.name, reason_field="stop_reason", reason_value=value)


class GeminiSafetyDetector:
    """Gemini / Vertex AI 安全相关 finish_reason。

    Gemini 用与 OpenAI 同名的 ``finish_reason`` 字段但值是大写枚举。默认集覆盖所有「模型因
    内容 / 图片触发安全、黑名单、引用、PII 滤镜而停」的情况——此时附带的 tool_calls 可能被
    截断 / 不可靠。

    **刻意排除**：``STOP``（正常）/ ``MAX_TOKENS``（长度截断非安全）/ ``LANGUAGE``·``NO_IMAGE``
    （能力不符）/ ``MALFORMED_FUNCTION_CALL``·``UNEXPECTED_TOOL_CALL``（工具协议错误，另类失败）/
    ``OTHER`` 系列（太宽泛）。
    """

    name = "gemini_safety"

    _DEFAULT_FINISH_REASONS = (
        "SAFETY",
        "BLOCKLIST",
        "PROHIBITED_CONTENT",
        "SPII",
        "RECITATION",
        "IMAGE_SAFETY",
        "IMAGE_PROHIBITED_CONTENT",
        "IMAGE_RECITATION",
    )

    def __init__(self, finish_reasons: list[str] | tuple[str, ...] | None = None) -> None:
        configured = finish_reasons if finish_reasons is not None else self._DEFAULT_FINISH_REASONS
        self._finish_reasons: frozenset[str] = frozenset(r.upper() for r in configured)

    def detect(self, message: AIMessage) -> SafetyTermination | None:
        value = _get_metadata_value(message, "finish_reason")
        if value is None or value.upper() not in self._finish_reasons:
            return None

        extras: dict[str, Any] = {}
        response_metadata = getattr(message, "response_metadata", None) or {}
        if isinstance(response_metadata, dict):
            ratings = response_metadata.get("safety_ratings")
            if ratings:
                extras["safety_ratings"] = ratings

        return SafetyTermination(detector=self.name, reason_field="finish_reason", reason_value=value, extras=extras)


def default_detectors() -> list[SafetyTerminationDetector]:
    """无自定义检测器时的内置集合。"""
    return [OpenAICompatibleContentFilterDetector(), AnthropicRefusalDetector(), GeminiSafetyDetector()]


__all__ = [
    "AnthropicRefusalDetector",
    "GeminiSafetyDetector",
    "OpenAICompatibleContentFilterDetector",
    "SafetyTermination",
    "SafetyTerminationDetector",
    "default_detectors",
]
