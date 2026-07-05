"""blocking-IO gate 的 smoke 与生产锚点测试。

【当前阶段（M-build）】
本目录是 blocking-IO gate 的基础设施。真正的**生产锚点**——
``LocalSkillStorage.load_skills`` / memory JSON 读写 / sqlite 路径准备 等
必须经 ``asyncio.to_thread`` 卸载的 IO——会在对应模块落地的 Phase（M4/M13/M14）
追加到本文件，用于「锁住」那些卸载点不被误删。

当前文件只放：
1. **gate smoke** —— 验证 gate 机制本身能捕获未经 ``to_thread`` 卸载的、发自
   deerflow 模块的同步阻塞 IO（避免「绿色的 gate 其实什么都没抓」这种最坏情况）。
2. **opt-out 验证** —— ``@pytest.mark.allow_blocking_io`` 确实关闭 gate。
3. **patch 还原** —— gate 抛异常后能正确还原被 patch 的函数（不污染后续测试）。

对齐 deer-flow ``tests/blocking_io/test_gate_smoke.py``。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from support.detectors.blocking_io_runtime import BlockingError, detect_blocking_io_strict

# 被测的生产模块一律在**模块顶部**（collect 阶段，gate 未激活）import：
# 若把 import 放进 async 测试函数体，首次 import 触发模块体执行，importlib 读
# .py 的 stat 会落在「栈经过 deerflow + 在事件循环里」的判定里，被 gate 误拦，
# 最终报成迷惑性的 ModuleNotFoundError。gate 只负责观察「运行时」的同步 IO 调用。
from deerflow.config.app_config import get_app_config, load_config_from_yaml


async def test_gate_catches_unoffloaded_blocking_io_from_deerflow_module(tmp_path: Path) -> None:
    """在 async 上下文里直接调用 deerflow 模块内的同步文件 IO，gate 应抛 BlockingError。

    锚定 ``deerflow.config.app_config.load_config_from_yaml``：它内部用同步 ``open()``
    读 YAML（栈经过 ``deerflow``，故被 gate 扫描）。同类「发自 deerflow 的同步阻塞 IO
    跑在事件循环上」都会被捕获——这正是后续模块必须用 ``asyncio.to_thread`` 卸载的原因。
    """
    cfg = tmp_path / "config.yaml"
    cfg.write_text("log_level: info\n", encoding="utf-8")

    with pytest.raises(BlockingError):
        load_config_from_yaml(cfg)


@pytest.mark.allow_blocking_io
async def test_allow_blocking_io_marker_opts_out_of_gate(tmp_path: Path) -> None:
    """``@pytest.mark.allow_blocking_io`` 应真正关闭 gate。"""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("log_level: info\n", encoding="utf-8")

    # 不应抛 BlockingError
    data = load_config_from_yaml(cfg)
    assert data == {"log_level": "info"}


async def test_gate_restores_patches_after_exceptions() -> None:
    """gate 上下文即便中途抛异常，被 patch 的底层函数也必须还原。

    防止一个失败的 gate 测试污染后续测试的 ``os.stat`` 等全局状态。
    """
    original_stat = os.stat

    with pytest.raises(RuntimeError, match="boom"):
        with detect_blocking_io_strict():
            raise RuntimeError("boom")

    assert os.stat is original_stat


async def test_get_app_config_cache_hit_in_event_loop_does_no_file_io(monkeypatch) -> None:
    """运行期（事件循环里）``get_app_config`` 命中缓存时不得做同步文件 IO。

    复现并锁住 §1 冒烟发现的违规：langgraph dev 运行期 ``make_lead_agent`` 每个 run
    都会调 ``get_app_config``；旧实现即便缓存命中也会 ``stat`` + ``Path.cwd()``（热
    重载 mtime 检查），在事件循环里触发 blocking-IO 红线——langgraph 运行期的
    blockbuster 抛 ``BlockingError``，mini gate（已补 ``os.getcwd``）也会拦。

    修复后：事件循环里且已加载 → 直接返缓存，零文件 IO。本测试模拟「启动期已加载」
    （``make_checkpointer`` lifespan 已触发首次加载），在 async 上下文 + gate 下再调
    一次：应命中缓存、不抛 ``BlockingError``。若有人回退早返回，本测试会因 gate 拦
    ``os.getcwd``/``os.stat`` 而失败。
    """
    import deerflow.config.app_config as cfg_mod

    sentinel = object()
    monkeypatch.setattr(cfg_mod, "_app_config", sentinel)
    monkeypatch.setattr(cfg_mod, "_config_mtime", 1234.5)

    # gate 激活 + 在事件循环里：若 get_app_config 仍 stat/getcwd，会从 deerflow 栈抛 BlockingError。
    result = get_app_config()

    assert result is sentinel
