"""记忆存储 provider（M13 memory）。

对齐 deer ``agents/memory/storage.py``，全面对标。两条核心约束：

- **per-user / per-agent 隔离**：记忆按 ``(user_id, agent_name)`` 分桶存储——
  ``{base_dir}/users/{user_id}/memory.json``（全局）、
  ``{base_dir}/users/{user_id}/agents/{name}/memory.json``（per-agent）。
  legacy 无隔离布局（``{base_dir}/memory.json`` / ``{base_dir}/agents/{name}/memory.json``）
  作为只读回退兼容旧安装。绝对 ``storage_path`` 显式退出隔离（所有用户共享一文件）。
- **agent_name 校验**：用 [agents_config](../../config/agents_config.py) 的
  ``AGENT_NAME_PATTERN``（红线 #32，v1.2 起从 agents_config 直接取，不再局部兜底），
  防路径穿越 / 注入。

``FileMemoryStorage`` 用 mtime 缓存（避免每轮重读 JSON）+ 原子写（temp + rename，防写一半崩溃
留下半截 JSON）。``MemoryStorage`` ABC 让自定义后端（如 DB）可经 ``memory.storage_class``
配置替换。
"""

import abc
import json
import logging
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from deerflow.config.agents_config import AGENT_NAME_PATTERN
from deerflow.config.memory_config import get_memory_config
from deerflow.config.paths import get_paths

logger = logging.getLogger(__name__)


def utc_now_iso_z() -> str:
    """当前 UTC 时间的 ISO-8601 + ``Z`` 后缀（与历史 naive-UTC 输出一致）。"""
    return datetime.now(UTC).isoformat().removesuffix("+00:00") + "Z"


def create_empty_memory() -> dict[str, Any]:
    """构造空记忆结构（user 三段 + history 三段 + facts 列表）。"""
    return {
        "version": "1.0",
        "lastUpdated": utc_now_iso_z(),
        "user": {
            "workContext": {"summary": "", "updatedAt": ""},
            "personalContext": {"summary": "", "updatedAt": ""},
            "topOfMind": {"summary": "", "updatedAt": ""},
        },
        "history": {
            "recentMonths": {"summary": "", "updatedAt": ""},
            "earlierContext": {"summary": "", "updatedAt": ""},
            "longTermBackground": {"summary": "", "updatedAt": ""},
        },
        "facts": [],
    }


class MemoryStorage(abc.ABC):
    """记忆存储 provider 抽象基类。"""

    @abc.abstractmethod
    def load(self, agent_name: str | None = None, *, user_id: str | None = None) -> dict[str, Any]:
        """加载记忆（带 mtime 缓存）。"""

    @abc.abstractmethod
    def reload(self, agent_name: str | None = None, *, user_id: str | None = None) -> dict[str, Any]:
        """强制重读（绕过缓存）。"""

    @abc.abstractmethod
    def save(self, memory_data: dict[str, Any], agent_name: str | None = None, *, user_id: str | None = None) -> bool:
        """保存记忆（原子写 + 刷缓存）。"""


