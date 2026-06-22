"""后端↔前端结构化子代理状态契约（deer-flow issue #3146）。

旧前端靠**字符串前缀匹配** ``task`` 工具的结果文本来推子任务卡片状态——只要后端
改个措辞，前端卡片生命周期就静默坏掉（#3107 BUG-007 / #3131 review 反复出现）。

本模块用一个小型**结构化契约**替代：把状态塞进 ``ToolMessage.additional_kwargs``：

- ``subagent_status``：``SUBAGENT_STATUS_VALUES`` 之一。
- ``subagent_error``（可选）：后端记录的可读错误文本。

「task 工具结果文本 → 状态」的映射是后端 stamper（``ToolErrorHandlingMiddleware``）
与前端 fallback 解析器唯一需要对齐的东西。共享 fixture
``contracts/subagent_status_contract.json`` 是单一真相源——两侧的测试都加载它并断言。
"""

from __future__ import annotations

from typing import Literal

SUBAGENT_STATUS_KEY = "subagent_status"
SUBAGENT_ERROR_KEY = "subagent_error"

SubagentStatusValue = Literal[
    "completed",
    "failed",
    "cancelled",
    "timed_out",
    "polling_timed_out",
]

#: ``subagent_status`` 可能取的所有值。镜像 fixture 里的 ``valid_status_values`` 数组；
#: 契约测试会把两者钉在一起。
SUBAGENT_STATUS_VALUES: tuple[SubagentStatusValue, ...] = (
    "completed",
    "failed",
    "cancelled",
    "timed_out",
    "polling_timed_out",
)

# 前缀表——按「最具体在前」排序，因为有些前缀是另一些的子串
# （"Task timed out" vs "Task polling timed out"、"Task failed" vs "Task failed. Error: ..."）。
# "Task " 前缀来自 task_tool 的 5 条正常返回串；裸 ``Error:`` 前缀兜住 3 条
# 执行前 Error 返回 + ToolErrorHandlingMiddleware 对任意 task 工具异常包出的 wrapper。
_PREFIX_TO_STATUS: tuple[tuple[str, SubagentStatusValue], ...] = (
    ("Task Succeeded. Result:", "completed"),
    ("Task polling timed out", "polling_timed_out"),
    ("Task timed out", "timed_out"),
    ("Task cancelled by user", "cancelled"),
    ("Task failed.", "failed"),
    ("Error", "failed"),
)


def extract_subagent_status(content: str) -> SubagentStatusValue | None:
    """从 ``task`` 工具结果字符串推断结构化状态。

    内容不匹配任何已知**终态**前缀时返回 None。非终态的流式分片按设计落到这里——
    中间件据此**不设** ``subagent_status``，让前端把卡片留在 in-progress 占位态，
    直到真正的终态帧到达。
    """
    trimmed = content.strip()
    for prefix, status in _PREFIX_TO_STATUS:
        if trimmed.startswith(prefix):
            return status
    return None


def make_subagent_additional_kwargs(
    status: SubagentStatusValue,
    *,
    error: str | None = None,
) -> dict[str, str]:
    """构造中间件要盖的 ``additional_kwargs`` 负载。

    error 为空时丢掉该字段，让 JSON 线格式永不带误导性的空 ``subagent_error: ""``。

    Raises:
        ValueError: ``status`` 不在 :data:`SUBAGENT_STATUS_VALUES` 时。我们不接受任意
            字符串：一个拼写错误会静默漏到前端并降级成 legacy 前缀 fallback，不如响亮报错。
    """
    if status not in SUBAGENT_STATUS_VALUES:
        raise ValueError(f"invalid subagent status {status!r}; expected one of {SUBAGENT_STATUS_VALUES}")
    payload: dict[str, str] = {SUBAGENT_STATUS_KEY: status}
    if error and error.strip():
        payload[SUBAGENT_ERROR_KEY] = error.strip()
    return payload
