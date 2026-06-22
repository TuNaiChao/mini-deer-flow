"""路径配置模块。

定位项目根目录、各类文件路径，并提供**运行时路径解析**（``resolve_path`` /
``runtime_home`` / ``get_paths``）。

mini 不使用 deer 的 ``runtime_paths`` 模块——本模块统一提供运行时路径 API，
新增代码一律用本模块的 ``resolve_path`` / ``project_root``，**不得** import
``runtime_paths``（outline M0 明确替代关系）。

两套「根」概念，职责不同：
- :data:`PROJECT_ROOT` / :func:`find_project_root`：定位 **backend/ 目录**（找
  ``pyproject.toml``），用于 ``config.yaml`` / ``.env`` 路径解析。
- :func:`project_root`：**运行时项目根**（优先 ``DEER_FLOW_PROJECT_ROOT`` 环境变量，
  否则当前工作目录），用于 ``resolve_path`` 派生数据目录。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final

# 沙箱虚拟路径前缀：agent 在沙箱内看到的「用户数据根」。
# LocalSandbox 把它翻译成宿主机上按 (user_id, thread_id) 隔离的真实目录；
# AIO/Docker provisioner 则把宿主目录 bind-mount 到容器内同名路径。
# 集中定义在 config 层，sandbox/tools 与 sandbox/local 共享，避免漂移。
VIRTUAL_PATH_PREFIX: Final[str] = "/mnt/user-data"


def find_project_root() -> Path:
    """查找项目根目录（backend/ 目录，含 pyproject.toml）。"""
    current = Path.cwd()
    for parent in [current, *current.parents]:
        if (parent / "pyproject.toml").exists():
            return parent
    return current


def get_config_file() -> Path:
    """获取 config.yaml 的路径（在项目根目录的父目录，即 mini-deer-flow/）。"""
    root = find_project_root()
    config_path = root.parent / "config.yaml"
    if not config_path.exists():
        config_path = root / "config.yaml"
    return config_path


def get_env_file() -> Path:
    """获取 .env 文件路径（与 config.yaml 同级，在项目根目录的父目录）。"""
    root = find_project_root()
    env_path = root.parent / ".env"
    if not env_path.exists():
        env_path = root / ".env"
    return env_path


# ---------------------------------------------------------------------------
# 运行时路径解析（替代 deer 的 runtime_paths）
# ---------------------------------------------------------------------------


def project_root() -> Path:
    """返回调用方项目根（运行时归属文件的根目录）。

    优先 ``DEER_FLOW_PROJECT_ROOT`` 环境变量（校验存在且为目录），否则当前工作目录。
    """
    if env_root := os.getenv("DEER_FLOW_PROJECT_ROOT"):
        root = Path(env_root).resolve()
        if not root.exists():
            raise ValueError(f"DEER_FLOW_PROJECT_ROOT 设为 '{env_root}'，但解析路径 '{root}' 不存在。")
        if not root.is_dir():
            raise ValueError(f"DEER_FLOW_PROJECT_ROOT 设为 '{env_root}'，但解析路径 '{root}' 不是目录。")
        return root
    return Path.cwd().resolve()


def runtime_home() -> Path:
    """可写的 DeerFlow 状态目录（base_dir）。

    优先 ``DEER_FLOW_HOME`` 环境变量，否则 ``{project_root}/.deer-flow``。
    """
    if env_home := os.getenv("DEER_FLOW_HOME"):
        return Path(env_home).resolve()
    return project_root() / ".deer-flow"


def resolve_path(value: str | os.PathLike[str], *, base: Path | None = None) -> Path:
    """绝对路径原样返回，相对路径相对项目根解析。"""
    path = Path(value)
    if not path.is_absolute():
        path = (base or project_root()) / path
    return path.resolve()


def existing_project_file(names: tuple[str, ...]) -> Path | None:
    """返回项目根下第一个存在的具名文件。"""
    root = project_root()
    for name in names:
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None


# ---------------------------------------------------------------------------
# 运行时路径集合
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Paths:
    """运行时路径集合（base_dir 及派生目录）。

    按用户 / 按线程的数据目录由各业务模块用 ``base_dir`` + ``user_id`` / ``thread_id``
    自行拼接（见 memory / sandbox / persistence）。per-user / per-agent 的自定义 agent
    目录（M22 agents_config）通过本对象的 :meth:`user_agent_dir` / :meth:`agent_dir` 暴露，
    名称小写归一防碰撞（见方法 docstring）。
    """

    base_dir: Path

    @property
    def users_dir(self) -> Path:
        """按用户隔离的数据根目录：``{base_dir}/users``。"""
        return self.base_dir / "users"

    def user_dir(self, user_id: str) -> Path:
        """单个用户的数据目录：``{base_dir}/users/{user_id}/``。

        memory（M13）/ 自定义 agent（M22）/ 沙箱线程数据都挂在它下面。
        """
        return self.users_dir / user_id

    # ------------------------------------------------------------------
    # 自定义 agent 目录（M22 agents_config）
    # ------------------------------------------------------------------
    #
    # 两套布局：
    # - per-user（当前，新写一律走这里）：``{base_dir}/users/{user_id}/agents/{name}/``
    # - legacy 共享（per-user-isolation 之前，只读回退）：``{base_dir}/agents/{name}/``
    #
    # 名称一律 ``.lower()`` 归一再拼目录：``AGENT_NAME_PATTERN`` 允许大写字母
    # （``^[A-Za-z0-9-]+$``），但磁盘目录小写化——既对齐 deer，又防大小写碰撞
    # （macOS APFS 默认大小写不敏感，``CodeReviewer`` 与 ``codereviewer`` 会落进同一目录）。
    # 校验后的原名仍保留在 ``AgentConfig.name`` 与 config.yaml 的 ``name`` 字段里。

    @property
    def agents_dir(self) -> Path:
        """legacy 共享自定义 agent 根目录：``{base_dir}/agents/``。

        只读回退用（pre-user-isolation 安装）。新写一律走 :meth:`user_agents_dir`。
        """
        return self.base_dir / "agents"

    def agent_dir(self, name: str) -> Path:
        """legacy per-agent 目录（无用户隔离）：``{base_dir}/agents/{name.lower()}/``。"""
        return self.agents_dir / name.lower()

    def user_agents_dir(self, user_id: str) -> Path:
        """per-user 自定义 agent 根目录：``{base_dir}/users/{user_id}/agents/``。"""
        return self.user_dir(user_id) / "agents"

    def user_agent_dir(self, user_id: str, name: str) -> Path:
        """per-user per-agent 目录：``{base_dir}/users/{user_id}/agents/{name.lower()}/``。

        ``SOUL.md``（人格）+ ``config.yaml``（工具/技能白名单）写在这里。
        """
        return self.user_agents_dir(user_id) / name.lower()

    # ------------------------------------------------------------------
    # 记忆文件（M13 memory）——派生自上面的 agent / user 目录
    # ------------------------------------------------------------------
    # per-user 全局记忆：``{base_dir}/users/{user_id}/memory.json``
    # per-user per-agent 记忆：``{base_dir}/users/{user_id}/agents/{name}/memory.json``
    # legacy 共享记忆（只读回退）：``{base_dir}/memory.json`` / ``{base_dir}/agents/{name}/memory.json``

    @property
    def memory_file(self) -> Path:
        """legacy 全局记忆文件（无用户隔离）：``{base_dir}/memory.json``。只读回退。"""
        return self.base_dir / "memory.json"

    def user_memory_file(self, user_id: str) -> Path:
        """per-user 全局记忆文件：``{base_dir}/users/{user_id}/memory.json``。"""
        return self.user_dir(user_id) / "memory.json"

    def agent_memory_file(self, name: str) -> Path:
        """legacy per-agent 记忆文件（无用户隔离）：``{base_dir}/agents/{name.lower()}/memory.json``。"""
        return self.agent_dir(name) / "memory.json"

    def user_agent_memory_file(self, user_id: str, name: str) -> Path:
        """per-user per-agent 记忆文件：``{base_dir}/users/{user_id}/agents/{name.lower()}/memory.json``。"""
        return self.user_agent_dir(user_id, name) / "memory.json"

    # ------------------------------------------------------------------
    # 线程用户数据目录（M10 sandbox / M23 uploads 共用）
    # ------------------------------------------------------------------
    # 单个线程的数据根：``{base_dir}/users/{user_id}/threads/{thread_id}/user-data``
    # 下设 ``workspace`` / ``uploads`` / ``outputs`` 三个子目录，对应沙箱虚拟路径
    # ``/mnt/user-data/{workspace,uploads,outputs}``（见 sandbox/local）。uploads 由
    # M23 uploads 模块写入（文件上传），outputs 由 agent 工具写入（present_files）。
    # mini 的「模块自拼路径」约定改为集中在本对象上，避免 uploads / sandbox 各拼一份
    # 造成布局漂移（M10b local_sandbox 的 _thread_user_data_root 已改为委托本方法）。

    def thread_user_data_dir(self, user_id: str, thread_id: str) -> Path:
        """某线程的用户数据根目录：``{base_dir}/users/{user_id}/threads/{thread_id}/user-data``。

        与 deer 的 ``paths.sandbox_user_data_dir`` 等价。``sandbox/local`` 的
        ``_thread_user_data_root`` 从这里反推 thread_id（靠 parent.parent.name），
        因此本布局是 **唯一真相源**——改这里要同步改注释里的反推约束。
        """
        return self.users_dir / user_id / "threads" / thread_id / "user-data"

    def sandbox_uploads_dir(self, thread_id: str, *, user_id: str) -> Path:
        """某线程的上传目录（**无副作用**，只算路径）：``thread_user_data_dir / "uploads"``。

        对应沙箱虚拟路径 ``/mnt/user-data/uploads``（agent 可见），物理上按
        (user_id, thread_id) 隔离。需要建目录时调 :meth:`ensure` 侧或在 uploads 模块
        里 mkdir。与 deer 的 ``paths.sandbox_uploads_dir`` 同名等价。
        """
        return self.thread_user_data_dir(user_id, thread_id) / "uploads"

    def sandbox_work_dir(self, thread_id: str, *, user_id: str) -> Path:
        """某线程的工作目录（**无副作用**，只算路径）：``thread_user_data_dir / "workspace"``。

        对应沙箱虚拟路径 ``/mnt/user-data/workspace``（agent 可见）。与 deer 的
        ``paths.sandbox_work_dir`` 同名等价。M16 ThreadDataMiddleware / local_sandbox
        共用本方法，是工作目录的**唯一真相源**。
        """
        return self.thread_user_data_dir(user_id, thread_id) / "workspace"

    def sandbox_outputs_dir(self, thread_id: str, *, user_id: str) -> Path:
        """某线程的输出目录（**无副作用**，只算路径）：``thread_user_data_dir / "outputs"``。

        对应沙箱虚拟路径 ``/mnt/user-data/outputs``（agent 可见）。present_files 工具
        与 ToolOutputBudgetMiddleware 外置大输出时写这里。与 deer 的
        ``paths.sandbox_outputs_dir`` 同名等价。
        """
        return self.thread_user_data_dir(user_id, thread_id) / "outputs"

    def ensure_thread_dirs(self, thread_id: str, *, user_id: str) -> Path:
        """确保某线程的 workspace/uploads/outputs 三目录存在，返回用户数据根。

        是「建线程目录」动作的**唯一真相源**——sandbox 的 ensure_thread_dirs 委托它，
        ThreadDataMiddleware（lazy_init=False）也用它，避免两处 mkdir 逻辑漂移。
        """
        root = self.thread_user_data_dir(user_id, thread_id)
        for sub in ("workspace", "uploads", "outputs"):
            (root / sub).mkdir(parents=True, exist_ok=True)
        return root


def get_paths() -> Paths:
    """返回运行时路径集合（base_dir = ``runtime_home()``）。

    不做缓存：``runtime_home`` / ``project_root`` 依赖环境变量，缓存会让测试改 env
    后取到旧值。每次调用都是几个轻量 Path 操作，开销可忽略。
    """
    return Paths(base_dir=runtime_home())


# 项目根目录（backend/，向后兼容现有引用）
PROJECT_ROOT = find_project_root()
