"""边界检查：harness 层不得 import app 层。

deerflow-harness 包（packages/harness/deerflow/）是独立、可发布的 agent 框架，
**永远不得依赖 app 层**（任何 FastAPI / Gateway 代码）。

mini 暂无 app/ 目录，本测试当前恒通过，作为**占位与未来护栏**：
一旦未来引入 app/ 目录，任何 ``from app...`` / ``import app...`` 都会立刻
让本测试失败，把「harness 边界」这条口头红线变成 CI 强制约束（红线 #28）。

对齐 deer-flow ``tests/test_harness_boundary.py``。
"""

from __future__ import annotations

import ast
from pathlib import Path

# harness 包根目录：backend/packages/harness/deerflow
HARNESS_ROOT = Path(__file__).resolve().parent.parent / "packages" / "harness" / "deerflow"

# 被禁的导入前缀。注意用 ``prefix + "."`` 判断，避免误伤 ``apple`` 之类同名前缀。
BANNED_PREFIXES = ("app",)


def _collect_imports(filepath: Path) -> list[tuple[int, str]]:
    """返回文件中所有 import 的 (行号, 模块路径)。

    覆盖 ``import X`` 与 ``from X import Y`` 两种形式；语法错误的文件跳过。
    """
    source = filepath.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(filepath))
    except SyntaxError:
        return []

    results: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                results.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom) and node.module:
            results.append((node.lineno, node.module))
    return results


def test_harness_does_not_import_app() -> None:
    """harness 包下任何 .py 文件都不得 import app.*。"""
    violations: list[str] = []

    for py_file in sorted(HARNESS_ROOT.rglob("*.py")):
        for lineno, module in _collect_imports(py_file):
            if any(module == prefix or module.startswith(prefix + ".") for prefix in BANNED_PREFIXES):
                rel = py_file.relative_to(HARNESS_ROOT.parent.parent.parent)
                violations.append(f"  {rel}:{lineno}  imports {module}")

    assert not violations, "harness 层不得 import app 层:\n" + "\n".join(violations)
