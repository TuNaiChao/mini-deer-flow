"""
模型工厂模块

通过 config.yaml 的 ``use`` 字段（如 ``langchain_deepseek:ChatDeepSeek``）
反射加载模型类并实例化。不硬编码任何 provider——完全配置驱动。

对齐 deer-flow 的能力：
- **thinking 模式**：``supports_thinking`` 门控 + ``when_thinking_enabled``/``thinking``
  合并 + 四种「关闭」路径（``when_thinking_disabled`` / OpenAI 兼容 / vLLM / Anthropic）。
- **OpenAI 兼容默认值**：``stream_usage=True``（防 token 用量丢失）、
  ``stream_chunk_timeout=240s``（防推理模型首 chunk 超时）。
- **reasoning_effort 门控**：``supports_reasoning_effort=False`` 时剔除该参数。
- **attach_tracing**：独立调用方在模型级挂追踪回调；图内调用方须传 ``False``
  （红线 #17：否则同一次 LLM 调用发重复 span，且 session_id/user_id 被剥离）。
- **缺包可操作安装提示**（由 ``reflection.resolve_class`` 提供）。

注意：Codex/MindIE 等 provider 专有分支（deer 有）本期不做。
"""
from __future__ import annotations

import logging
from typing import Any

from langchain_core.language_models import BaseChatModel

from ..config import AppConfig, get_app_config
from ..reflection import resolve_class

logger = logging.getLogger(__name__)

# OpenAI 兼容流式默认：相邻 chunk 间最长等待秒数。
# langchain-openai 自带默认是 60s，对推理模型（DeepSeek-R1、GPT-5 等）首 chunk
# 可合法达 90~150s，60s 会误判超时（StreamChunkTimeoutError）。这里放宽到 240s；
# 可在 config.yaml 单模型覆盖（stream_chunk_timeout 字段）。
_DEFAULT_STREAM_CHUNK_TIMEOUT_SECONDS: float = 240.0


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------


def _deep_merge_dicts(base: dict | None, override: dict) -> dict:
    """递归合并两个字典，不修改入参。"""
    merged = dict(base or {})
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_dicts(merged[key], value)
        else:
            merged[key] = value
    return merged


def _vllm_disable_chat_template_kwargs(chat_template_kwargs: dict) -> dict:
    """为 vLLM/Qwen 构造「关闭 thinking」的 chat_template_kwargs 负载。

    只有当原配置里出现了 thinking/enable_thinking 时才返回非空——
    否则不该向模型注入它不认识的键。
    """
    disable_kwargs: dict[str, bool] = {}
    if "thinking" in chat_template_kwargs:
        disable_kwargs["thinking"] = False
    if "enable_thinking" in chat_template_kwargs:
        disable_kwargs["enable_thinking"] = False
    return disable_kwargs


def _enable_stream_usage_by_default(model_use_path: str, model_settings_from_config: dict) -> None:
    """OpenAI 兼容网关默认开启 stream_usage。

    LangChain 只在「无自定义 base_url」时自动开 stream_usage。DeerFlow 常用
    OpenAI 兼容网关（doubao/deepseek 等），否则 TokenUsageMiddleware 拿不到用量。
    """
    if model_use_path != "langchain_openai:ChatOpenAI":
        return
    if "stream_usage" in model_settings_from_config:
        return
    if "base_url" in model_settings_from_config or "openai_api_base" in model_settings_from_config:
        model_settings_from_config["stream_usage"] = True


def _apply_stream_chunk_timeout_default(model_use_path: str, model_settings_from_config: dict) -> None:
    """为 ChatOpenAI 注入宽松的 stream_chunk_timeout；其它 provider 剔除该键。

    ``stream_chunk_timeout`` 是 ``langchain_openai:ChatOpenAI`` 专有参数，
    传给其它 provider 构造函数会触发 ``TypeError: unexpected keyword argument``。
    """
    if model_use_path != "langchain_openai:ChatOpenAI":
        model_settings_from_config.pop("stream_chunk_timeout", None)
        return
    if "stream_chunk_timeout" in model_settings_from_config:
        return
    model_settings_from_config["stream_chunk_timeout"] = _DEFAULT_STREAM_CHUNK_TIMEOUT_SECONDS


def _maybe_build_tracing_callbacks() -> list:
    """懒加载追踪回调。

    tracing 模块（M12）未落地时返回空列表——零副作用、零依赖，
    Phase 2 tracing 落地后自动生效，无需改本文件。
    """
    try:
        from deerflow.tracing import build_tracing_callbacks
    except ImportError:
        return []
    try:
        return list(build_tracing_callbacks() or [])
    except Exception:
        logger.warning("构建追踪回调失败，跳过模型级 tracing", exc_info=True)
        return []


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------


