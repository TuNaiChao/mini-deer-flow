"""沙箱子包：抽象接口、provider 单例、异常。

工具层与中间件层都从这里取 provider 单例（``get_sandbox_provider``），不直接 import
具体实现，便于运行时按 ``config.sandbox.use`` 切换 Local / AIO（M10b）provider。
"""

from deerflow.sandbox.sandbox import Sandbox
from deerflow.sandbox.sandbox_provider import SandboxProvider, get_sandbox_provider

__all__ = [
    "Sandbox",
    "SandboxProvider",
    "get_sandbox_provider",
]
