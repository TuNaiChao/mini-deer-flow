"""单次 run 的状态枚举与断连模式枚举。

这两个枚举是 runs 子系统的「领域词汇表」，被 RunStore（存储层）、RunManager
（运行管理层，Phase 8）共享。这里只定义枚举，不含任何 IO 或状态机逻辑。
"""

from enum import StrEnum


class RunStatus(StrEnum):
    """单次 run 的生命周期状态。

    - pending：已记录、尚未开始执行。
    - running：worker 正在跑。
    - success：正常结束。
    - error：抛异常结束。
    - timeout：超时结束。
    - interrupted：被 multitask 策略（interrupt / rollback）打断。
    """

    pending = "pending"
    running = "running"
    success = "success"
    error = "error"
    timeout = "timeout"
    interrupted = "interrupted"


class DisconnectMode(StrEnum):
    """SSE 消费方断连时的行为。

    - cancel：客户端断连即取消后台 run（默认）。
    - ``continue_``：客户端断连后 run 继续跑完，结果仍可被回放。
    """

    cancel = "cancel"
    continue_ = "continue"
