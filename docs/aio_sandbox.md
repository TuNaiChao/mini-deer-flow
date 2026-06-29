# 14. aio_sandbox.md — AIO 沙箱（生产容器隔离 / 暖池 / 跨进程发现）

> **一句话定位**：AIO 沙箱是「把 agent 的 bash / 文件操作关进 Docker 容器」的生产级隔离方案——
> 本地沙箱（[sandbox.md](sandbox.md)）不是安全边界，AIO 才是。它用 HTTP API 连运行中的容器，
> 配暖池复用、跨进程文件锁发现、idle 回收、优雅关闭，让多线程 / 多进程 / 多 pod 共享一组容器而不撞车。

读完 [sandbox.md](sandbox.md)（懂了虚拟路径 + LocalSandbox 为何不是安全边界）再看本篇最省事。

> **Phase 2 全维重审（2026-06-29）**：逐文件 diff `community/aio_sandbox/*` vs 最新上游（剥 docstring 后
> 判逻辑差）。补 **2 项真实对齐**：① provider 单例生命周期加锁（**#3730**）——锁在 `sandbox_provider.py`
> 层，对所有 provider（含 `AioSandboxProvider`）生效，详见 [sandbox.md](sandbox.md) §provider 单例（核心收益
> 正是防 AIO 的 idle-checker 线程在双重初始化时泄漏）；② **ErrorObservation 恢复重试**显式
> `create_session` + `finally cleanup_session`——旧版只在新 id 上 `exec_command` 不建/拆 session，每次错误
> 恢复都泄漏一个 session（见 §命令串行化）。**defer**：**#3729**（`acquire(*, user_id)` + `create(*, user_id)` +
> 按 `(user_id, thread_id)` 归桶 + sandbox_id 嵌 user_id）属 IM channel owner-scoping（Gateway，触及
> `app/channels/feishu.py`），同 task_tool #2676 / uploads #3579 不 port——mini 经 `get_effective_user_id()`
> 已正确按用户隔离路径，无 IM channel 故无 owner 覆盖需求；AIO backend 的 `create`/`discover` 签名随 #3729
> defer。mini 的 soft-load `agent_sandbox`（红线 #24，上游改硬 import）+ `%`-format 日志为 mini 已知选择 / cosmetic。

---

## 为什么需要 AIO 沙箱（痛点）

[sandbox.md](sandbox.md) 讲过：`LocalSandbox.execute_command` 直接在**宿主机**跑 bash，隔离只靠
虚拟路径翻译（**不是**安全边界）。这对「跑自己写的代码」够了，但有三类场景必须有真隔离：

1. **跑 untrusted 代码**：agent 要执行用户提交的 / 网上拉的脚本，可能含 `rm -rf`、提权、读 `/etc/passwd`。
2. **多租户**：多个用户的 agent 同跑，不能让 A 用户碰到 B 用户的文件系统。
3. **生产部署**：网关长跑，不能因为一条坏命令搞挂整台宿主。

