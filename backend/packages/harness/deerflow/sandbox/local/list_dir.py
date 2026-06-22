"""目录树列出（``ls`` 工具的底层实现）。

把 ``list_dir`` 从 ``local_sandbox.py`` 抽成独立模块，是为了让 ``LocalSandbox.list_dir``
与未来的 ``AioSandbox.list_dir``（M10b）共用同一份「树形列出 + 忽略噪音 + 防穿越」逻辑——
两端对 ``ls`` 的输出格式必须一致，否则 agent 在 local 与容器模式间切换会困惑。

行为：
- 默认 ``max_depth=2``（直接子项 + 孙项）。
- 命中 ``IGNORE_PATTERNS``（.git / __pycache__ / .venv 等）的项跳过。
- 目录项以 ``/`` 结尾，文件项无后缀——agent 据此区分类型。
- 越出 root 的符号链接跳过（防穿越）。
- 结果排序，输出稳定可预测。
"""

from __future__ import annotations

from pathlib import Path

from deerflow.sandbox.search import should_ignore_name


def list_dir(path: str, max_depth: int = 2) -> list[str]:
    """以树形列出目录内容（默认 2 层深）。

    Args:
        path: 根目录的（已解析的宿主）路径。
        max_depth: 最大递归深度（1 = 直接子项，2 = 含孙项）。

    Returns:
        绝对路径列表（已排序）；目录项以 ``/`` 结尾。根不是目录时返回空列表。
    """
    result: list[str] = []
    root_path = Path(path).resolve()

    if not root_path.is_dir():
        return result

    def _is_within_root(candidate: Path) -> bool:
        try:
            candidate.relative_to(root_path)
            return True
        except ValueError:
            return False

    def _traverse(current_path: Path, current_depth: int) -> None:
        """递归遍历到 max_depth。"""
        if current_depth > max_depth:
            return

        try:
            for item in current_path.iterdir():
                if should_ignore_name(item.name):
                    continue

                # 符号链接：resolve 后必须仍在 root 内，否则跳过（防穿越）。
                if item.is_symlink():
                    try:
                        item_resolved = item.resolve()
                        if not _is_within_root(item_resolved):
                            continue
                    except OSError:
                        continue
                    post_fix = "/" if item_resolved.is_dir() else ""
                    result.append(str(item_resolved) + post_fix)
                    continue

                item_resolved = item.resolve()
                if not _is_within_root(item_resolved):
                    continue

                post_fix = "/" if item.is_dir() else ""
                result.append(str(item_resolved) + post_fix)

                # 目录且未到最大深度则递归。
                if item.is_dir() and current_depth < max_depth:
                    _traverse(item, current_depth + 1)
        except PermissionError:
            # 无权限的目录跳过，不整体失败。
            pass

    _traverse(root_path, 1)

    return sorted(result)
