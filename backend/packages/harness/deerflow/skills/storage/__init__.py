"""``SkillStorage`` 单例 + 反射工厂（M14 skills）。

对齐 deer ``skills/storage/__init__.py``，镜像 ``sandbox/sandbox_provider.py`` 的模式。

mini 适配：用 ``deerflow.config.paths.resolve_path``（非 ``runtime_paths``）。
``reset_skill_storage`` 被 conftest autouse fixture 调用（跨测试清单例）。
"""

from __future__ import annotations

import threading

from deerflow.skills.storage.local_skill_storage import LocalSkillStorage
from deerflow.skills.storage.skill_storage import SkillStorage

_default_skill_storage: SkillStorage | None = None
_default_skill_storage_config: object | None = None  # 单例所基于的 AppConfig 身份
# 保护单例构建/重置的进程级锁（#3778）：冷启动并发调用只能构出一个实例，
# reset_skill_storage() 也不能在并发读的当口把全局清空。
_skill_storage_lock = threading.Lock()


def get_or_new_skill_storage(**kwargs) -> SkillStorage:
    """返回一个 ``SkillStorage`` 实例——新实例或进程单例。

    **新实例**（不缓存）：给了 ``skills_path``（作 host_path 覆盖）或 ``app_config``
    （按 app_config.skills 构造，尊重请求级配置而不污染进程单例）。

    **单例**（首次创建后复用）：两者都没给——用 ``get_app_config()`` 解析当前配置。
    """
    global _default_skill_storage, _default_skill_storage_config

    from deerflow.config import get_app_config
    from deerflow.config.skills_config import SkillsConfig

    def _make_storage(skills_config: SkillsConfig, *, host_path: str | None = None, **kwargs) -> SkillStorage:
        from deerflow.reflection import resolve_class

        cls = resolve_class(skills_config.use, SkillStorage)
        return cls(
            host_path=host_path if host_path is not None else str(skills_config.get_skills_path()),
            container_path=skills_config.container_path,
            **kwargs,
        )

    skills_path = kwargs.pop("skills_path", None)
    app_config = kwargs.pop("app_config", None)

    if skills_path is not None:
        if app_config is not None:
            return _make_storage(app_config.skills, host_path=str(skills_path), **kwargs)
        # 无 app_config：用默认 SkillsConfig，这样调用方给了显式 host 路径时无需读 config.yaml。
        from deerflow.config.skills_config import SkillsConfig

        return _make_storage(SkillsConfig(), host_path=str(skills_path), **kwargs)

    if app_config is not None:
        return _make_storage(app_config.skills, **kwargs)

    # 单例被手动注入（如测试）且无 config 身份（_default_skill_storage_config is None）时，
    # 跳过 get_app_config() 避免要求磁盘上有 config.yaml。
    if _default_skill_storage is not None and _default_skill_storage_config is None:
        return _default_skill_storage

    app_config_now = get_app_config()
    # 在锁内做双检构建（#3778）：竞态的冷启动调用方只能构出一个实例，
    # reset_skill_storage() 也没法在并发读的当口把全局清空。这里选择「在锁内构造」
    # ——镜像 get_memory_storage()，而非 sandbox_provider 的「锁外构造再丢弃败者」——
    # 因为 SkillStorage 没有 teardown 钩子，败者留下的孤儿实例无法被清理。
    with _skill_storage_lock:
        if _default_skill_storage is None or _default_skill_storage_config is not app_config_now:
            _default_skill_storage = _make_storage(app_config_now.skills, **kwargs)
            _default_skill_storage_config = app_config_now
        return _default_skill_storage


def reset_skill_storage() -> None:
    """清缓存单例（测试与热重载场景用）。"""
    global _default_skill_storage, _default_skill_storage_config
    with _skill_storage_lock:
        _default_skill_storage = None
        _default_skill_storage_config = None


__all__ = [
    "LocalSkillStorage",
    "SkillStorage",
    "get_or_new_skill_storage",
    "reset_skill_storage",
]
