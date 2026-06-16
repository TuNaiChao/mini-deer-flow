"""
中间件模块——Agent 的行为骨架

提供中间件的管理和装配。
中间件通过 AgentMiddleware hook 机制工作，
按 build_middlewares() 中的顺序装配。
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # 仅类型注解用，运行时不 import（避免循环导入 + 保持轻量）
    from langchain_core.runnables import RunnableConfig

    from deerflow.config.app_config import AppConfig

from langchain.agents.middleware import AgentMiddleware

from .clarification_middleware import ClarificationMiddleware
from .dynamic_context_middleware import DynamicContextMiddleware
from .llm_error_handling_middleware import LLMErrorHandlingMiddleware
from .loop_detection_middleware import LoopDetectionMiddleware
from .memory_middleware import MemoryMiddleware
from .title_middleware import TitleMiddleware
from .tool_error_handling_middleware import ToolErrorHandlingMiddleware

# ViewImageMiddleware 在阶段5 §步骤6 实现。为了让本阶段的 build_middlewares()
# 提前具备多模态条件挂载能力（避免阶段5 还要回头改本文件），这里用 try/except
# 做前向兼容：阶段5 创建该文件后即自动生效，阶段3 暂未创建时也不影响运行。
try:
    from .view_image_middleware import ViewImageMiddleware

    _HAS_VIEW_IMAGE_MIDDLEWARE = True
except ImportError:
    ViewImageMiddleware = None  # type: ignore[assignment,misc]
    _HAS_VIEW_IMAGE_MIDDLEWARE = False


def build_middlewares(
    config: "RunnableConfig | None" = None,
    model_name: str | None = None,
    *,
    app_config: "AppConfig | None" = None,
    custom_middlewares: list[AgentMiddleware] | None = None,
) -> list[AgentMiddleware]:
    """
    按严格顺序装配中间件链。

    顺序的设计理由见下面的注释。
    这是实际项目的标准装配方式——列表 append + 条件判断，
    而非链式对象或注册表模式。各功能开关从 app_config 读取（配置驱动）。

    Args:
        config: LangGraph 运行时配置（含 configurable 选项，如 is_plan_mode）
        model_name: 解析出的模型名（用于按模型能力决定是否挂某些中间件）
        app_config: 应用配置；为 None 时读全局 get_app_config()
        custom_middlewares: 额外的自定义中间件（插在 Clarification 之前）

    Returns:
        按顺序排列的中间件列表

    Note:
        本教学版只覆盖 7 个核心中间件。真实项目的 build_middlewares（位于
        agents/lead_agent/agent.py:270-377 + tool_error_handling_middleware.py
        的 build_lead_runtime_middlewares）覆盖约 19 个中间件，包括沙箱、
        摘要、工具预算、延迟工具过滤、子代理限流等，全部从 app_config 驱动。
    """
    from ...config import get_app_config

    cfg = app_config or get_app_config()

    # 各功能开关从配置读取（配置驱动）
    # config.yaml 中 title/memory/loop_detection 都是 dict 类型，用 .get() 取值
    enable_title = cfg.title.get("enabled", True) if isinstance(cfg.title, dict) else True
    enable_memory = cfg.memory.get("enabled", True) if isinstance(cfg.memory, dict) else True
    enable_loop_detection = cfg.loop_detection.get("enabled", True) if isinstance(cfg.loop_detection, dict) else True

    middlewares: list[AgentMiddleware] = []

    # === 核心基础设施层 ===

    # 0. LLM 错误处理（包装模型调用，捕获 API 错误）
    middlewares.append(LLMErrorHandlingMiddleware())

    # 1. 动态上下文（在每个轮次前注入日期/记忆信息）
    #    必须在模型调用之前执行
    middlewares.append(DynamicContextMiddleware())

    # === 工具处理层 ===

    # 2. 工具错误处理（捕获工具异常，转为错误消息）
    #    在 LLM 错误处理之后，澄清拦截之前
    middlewares.append(ToolErrorHandlingMiddleware())

    # === 质量/辅助功能层 ===

    # 3. 标题生成（在首次对话后生成线程标题）
    #    需要在 after_model 中读取模型输出
    if enable_title:
        middlewares.append(TitleMiddleware(max_words=10))

    # 4. 记忆队列（将对话加入后台记忆更新队列）
    #    需要在 after_agent 中收集对话对
    if enable_memory:
        middlewares.append(MemoryMiddleware())

    # 5. 循环检测（检测重复工具调用模式）
    #    需要在 after_model 中分析工具调用
    if enable_loop_detection:
        middlewares.append(
            LoopDetectionMiddleware(
                warn_threshold=3,
                hard_limit=5,
            )
        )

    # === 多模态层（条件挂载）===
    # 6. 图片查看（仅当模型支持视觉时挂载）
    #    ViewImageMiddleware 在阶段5 §步骤6 实现。本文件顶部用 try/except
    #    做了前向兼容导入——阶段5 创建该文件后即自动生效。
    #    这一步是"多模态使用 Qwen"链路的关键：没有它，即使配置了
    #    qwen-vl（supports_vision: true），Agent 也看不到图片。
    # 解析当前模型配置（用于按 supports_vision 决定是否挂 ViewImageMiddleware）
    # model_name 为 None 时回退到默认模型（config.yaml 中第一个模型），
    # 与 create_chat_model(name=None) 的默认行为对齐
    model_config = None
    if cfg.models:
        if model_name:
            matched = [m for m in cfg.models if m.name == model_name]
            model_config = matched[0] if matched else cfg.models[0]
        else:
            # 未指定模型名 → 用默认（第一个）模型的能力判断
            model_config = cfg.models[0]
    if _HAS_VIEW_IMAGE_MIDDLEWARE and model_config is not None and getattr(model_config, "supports_vision", False):
        middlewares.append(ViewImageMiddleware())

    # === 用户自定义中间件（可选）===
    if custom_middlewares:
        middlewares.extend(custom_middlewares)

    # === 7. 澄清拦截（必须排在最后！）===
    #    确保所有其他中间件已处理完毕，且可以中断整个执行
    middlewares.append(ClarificationMiddleware())

    return middlewares
