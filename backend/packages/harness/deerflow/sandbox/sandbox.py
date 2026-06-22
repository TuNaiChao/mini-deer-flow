"""沙箱抽象基类。

沙箱（sandbox）是「让 agent 在受控环境里跑 bash / 读写文件 / 搜索文件」的抽象。
deer-flow 设计了两类实现：

- ``LocalSandbox``（本仓库 ``sandbox/local/``）：直接在宿主机进程里跑命令、读写文件，
  靠**虚拟路径翻译 + 路径穿越防御**做隔离（不是真正的安全边界，host bash 默认禁用）。
- ``AioSandboxProvider``（deer 的 ``community/aio_sandbox/``，Docker 隔离）：见 M10b，
  生产 / 多租户 / untrusted 代码用它做真正的容器隔离。

本模块只定义抽象接口。异常类型集中在 :mod:`deerflow.sandbox.exceptions`，这里**再导出**
一份，既符合「基类与其错误在同一处可发现」的直觉，也兼容旧调用方
``from deerflow.sandbox.sandbox import SandboxError`` 的写法——规范定义点在 exceptions.py。

七个沙箱工具（bash / ls / glob / grep / read_file / write_file / str_replace）只依赖这里的
抽象方法：``execute_command`` / ``read_file`` / ``download_file`` / ``list_dir`` /
``write_file`` / ``glob`` / ``grep`` / ``update_file``。``str_replace`` 由工具层用
``read_file`` + ``write_file`` 组合实现，故基类不单列。具体实现的路径翻译
（虚拟路径 → 宿主路径）在各 ``Sandbox`` 子类内部完成，工具层拿到的已是「容器视角」的路径。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from deerflow.sandbox.exceptions import (
    SandboxCommandError,
    SandboxError,
    SandboxFileError,
    SandboxFileNotFoundError,
    SandboxNotFoundError,
    SandboxPermissionError,
    SandboxRuntimeError,
)
from deerflow.sandbox.search import GrepMatch

# 异常在此再导出，保持 ``from deerflow.sandbox.sandbox import SandboxError`` 可用。
# 规范定义点在 :mod:`deerflow.sandbox.exceptions`。
__all__ = [
    "Sandbox",
    "SandboxError",
    "SandboxNotFoundError",
    "SandboxRuntimeError",
    "SandboxCommandError",
    "SandboxFileError",
    "SandboxPermissionError",
    "SandboxFileNotFoundError",
]


class Sandbox(ABC):
    """沙箱环境的抽象基类。

    工具层拿到的路径都是**容器视角**（如 ``/mnt/user-data/workspace/a.py``），子类负责
    在内部翻译成宿主真实路径。输出 / 命令里的宿主绝对路径也应被子类「反解析」回容器路径，
    避免向 agent 泄露宿主目录布局。
    """

    _id: str

    def __init__(self, id: str):
        self._id = id

    @property
    def id(self) -> str:
        return self._id

    @abstractmethod
    def execute_command(self, command: str) -> str:
        """在沙箱里执行 bash 命令，返回 stdout（失败时附 stderr / exit code）。

        Args:
            command: 要执行的命令（具体实现内部会翻译虚拟路径）。

        Returns:
            命令输出；无输出时返回占位文本。
        """

    @abstractmethod
    def read_file(self, path: str) -> str:
        """读取文本文件内容。

        Args:
            path: 文件的（容器视角）绝对路径。

        Returns:
            文件文本内容。

        Raises:
            FileNotFoundError: 文件不存在。
            IsADirectoryError: 路径是目录。
            PermissionError: 路径越界（穿越防御）。
        """

    @abstractmethod
    def download_file(self, path: str) -> bytes:
        """下载文件的二进制内容（供 view_image 等读二进制场景）。

        Args:
            path: 文件的（容器视角）绝对路径。

        Returns:
            原始字节。

        Raises:
            PermissionError: 路径越界或不在允许的虚拟前缀内。
            OSError: 文件读不出 / 不存在 / 超过 100MB 上限。
        """

    @abstractmethod
    def list_dir(self, path: str, max_depth: int = 2) -> list[str]:
        """以树形列出目录内容（默认 2 层深）。

        Args:
            path: 目录的（容器视角）绝对路径。
            max_depth: 最大递归深度（1 = 直接子项，2 = 含孙项）。

        Returns:
            绝对路径列表，目录项以 ``/`` 结尾；空目录返回空列表。
        """

    @abstractmethod
    def write_file(self, path: str, content: str, append: bool = False) -> None:
        """写文本到文件（``append=False`` 覆盖，``True`` 追加）。

        需要时自动创建父目录。对只读挂载路径应 raise ``OSError(EROFS)``。

        Args:
            path: 文件的（容器视角）绝对路径。
            content: 文本内容。
            append: True 追加，False 覆盖（默认）。
        """

    @abstractmethod
    def glob(self, path: str, pattern: str, *, include_dirs: bool = False, max_results: int = 200) -> tuple[list[str], bool]:
        """在 root 目录下按 glob 模式找文件/目录路径。

        Args:
            path: 搜索根的（容器视角）绝对路径。
            pattern: glob 模式（相对 root，如 ``**/*.py``）。
            include_dirs: 是否返回匹配的目录（默认仅文件）。
            max_results: 最多返回多少条（超限截断，第二返回值置 True）。

        Returns:
            ``(匹配路径列表, 是否被截断)``。
        """

    @abstractmethod
    def grep(
        self,
        path: str,
        pattern: str,
        *,
        glob: str | None = None,
        literal: bool = False,
        case_sensitive: bool = False,
        max_results: int = 100,
    ) -> tuple[list[GrepMatch], bool]:
        """在 root 目录下的文本文件里搜匹配行。

        Args:
            path: 搜索根的（容器视角）绝对路径。
            pattern: 字符串或正则。
            glob: 可选的候选文件 glob 过滤（如 ``**/*.py``）。
            literal: True 则把 pattern 当纯字符串（自动 re.escape）。
            case_sensitive: 是否大小写敏感（默认 False）。
            max_results: 最多返回多少条匹配行。

        Returns:
            ``(GrepMatch 列表, 是否被截断)``。
        """

    @abstractmethod
    def update_file(self, path: str, content: bytes) -> None:
        """用二进制内容更新文件（覆盖写）。

        供「二进制写」场景使用（如产物下载回填）；文本写走 ``write_file``。

        Args:
            path: 文件的（容器视角）绝对路径。
            content: 原始字节。
        """
