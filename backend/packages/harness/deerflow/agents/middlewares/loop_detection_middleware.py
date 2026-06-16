"""
循环检测中间件

检测 Agent 是否陷入重复工具调用的循环，
在检测到循环时强制 Agent 给出文本回复。
"""
import hashlib
import json
import logging
from collections import deque

from langchain.agents.middleware import AgentMiddleware

logger = logging.getLogger(__name__)


class LoopDetectionMiddleware(AgentMiddleware):
    """
    检测重复的工具调用模式。

    Hook 使用: after_model + wrap_model_call
    执行顺序: 较后（在其他中间件处理完模型输出后）

    工作原理：
    1. after_model: 哈希模型的工具调用列表
    2. 将哈希与最近的调用历史比较
    3. 如果同一工具调用模式重复超过阈值 → 强制文本回复
    4. wrap_model_call: 在下一轮模型调用前，如果上一轮触发了警告，
       插入警告消息提示模型
    """

    def __init__(
        self,
        warn_threshold: int = 3,
        hard_limit: int = 5,
        window_size: int = 10,
    ):
        """
        Args:
            warn_threshold: 警告阈值（第几次重复时警告）
            hard_limit: 硬限制（第几次重复时强制停止）
            window_size: 滑动窗口大小
        """
        super().__init__()
        self.warn_threshold = warn_threshold
        self.hard_limit = hard_limit
        self.window_size = window_size

        # 每个线程的工具调用历史
        self._call_history: dict[str, deque] = {}
        # 待注入的警告消息
        self._pending_warnings: dict[str, str] = {}

    def _hash_tool_calls(self, tool_calls: list) -> str:
        """计算工具调用的哈希（用于比较是否重复）"""
        if not tool_calls:
            return "no_tool_calls"

        # 提取名称+参数的规范形式
        normalized = []
        for tc in tool_calls:
            normalized.append({
                "name": tc.get("name", ""),
                "args": json.dumps(tc.get("args", {}), sort_keys=True),
            })

        serialized = json.dumps(normalized, sort_keys=True)
        return hashlib.md5(serialized.encode()).hexdigest()[:12]

    def _get_thread_id(self, runtime) -> str:
        """从 runtime 获取线程 ID"""
        return getattr(runtime, "thread_id", "default")

    def after_model(self, state, runtime):
        """分析模型输出的工具调用模式"""
        thread_id = self._get_thread_id(runtime)

        # 获取最后一条 AIMessage
        messages = state.get("messages", [])
        if not messages:
            return None

        last_msg = messages[-1]
        tool_calls = getattr(last_msg, "tool_calls", None) or []

        if not tool_calls:
            # 没有工具调用，清空警告
            self._pending_warnings.pop(thread_id, None)
            return None

        # 计算哈希
        call_hash = self._hash_tool_calls(tool_calls)

        # 初始化或获取历史
        if thread_id not in self._call_history:
            self._call_history[thread_id] = deque(maxlen=self.window_size)
        history = self._call_history[thread_id]

        # 记录本次调用
        history.append(call_hash)

        # 统计重复次数
        repeat_count = list(history).count(call_hash)

        if repeat_count >= self.hard_limit:
            # 硬限制：移除工具调用，强制文本回复
            logger.warning(
                f"[{thread_id}] 工具调用检测到硬循环 ({repeat_count}次)，强制文本回复"
            )
            # ⚠️ 关键：AIMessage 是 Pydantic v2 的冻结模型，不能原地改字段
            # （last_msg.tool_calls = [] 会抛 ValidationError）。
            # 必须用 model_copy(update={...}) 生成一个清空了工具调用的新消息。
            # 真实实现还清空 additional_kwargs["tool_calls"] 并改写 finish_reason。
            cleared = last_msg.model_copy(
                update={
                    "tool_calls": [],
                    "additional_kwargs": {
                        k: v
                        for k, v in (last_msg.additional_kwargs or {}).items()
                        if k != "tool_calls"
                    },
                }
            )
            return {"messages": [cleared]}

        elif repeat_count >= self.warn_threshold:
            # 警告阈值：下一轮提示模型
            warning = (
                f"⚠️ 你已重复执行相同的工具调用 {repeat_count} 次。"
                f"请检查执行结果，考虑不同的方法或告知用户当前状况。"
                f"不要再次重复相同的工具调用。"
            )
            self._pending_warnings[thread_id] = warning
            logger.info(f"[{thread_id}] 循环警告: {repeat_count}次")

        return None

    def wrap_model_call(self, request, handler):
        """在模型调用前，注入未完成的循环警告"""
        thread_id = self._get_thread_id(request.runtime)

        # 检查是否有待注入的警告
        warning = self._pending_warnings.pop(thread_id, None)
        if warning:
            from langchain_core.messages import HumanMessage

            # 将警告作为系统级消息注入
            warning_msg = HumanMessage(
                content=f"<loop_warning>{warning}</loop_warning>",
            )
            # 追加到消息列表末尾
            modified_request = request.override(
                messages=request.messages + [warning_msg]
            )
            return handler(modified_request)

        return handler(request)

    async def aafter_model(self, state, runtime):
        return self.after_model(state, runtime)

    async def awrap_model_call(self, request, handler):
        return self.wrap_model_call(request, handler)