"""路径配置模块。

定位项目根目录、各类文件路径，并提供**运行时路径解析**（``resolve_path`` /
``runtime_home`` / ``get_paths``）。

mini 不使用 deer 的 ``runtime_paths`` 模块——本模块统一提供运行时路径 API，
新增代码一律用本模块的 ``resolve_path`` / ``project_root``，**不得** import
``runtime_paths``（outline M0 明确替代关系）。

两套「根」概念，职责不同：
- :data:`PROJECT_ROOT` / :func:`find_project_root`：定位 **backend/ 目录**（找
  ``pyproject.toml``），用于 ``config.yaml`` / ``.env`` 路径解析。
- :func:`project_root`：**运行时项目根**（优先 ``DEER_FLOW_PROJECT_ROOT`` 环境变量，
  否则当前工作目录），用于 ``resolve_path`` 派生数据目录。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def find_project_root() -> Path:
    """查找项目根目录（backend/ 目录，含 pyproject.toml）。"""
    current = Path.cwd()
    for parent in [current, *current.parents]:
        if (parent / "pyproject.toml").exists():
            return parent
    return current


def get_config_file() -> Path:
    """获取 config.yaml 的路径（在项目根目录的父目录，即 mini-deer-flow/）。"""
    root = find_project_root()
    config_path = root.parent / "config.yaml"
    if not config_path.exists():
        config_path = root / "config.yaml"
    return config_path


def get_env_file() -> Path:
    """获取 .env 文件路径（与 config.yaml 同级，在项目根目录的父目录）。"""
    root = find_project_root()
    env_path = root.parent / ".env"
    if not env_path.exists():
        env_path = root / ".env"
    return env_path


# ---------------------------------------------------------------------------
# 运行时路径解析（替代 deer 的 runtime_paths）
# ---------------------------------------------------------------------------


def project_root() -> Path:
    """返回调用方项目根（运行时归属文件的根目录）。

    优先 ``DEER_FLOW_PROJECT_ROOT`` 环境变量（校验存在且为目录），否则当前工作目录。
    """
    if env_root := os.getenv("DEER_FLOW_PROJECT_ROOT"):
        root = Path(env_root).resolve()
        if not root.exists():
            raise ValueError(f"DEER_FLOW_PROJECT_ROOT 设为 '{env_root}'，但解析路径 '{root}' 不存在。")
        if not root.is_dir():
            raise ValueError(f"DEER_FLOW_PROJECT_ROOT 设为 '{env_root}'，但解析路径 '{root}' 不是目录。")
        return root
    return Path.cwd().resolve()


def runtime_home() -> Path:
    """可写的 DeerFlow 状态目录（base_dir）。

    优先 ``DEER_FLOW_HOME`` 环境变量，否则 ``{project_root}/.deer-flow``。
    """
    if env_home := os.getenv("DEER_FLOW_HOME"):
        return Path(env_home).resolve()
    return project_root() / ".deer-flow"


def resolve_path(value: str | os.PathLike[str], *, base: Path | None = None) -> Path:
    """绝对路径原样返回，相对路径相对项目根解析。"""
    path = Path(value)
    if not path.is_absolute():
        path = (base or project_root()) / path
    return path.resolve()


def existing_project_file(names: tuple[str, ...]) -> Path | None:
    """返回项目根下第一个存在的具名文件。"""
    root = project_root()
    for name in names:
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None


# ---------------------------------------------------------------------------
# 运行时路径集合
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Paths:
    """运行时路径集合（base_dir 及派生目录）。

    按用户 / 按线程的数据目录由各业务模块用 ``base_dir`` + ``user_id`` / ``thread_id``
    自行拼接（见 memory / sandbox / persistence）。per-user / per-agent 的记忆路径因
    依赖 user_id，不是本对象的属性，由 memory 模块构造。
    """

    base_dir: Path

    @property
    def users_dir(self) -> Path:
        """按用户隔离的数据根目录：``{base_dir}/users``。"""
        return self.base_dir / "users"


def get_paths() -> Paths:
    """返回运行时路径集合（base_dir = ``runtime_home()``）。

    不做缓存：``runtime_home`` / ``project_root`` 依赖环境变量，缓存会让测试改 env
    后取到旧值。每次调用都是几个轻量 Path 操作，开销可忽略。
    """
    return Paths(base_dir=runtime_home())


# 项目根目录（backend/，向后兼容现有引用）
PROJECT_ROOT = find_project_root()
