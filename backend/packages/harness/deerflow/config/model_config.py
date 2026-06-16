"""
模型配置模块

每个模型配置使用 'use' 字段指定动态加载的类路径。
由于 model_config = ConfigDict(extra="allow")，
可以传递任意额外的提供者特定参数（如 api_key, temperature 等）。
"""
from typing import Any
from pydantic import BaseModel, ConfigDict


class ModelConfig(BaseModel):
    """单个模型的配置"""

    model_config = ConfigDict(extra="allow")  # 允许任意额外字段

    # --- 必需字段 ---
    name: str
    """模型的唯一名称（在 Agent 代码中引用）"""

    use: str
    """模型类的导入路径，如 'langchain_deepseek:ChatDeepSeek'"""

    model: str
    """传递给提供者的模型标识符，如 'deepseek-chat'"""

    # --- 可选元数据 ---
    display_name: str | None = None
    """在 UI 中显示的友好名称"""

    description: str | None = None
    """模型的描述文本"""

    # --- 能力声明 ---
    supports_thinking: bool = False
    """模型是否支持扩展思考模式（如 DeepSeek-R1 的 reasoning）"""

    supports_vision: bool = False
    """模型是否支持图片/多模态输入"""

    supports_reasoning_effort: bool = False
    """模型是否接受 reasoning_effort 参数"""

    # --- 思考模式配置 ---
    when_thinking_enabled: dict[str, Any] | None = None
    """开启思考时传递给模型的额外参数"""

    when_thinking_disabled: dict[str, Any] | None = None
    """关闭思考时传递给模型的额外参数"""

    thinking: dict[str, Any] | None = None
    """when_thinking_enabled 的快捷别名"""

    # --- 其他可选字段 ---
    use_responses_api: bool | None = None
    """是否使用 OpenAI /v1/responses API（仅限 OpenAI 提供者）"""

    output_version: str | None = None
    """结构化输出版本标识"""

    stream_chunk_timeout: float | None = None
    """流式传输块之间的最大等待时间（秒）"""