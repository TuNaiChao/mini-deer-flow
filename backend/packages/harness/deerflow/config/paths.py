"""
路径配置模块

定位项目根目录和各类文件的路径
"""

from pathlib import Path


def find_project_root() -> Path:
    """
    查找项目根目录（backend/ 目录）

    通过向上查找 pyproject.toml 来确定
    """
    current = Path.cwd()

    for parent in [current, *current.parents]:
        if (parent / "pyproject.toml").exists():
            return parent

    return current


def get_config_file() -> Path:
    """获取 config.yaml 的路径（在项目根目录的父目录）"""
    root = find_project_root()
    # config.yaml 在 backend/ 的父目录（即 deer-flow/）
    config_path = root.parent / "config.yaml"

    if not config_path.exists():
        # 回退：在 backend/ 目录下查找
        config_path = root / "config.yaml"

    return config_path


def get_env_file() -> Path:
    """获取 .env 文件路径（与 config.yaml 同级，在项目根目录的父目录）"""
    root = find_project_root()
    # .env 在 backend/ 的父目录（即项目根目录），与 config.yaml 同级
    env_path = root.parent / ".env"

    if not env_path.exists():
        # 回退：在 backend/ 目录下查找
        env_path = root / ".env"

    return env_path


# 项目根目录
PROJECT_ROOT = find_project_root()
