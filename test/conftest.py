"""测试套件全局配置。

职责（对应 ALIGNMENT_OUTLINE M-build）：
1. 让 ``deerflow`` 包与测试内部包（``support``）从任意工作目录可 import。
   uv 已 editable 安装 deerflow-harness，这里再补一条 sys.path 兜底，确保
   ``PYTHONPATH=.`` 与直接 ``pytest`` 两种跑法都能 import。
2. 提供 sys.modules mock 模板：当生产代码出现循环导入链时，在此预注入 mock
   打断循环（参考 deer-flow conftest 对 ``deerflow.subagents.executor`` 的处理）。
   当前 mini 尚无此问题，保留模板供后续 Phase 启用。
3. 公共 fixtures：``tmp_data_dir``（隔离的持久化数据目录）。
4. autouse 软加载 fixtures：重置全局单例 / 注入默认 user 上下文。对应模块
   尚未落地时 ``try/except ImportError`` 自动跳过，落地后自动生效。

可靠性：所有 fixture 不依赖未来模块即可工作（红线 #25 空配置可启动的测试版）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

# test/ 位于 backend 外（mini-deer-flow/test）；backend 在项目根下。
_HERE = Path(__file__).resolve().parent
_BACKEND_ROOT = _HERE.parent / "backend"

# support 包在 test/ 下，让 ``from support...`` 可导入（gate detector 在 support/ 下）
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

# 环境适配：Python 3.14 + uv 创建的 venv 会给 site-packages 下的 .pth 加 macOS
# ``hidden`` flag，而 Python 3.14 的 site.py 会跳过 hidden .pth，导致 editable
# 安装（deerflow-harness）失效、``import deerflow`` 失败。这里显式把 harness 源码
# 目录加入 sys.path，绕过坏掉的 .pth，保证测试从任意方式（``uv run`` / 直接
# ``python -m pytest``）都能 import deerflow。详见 docs/build.md「常见问题」。
_HARNESS_ROOT = _BACKEND_ROOT / "packages" / "harness"
if _HARNESS_ROOT.is_dir() and str(_HARNESS_ROOT) not in sys.path:
    sys.path.insert(0, str(_HARNESS_ROOT))

# ---------------------------------------------------------------------------
# 循环导入 mock 模板（按需启用）
# ---------------------------------------------------------------------------
# 生产代码未来可能出现循环导入链，例如（M11/M17 落地后）：
#   deerflow.subagents -> .executor -> deerflow.agents.thread_state
#     -> deerflow.agents -> lead_agent.agent -> subagent_limit_middleware
#       -> deerflow.subagents.executor   # 循环！
# 单测轻量模块时在此预注入 mock 即可解开，无需改生产代码。当前 mini 无此问题，
# 保留模板，启用时取消注释并按实际属性补全：
#
#   from unittest.mock import MagicMock
#   _executor_mock = MagicMock()
#   _executor_mock.SubagentExecutor = MagicMock
#   _executor_mock.MAX_CONCURRENT_SUBAGENTS = 3
#   sys.modules["deerflow.subagents.executor"] = _executor_mock


# ---------------------------------------------------------------------------
# 公共 fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_data_dir(tmp_path: Path) -> Path:
    """提供一个隔离的临时数据目录（base_dir），供所有持久化测试使用。

    每份测试拿到独立目录，避免跨测试状态污染（memory.json / threads / 沙箱目录等）。
    测试结束后由 pytest 的 ``tmp_path`` 自动清理。
    """
    data_dir = tmp_path / "deer-flow-data"
    data_dir.mkdir()
    return data_dir


# ---------------------------------------------------------------------------
# autouse fixtures：软加载，模块未落地时自动跳过
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_singletons_between_tests():
    """每个测试后重置可能存在的全局单例，防止跨测试污染。

    采用 ``try/except ImportError`` 软加载：对应模块（skill storage 等）尚未
    落地时不报错；落地后自动生效。新增可重置单例时在此追加即可。
    """
    resets: list = []
    try:
        from deerflow.skills.storage import reset_skill_storage

        reset_skill_storage()
        resets.append(reset_skill_storage)
    except ImportError:
        pass

    # M20 mcp：重置工具缓存 + 会话池单例（避免跨测试泄漏持久会话 / 缓存 mtime 状态）。
    try:
        from deerflow.mcp.cache import reset_mcp_tools_cache

        reset_mcp_tools_cache()
    except ImportError:
        pass

    yield

    for reset in resets:
        reset()


@pytest.fixture(autouse=True)
def _auto_user_context(request):
    """为每个测试注入默认 user 上下文（persistence/memory 读取 user_id 的来源）。

    opt-out: ``@pytest.mark.no_auto_user``。
    ``deerflow.runtime.user_context``（M3）未落地时 ``try/except`` 跳过，不报错。
    """
    if request.node.get_closest_marker("no_auto_user"):
        yield
        return

    token = None
    try:
        from deerflow.runtime.user_context import reset_current_user, set_current_user

        token = set_current_user(SimpleNamespace(id="test-user-autouse", email="test@local"))
    except ImportError:
        pass

    yield

    if token is not None:
        try:
            reset_current_user(token)
        except Exception:
            pass
