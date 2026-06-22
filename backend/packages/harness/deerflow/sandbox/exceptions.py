"""沙箱相关异常（带结构化 ``details``，方便排查与日志）。

异常集中在本模块（而非散落在 ``sandbox.py`` / 各 provider），是为了让「错误类型层次」
有单一真相源：工具层、中间件层、provider 层都从这里 import，不会出现两处各定义一个
``SandboxError`` 导致 ``isinstance`` 漏判的情况。

层次：

``SandboxError``（基类，带 ``message`` + ``details`` dict）
├── ``SandboxNotFoundError``      —— 按 id 取不到沙箱（已释放 / 从未创建）
├── ``SandboxRuntimeError``       —— 运行时不可用或配置错（runtime / thread_id 缺失）
├── ``SandboxCommandError``       —— 命令执行失败（带 command / exit_code）
└── ``SandboxFileError``          —— 文件操作失败（带 path / operation）
    ├── ``SandboxPermissionError`` —— 权限/穿越拒绝
    └── ``SandboxFileNotFoundError``—— 文件/目录不存在

``SandboxFileError`` 是「文件操作类错误」的公共父类，``SandboxPermissionError`` 与
``SandboxFileNotFoundError`` 继承它，这样调用方可以 ``except SandboxFileError`` 一网打尽
所有文件类问题，也可以精确 catch 子类做不同处理（如 NotFound → 提示重试）。
"""

from __future__ import annotations


class SandboxError(Exception):
    """所有沙箱相关错误的基类。带结构化 ``details``，方便排查。"""

    def __init__(self, message: str, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def __str__(self) -> str:
        if self.details:
            detail_str = ", ".join(f"{k}={v}" for k, v in self.details.items())
            return f"{self.message} ({detail_str})"
        return self.message


class SandboxNotFoundError(SandboxError):
    """按 id 取不到沙箱（已被释放 / 从未创建）。"""

    def __init__(self, message: str = "Sandbox not found", sandbox_id: str | None = None):
        details = {"sandbox_id": sandbox_id} if sandbox_id else None
        super().__init__(message, details)
        self.sandbox_id = sandbox_id


class SandboxRuntimeError(SandboxError):
    """沙箱运行时不可用或配置错误（如 runtime 缺失、thread_id 缺失）。"""

    pass


class SandboxCommandError(SandboxError):
    """沙箱内命令执行失败（带 command / exit_code 详情）。"""

    def __init__(self, message: str, command: str | None = None, exit_code: int | None = None):
        details: dict = {}
        if command:
            # command 可能很长，截到 100 字防日志爆炸。
            details["command"] = command[:100] + "..." if len(command) > 100 else command
        if exit_code is not None:
            details["exit_code"] = exit_code
        super().__init__(message, details)
        self.command = command
        self.exit_code = exit_code


class SandboxFileError(SandboxError):
    """沙箱内文件操作失败（带 path / operation 详情）。"""

    def __init__(self, message: str, path: str | None = None, operation: str | None = None):
        details: dict = {}
        if path:
            details["path"] = path
        if operation:
            details["operation"] = operation
        super().__init__(message, details)
        self.path = path
        self.operation = operation


class SandboxPermissionError(SandboxFileError):
    """文件操作权限错误（路径穿越 / 越界 / 只读挂载写入）。"""

    pass


class SandboxFileNotFoundError(SandboxFileError):
    """文件或目录不存在。"""

    pass
