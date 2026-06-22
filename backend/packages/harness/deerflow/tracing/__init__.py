"""链路追踪模块（M12）。

LangSmith / Langfuse 两套追踪。回调在**图根**注入（lead agent / client 入口），
让单次 run 产出一条含全部 node / LLM / tool 调用为子 span 的 trace。

导出：
- :func:`build_tracing_callbacks`：按当前启用的 provider 构造回调列表（未配置返回空）。
- :func:`build_langfuse_trace_metadata` / :func:`inject_langfuse_metadata`：Langfuse
  trace 属性元数据（session_id / user_id / trace_name / tags）。
"""

from .factory import build_tracing_callbacks
from .metadata import build_langfuse_trace_metadata, inject_langfuse_metadata

__all__ = [
    "build_langfuse_trace_metadata",
    "build_tracing_callbacks",
    "inject_langfuse_metadata",
]