class FileMemoryStorage(MemoryStorage):
    """基于文件的记忆存储（默认实现）。

    缓存按 ``(user_id, agent_name)`` 分桶，每桶存 ``(memory_data, file_mtime)``；load 时比对
    mtime，变了才重读盘。所有读写经 ``_cache_lock`` 串行化。
    """

    def __init__(self) -> None:
        # per-user/agent 记忆缓存：键 (user_id, agent_name)（None = 全局）；值 (data, mtime)。
        self._memory_cache: dict[tuple[str | None, str | None], tuple[dict[str, Any], float | None]] = {}
        # 保护 _memory_cache 跨并发调用方。
        self._cache_lock = threading.Lock()

    def _validate_agent_name(self, agent_name: str) -> None:
        """校验 agent 名拼路径前安全（红线 #32，复用 agents_config 的 AGENT_NAME_PATTERN）。"""
        if not agent_name:
            raise ValueError("Agent name must be a non-empty string.")
        if not AGENT_NAME_PATTERN.match(agent_name):
            raise ValueError(f"Invalid agent name {agent_name!r}: names must match {AGENT_NAME_PATTERN.pattern}")

    def _get_memory_file_path(self, agent_name: str | None = None, *, user_id: str | None = None) -> Path:
        """解析记忆文件路径。

        解析优先级：
        1. ``user_id`` + ``agent_name`` → per-user per-agent 文件。
        2. ``user_id``（无 agent_name）→ per-user 全局文件（绝对 ``storage_path`` 退出隔离）。
        3. 仅 ``agent_name``（无 user_id）→ legacy per-agent 文件。
        4. 都没有 → legacy 全局文件（``storage_path`` 相对路径相对 base_dir）。
        """
        if user_id is not None:
            if agent_name is not None:
                self._validate_agent_name(agent_name)
                return get_paths().user_agent_memory_file(user_id, agent_name)
            config = get_memory_config()
            if config.storage_path and Path(config.storage_path).is_absolute():
                return Path(config.storage_path)
            return get_paths().user_memory_file(user_id)
        # Legacy：无 user_id
        if agent_name is not None:
            self._validate_agent_name(agent_name)
            return get_paths().agent_memory_file(agent_name)
        config = get_memory_config()
        if config.storage_path:
            p = Path(config.storage_path)
            return p if p.is_absolute() else get_paths().base_dir / p
        return get_paths().memory_file

    def _load_memory_from_file(self, agent_name: str | None = None, *, user_id: str | None = None) -> dict[str, Any]:
        """从文件读记忆；文件缺失或损坏 JSON 回退空结构（不抛）。"""
        file_path = self._get_memory_file_path(agent_name, user_id=user_id)

        if not file_path.exists():
            return create_empty_memory()

        try:
            with open(file_path, encoding="utf-8") as f:
                data = json.load(f)
            return data
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load memory file: %s", e)
            return create_empty_memory()

    @staticmethod
    def _cache_key(agent_name: str | None = None, *, user_id: str | None = None) -> tuple[str | None, str | None]:
        return (user_id, agent_name)

    def load(self, agent_name: str | None = None, *, user_id: str | None = None) -> dict[str, Any]:
        """加载记忆（mtime 缓存命中直接返回，否则读盘并回填缓存）。"""
        file_path = self._get_memory_file_path(agent_name, user_id=user_id)
        cache_key = self._cache_key(agent_name, user_id=user_id)

        try:
            current_mtime = file_path.stat().st_mtime if file_path.exists() else None
        except OSError:
            current_mtime = None

        with self._cache_lock:
            cached = self._memory_cache.get(cache_key)
            if cached is not None and cached[1] == current_mtime:
                return cached[0]

        memory_data = self._load_memory_from_file(agent_name, user_id=user_id)

        with self._cache_lock:
            self._memory_cache[cache_key] = (memory_data, current_mtime)

        return memory_data

    def reload(self, agent_name: str | None = None, *, user_id: str | None = None) -> dict[str, Any]:
        """强制重读（绕过缓存，回填新 mtime）。"""
        file_path = self._get_memory_file_path(agent_name, user_id=user_id)
        memory_data = self._load_memory_from_file(agent_name, user_id=user_id)
        cache_key = self._cache_key(agent_name, user_id=user_id)

        try:
            mtime = file_path.stat().st_mtime if file_path.exists() else None
        except OSError:
            mtime = None

        with self._cache_lock:
            self._memory_cache[cache_key] = (memory_data, mtime)
        return memory_data

    def save(self, memory_data: dict[str, Any], agent_name: str | None = None, *, user_id: str | None = None) -> bool:
        """原子写（temp + rename）记忆并刷缓存。

        先浅拷贝再加 ``lastUpdated``——既不副作用修改调用方的 dict，也保证文件写成功前
        缓存引用不被悄悄更新。
        """
        file_path = self._get_memory_file_path(agent_name, user_id=user_id)
        cache_key = self._cache_key(agent_name, user_id=user_id)

        try:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            memory_data = {**memory_data, "lastUpdated": utc_now_iso_z()}

            temp_path = file_path.with_suffix(f".{uuid.uuid4().hex}.tmp")
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(memory_data, f, indent=2, ensure_ascii=False)

            temp_path.replace(file_path)

            try:
                mtime = file_path.stat().st_mtime
            except OSError:
                mtime = None

            with self._cache_lock:
                self._memory_cache[cache_key] = (memory_data, mtime)
            logger.info("Memory saved to %s", file_path)
            return True
        except OSError as e:
            logger.error("Failed to save memory file: %s", e)
            return False


_storage_instance: MemoryStorage | None = None
_storage_lock = threading.Lock()


def get_memory_storage() -> MemoryStorage:
    """返回配置的记忆存储单例（``memory.storage_class``，加载失败回退 FileMemoryStorage）。"""
    global _storage_instance
    if _storage_instance is not None:
        return _storage_instance

    with _storage_lock:
        if _storage_instance is not None:
            return _storage_instance

        config = get_memory_config()
        storage_class_path = config.storage_class

        try:
            module_path, class_name = storage_class_path.rsplit(".", 1)
            import importlib

            module = importlib.import_module(module_path)
            storage_class = getattr(module, class_name)

            if not isinstance(storage_class, type):
                raise TypeError(f"Configured memory storage '{storage_class_path}' is not a class: {storage_class!r}")
            if not issubclass(storage_class, MemoryStorage):
                raise TypeError(f"Configured memory storage '{storage_class_path}' is not a subclass of MemoryStorage")

            _storage_instance = storage_class()
        except Exception as e:
            logger.error(
                "Failed to load memory storage %s, falling back to FileMemoryStorage: %s",
                storage_class_path,
                e,
            )
            _storage_instance = FileMemoryStorage()

    return _storage_instance
