"""AIO 沙箱模块测试（M10b）。

hermetic 约定（对齐 docs/testing-setup.md）：
- ``agent_sandbox`` SDK 未安装（CI 不装 aio_sandbox extra）→ soft-load 分支天然可测；
  测带 SDK 的行为时 monkeypatch 掉 ``aio_sandbox`` 模块的 ``AioSandboxClient`` / ``_HAS_AGENT_SANDBOX``。
- 不碰真 Docker / K8s：``LocalContainerBackend`` 的 ``subprocess.run``、``RemoteSandboxBackend`` 的
  ``requests``、provider 的 backend 全用 fake 替换。
- ``DEER_FLOW_HOME`` 指向 tmp_path，跨进程文件锁建临时盘。
- provider 构造会注册信号处理器 + 起 idle 线程——测试里 monkeypatch 成 no-op + idle_timeout=0，
  避免干扰 pytest 信号处理 / 留后台线程。

覆盖：SandboxInfo / utils.network 端口分配 / backend 就绪轮询 + ABC / soft-load ImportError /
AioSandbox（mock SDK：exec + ErrorObservation 重试 + download 穿越/前缀/上限 + glob/grep +
close 幂等）/ RemoteSandboxBackend（mock requests）/ LocalContainerBackend（mock subprocess：端口
重试 + 名冲突发现 + list_running 批量 inspect）/ AioSandboxProvider（暖池 + 跨进程发现 + replicas
淘汰 + idle 回收 + reconcile 孤儿 + shutdown 幂等 + remote vs local backend 选择）。
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from deerflow.community.aio_sandbox import (
    AioSandbox,
    AioSandboxProvider,
    LocalContainerBackend,
    RemoteSandboxBackend,
    SandboxBackend,
    SandboxInfo,
)
from deerflow.community.aio_sandbox import aio_sandbox as aio_sandbox_module
from deerflow.community.aio_sandbox import aio_sandbox_provider as provider_module
from deerflow.community.aio_sandbox import local_backend as local_backend_module
from deerflow.community.aio_sandbox import remote_backend as remote_backend_module
from deerflow.utils.network import PortAllocator, get_free_port, release_port

# ===========================================================================
# fixtures
# ===========================================================================


@pytest.fixture()
def home_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """DEER_FLOW_HOME → tmp_path，让跨进程文件锁 / 线程目录建临时盘。"""
    home = tmp_path / "deer-flow-home"
    home.mkdir()
    monkeypatch.setenv("DEER_FLOW_HOME", str(home))
    return home


@pytest.fixture(autouse=True)
def _reset_provider_singleton():
    """每个测试后重置沙箱 provider 单例，防跨测试污染。"""
    yield
    try:
        from deerflow.sandbox.sandbox_provider import reset_sandbox_provider

        reset_sandbox_provider()
    except Exception:
        pass


# ===========================================================================
# 1. SandboxInfo
# ===========================================================================


def test_sandbox_info_defaults_and_roundtrip():
    info = SandboxInfo(sandbox_id="abc", sandbox_url="http://localhost:8080")
    assert info.container_name is None
    assert info.container_id is None
    assert info.created_at > 0
    d = info.to_dict()
    assert d["sandbox_id"] == "abc"
    restored = SandboxInfo.from_dict(d)
    assert restored.sandbox_id == info.sandbox_id
    assert restored.sandbox_url == info.sandbox_url


def test_sandbox_info_from_dict_legacy_base_url():
    """旧字段名 base_url 兼容 → sandbox_url。"""
    info = SandboxInfo.from_dict({"sandbox_id": "x", "base_url": "http://legacy:8080"})
    assert info.sandbox_url == "http://legacy:8080"


def test_sandbox_info_from_dict_missing_created_at_defaults_now():
    info = SandboxInfo.from_dict({"sandbox_id": "x", "sandbox_url": "http://x"})
    assert info.created_at > 0


# ===========================================================================
# 2. utils/network 端口分配
# ===========================================================================


def test_port_allocator_allocate_then_release_real_port():
    alloc = PortAllocator()
    port = alloc.allocate(start_port=20000, max_range=500)
    assert isinstance(port, int)
    # 同一端口再分配（未释放）应跳过它。
    port2 = alloc.allocate(start_port=port, max_range=500)
    assert port2 != port
    alloc.release(port)
    alloc.release(port2)


def test_port_allocator_allocate_context_releases():
    alloc = PortAllocator()
    with alloc.allocate_context(start_port=21000, max_range=500) as port:
        assert port in alloc._reserved_ports
    assert port not in alloc._reserved_ports


def test_port_allocator_no_port_raises():
    alloc = PortAllocator()
    # 全占用区间：bind 一个端口占住，再要求同端口必失败。
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("0.0.0.0", 0))
    bound = s.getsockname()[1]
    s.listen()
    try:
        with pytest.raises(RuntimeError):
            alloc.allocate(start_port=bound, max_range=1)
    finally:
        s.close()


def test_global_get_free_port_release():
    port = get_free_port(start_port=22000, max_range=500)
    assert port in _global_reserved() or port > 0  # 分配成功
    release_port(port)


def _global_reserved() -> set[int]:
    from deerflow.utils import network

    return network._global_port_allocator._reserved_ports  # type: ignore[attr-defined]


# ===========================================================================
# 3. backend.py：就绪轮询 + ABC
# ===========================================================================


def test_wait_for_sandbox_ready_success(monkeypatch: pytest.MonkeyPatch):
    """requests.get 返回 200 → 立即就绪。"""
    from deerflow.community.aio_sandbox import backend as backend_module

    fake_resp = SimpleNamespace(status_code=200)
    monkeypatch.setattr(backend_module.requests, "get", lambda *a, **k: fake_resp)
    assert backend_module.wait_for_sandbox_ready("http://x", timeout=5) is True


def test_wait_for_sandbox_ready_timeout(monkeypatch: pytest.MonkeyPatch):
    """requests.get 一直非 200 → 超时返回 False。"""
    from deerflow.community.aio_sandbox import backend as backend_module

    fake_resp = SimpleNamespace(status_code=503)

    class FakeReq:
        exceptions = backend_module.requests.exceptions

        def get(self, *a, **k):
            return fake_resp

    monkeypatch.setattr(backend_module, "requests", FakeReq())
    # timeout=1 配合 sleep(1) 轮询，应很快返回 False。
    assert backend_module.wait_for_sandbox_ready("http://x", timeout=1) is False


def test_sandbox_backend_is_abstract():
    """SandboxBackend 有抽象方法，不能直接实例化。"""
    assert SandboxBackend.__abstractmethods__
    with pytest.raises(TypeError):
        SandboxBackend()  # type: ignore[abstract]


def test_sandbox_backend_list_running_default_empty():
    """默认 list_running 返回空（remote backend 覆盖它）。"""

    class MinimalBackend(SandboxBackend):
        def create(self, thread_id, sandbox_id, extra_mounts=None):
            return SandboxInfo(sandbox_id=sandbox_id, sandbox_url="http://x")

        def destroy(self, info):
            pass

        def is_alive(self, info):
            return True

        def discover(self, sandbox_id):
            return None

    assert MinimalBackend().list_running() == []


# ===========================================================================
# 4. soft-load：agent_sandbox 缺包时 AioSandbox 抛 ImportError
# ===========================================================================


def test_aio_sandbox_raises_without_sdk(monkeypatch: pytest.MonkeyPatch):
    """agent_sandbox 缺包（CI 默认）→ AioSandbox.__init__ 抛带安装提示的 ImportError。"""
    monkeypatch.setattr(aio_sandbox_module, "_HAS_AGENT_SANDBOX", False)
    with pytest.raises(ImportError) as exc_info:
        AioSandbox("abc", "http://localhost:8080")
    assert "agent-sandbox" in str(exc_info.value)


# ===========================================================================
# 5. AioSandbox（mock SDK client）
# ===========================================================================


class _FakeData:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _FakeResult:
    def __init__(self, **data):
        self.data = _FakeData(**data) if data else None


class _FakeFileApi:
    def __init__(self):
        self.files_data = SimpleNamespace(files=["/mnt/user-data/workspace/a.py", "/mnt/user-data/workspace/b.txt"])
        self.list_entries = SimpleNamespace(
            files=[
                SimpleNamespace(path="/mnt/user-data/workspace/a.py", is_directory=False),
                SimpleNamespace(path="/mnt/user-data/workspace/sub", is_directory=True),
            ]
        )
        self.read_content = "hello"
        self.search_result = SimpleNamespace(data=SimpleNamespace(line_numbers=[3], matches=["    return 'bar'"]))

    def read_file(self, file):
        return _FakeResult(content=self.read_content)

    def write_file(self, file, content, encoding=None):
        self.last_write = (file, content, encoding)
        return _FakeResult()

    def find_files(self, path, glob):
        return _FakeResult(files=self.files_data.files)

    def list_path(self, path, recursive=True, show_hidden=False):
        return _FakeResult(files=self.list_entries.files)

    def search_in_file(self, file, regex):
        return self.search_result

    def download_file(self, path):
        yield b"chunk1"
        yield b"chunk2"


class _FakeShellApi:
    def __init__(self, outputs: list[str] | None = None):
        self._outputs = outputs or []
        self.calls = []
        self.created_sessions: list[str] = []
        self.cleaned_sessions: list[str] = []

    def exec_command(self, command, id=None, no_change_timeout=None):
        self.calls.append(command)
        out = self._outputs.pop(0) if self._outputs else "ok"
        return _FakeResult(output=out)

    def create_session(self, id=None):
        # #3730/#3786 恢复路径：ErrorObservation 重试现在显式 create/cleanup session。
        self.created_sessions.append(id)

    def cleanup_session(self, id):
        self.cleaned_sessions.append(id)


class _FakeSandboxApi:
    def get_context(self):
        return SimpleNamespace(home_dir="/root")


class _FakeClient:
    """模拟 agent_sandbox SDK 的 client（含 file/shell/sandbox 子 API + close 属性链）。"""

    def __init__(self, shell: _FakeShellApi | None = None, file: _FakeFileApi | None = None):
        self.shell = shell or _FakeShellApi()
        self.file = file or _FakeFileApi()
        self.sandbox = _FakeSandboxApi()
        # close() 走属性链：_client_wrapper.httpx_client.httpx_client
        self._client_wrapper = SimpleNamespace(httpx_client=SimpleNamespace(httpx_client=SimpleNamespace(close=lambda: None)))


@pytest.fixture()
def fake_sdk(monkeypatch: pytest.MonkeyPatch):
    """注入 fake agent_sandbox client，让 AioSandbox 可实例化。"""
    monkeypatch.setattr(aio_sandbox_module, "_HAS_AGENT_SANDBOX", True)
    created_clients: list[_FakeClient] = []

    def factory(base_url, timeout):
        c = _FakeClient()
        created_clients.append(c)
        return c

    monkeypatch.setattr(aio_sandbox_module, "AioSandboxClient", factory)
    return created_clients


def test_aio_sandbox_execute_command(fake_sdk):
    sbx = AioSandbox("a1", "http://localhost:8080")
    assert sbx.execute_command("echo hi") == "ok"
    assert fake_sdk[0].shell.calls == ["echo hi"]


def test_aio_sandbox_execute_command_empty_output_returns_placeholder(fake_sdk):
    sbx = AioSandbox("a1", "http://localhost:8080")
    sbx._client.shell = _FakeShellApi(outputs=[""])
    assert sbx.execute_command("x") == "(no output)"


def test_aio_sandbox_execute_command_error_observation_retry(fake_sdk):
    """输出含 ErrorObservation 签名 → 在新 session 重试，并显式建/拆恢复 session（不泄漏）。"""
    sbx = AioSandbox("a1", "http://localhost:8080")
    sbx._client.shell = _FakeShellApi(outputs=["'ErrorObservation' object has no attribute 'exit_code'", "recovered"])
    out = sbx.execute_command("bad")
    assert out == "recovered"
    # 第二次调用带 id 参数（新 session）。
    assert len(sbx._client.shell.calls) == 2
    # 恢复 session 被显式创建并在 finally 里清理——不泄漏。
    assert len(sbx._client.shell.created_sessions) == 1
    assert sbx._client.shell.created_sessions == sbx._client.shell.cleaned_sessions


def test_aio_sandbox_read_file(fake_sdk):
    sbx = AioSandbox("a1", "http://localhost:8080")
    assert sbx.read_file("/mnt/user-data/workspace/a.py") == "hello"


def test_aio_sandbox_download_file_bytes(fake_sdk):
    sbx = AioSandbox("a1", "http://localhost:8080")
    data = sbx.download_file("/mnt/user-data/workspace/blob.bin")
    assert data == b"chunk1chunk2"


def test_aio_sandbox_download_file_rejects_traversal(fake_sdk):
    sbx = AioSandbox("a1", "http://localhost:8080")
    with pytest.raises(PermissionError):
        sbx.download_file("/mnt/user-data/workspace/../../etc/passwd")


def test_aio_sandbox_download_file_rejects_outside_user_data(fake_sdk):
    sbx = AioSandbox("a1", "http://localhost:8080")
    with pytest.raises(PermissionError):
        sbx.download_file("/etc/passwd")


def test_aio_sandbox_download_file_size_limit(fake_sdk, monkeypatch: pytest.MonkeyPatch):
    """超 100MB → OSError(EFBIG)。"""
    monkeypatch.setattr(aio_sandbox_module, "_MAX_DOWNLOAD_SIZE", 10)
    sbx = AioSandbox("a1", "http://localhost:8080")
    big_file = _FakeFileApi()
    big_file.download_file = lambda path: (yield b"x" * 100)
    sbx._client.file = big_file
    with pytest.raises(OSError):
        sbx.download_file("/mnt/user-data/workspace/big.bin")


def test_aio_sandbox_list_dir(fake_sdk):
    sbx = AioSandbox("a1", "http://localhost:8080")
    sbx._client.shell = _FakeShellApi(outputs=["/mnt/user-data/workspace/a.py\n/mnt/user-data/workspace/sub"])
    entries = sbx.list_dir("/mnt/user-data/workspace")
    assert "/mnt/user-data/workspace/a.py" in entries
    assert "/mnt/user-data/workspace/sub" in entries


def test_aio_sandbox_write_file_append(fake_sdk):
    sbx = AioSandbox("a1", "http://localhost:8080")
    sbx._client.file.read_content = "old-"
    sbx.write_file("/mnt/user-data/workspace/a.txt", "new", append=True)
    # append=True 先读旧内容再拼。
    assert sbx._client.file.last_write[1] == "old-new"


def test_aio_sandbox_write_file_overwrite(fake_sdk):
    sbx = AioSandbox("a1", "http://localhost:8080")
    sbx.write_file("/mnt/user-data/workspace/a.txt", "fresh", append=False)
    assert fake_sdk[0].file.last_write[1] == "fresh"


def test_aio_sandbox_glob_filters_and_matches(fake_sdk):
    sbx = AioSandbox("a1", "http://localhost:8080")
    matches, truncated = sbx.glob("/mnt/user-data/workspace", "**/*.py")
    assert "/mnt/user-data/workspace/a.py" in matches
    assert truncated is False


def test_aio_sandbox_grep(fake_sdk):
    sbx = AioSandbox("a1", "http://localhost:8080")
    matches, truncated = sbx.grep("/mnt/user-data/workspace", "return")
    assert len(matches) == 1
    assert matches[0].line_number == 3
    assert "return" in matches[0].line


def test_aio_sandbox_grep_invalid_regex(fake_sdk):
    sbx = AioSandbox("a1", "http://localhost:8080")
    with pytest.raises(__import__("re").error):
        sbx.grep("/mnt/user-data/workspace", "[unclosed")


def test_aio_sandbox_update_file_base64(fake_sdk):
    sbx = AioSandbox("a1", "http://localhost:8080")
    sbx.update_file("/mnt/user-data/workspace/bin.dat", b"BIN")
    file_path, content, encoding = fake_sdk[0].file.last_write
    assert encoding == "base64"
    import base64

    assert base64.b64decode(content) == b"BIN"


def test_aio_sandbox_close_idempotent(fake_sdk):
    sbx = AioSandbox("a1", "http://localhost:8080")
    sbx.close()
    sbx.close()  # 幂等：第二次不抛。
    # close 后 client 引用被置 None。
    assert sbx._client is None


def test_aio_sandbox_home_dir_lazy(fake_sdk):
    sbx = AioSandbox("a1", "http://localhost:8080")
    assert sbx.home_dir == "/root"


# ===========================================================================
# 6. RemoteSandboxBackend（mock requests）
# ===========================================================================


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self._json = json_data if json_data is not None else {}
        self.text = text

    def json(self):
        return self._json

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeRequests:
    """模拟 requests 模块（remote_backend 用）。"""

    exceptions = SimpleNamespace(RequestException=Exception)

    def __init__(self):
        self.routes: dict[tuple[str, str], _FakeResponse] = {}

    def add(self, method: str, url_suffix: str, resp: _FakeResponse):
        self.routes[(method, url_suffix)] = resp

    def _call(self, method, url, **kwargs):
        for (m, suffix), resp in self.routes.items():
            if m == method and url.endswith(suffix):
                return resp
        return _FakeResponse(status_code=404, text="not found")

    def get(self, url, **kwargs):
        return self._call("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._call("POST", url, **kwargs)

    def delete(self, url, **kwargs):
        return self._call("DELETE", url, **kwargs)


@pytest.fixture()
def fake_requests(monkeypatch: pytest.MonkeyPatch):
    fr = _FakeRequests()
    monkeypatch.setattr(remote_backend_module, "requests", fr)
    return fr


def test_remote_backend_create(fake_requests, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("deerflow.community.aio_sandbox.remote_backend.get_effective_user_id", lambda: "u1")
    fake_requests.add("POST", "/api/sandboxes", _FakeResponse(200, {"sandbox_url": "http://k3s:30001"}))
    backend = RemoteSandboxBackend("http://provisioner:8002")
    info = backend.create("t1", "abc123")
    assert info.sandbox_url == "http://k3s:30001"
    assert backend.provisioner_url == "http://provisioner:8002"


def test_remote_backend_is_alive(fake_requests):
    fake_requests.add("GET", "/api/sandboxes/abc", _FakeResponse(200, {"status": "Running"}))
    backend = RemoteSandboxBackend("http://p:8002")
    assert backend.is_alive(SandboxInfo("abc", "http://x")) is True


def test_remote_backend_is_alive_404_false(fake_requests):
    backend = RemoteSandboxBackend("http://p:8002")
    # 404（路由未注册）→ False。
    assert backend.is_alive(SandboxInfo("missing", "http://x")) is False


def test_remote_backend_discover_found(fake_requests):
    fake_requests.add("GET", "/api/sandboxes/abc", _FakeResponse(200, {"sandbox_url": "http://k3s:30001"}))
    backend = RemoteSandboxBackend("http://p:8002")
    info = backend.discover("abc")
    assert info is not None
    assert info.sandbox_url == "http://k3s:30001"


def test_remote_backend_discover_404_none(fake_requests):
    backend = RemoteSandboxBackend("http://p:8002")
    assert backend.discover("missing") is None


def test_remote_backend_list_running(fake_requests):
    fake_requests.add(
        "GET",
        "/api/sandboxes",
        _FakeResponse(200, {"sandboxes": [{"sandbox_id": "a", "sandbox_url": "http://x:1"}, {"sandbox_id": "b", "sandbox_url": "http://x:2"}]}),
    )
    backend = RemoteSandboxBackend("http://p:8002")
    infos = backend.list_running()
    assert len(infos) == 2
    assert {i.sandbox_id for i in infos} == {"a", "b"}


def test_remote_backend_destroy_swallow_error(fake_requests):
    """destroy 失败（非 2xx）只 warn 不抛。"""
    backend = RemoteSandboxBackend("http://p:8002")
    backend.destroy(SandboxInfo("abc", "http://x"))  # 404 路由 → 不抛


# ===========================================================================
# 7. LocalContainerBackend（mock subprocess）
# ===========================================================================


@pytest.fixture()
def local_backend(monkeypatch: pytest.MonkeyPatch):
    """造一个 LocalContainerBackend，runtime 强制 docker（避开 Apple Container 探测）。"""
    monkeypatch.setattr(LocalContainerBackend, "_detect_runtime", lambda self: "docker")
    return LocalContainerBackend(
        image="img:latest",
        base_port=30000,
        container_prefix="test-sandbox",
        config_mounts=[],
        environment={"K": "V"},
    )


def test_local_backend_detect_runtime_default_docker(monkeypatch: pytest.MonkeyPatch):
    """非 macOS 或无 Apple Container → docker。"""
    backend = LocalContainerBackend(image="i", base_port=1, container_prefix="p", config_mounts=[], environment={})
    # CI（Linux/macOS 无 container CLI）应回退 docker。
    assert backend.runtime in {"docker", "container"}


def test_local_backend_is_alive_uses_inspect(local_backend, monkeypatch: pytest.MonkeyPatch):
    def fake_run(cmd, **kw):
        return SimpleNamespace(returncode=0, stdout="true\n", stderr="")

    monkeypatch.setattr(local_backend_module.subprocess, "run", fake_run)
    assert local_backend.is_alive(SandboxInfo("a", "http://x", container_name="test-sandbox-a")) is True


def test_local_backend_is_alive_not_running(local_backend, monkeypatch: pytest.MonkeyPatch):
    def fake_run(cmd, **kw):
        return SimpleNamespace(returncode=0, stdout="false\n", stderr="")

    monkeypatch.setattr(local_backend_module.subprocess, "run", fake_run)
    assert local_backend.is_alive(SandboxInfo("a", "http://x", container_name="test-sandbox-a")) is False


def test_local_backend_create_success(local_backend, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """create 成功：分配端口 + start_container + 返回 SandboxInfo。"""
    monkeypatch.setattr(local_backend_module, "get_free_port", lambda start_port=8080, max_range=100: 31000)

    started: list[list[str]] = []

    def fake_run(cmd, **kw):
        if "run" in cmd:
            started.append(cmd)
            return SimpleNamespace(returncode=0, stdout="container-id-123\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(local_backend_module.subprocess, "run", fake_run)
    info = local_backend.create("t1", "abc12345")
    assert info.container_id == "container-id-123"
    assert info.container_name == "test-sandbox-abc12345"
    assert "31000" in info.sandbox_url


def test_local_backend_create_port_conflict_retries(local_backend, monkeypatch: pytest.MonkeyPatch):
    """Docker 报 port already allocated → 换端口重试。"""
    ports = iter([31000, 31001, 31002])
    monkeypatch.setattr(local_backend_module, "get_free_port", lambda start_port=8080, max_range=100: next(ports))
    attempts = {"n": 0}

    def fake_run(cmd, **kw):
        if "run" in cmd:
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise subprocess.CalledProcessError(1, cmd, stderr="Bind for 0.0.0.0:31000 failed: port is already allocated")
            return SimpleNamespace(returncode=0, stdout="cid\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(local_backend_module.subprocess, "run", fake_run)
    local_backend.create("t1", "abc")
    assert attempts["n"] == 2  # 第一次冲突，第二次成功


def test_local_backend_create_name_conflict_discovers(local_backend, monkeypatch: pytest.MonkeyPatch):
    """容器名冲突 → 走 discover 收养。"""
    monkeypatch.setattr(local_backend_module, "get_free_port", lambda start_port=8080, max_range=100: 31000)

    def fake_run(cmd, **kw):
        if "run" in cmd:
            raise subprocess.CalledProcessError(1, cmd, stderr="Conflict. The container name is already in use by container")
        if "inspect" in cmd and "-f" in cmd:
            return SimpleNamespace(returncode=0, stdout="true\n", stderr="")
        if cmd[1] == "port":
            return SimpleNamespace(returncode=0, stdout="0.0.0.0:31005\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(local_backend_module.subprocess, "run", fake_run)
    monkeypatch.setattr(local_backend_module, "wait_for_sandbox_ready", lambda url, timeout=30: True)
    info = local_backend.create("t1", "abc")
    assert info is not None
    assert "31005" in info.sandbox_url


def test_local_backend_discover_not_running_returns_none(local_backend, monkeypatch: pytest.MonkeyPatch):
    def fake_run(cmd, **kw):
        return SimpleNamespace(returncode=1, stdout="", stderr="Error: No such object: test-sandbox-abc")

    monkeypatch.setattr(local_backend_module.subprocess, "run", fake_run)
    assert local_backend.discover("abc") is None


def test_local_backend_destroy_stops_and_releases_port(local_backend, monkeypatch: pytest.MonkeyPatch):
    stopped: list[list[str]] = []

    def fake_run(cmd, **kw):
        if "stop" in cmd:
            stopped.append(cmd)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(local_backend_module.subprocess, "run", fake_run)
    released: list[int] = []
    monkeypatch.setattr(local_backend_module, "release_port", lambda p: released.append(p))
    local_backend.destroy(SandboxInfo("abc", "http://localhost:31000", container_id="cid"))
    assert stopped and stopped[0][1] == "stop"
    assert released == [31000]


def test_local_backend_list_running_batch_inspect(local_backend, monkeypatch: pytest.MonkeyPatch):
    """list_running: ps 列名 + 批量 inspect 取 created/port。"""
    ps_called = {"n": 0}

    def fake_run(cmd, **kw):
        if "ps" in cmd:
            ps_called["n"] += 1
            return SimpleNamespace(returncode=0, stdout="test-sandbox-a\ntest-sandbox-b\n", stderr="")
        if "inspect" in cmd and "-f" not in cmd:
            # 批量 inspect：返回 JSON 数组。
            payload = [
                {"Name": "/test-sandbox-a", "Created": "2026-01-01T00:00:00Z", "NetworkSettings": {"Ports": {"8080/tcp": [{"HostPort": "31001"}]}}},
                {"Name": "/test-sandbox-b", "Created": "2026-01-02T00:00:00Z", "NetworkSettings": {"Ports": {"8080/tcp": [{"HostPort": "31002"}]}}},
            ]
            return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(local_backend_module.subprocess, "run", fake_run)
    infos = local_backend.list_running()
    ids = {i.sandbox_id for i in infos}
    assert ids == {"a", "b"}
    assert any("31001" in i.sandbox_url for i in infos)
    assert ps_called["n"] == 1  # 单次 ps


# ===========================================================================
# 8. AioSandboxProvider（核心：暖池 / 跨进程发现 / replicas / idle / shutdown）
# ===========================================================================


class FakeBackend(SandboxBackend):
    """可控的假 backend：记录调用，返回预设 SandboxInfo。"""

    def __init__(self):
        self.created: list[str] = []
        self.destroyed: list[str] = []
        self.alive_ids: set[str] = set()
        self.discover_map: dict[str, SandboxInfo] = {}
        self.list_running_result: list[SandboxInfo] = []
        self.next_port = 40000
        self.create_calls: list[Any] = []

    def create(self, thread_id, sandbox_id, extra_mounts=None):
        self.created.append(sandbox_id)
        self.create_calls.append((thread_id, sandbox_id, extra_mounts))
        self.alive_ids.add(sandbox_id)
        info = SandboxInfo(sandbox_id=sandbox_id, sandbox_url=f"http://localhost:{self.next_port}", container_name=f"c-{sandbox_id}")
        self.next_port += 1
        return info

    def destroy(self, info):
        self.destroyed.append(info.sandbox_id)
        self.alive_ids.discard(info.sandbox_id)

    def is_alive(self, info):
        return info.sandbox_id in self.alive_ids

    def discover(self, sandbox_id):
        info = self.discover_map.get(sandbox_id)
        if info is not None:
            self.alive_ids.add(sandbox_id)
        return info

    def list_running(self):
        return list(self.list_running_result)


class _CloseRecordingSandbox:
    """假的 AioSandbox：记录 close 调用。"""

    def __init__(self, id, base_url, home_dir=None):
        self.id = id
        self.base_url = base_url
        self.closed = False

    def close(self):
        self.closed = True


@pytest.fixture()
def provider(home_env: Path, monkeypatch: pytest.MonkeyPatch):
    """造一个 hermetic AioSandboxProvider：fake backend + no signal + idle_timeout=0 + 真 AioSandbox 替身。"""
    # 关掉信号注册 + 就绪检查恒真 + AioSandbox 替身。
    monkeypatch.setattr(provider_module.AioSandboxProvider, "_register_signal_handlers", lambda self: None)
    monkeypatch.setattr(provider_module, "wait_for_sandbox_ready", lambda url, timeout=30: True)
    monkeypatch.setattr(provider_module, "wait_for_sandbox_ready_async", lambda url, timeout=30, poll_interval=1.0: _async_true())
    monkeypatch.setattr(provider_module, "AioSandbox", _CloseRecordingSandbox)

    fake_backend_holder: dict[str, FakeBackend] = {}

    def make_provider(**config_overrides):
        cfg = {
            "image": "img",
            "port": 40000,
            "container_prefix": "test",
            "idle_timeout": 0,  # 不起 idle 线程
            "replicas": 3,
            "mounts": [],
            "environment": {},
            "provisioner_url": "",
        }
        cfg.update(config_overrides)

        def fake_load_config(self):
            return cfg

        backend = FakeBackend()

        def fake_create_backend(self):
            return backend

        monkeypatch.setattr(provider_module.AioSandboxProvider, "_load_config", fake_load_config)
        monkeypatch.setattr(provider_module.AioSandboxProvider, "_create_backend", fake_create_backend)
        p = provider_module.AioSandboxProvider()
        fake_backend_holder["backend"] = backend
        return p, backend

    return make_provider


async def _async_true() -> bool:
    return True


def test_deterministic_sandbox_id_stable():
    """同 thread_id → 同 sandbox_id；不同 thread → 不同。"""
    a = AioSandboxProvider._deterministic_sandbox_id("t1")
    b = AioSandboxProvider._deterministic_sandbox_id("t1")
    c = AioSandboxProvider._deterministic_sandbox_id("t2")
    assert a == b
    assert a != c
    assert len(a) == 8


def test_provider_acquire_creates_then_reuses(provider):
    p, backend = provider()
    sid1 = p.acquire("t1")
    assert sid1.startswith  # 返回 id
    assert backend.created == [sid1]
    # 第二次同 thread → 复用进程内缓存，不新建。
    sid2 = p.acquire("t1")
    assert sid2 == sid1
    assert backend.created == [sid1]  # 仍只建过 1 个


def test_provider_get_returns_sandbox(provider):
    p, backend = provider()
    sid = p.acquire("t1")
    sbx = p.get(sid)
    assert sbx is not None
    assert sbx.id == sid


def test_provider_get_unknown_returns_none(provider):
    p, backend = provider()
    assert p.get("nonexistent") is None


def test_provider_release_to_warm_pool(provider):
    p, backend = provider()
    sid = p.acquire("t1")
    p.release(sid)
    # release 后 get（活跃 map）取不到，但暖池里有。
    assert p.get(sid) is None
    assert sid in p._warm_pool


def test_provider_release_then_acquire_reclaims_warm(provider):
    """release 入暖池后，再 acquire 同 thread → 暖池回收（不新建）。"""
    p, backend = provider()
    sid1 = p.acquire("t1")
    p.release(sid1)
    sid2 = p.acquire("t1")
    assert sid1 == sid2  # 暖池回收，同一 id
    assert backend.created == [sid1]  # 没新建


def test_provider_destroy_closes_and_backend_destroys(provider):
    p, backend = provider()
    sid = p.acquire("t1")
    sbx = p.get(sid)
    p.destroy(sid)
    assert backend.destroyed == [sid]
    assert sbx.closed is True  # AioSandbox.close 被调
    assert p.get(sid) is None


def test_provider_replica_eviction(provider):
    """超 replicas 软上限 → 淘汰暖池最老的腾位。"""
    p, backend = provider(replicas=2)
    s1 = p.acquire("t1")
    s2 = p.acquire("t2")
    assert s1 != s2  # 不同 thread → 不同沙箱
    # 把 s1 释放进暖池。
    p.release(s1)
    assert s1 in p._warm_pool
    # 第 3 个线程 acquire：总跟踪数（活跃 s2 + 暖池 s1 = 2）>= replicas(2) → 淘汰暖池最老。
    s3 = p.acquire("t3")
    assert s3 not in (s1, s2)
    assert backend.destroyed == [s1]  # s1 被淘汰销毁


def test_provider_shutdown_clears_all_and_idempotent(provider):
    p, backend = provider()
    s1 = p.acquire("t1")
    s2 = p.acquire("t2")
    p.release(s2)  # s2 入暖池
    p.shutdown()
    # 活跃 s1 与暖池 s2 都被 destroy。
    assert set(backend.destroyed) == {s1, s2}
    # 幂等：再 shutdown 不抛、不重复 destroy。
    before = len(backend.destroyed)
    p.shutdown()
    assert len(backend.destroyed) == before


def test_provider_acquire_anonymous_thread_none(provider):
    """thread_id=None → 随机 id，直接 create（不走跨进程锁）。"""
    p, backend = provider()
    sid = p.acquire(None)
    assert sid not in (None, "")
    assert backend.created == [sid]


def test_provider_backend_discover(provider):
    """backend.discover 命中 → 注册发现的沙箱，不 create。"""
    p, backend = provider()
    # 预置：discover 会返回一个已存在的沙箱。
    target_id = AioSandboxProvider._deterministic_sandbox_id("t1")
    backend.discover_map[target_id] = SandboxInfo(sandbox_id=target_id, sandbox_url="http://localhost:41000")
    sid = p.acquire("t1")
    assert sid == target_id
    assert backend.created == []  # 没新建，是 discover 来的


def test_provider_reconcile_orphans_adopts(provider, monkeypatch: pytest.MonkeyPatch):
    """启动时 backend.list_running 有容器 → 收养进暖池。"""
    # 通过重跑 reconcile 测：先造 provider，再注入 list_running 结果，再手动调 reconcile。
    p, backend = provider()
    orphan = SandboxInfo(sandbox_id="orphan1", sandbox_url="http://localhost:42000")
    backend.list_running_result = [orphan]
    p._reconcile_orphans()
    assert "orphan1" in p._warm_pool


def test_provider_uses_thread_data_mounts_local_vs_remote(provider, monkeypatch: pytest.MonkeyPatch):
    """uses_thread_data_mounts 按 backend 类型判定：FakeBackend 非 LocalContainerBackend → False。"""
    p, backend = provider()
    # FakeBackend 不是 LocalContainerBackend，故属性为 False。
    # 真正的 LocalContainerBackend 场景由 test_provider_backend_selection_local_default 覆盖。
    assert p.uses_thread_data_mounts is False


def test_provider_idle_cleanup_destroys_active(provider):
    """_cleanup_idle_sandboxes：活跃沙箱超 idle_timeout → destroy（含锁内再验）。"""
    p, backend = provider(idle_timeout=0)  # idle_timeout 仅控制是否起线程；清理逻辑可直测
    sid = p.acquire("t1")
    # 把 last_activity 调到很久以前，模拟空闲超时。
    p._last_activity[sid] = time.time() - 99999
    p._cleanup_idle_sandboxes(idle_timeout=60)
    assert backend.destroyed == [sid]


def test_provider_idle_cleanup_warm_pool(provider):
    """暖池条目超 idle_timeout → backend.destroy。"""
    p, backend = provider()
    sid = p.acquire("t1")
    p.release(sid)
    # 把暖池 release 时间调到很久以前。
    info, _ = p._warm_pool[sid]
    p._warm_pool[sid] = (info, time.time() - 99999)
    p._cleanup_idle_sandboxes(idle_timeout=60)
    assert sid not in p._warm_pool
    assert backend.destroyed == [sid]


def test_provider_backend_selection_provisioner(monkeypatch: pytest.MonkeyPatch, home_env: Path):
    """config 有 provisioner_url → 选 RemoteSandboxBackend。"""
    monkeypatch.setattr(provider_module.AioSandboxProvider, "_register_signal_handlers", lambda self: None)

    def fake_load_config(self):
        return {
            "image": "i",
            "port": 40000,
            "container_prefix": "p",
            "idle_timeout": 0,
            "replicas": 3,
            "mounts": [],
            "environment": {},
            "provisioner_url": "http://provisioner:8002",
        }

    monkeypatch.setattr(provider_module.AioSandboxProvider, "_load_config", fake_load_config)

    # _create_backend 会真的 new RemoteSandboxBackend（只存 URL，不发请求）。
    p = provider_module.AioSandboxProvider()
    assert isinstance(p._backend, RemoteSandboxBackend)
    assert p._backend.provisioner_url == "http://provisioner:8002"
    p.shutdown()


def test_provider_backend_selection_local_default(monkeypatch: pytest.MonkeyPatch, home_env: Path):
    """config 无 provisioner_url → 选 LocalContainerBackend。"""
    monkeypatch.setattr(provider_module.AioSandboxProvider, "_register_signal_handlers", lambda self: None)

    def fake_load_config(self):
        return {
            "image": "i",
            "port": 40000,
            "container_prefix": "p",
            "idle_timeout": 0,
            "replicas": 3,
            "mounts": [],
            "environment": {},
            "provisioner_url": "",
        }

    monkeypatch.setattr(provider_module.AioSandboxProvider, "_load_config", fake_load_config)
    p = provider_module.AioSandboxProvider()
    assert isinstance(p._backend, LocalContainerBackend)
    p.shutdown()


def test_provider_thread_mounts_includes_workspace(home_env: Path):
    """_get_thread_mounts 复用 M10 ensure_thread_dirs 建 4 个挂载（workspace/uploads/outputs + acp 只读）。"""
    # _get_thread_mounts 是 staticmethod，直接调（autouse fixture 注入了 user_id）。
    mounts = AioSandboxProvider._get_thread_mounts("t-mounts")
    container_paths = [m[1] for m in mounts]
    assert "/mnt/user-data/workspace" in container_paths
    assert "/mnt/user-data/uploads" in container_paths
    assert "/mnt/user-data/outputs" in container_paths
    assert "/mnt/acp-workspace" in container_paths
    # acp-workspace 只读。
    acp = [m for m in mounts if m[1] == "/mnt/acp-workspace"][0]
    assert acp[2] is True


def test_security_allows_host_bash_for_aio_provider(monkeypatch: pytest.MonkeyPatch):
    """is_host_bash_allowed 对 AIO（非 local）provider 返回 True。"""
    from deerflow.sandbox import security as security_module

    fake_cfg = SimpleNamespace(sandbox=SimpleNamespace(use="deerflow.community.aio_sandbox:AioSandboxProvider", allow_host_bash=False))
    monkeypatch.setattr(security_module, "get_app_config", lambda: fake_cfg)
    assert security_module.uses_local_sandbox_provider() is False
    assert security_module.is_host_bash_allowed() is True
