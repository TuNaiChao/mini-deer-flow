"""严格 blocking-IO gate 的 pytest 配置。

把 ``detect_blocking_io_strict()`` 包裹在整个 pytest item 协议外
（setup + call + teardown），让 async fixture 与 lifespan 代码里的阻塞 IO 也被
捕获，而不仅仅是测试函数体内的。

作用域：仅对路径位于 ``test/blocking_io/`` 下的用例激活。pytest 一旦加载本文件就
全局注册 hookwrapper，所以必须用显式路径过滤，避免 gate 在无关测试上误触发。

opt-out：用 ``@pytest.mark.allow_blocking_io`` 标记的用例跳过 gate
（该 marker 在 backend/pyproject.toml 的 ``[tool.pytest.ini_options].markers`` 注册）。

对齐 deer-flow ``tests/blocking_io/conftest.py``。
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest
from support.detectors.blocking_io_runtime import detect_blocking_io_strict

_BLOCKING_IO_TEST_ROOT = Path(__file__).resolve().parent


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item: pytest.Item, nextitem: pytest.Item | None) -> Generator[None, None, None]:
    if not _is_blocking_io_item(item) or item.get_closest_marker("allow_blocking_io") is not None:
        yield
        return

    with detect_blocking_io_strict():
        yield


def _is_blocking_io_item(item: pytest.Item) -> bool:
    return Path(item.path).resolve().is_relative_to(_BLOCKING_IO_TEST_ROOT)
