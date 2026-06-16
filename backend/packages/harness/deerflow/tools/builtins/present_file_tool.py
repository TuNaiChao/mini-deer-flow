"""
present_files 工具

把 Agent 生成的输出文件展示给用户。只有 /mnt/user-data/outputs/ 下的文件
才能展示（安全边界）。展示的文件路径写入 ThreadState.artifacts。

这是"需要访问线程上下文"的工具范例——通过 runtime 参数拿到 thread_id 和 state。
"""
from langchain.tools import tool
from langgraph.types import Command

from deerflow.tools.types import Runtime

# 允许展示的虚拟路径前缀（沙箱内视角）
OUTPUTS_VIRTUAL_PREFIX = "/mnt/user-data/outputs"


@tool("present_files", parse_docstring=True)
def present_file_tool(
    runtime: Runtime,
    filepath: str,
) -> Command:
    """Present one generated output file to the user.

    Only files under /mnt/user-data/outputs can be presented. The presented
    file path is recorded in thread artifacts so the UI can show it.

    Args:
        runtime: 注入的运行时上下文（thread_id + state），由 LangGraph 自动提供，模型不可见。
        filepath: 要展示的文件路径，必须在 /mnt/user-data/outputs/ 下。
    """
    # 1. 校验路径在允许范围内
    if not filepath.startswith(OUTPUTS_VIRTUAL_PREFIX):
        raise ValueError(
            f"只能展示 {OUTPUTS_VIRTUAL_PREFIX}/ 下的文件，收到: {filepath}"
        )

    # 2. 通过 runtime.state 拿到当前线程状态
    if runtime.state is None:
        raise ValueError("线程状态不可用")

    # 3. 返回 Command 更新 artifacts（触发 ThreadState 的 merge_artifacts reducer）
    return Command(
        update={"artifacts": [filepath]},
    )