def create_chat_model(
    name: str | None = None,
    thinking_enabled: bool = False,
    *,
    app_config: AppConfig | None = None,
    attach_tracing: bool = True,
    **kwargs: Any,
) -> BaseChatModel:
    """根据配置创建聊天模型实例。

    Args:
        name: 模型配置名（对应 config.yaml 的 ``models[].name``）。
            ``None`` 时用配置中第一个模型。
        thinking_enabled: 开启扩展思考（仅当 ``supports_thinking=True`` 生效；
            不支持却开启会抛 ValueError，fail-fast 防静默错误）。
        app_config: 显式配置；``None`` 时用全局 ``get_app_config()``。
            单测与请求级注入应显式传入，避免依赖磁盘 config.yaml。
        attach_tracing: ``True``（默认）在模型级挂追踪回调——供**独立调用方**
            （如 MemoryUpdater、临时脚本）使用，使模型级回调仍能产出 trace。
            **图内调用方**（make_lead_agent、in-graph 的 TitleMiddleware 等）
            **必须传 ``False``**：图根已注入回调，再在模型级挂一次会发重复 span，
            且模型成为嵌套观测后 langfuse_* 元数据会被剥离（红线 #17）。
        **kwargs: 额外构造参数（如 ``reasoning_effort``）。
            注意：**config 派生设置优先级高于 kwargs**（对齐 deer）——
            即 config.yaml 的设置最终覆盖同名 kwargs（reasoning_effort 有专门门控）。

    Returns:
        BaseChatModel 实例。

    Raises:
        ValueError: 配置中无任何模型，或找不到 ``name``，或
            ``thinking_enabled=True`` 但模型 ``supports_thinking=False``。
    """
    config = app_config or get_app_config()
    model_config = config.get_model_config(name)
    if model_config is None:
        if not config.models:
            raise ValueError("未配置任何模型。请在 config.yaml 中添加 models 配置。") from None
        raise ValueError(f"在配置中找不到模型 {name!r}。") from None
    # 用解析后的真实 name 做日志/报错
    name = model_config.name

    # 1. 反射加载模型类（缺包时 resolve_class 抛可操作安装提示）
    model_class = resolve_class(model_config.use, BaseChatModel)

    # 2. 从 ModelConfig 导出构造参数。
    #    排除纯元数据字段；use_responses_api/output_version/stream_chunk_timeout
    #    虽是 ModelConfig 显式字段，但它们本身就是有效构造参数，必须保留透传。
    model_settings_from_config: dict[str, Any] = model_config.model_dump(
        exclude_none=True,
        exclude={
            "use",
            "name",
            "display_name",
            "description",
            "supports_thinking",
            "supports_reasoning_effort",
            "when_thinking_enabled",
            "when_thinking_disabled",
            "thinking",
            "supports_vision",
        },
    )

    # 3. thinking 模式处理 -------------------------------------------------
    # thinking 快捷字段等价于 when_thinking_enabled["thinking"]，先合并出 effective_wte。
    has_thinking_settings = (
        model_config.when_thinking_enabled is not None or model_config.thinking is not None
    )
    effective_wte: dict[str, Any] = (
        dict(model_config.when_thinking_enabled) if model_config.when_thinking_enabled else {}
    )
    if model_config.thinking is not None:
        merged_thinking = {**(effective_wte.get("thinking") or {}), **model_config.thinking}
        effective_wte = {**effective_wte, "thinking": merged_thinking}

    if thinking_enabled and has_thinking_settings:
        if not model_config.supports_thinking:
            raise ValueError(
                f"模型 {name!r} 不支持 thinking。请在 config.yaml 中为其设置 supports_thinking: true。"
            ) from None
        if effective_wte:
            model_settings_from_config.update(effective_wte)

    if not thinking_enabled:
        # 某些模型默认就开 thinking，需显式关闭。按优先级尝试四种关闭路径。
        if model_config.when_thinking_disabled is not None:
            # 用户显式关闭设置优先
            model_settings_from_config.update(model_config.when_thinking_disabled)
        elif has_thinking_settings and effective_wte.get("extra_body", {}).get("thinking", {}).get("type"):
            # OpenAI 兼容网关：thinking 嵌在 extra_body 下
            model_settings_from_config["extra_body"] = _deep_merge_dicts(
                model_settings_from_config.get("extra_body"),
                {"thinking": {"type": "disabled"}},
            )
            model_settings_from_config["reasoning_effort"] = "minimal"
        elif has_thinking_settings and (
            disable_chat_template_kwargs := _vllm_disable_chat_template_kwargs(
                effective_wte.get("extra_body", {}).get("chat_template_kwargs") or {}
            )
        ):
            # vLLM 用 chat_template_kwargs 开关 thinking
            model_settings_from_config["extra_body"] = _deep_merge_dicts(
                model_settings_from_config.get("extra_body"),
                {"chat_template_kwargs": disable_chat_template_kwargs},
            )
        elif has_thinking_settings and effective_wte.get("thinking", {}).get("type"):
            # 原生 langchain_anthropic：thinking 是直接构造参数
            model_settings_from_config["thinking"] = {"type": "disabled"}

    # 4. reasoning_effort 门控：不支持时从 kwargs 与 config 中剔除
    if not model_config.supports_reasoning_effort:
        kwargs.pop("reasoning_effort", None)
        model_settings_from_config.pop("reasoning_effort", None)

    # 5. OpenAI 兼容默认值
    _enable_stream_usage_by_default(model_config.use, model_settings_from_config)
    _apply_stream_chunk_timeout_default(model_config.use, model_settings_from_config)

    # 6. stream_usage 兜底：模型类自身接受该字段且未显式配置时开启
    if "stream_usage" not in model_settings_from_config and "stream_usage" not in kwargs:
        if "stream_usage" in getattr(model_class, "model_fields", {}):
            model_settings_from_config["stream_usage"] = True

    # 7. 实例化（config 派生设置优先于 kwargs）
    model_instance = model_class(**kwargs, **model_settings_from_config)

    # 8. 模型级追踪回调（仅独立调用方；图内须 attach_tracing=False）
    if attach_tracing:
        callbacks = _maybe_build_tracing_callbacks()
        if callbacks:
            existing_callbacks = getattr(model_instance, "callbacks", None) or []
            model_instance.callbacks = [*existing_callbacks, *callbacks]
            logger.debug("已为模型 %r 挂载 %d 个追踪回调", name, len(callbacks))

    return model_instance


def get_default_model() -> BaseChatModel:
    """获取默认模型（config.yaml 中第一个模型）。便捷入口。"""
    return create_chat_model()