AIO 把每个沙箱关进一个 Docker（或 Apple Container）容器：agent 的 `rm -rf /` 只删容器里的文件，
逃不出容器。容器跑一个 HTTP API（[agent-infra/sandbox](https://github.com/langchain-ai/agent-infra)），
`AioSandbox` 经这个 API 操作容器内的 shell 与文件系统。

**关键不变量**：AIO 和 Local 的公开 `Sandbox` API 完全一致（8 个抽象方法），且都接受
`/mnt/user-data/...` 虚拟路径——所以 agent 代码不用为「本地/容器」写两套，只改 config 的
`sandbox.use` 指向哪个 provider。

---

## 核心概念（名词 + 类比）

### ① SandboxBackend（怎么把容器弄起来）

「backend」回答「**怎么起 / 查活没活 / 销毁一个容器**」，两种实现：

- **`LocalContainerBackend`**：本机起 Docker / Apple Container。自管端口分配、容器命名、卷挂载、
  健康检查。靠 `docker`/`container` CLI（subprocess），不用 docker-py。
- **`RemoteSandboxBackend`**：连远端 provisioner（K8s）。Pod 生命周期委托给 provisioner 服务，
  本地只是个薄 HTTP client。

**类比**：backend 像「租车公司」——你可以自己买车上牌（本地 Docker），也可以叫网约车（远端 K8s
provisioner 派车）。provider（下面）只管「我要一辆车」，不关心车怎么来的。

### ② SandboxInfo（跨进程发现的「寻人启事」）

一个 dataclass：`sandbox_id` + `sandbox_url` + `container_name/id` + `created_at`。它持久化容器
的连接信息，让**另一个进程**能发现并复用前一个进程起的容器。

为什么需要跨进程？gateway 和 langgraph dev 是两个进程；多 worker；K8s 多 pod 共享存储。
若进程 A 起了容器 `c-abc12345`，进程 B 不该再起一个——它应该「发现」A 起的那个并连上去。

### ③ 暖池（warm pool）

`release` 一个沙箱时**不停容器**，而是把它「停」进暖池（容器还跑着，只是没线程在用）。下次同
thread `acquire` 时从暖池秒级回收——**免冷启动**（起一个新容器要拉镜像 + 健康检查，几秒到几十秒）。

暖池条目只在两种情况被清：① `replicas` 软上限到了，淘汰最老的腾位；② idle 超时（默认 10 分钟）没人用。

### ④ 确定性 sandbox_id + 跨进程文件锁

`sandbox_id = sha256(thread_id)[:8]`——**确定性**：所有进程对同一 thread_id 派生出同一 id，从而
推出同一容器名（`{prefix}-{sandbox_id}`）。这让 backend `discover(sandbox_id)` 能凭名字找到别人
起的容器。

但「发现」有竞态：两个进程同时为同 thread 建容器，会撞容器名冲突。故建容器前先抢
`{thread_dir}/{sandbox_id}.lock` 文件锁（`fcntl.flock` 排他锁）：后到的进程拿到锁后再 discover，
发现先到进程起的容器，复用而非撞名。

---

## 设计原理（权衡 / 不变量 / 踩坑）

### 缓存层级（acquire 依次试，越快越先试）

```
① 进程内活跃缓存  ── 命中（同进程同 thread 重复访问）→ 秒回
       ↓ miss
② 暖池复用         ── 命中（容器还跑着，免冷启动）→ 新建 AioSandbox client 连上
       ↓ miss
③ 跨进程文件锁内 backend discover ── 命中（别的进程起的容器）→ 注册并连
       ↓ miss
④ backend create   ── 真起一个新容器 + 等就绪
```

### 暖池复用要关旧 HTTP client（#2872）

`release` 把沙箱停进暖池时，**关掉**旧 `AioSandbox` 持有的宿主侧 HTTP client（释放池化 socket），
暖池只存 `SandboxInfo`（轻量）。回收时新建一个 `AioSandbox`（与 client）。否则长跑 gateway 会
累积一堆没关的 httpx client → 套接字泄漏。`destroy` 同理先 `close()` 再销容器。

### `AioSandbox.close()` 摸属性链关 httpx（#2872）

`agent_sandbox` SDK 是 Fern 生成的，没暴露 `close()` / `__exit__`。`close()` 沿属性链
`_client_wrapper.httpx_client.httpx_client` 摸到真正的 `httpx.Client`（socket 持有者）显式关。
取「第一个暴露 `close()` 的对象」，未来 SDK 加了顶层 `close()` 也自动用上。幂等、线程安全、非致命。

### 命令串行化（#1433）

AIO 容器维护**单个**持久 shell session。并发 `exec_command` 会把 session 搞坏，返回
`ErrorObservation`（非真输出）。故 `AioSandbox.execute_command` 用 `self._lock` 串行化。即便加了锁
仍检测到 `ErrorObservation` 签名（如多进程共享同一沙箱），就在**新 session**（新 id）上重试一次。

> **恢复 session 显式建/拆**：重试不是直接 `exec_command(id=fresh_id)` 了事——那样 fresh_id 对应的 session
> 从未被显式创建，事后也没人清理，每次错误恢复都泄漏一个 session。修法是先 `shell.create_session(id=fresh_id)`，
> `try` 里跑 `exec_command`，`finally` 里 `shell.cleanup_session(fresh_id)`（清理本身失败只 warning 不抛，
> 免得掩盖原异常）。这样恢复路径的 session 生命周期自洽，长跑的多进程共享场景不再慢慢漏 session。

### `download_file` 显式防穿越

AIO 把路径**原样**转发给容器 API（不像 LocalSandbox 经 `_resolve_path` 隐式翻译防穿越）。故
`download_file` 先显式查 `..` 段 + 校验在 `/mnt/user-data` 前缀内，再流式分块读（累计字节数，
超 100MB 抛 `OSError(EFBIG)`）。

### replicas 是软上限

`replicas`（默认 3）是**软**上限：超限时淘汰**暖池**里最老的腾位（暖池容器没人用，可安全停）。
但**不强停正在服务线程的活跃容器**——若所有槽都被活跃沙箱占，照建并记 warning（「beyond the soft limit」）。
这避免「为守预算掐断正在跑的任务」。

### idle 回收的锁内再验

后台线程每 60s 查 `_last_activity` / 暖池释放时间。快照出「该销毁的」列表后，**销毁前在锁内再验一次**
仍空闲——因为快照到现在期间，沙箱可能已被 re-acquire（`last_activity` 更新）或已 release/destroy。
避免误杀刚被重新启用的沙箱。

### 启动收养孤儿（reconcile）

进程重启 / 崩溃 / SIGKILL 后，内存状态丢失，但 Docker 容器还跑着（成了「孤儿」）。`__init__` 调
`backend.list_running()` 枚举所有匹配前缀的运行中容器，**全收养**进暖池，让 idle 检查器决定。
无条件全收养是因为光凭 age 分不清「孤儿」与「另一进程正在用」——`idle_timeout` 表「不活跃」非「uptime」，
收养进暖池让 idle 检查器决定，避免误毁并发进程正用的容器。

### 优雅关闭（红线 #33）

注册 `SIGTERM` / `SIGINT` / `SIGHUP`（终端关闭）+ `atexit`。`shutdown()` 幂等：先停 idle 检查线程，
再逐个 `destroy` 活跃 + 暖池沙箱。handler 调完 `shutdown` 后链回原信号处理器（或恢复 SIG_DFL 重抛），
确保用户的 Ctrl-C 仍能终止进程。

### host bash 对 AIO 自动放行

`is_host_bash_allowed()`（[security.py](../backend/packages/harness/deerflow/sandbox/security.py)）对
非 local provider 返回 **True**——AIO 有真隔离，host bash 安全。所以 `sandbox.allow_host_bash` 只对
Local provider 生效，AIO 不用设。

### soft-load（红线 #24）

`agent_sandbox` SDK 缺包时：`AioSandboxClient = None`，`AioSandbox.__init__` 抛带可操作安装提示的
`ImportError`。但**模块导入不炸**（类定义不依赖 SDK），provider 的 backend 选择 / acquire 才触发。
安装：`uv pip install 'deerflow-harness[aio_sandbox]'`。未装时配了 AIO provider 会在 acquire 时报错，
回退 Local provider（改 `config.sandbox.use`）。

---

## 文件结构

```
community/
├── __init__.py                          # 社区扩展包（不在 __init__ eager import，按需 import 子模块）
└── aio_sandbox/
    ├── __init__.py                      # 导出 AioSandbox / AioSandboxProvider / backends / SandboxInfo
    ├── sandbox_info.py                  # SandboxInfo dataclass（跨进程发现元数据）
    ├── backend.py                       # SandboxBackend ABC + wait_for_sandbox_ready[_async]
    ├── local_backend.py                 # LocalContainerBackend（Docker/Apple Container CLI 编排）
    ├── remote_backend.py                # RemoteSandboxBackend（K8s provisioner 薄 HTTP client）
    ├── aio_sandbox.py                   # AioSandbox（HTTP client Sandbox 实现，soft-load agent_sandbox）
    └── aio_sandbox_provider.py          # AioSandboxProvider（暖池 + 跨进程锁 + idle + shutdown）

utils/
└── network.py                           # PortAllocator + get_free_port/release_port（容器端口分配）

config/
└── sandbox_config.py                    # + provisioner_url 字段（选远端 vs 本地 backend）
```

> **为什么放 `community/`？** 社区扩展按需启用、soft-load 外部 SDK。AIO 依赖 `agent_sandbox` SDK
> + Docker/K8s，属「按需启用」，故归 community。`LocalSandboxProvider`（核心、无外部重依赖）在
> `sandbox/local/`（M10）。

---

## 关键接口

### `SandboxBackend`（ABC，`backend.py`）

```python
class SandboxBackend(ABC):
    def create(self, thread_id, sandbox_id, extra_mounts=None) -> SandboxInfo: ...
    def destroy(self, info: SandboxInfo) -> None: ...
    def is_alive(self, info: SandboxInfo) -> bool: ...        # 轻量（container inspect，不打 HTTP）
    def discover(self, sandbox_id: str) -> SandboxInfo | None: ...  # 跨进程发现
    def list_running(self) -> list[SandboxInfo]: ...           # 启动 reconcile（默认空）
```

### `AioSandboxProvider`（`aio_sandbox_provider.py`，继承 `SandboxProvider`）

```python
class AioSandboxProvider(SandboxProvider):
    uses_thread_data_mounts: bool          # LocalContainerBackend → True（bind-mount 线程目录）
    def acquire(self, thread_id=None) -> str: ...        # 四级缓存：进程内 → 暖池 → discover → create
    async def acquire_async(self, thread_id=None) -> str: ...  # 不卡事件循环
    def get(self, sandbox_id) -> Sandbox | None: ...     # 更新 last_activity
    def release(self, sandbox_id) -> None: ...           # 停进暖池（容器继续跑），关 HTTP client
    def destroy(self, sandbox_id) -> None: ...           # 真停容器 + 关 client
    def shutdown(self) -> None: ...                      # 幂等：停 idle 线程 + destroy 全部
```

### `AioSandbox`（`aio_sandbox.py`，继承 `Sandbox`）

8 个 `Sandbox` 抽象方法的 HTTP 实现：`execute_command`（锁串行 + ErrorObservation 重试）、
`read_file` / `download_file`（防穿越 + 100MB 上限）/ `list_dir` / `write_file` / `glob` / `grep`
（远端搜本端滤）/ `update_file`（base64）。`close()` 释放 httpx socket。

---

## 应用方法

### 配置（`config.yaml`）—— 本地 Docker 模式

```yaml
sandbox:
  use: deerflow.community.aio_sandbox:AioSandboxProvider
  image: enterprise-public-cn-beijing.cr.volces.com/vefaas-public/all-in-one-sandbox:latest
  port: 8080                      # 本地容器基准端口（端口分配从此往后搜）
  container_prefix: deer-flow-sandbox
  idle_timeout: 600               # 空闲秒数（0 禁用 idle 回收）
  replicas: 3                     # 最大并发容器（软上限，超限淘汰暖池最老）
  mounts:                         # 额外卷挂载
    - host_path: /path/on/host
      container_path: /path/in/container
      read_only: false
  environment:                    # 容器环境变量（$ 开头从宿主解析）
    NODE_ENV: production
```

### 配置 —— 远端 K8s provisioner 模式

```yaml
sandbox:
  use: deerflow.community.aio_sandbox:AioSandboxProvider
  provisioner_url: http://provisioner:8002   # 设了 → RemoteSandboxBackend，否则 → 本地
```

### 安装 SDK

```bash
uv pip install 'deerflow-harness[aio_sandbox]'   # 装 agent-sandbox + requests
```

### 跑测试

```bash
cd backend && make test    # 含 test/test_aio_sandbox.py（64 个 hermetic 测试，全 mock）
```

测试约定：`agent_sandbox` SDK 未装（CI 不装 extra）→ soft-load 分支天然可测；带 SDK 的行为用
fake client（`AioSandboxClient` 替身）测；`LocalContainerBackend` 的 `subprocess.run`、
`RemoteSandboxBackend` 的 `requests`、provider 的 backend 全 mock；provider 构造的信号注册 + idle
线程 monkeypatch 成 no-op + `idle_timeout=0`，避免干扰 pytest。

---

## 与其它模块的关系

```
config (sandbox_config: use/image/port/replicas/idle_timeout/mounts/environment/provisioner_url)
  │
config/paths (VIRTUAL_PATH_PREFIX=/mnt/user-data)
runtime/user_context (get_effective_user_id → per-user 隔离)
  │
sandbox (M10：Sandbox 抽象 + SandboxProvider ABC + security + search)  ← AIO 复用
  │   ▲
  │   └── AioSandbox 继承 Sandbox（8 抽象方法），AioSandboxProvider 继承 SandboxProvider
  │
community/aio_sandbox ◄── AioSandboxProvider
  │   ├── LocalContainerBackend ── Docker/Apple Container CLI（subprocess）
  │   └── RemoteSandboxBackend ─── K8s provisioner（requests HTTP）
  │
utils/network (get_free_port/release_port)  ← LocalContainerBackend 端口分配
```

- **上游依赖 M10**：`Sandbox` 抽象基类、`SandboxProvider` ABC、`security.is_host_bash_allowed`、
  `search.py`（glob/grep 的本端过滤）、`local_sandbox.ensure_thread_dirs`（建线程目录，复用 M10）。
- **下游**：与 `LocalSandboxProvider` 二选一（`config.sandbox.use` 切换），工具层（M15 的 7 工具）
  和中间件（M16 的 `SandboxMiddleware`）对两者透明——都经 `get_sandbox_provider()` 取实例。
- **未来 M21**：`community/` 还会放联网 provider（ddg/tavily/jina），与 AIO 共用「soft-load 外部 SDK」约定。

---

## 常见问题 / 排错

**Q：配了 AIO provider，但 acquire 报 `AIO 沙箱需要 agent-sandbox SDK`？**
A：`agent_sandbox` 没装。`uv pip install 'deerflow-harness[aio_sandbox]'`（或单独 `pip install agent-sandbox`）。
不想用 AIO 就把 `config.sandbox.use` 改回 `deerflow.sandbox.local:LocalSandboxProvider`。

**Q：容器起不来，报 `failed to become ready within timeout`？**
A：`wait_for_sandbox_ready` 轮询容器 `/v1/sandbox` 健康端点 60s 没就绪。常见原因：镜像拉不下来
（网络）、端口被占（`get_free_port` 找的端口 Docker 释放异步，已重试 10 个都不行）、镜像启动慢。
看日志里 `Starting container using docker: ...`（环境变量已脱敏）确认命令对。

**Q：日志里 `port is already allocated, retrying with next port`？**
A：正常——Docker 释端口有微秒级异步，`get_free_port` 的 socket bind 检查通过但 Docker 实际还占着。
backend 会自动换下一个端口重试（最多 10 个）。若 10 个都不行才报错（极少见，通常是 Docker daemon 卡了）。

**Q：`Container name X already in use, attempting to discover`？**
A：正常——另一进程已为同 thread 起了同名容器（确定性命名）。backend 走 `discover` 收养那个容器
而不是报错。这是跨进程发现的设计意图。

**Q：进程重启后，旧的容器怎么处理？**
A：`__init__` 的 `_reconcile_orphans` 会 `list_running` 枚举所有匹配前缀的运行中容器，全收养进暖池。
idle 检查器会在 `idle_timeout`（默认 10 分钟）后回收没人用的。所以重启不会留永久孤儿。

**Q：`replicas=3` 但我有 5 个活跃线程，会怎样？**
A：前 3 个各起一个容器。第 4、5 个 acquire 时，若暖池有空（有人 release 过）就回收暖池；否则
**软上限**——照建新容器并记 warning（`beyond the soft limit`）。不会强停正在服务线程的容器。
真正受限的是暖池：超 replicas 时淘汰暖池最老的腾位。

**Q：本地和 AIO 的工具行为一样吗？**
A：一样。两者都实现 `Sandbox` 的 8 个抽象方法，agent 用的 7 工具（bash/ls/glob/grep/read_file/
write_file/str_replace）对两者透明。区别只在隔离强度：本地宿主机进程（非边界），AIO 容器（真隔离）。
`is_host_bash_allowed` 对 AIO 自动 True（有隔离），对本地默认 False。

**Q：为什么 AIO 的 `download_file` 要显式查 `..`，Local 不用？**
A：Local 经 `_resolve_path` 翻译虚拟路径时隐式防了穿越（翻译后逃出挂载根就拒）。AIO 把路径原样
转发给容器 API，没翻译步骤，故 `download_file` 显式查 `..` 段 + 校验在 `/mnt/user-data` 前缀内。
两层的防穿越语义一致，实现位置不同。
