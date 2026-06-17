"""store 与 checkpointer 共用的 SQLite 连接串工具。

两个纯函数：
- :func:`resolve_sqlite_conn_str`：把用户给的路径归一成 SQLite 连接串（``":memory:"`` /
  ``file:`` URI 原样返回，普通路径转绝对路径）。
- :func:`ensure_sqlite_parent_dir`：确保 SQLite 文件路径的父目录存在（红线 #1912：
  防止「unable to open database file」——当 .deer-flow 目录还没建时）。

对 ``":memory:"`` 与 ``file:`` URI 都是 no-op。
"""

from __future__ import annotations

import pathlib

from deerflow.config.paths import resolve_path


def resolve_sqlite_conn_str(raw: str) -> str:
    """返回可用于 store/checkpointer 后端的 SQLite 连接串。

    SQLite 特殊串（``":memory:"`` 与 ``file:`` URI）原样返回。普通文件路径（相对或
    绝对）经 :func:`resolve_path` 解析成绝对路径串。
    """
    if raw == ":memory:" or raw.startswith("file:"):
        return raw
    return str(resolve_path(raw))


def ensure_sqlite_parent_dir(conn_str: str) -> None:
    """为 SQLite 文件路径创建父目录。

    对内存库（``":memory:"``）与 ``file:`` URI 是 no-op。
    """
    if conn_str != ":memory:" and not conn_str.startswith("file:"):
        pathlib.Path(conn_str).parent.mkdir(parents=True, exist_ok=True)
