"""技能目录的文件权限收紧 helper（M14 skills）。

对齐 deer ``skills/permissions.py``。把安装的技能目录设为只读（目录 0o555 / 文件 0o444），
跳过 symlink，防止沙箱内 agent 改写技能内容。
"""

import stat
from pathlib import Path


def make_skill_path_sandbox_readable(path: Path) -> None:
    """把单条路径设为沙箱只读（剥 sandbox 组/其他写位，按文件/目录补只读模式）。symlink 跳过。"""
    if path.is_symlink():
        return
    mode = stat.S_IMODE(path.stat().st_mode)
    without_sandbox_write = mode & ~(stat.S_IWGRP | stat.S_IWOTH)
    if path.is_dir():
        path.chmod(without_sandbox_write | 0o555)
    elif path.is_file():
        path.chmod(without_sandbox_write | 0o444)


def make_skill_tree_sandbox_readable(target: Path) -> None:
    """把整棵技能子树设为沙箱只读。"""
    make_skill_path_sandbox_readable(target)
    for path in target.rglob("*"):
        make_skill_path_sandbox_readable(path)


def make_skill_written_path_sandbox_readable(skill_root: Path, target: Path) -> None:
    """把 skill_manage 写入的路径（含其父链直到 skill_root）设为沙箱只读。

    校验 target 解析后仍在 skill_root 内（防穿越），失败抛 ValueError。
    """
    resolved_root = skill_root.resolve()
    resolved_target = target.resolve()
    resolved_target.relative_to(resolved_root)

    make_skill_path_sandbox_readable(resolved_root)
    current = resolved_root
    for part in resolved_target.parent.relative_to(resolved_root).parts:
        current = current / part
        make_skill_path_sandbox_readable(current)
    make_skill_path_sandbox_readable(resolved_target)
