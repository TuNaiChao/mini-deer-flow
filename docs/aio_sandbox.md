# 14. aio_sandbox.md — AIO 沙箱（生产容器隔离 / 暖池 / 跨进程发现）

> 📝 重写于 2026-07-05 · 对照代码 commit ffc5e5d · **2026-07-05 复审**（更面向小白 + 加「实现差异 vs 上游 deer-flow 源码」）

> **一句话定位**：AIO 沙箱是「把 agent 的 bash / 文件操作关进 Docker 容器」的生产级隔离方案——
> 本地沙箱（[sandbox.md](sandbox.md)）不是安全边界，AIO 才是。它经 HTTP API 连运行中的容器，
> 配暖池复用、跨进程文件锁发现、idle 回收、优雅关闭，让多线程 / 多进程 / 多 pod 共享一组容器而不撞车。

> **配套代码**：[community/aio_sandbox/](../backend/packages/harness/deerflow/community/aio_sandbox/)（7 个文件 ~2200 行）+ 复用 [sandbox/](../backend/packages/harness/deerflow/sandbox/) 核心（`Sandbox` ABC / `SandboxProvider` / `security` / `search`）+ [utils/network.py](../backend/packages/harness/deerflow/utils/network.py)（端口分配）+ [config/sandbox_config.py](../backend/packages/harness/deerflow/config/sandbox_config.py)
> **配套测试**：[test/test_aio_sandbox.py](../test/test_aio_sandbox.py)（64 个 hermetic 测试，`agent_sandbox` SDK 未装走 soft-load 分支，带 SDK 的行为用 fake client，backend 全 mock）
> **参考**：deerflow-book [14-sandbox-implementations.md](../deerflow-book/chapters/14-sandbox-implementations.md)（**仅借「确定性 id + 暖池 + 缓存」的概念叙事框架**，不作差异基线）；**实现差异一律对照上游源码** `deer-flow/backend/packages/harness/deerflow/community/aio_sandbox/`，见本文 §9
> 本文面向「刚接触容器隔离 / Docker / K8s 的小白」。读完 [sandbox.md](sandbox.md)（懂了虚拟路径 + `LocalSandbox` 为何不是安全边界）再看本篇最省事。每个名词第一次出现都会解释。

---

## 学完能回答（learning outcomes）

1. 为什么 `LocalSandbox` 不是安全边界、AIO 才是？「跑 untrusted 代码 / 多租户 / 生产部署」三类场景各缺什么？host bash 为什么对 AIO 自动放行、对 Local 默认禁用？
2. AIO 的四级缓存（进程内活跃 → 暖池 → 跨进程文件锁内 discover → create）各自命中什么、为什么这个顺序（越快越先试）？
3. 「确定性 `sandbox_id = sha256(thread_id)[:8]`」解决了什么？为什么需要它 + 跨进程文件锁（`fcntl.flock`）**配合**才能安全发现别人起的容器？（光有确定性 id 会撞名，光有锁没确定性 id 发现不了）
4. 暖池（warm pool）和 replicas 软上限怎么配合？为什么 `release` 不停容器？超 replicas 时淘汰谁、为什么**不**强停正在服务线程的活跃容器？
5. idle 回收为什么销毁前要**在锁内再验一次**仍空闲？启动时为什么无条件**全收养**孤儿容器（而不是按 age 判断）？
6. `AioSandbox.execute_command` 为什么用 `self._lock` 串行？加了锁为什么还要检测 `ErrorObservation` 并在新 session 重试（建/拆）？`close()` 为什么沿属性链摸到真 `httpx.Client`？
7. `LocalContainerBackend` 的端口冲突重试 + 容器名冲突→discover 各解决什么？`list_running` 为什么只用 2 次 subprocess（而非 2N+1）？
8. soft-load `agent_sandbox` 是什么意思？缺包时为什么模块仍能 import、只在实例化时才报错？怎么装、装了不想要怎么回退 Local？

---

## §1 为什么需要 AIO 沙箱（痛点）

[sandbox.md](sandbox.md) 讲过：`LocalSandbox.execute_command` 直接在**宿主机**跑 bash，隔离只靠虚拟路径翻译（[local_sandbox.py](../backend/packages/harness/deerflow/sandbox/local/local_sandbox.py)，**不是**安全边界）。这对「跑自己写的代码」够了，但有三类场景必须有真隔离：

| 场景 | LocalSandbox 会怎样 | 为什么必须有容器 |
|------|---------------------|------------------|
| **跑 untrusted 代码** | agent 执行用户提交的 / 网上拉的脚本，可能含 `rm -rf /`、提权、读 `/etc/passwd` | 没隔离就真把宿主搞坏 / 把敏感文件读走 |
| **多租户** | 多个用户的 agent 同跑，A 用户的 `ls /` 能看到 B 用户的文件系统 | 不能让租户互相碰到对方的文件 |
| **生产部署** | 网关长跑，一条坏命令（fork 炸弹 / 吃满磁盘）搞挂整台宿主 | 不能因为一个 agent 任务搞挂服务 |

AIO 把每个沙箱关进一个 Docker（或 Apple Container）容器：agent 的 `rm -rf /` 只删容器里的文件，逃不出容器。容器跑一个 HTTP API（[agent-infra/sandbox](https://github.com/langchain-ai/agent-infra)），`AioSandbox` 经这个 API 操作容器内的 shell 与文件系统（[aio_sandbox.py:63](../backend/packages/harness/deerflow/community/aio_sandbox/aio_sandbox.py#L63)）。

**关键不变量**：AIO 和 Local 的公开 `Sandbox` API 完全一致（8 个抽象方法，见 [sandbox.py](../backend/packages/harness/deerflow/sandbox/sandbox.py)），且都接受 `/mnt/user-data/...` 虚拟路径——所以 **agent 代码不用为「本地/容器」写两套**，只改 config 的 `sandbox.use` 指向哪个 provider（[sandbox_config.py:35](../backend/packages/harness/deerflow/config/sandbox_config.py#L35)）。

---

## §2 零基础名词（先认这些词）

> 本篇假设你已读过 [sandbox.md](sandbox.md) §2.0 的计算机基础（文件系统/进程/shell/挂载）+ §2 的「虚拟路径 / Sandbox ABC / provider」。这里往下挖一层：**容器凭什么能把进程关起来**，以及它用的几个词。

### 2.0 最基础（容器隔离的原理，不熟先看这）

- **容器 vs 虚拟机 vs 普通进程**：① **普通进程**：和宿主共用内核，基本不隔离（能看到全局文件/进程）；② **虚拟机（VM）**：模拟一整套硬件、跑自己的内核，隔离最强但重（启动慢、吃资源）；③ **容器**：和宿主**共用内核**，但内核给进程套一层「滤镜」让它**以为自己独占系统**——隔离够强又轻（秒级启动）。**类比**：虚拟机像「独栋别墅」（自带地基），容器像「酒店单间」（共用大楼骨架、但有自己的门锁和独立空间）。
- **namespace（命名空间）**：Linux 内核的隔离机制——给进程一副「滤镜」：让它看到的进程列表、网络、文件挂载点、用户都和宿主不同。容器里的 `rm -rf /` 只删容器视角的 `/`，碰不到宿主真根。**这是「容器能隔离」的底层原理**，也是本地沙箱（普通进程）做不到容器隔离的根本原因。
- **cgroup（控制组）**：Linux 内核的限制机制——限制一组进程能用多少 CPU/内存/磁盘 IO。容器用它防「一个容器吃光宿主资源」。
- **镜像（image）/ 可写层**：容器的「只读模子」——预装好 shell、Python 等环境的文件系统快照。起容器就是从镜像上盖一层可写层（你的写入只落可写层，镜像本身不变）。**类比**：镜像是「干净系统安装盘」，容器是「用这盘装出来的、可乱装的运行实例」。
- **bind-mount（绑定挂载）/ Volume**：把宿主一个目录「接」进容器文件系统树某处，让容器能读写宿主文件。AIO 把 `/mnt/user-data` 这个宿主目录 bind-mount 进每个容器——故 agent 用的虚拟路径就是容器内真实路径（恒等映射，无需翻译）。
- **端口（port）/ localhost**：网络服务的「门牌号」。容器里跑的 HTTP API 监听一个端口（如 8080），宿主经 `http://localhost:8080` 访问。`localhost`=`127.0.0.1`=本机，只本机能访问（不暴露到公网）。

### 2.1 本模块名词

- **容器（container）**：一个「隔离的 Linux 环境」——有自己的文件系统视图、进程空间，跑在宿主机内核上但被关起来。Docker 是最常见的容器运行时。**类比**：容器像酒店里的一个房间，客人（agent 的命令）只能在房间里活动，看不见也碰不到别的房间和酒店大堂（宿主机）。
- **Docker / Apple Container**：两种容器运行时（runtime）。Docker 跨平台；Apple Container 是 macOS 上的轻量替代。本 backend 在 macOS 优先用 Apple Container，没有才回退 Docker（[local_backend.py:224](../backend/packages/harness/deerflow/community/aio_sandbox/local_backend.py#L224)）。
- **镜像（image）**：容器的「模子」——一个预装好 shell、Python 等环境的只读文件。起容器就是从镜像拷贝出一个可写的运行实例。本沙箱默认用 `all-in-one-sandbox:latest` 镜像（[aio_sandbox_provider.py:59](../backend/packages/harness/deerflow/community/aio_sandbox/aio_sandbox_provider.py#L59)）。
- **冷启动（cold start）**：从镜像起一个新容器的过程——拉镜像 + 启动 + 等 HTTP API 就绪，要几秒到几十秒。**暖池**（下面）就是为了避开它。
- **K8s（Kubernetes）/ Pod**：生产级的容器编排系统。一个 Pod 是 K8s 里跑容器的最小单位。本 backend 的远端模式把 Pod 生命周期委托给一个 provisioner 服务（[remote_backend.py](../backend/packages/harness/deerflow/community/aio_sandbox/remote_backend.py)）。
- **DooD（Docker-outside-of-Docker）**：gateway 自己也跑在容器里、但借用宿主机的 Docker daemon 来起沙箱容器的部署方式。此时挂载路径要用宿主侧路径才能被宿主 Docker daemon 解析（[aio_sandbox_provider.py:306](../backend/packages/harness/deerflow/community/aio_sandbox/aio_sandbox_provider.py#L306)）。
- **虚拟路径 `/mnt/user-data`**：agent 看到的「假」根路径。在 Local 模式它被翻译成宿主目录；在 AIO 模式它经 Docker Volume 真实挂载，故 agent 用的虚拟路径就是容器内的真实路径（这正是 [sandbox.md](sandbox.md) 里 `/mnt` 语义锚点的来由）。
- **HTTP API**：一个程序通过 HTTP 协议（就是网页用的那个协议）暴露给别人调用的接口。AIO 容器里跑着一个 HTTP 服务，`AioSandbox` 经 `POST/GET` 请求让它「跑这条命令」「读这个文件」。**类比**：HTTP API 像酒店客房服务的电话——你在房间（容器外）拨号点服务，客房服务（容器内的 HTTP 服务）替你跑腿，你不用亲自进厨房。
- **SDK（Software Development Kit）**：别人打包好给你调用的库。`agent_sandbox` 是容器 HTTP API 的官方 Python SDK，把 HTTP 请求包成 `client.shell.exec_command(...)` 这种好用的方法。**soft-load**：mini import 时先试 `from agent_sandbox import ...`，失败就把类设成 `None` 不报错——等真正要用时才报「请装 SDK」。这样没装 AIO extra 的用户完全不受影响（§5.4）。
- **dataclass**：Python 的一种「纯数据容器」类，写 `@dataclass` 后只要列字段名，Python 自动帮你生成构造函数等样板。`SandboxInfo`（§4.2）就是 dataclass。
- **`fcntl.flock` 文件锁**：操作系统级的「文件锁」——给一个打开的文件加排他锁后，别的进程想给同一文件加锁就得**排队等着**。mini 用它协调多个进程「谁有权建容器」，避免撞名（§4.4）。
- **后台线程（daemon thread）**：程序里跑的「子任务」，标记为 daemon 意味着「主程序退出时不用等它、直接跟着结束」。mini 的 idle 回收检查器跑在一个 daemon 后台线程里，每 60s 醒一次清没人用的容器（§5.1）。
- **`asyncio.to_thread` / 事件循环**：异步程序（事件循环）里跑「会阻塞的慢操作」（如等容器启动、抢文件锁）时，用 `to_thread` 把它丢到一条旁路线程去跑，免得卡住主事件循环。`acquire_async` 把所有阻塞操作都这样卸载（§5.1）。

---

## §3 整体结构（三层：provider ↔ backend ↔ AioSandbox）

```
config (sandbox_config: use/image/port/replicas/idle_timeout/mounts/environment/provisioner_url)
  │
  ▼
┌─────────────────────────────────────────────────────────────────┐
│ AioSandboxProvider（aio_sandbox_provider.py · 911 行）          │
│  管「谁在用哪个容器」：进程内缓存 + 暖池 + idle 回收 + 优雅关闭 │
│  ─ acquire(thread_id) ──► 四级缓存定位 sandbox_id               │
│  ─ release(id) ─────────► 停进暖池（容器继续跑）                │
│  ─ shutdown() ──────────► 停 idle 线程 + destroy 全部            │
└──────────────┬──────────────────────────────────────────────────┘
               │ 组合一个 backend（怎么把容器弄起来）
               ▼
┌──────────────────────────────────────┐   二选一（看 provisioner_url）
│ SandboxBackend（backend.py · ABC）   │
│  create / destroy / is_alive /        │
│  discover / list_running              │
└────┬──────────────────────┬───────────┘
     │                      │
     ▼                      ▼
┌────────────────┐   ┌──────────────────┐
│ LocalContainer │   │ RemoteSandbox    │
│ Backend        │   │ Backend          │
│ (Docker/Apple  │   │ (K8s provisioner │
│  CLI 编排)     │   │  薄 HTTP client) │
└────────────────┘   └──────────────────┘

         backend 起好容器、返回 SandboxInfo（sandbox_url）
               │
               ▼
┌──────────────────────────────────────────────────┐
│ AioSandbox（aio_sandbox.py · Sandbox 子类）       │
│  拿 sandbox_url 连容器，经 agent_sandbox SDK 调   │
│  shell.exec_command / file.read_file 等 HTTP API │
│  实现 Sandbox 的 8 个抽象方法（与 Local 同接口）  │
└──────────────────────────────────────────────────┘
```

### 文件结构

```
community/
├── __init__.py                       # 社区扩展包（按需 import 子模块，不 eager import）
└── aio_sandbox/
    ├── __init__.py                   # 导出 AioSandbox / AioSandboxProvider / backends / SandboxInfo
    ├── sandbox_info.py   (50 行)     # SandboxInfo dataclass（跨进程发现元数据）
    ├── backend.py       (119 行)     # SandboxBackend ABC + wait_for_sandbox_ready[_async]
    ├── local_backend.py (586 行)     # LocalContainerBackend（Docker/Apple Container CLI 编排）
    ├── remote_backend.py(193 行)     # RemoteSandboxBackend（K8s provisioner 薄 HTTP client）
    ├── aio_sandbox.py   (340 行)     # AioSandbox（HTTP client Sandbox 实现，soft-load agent_sandbox）
    └── aio_sandbox_provider.py(911行)# AioSandboxProvider（暖池 + 跨进程锁 + idle + shutdown）

utils/network.py                      # get_free_port / release_port（容器端口分配，复用 #4）
config/sandbox_config.py              # + provisioner_url 字段（选远端 vs 本地 backend）
```

> **为什么放 `community/`？** 社区扩展按需启用、soft-load 外部 SDK。AIO 依赖 `agent_sandbox` SDK + Docker/K8s，属「按需启用」，故归 `community/`。`LocalSandboxProvider`（核心、无外部重依赖）在 [sandbox/local/](../backend/packages/harness/deerflow/sandbox/local/)（见 [sandbox.md](sandbox.md)）。

---

## §4 核心概念

### 4.1 SandboxBackend（怎么把容器弄起来）

「backend」回答「**怎么起 / 查活没活 / 销毁一个容器**」。它是个 ABC（[backend.py:76](../backend/packages/harness/deerflow/community/aio_sandbox/backend.py#L76)），4 个抽象方法 + 1 个带默认实现的 `list_running`：

```python
class SandboxBackend(ABC):
    def create(self, thread_id, sandbox_id, extra_mounts=None) -> SandboxInfo: ...   # 起一个新容器
    def destroy(self, info: SandboxInfo) -> None: ...                                 # 销毁容器
    def is_alive(self, info: SandboxInfo) -> bool: ...                                # 轻量存活检查（不打 HTTP）
    def discover(self, sandbox_id: str) -> SandboxInfo | None: ...                    # 跨进程发现已存在容器
    def list_running(self) -> list[SandboxInfo]: ...                                  # 枚举运行中容器（默认空，启动 reconcile 用）
```

两种实现：

- **`LocalContainerBackend`**（[local_backend.py:186](../backend/packages/harness/deerflow/community/aio_sandbox/local_backend.py#L186)）：本机起 Docker / Apple Container。自管端口分配、容器命名、卷挂载、健康检查。靠 `docker`/`container` CLI（subprocess），不用 docker-py。
- **`RemoteSandboxBackend`**（[remote_backend.py:38](../backend/packages/harness/deerflow/community/aio_sandbox/remote_backend.py#L38)）：连远端 provisioner（K8s）。Pod 生命周期委托给 provisioner 服务，本地只是个薄 HTTP client（`POST/DELETE/GET /api/sandboxes`）。

provider 按 `config.sandbox.provisioner_url` 选哪个：设了 → 远端，没设 → 本地（[aio_sandbox_provider.py:190](../backend/packages/harness/deerflow/community/aio_sandbox/aio_sandbox_provider.py#L190)）。

**类比**：backend 像「租车公司」——你可以自己买车上牌（本地 Docker），也可以叫网约车（远端 K8s provisioner 派车）。provider（下面）只管「我要一辆车」，不关心车怎么来的。

### 4.2 SandboxInfo（跨进程发现的「寻人启事」）

一个 dataclass（[sandbox_info.py:14](../backend/packages/harness/deerflow/community/aio_sandbox/sandbox_info.py#L14)）：`sandbox_id` + `sandbox_url` + `container_name/id` + `created_at`。它持久化容器的连接信息，让**另一个进程**能发现并复用前一个进程起的容器。

```python
@dataclass
class SandboxInfo:
    sandbox_id: str                      # 确定性 id（thread_id 哈希派生）
    sandbox_url: str                     # 容器 API 地址，如 http://localhost:8080
    container_name: str | None = None    # 仅本地容器 backend 用
    container_id: str | None = None      # 仅本地容器 backend 用
    created_at: float = time.time()      # 创建时间戳，供 idle 判定 / 孤儿收养排序
```

为什么需要跨进程？gateway 和 `langgraph dev` 是两个进程；多 worker；K8s 多 pod 共享存储。若进程 A 起了容器 `deer-flow-sandbox-c3a1b2f0`，进程 B 不该再起一个——它应该「发现」A 起的那个并连上去（§4.4 的确定性 id + 文件锁就是干这个的）。

### 4.3 暖池（warm pool）

`release` 一个沙箱时**不停容器**，而是把它「停」进暖池（容器还跑着，只是没线程在用，[aio_sandbox_provider.py:828](../backend/packages/harness/deerflow/community/aio_sandbox/aio_sandbox_provider.py#L828)）。下次同 thread `acquire` 时从暖池秒级回收——**免冷启动**。

暖池条目只在两种情况被清：① `replicas` 软上限到了，淘汰最老的腾位（[aio_sandbox_provider.py:763](../backend/packages/harness/deerflow/community/aio_sandbox/aio_sandbox_provider.py#L763)）；② idle 超时（默认 10 分钟）没人用（[aio_sandbox_provider.py:358](../backend/packages/harness/deerflow/community/aio_sandbox/aio_sandbox_provider.py#L358)）。

### 4.4 确定性 sandbox_id + 跨进程文件锁

```python
@staticmethod
def _deterministic_sandbox_id(thread_id: str) -> str:
    return hashlib.sha256(thread_id.encode()).hexdigest()[:8]   # aio_sandbox_provider.py:278
```

`sandbox_id` 由 `thread_id` 哈希取前 8 位——**确定性**：所有进程对同一 `thread_id` 派生出同一 id，从而推出同一容器名（`{prefix}-{sandbox_id}`，[local_backend.py:252](../backend/packages/harness/deerflow/community/aio_sandbox/local_backend.py#L252)）。这让 backend `discover(sandbox_id)` 能凭名字找到别人起的容器。

但「发现」有竞态：两个进程同时为同 thread 建容器，会撞容器名冲突。故建容器前先抢 `{thread_dir}/{sandbox_id}.lock` 文件锁（`fcntl.flock` 排他锁，[aio_sandbox_provider.py:70](../backend/packages/harness/deerflow/community/aio_sandbox/aio_sandbox_provider.py#L70)）：

```python
with open(lock_path, "a") as lock_file:
    _lock_file_exclusive(lock_file)                    # 后到的进程在此阻塞
    cached_id = self._recheck_cached_sandbox(...)      # 等锁期间本进程另一线程可能已赢
    discovered = self._backend.discover(sandbox_id)    # 发现先到进程起的容器
    if discovered is not None:
        return self._register_discovered_sandbox(...)  # 复用而非撞名
    return self._create_sandbox(...)                   # 都没有才真建
```

> **确定性 id 和文件锁缺一不可**：光有确定性 id，两进程同时建会撞名；光有锁没确定性 id，后到进程不知道该 discover 什么名字。两者配合才安全。

### 4.5 四级缓存（acquire 依次试，越快越先试）

```
① 进程内活跃缓存  ── 命中（同进程同 thread 重复访问）→ 秒回
       ↓ miss
② 暖池复用         ── 命中（容器还跑着，免冷启动）→ 新建 AioSandbox client 连上
       ↓ miss
③ 跨进程文件锁内 backend discover ── 命中（别的进程起的容器）→ 注册并连
       ↓ miss
④ backend create   ── 真起一个新容器 + 等就绪（冷启动）
```

对应 `_acquire_internal`（[aio_sandbox_provider.py:659](../backend/packages/harness/deerflow/community/aio_sandbox/aio_sandbox_provider.py#L659)）。每级都先做**健康检查**（`is_alive`），失败就丢弃重建（[aio_sandbox_provider.py:592](../backend/packages/harness/deerflow/community/aio_sandbox/aio_sandbox_provider.py#L592)），避免复用一个已死的容器。

### 4.6 与上游 deer-flow 的关系（先看这句）

> **结论先放这**：mini 的 AIO 沙箱是上游 `deer-flow/.../community/aio_sandbox/` 的**忠实移植**——下面 §5 讲的四级缓存、`fcntl.flock` 跨进程锁、孤儿收养（reconcile）、优雅关闭（shutdown）、`ErrorObservation` 重试、`close()` 防 socket 泄漏，**上游源码里全有**，不是 mini 自己长出来的。真正的实现差异很小（user_id 不显式穿参 + 几个 mini 新增的小 helper），详见 §9。

---

## §5 代码走读

### 5.1 `AioSandboxProvider`：管「谁在用哪个容器」

provider 自己**不碰容器**（那是 backend 的活），它管的是 6 个进程内 map + 一把全局锁（[aio_sandbox_provider.py:146](../backend/packages/harness/deerflow/community/aio_sandbox/aio_sandbox_provider.py#L146)）：

```python
self._lock = threading.Lock()
self._sandboxes: dict[str, AioSandbox]              = {}   # sandbox_id -> 活跃 AioSandbox 实例
self._sandbox_infos: dict[str, SandboxInfo]         = {}   # sandbox_id -> 连接信息（destroy 用）
self._thread_sandboxes: dict[str, str]              = {}   # thread_id  -> sandbox_id
self._thread_locks: dict[str, threading.Lock]       = {}   # thread_id  -> 进程内锁（串行同 thread 的 acquire）
self._last_activity: dict[str, float]               = {}   # sandbox_id -> 最后活动时间戳（idle 判定）
self._warm_pool: dict[str, tuple[SandboxInfo, float]] = {} # sandbox_id -> (info, release_ts)（暖池）
```

构造时（[aio_sandbox_provider.py:160](../backend/packages/harness/deerflow/community/aio_sandbox/aio_sandbox_provider.py#L160)）依次：读 config → 选 backend → 注册 `atexit` + 信号 handler → `_reconcile_orphans` 收养孤儿 → 若 `idle_timeout > 0` 起后台 idle 检查线程。

#### acquire：四级缓存定位 sandbox_id

入口 `acquire(thread_id)`（[aio_sandbox_provider.py:631](../backend/packages/harness/deerflow/community/aio_sandbox/aio_sandbox_provider.py#L631)）。有 `thread_id` 时先抢该 thread 的**进程内锁**（[aio_sandbox_provider.py:439](../backend/packages/harness/deerflow/community/aio_sandbox/aio_sandbox_provider.py#L439)）——把同 thread 的并发 acquire 串行化，再进 `_acquire_internal`：

```python
def _acquire_internal(self, thread_id):
    cached_id = self._reuse_in_process_sandbox(thread_id)          # ① 进程内活跃缓存
    if cached_id is not None: return cached_id
    sandbox_id = self._sandbox_id_for_thread(thread_id)            # 确定性 id（或匿名随机）
    reclaimed_id = self._reclaim_warm_pool_sandbox(thread_id, sandbox_id)  # ② 暖池
    if reclaimed_id is not None: return reclaimed_id
    if thread_id:
        return self._discover_or_create_with_lock(thread_id, sandbox_id)   # ③+④ 文件锁内 discover/create
    return self._create_sandbox(thread_id, sandbox_id)
```

每级命中前都查 `is_alive`（[aio_sandbox_provider.py:551](../backend/packages/harness/deerflow/community/aio_sandbox/aio_sandbox_provider.py#L551)）：明确死了就 `_drop_unhealthy_sandbox` 丢弃并销毁（[aio_sandbox_provider.py:592](../backend/packages/harness/deerflow/community/aio_sandbox/aio_sandbox_provider.py#L592)），查不出（`None`）就当活着继续用——**fail-open**，免得瞬时 daemon 故障误杀。

还有一个 `acquire_async`（[aio_sandbox_provider.py:644](../backend/packages/harness/deerflow/community/aio_sandbox/aio_sandbox_provider.py#L644)）：镜像 `acquire`，但阻塞操作（backend create、就绪轮询、文件锁）全 `asyncio.to_thread` 跑线程外，不卡事件循环。锁的 await 用专门 executor + `asyncio.shield` + 取消回调释放（[aio_sandbox_provider.py:92](../backend/packages/harness/deerflow/community/aio_sandbox/aio_sandbox_provider.py#L92)），避免轮询和默认 executor。

#### release：停进暖池，关旧 HTTP client

```python
def release(self, sandbox_id):
    with self._lock:
        sandbox = self._sandboxes.pop(sandbox_id, None)            # 从活跃 map 移除
        info = self._sandbox_infos.pop(sandbox_id, None)
        # ... 清 _thread_sandboxes / _last_activity
        if info and sandbox_id not in self._warm_pool:
            self._warm_pool[sandbox_id] = (info, time.time())      # 停进暖池，容器继续跑
    if sandbox is not None:
        sandbox.close()                                            # 关旧 HTTP client，防套接字泄漏
```

关键：暖池只存 `SandboxInfo`（轻量），不存 `AioSandbox` 实例。回收时新建一个 `AioSandbox`（与 client）。否则长跑 gateway 会累积一堆没关的 httpx client → 套接字泄漏（`close()` 见 §5.3）。

#### idle 回收：销毁前锁内再验

后台线程每 60s（`IDLE_CHECK_INTERVAL`）跑一次 `_cleanup_idle_sandboxes`（[aio_sandbox_provider.py:358](../backend/packages/harness/deerflow/community/aio_sandbox/aio_sandbox_provider.py#L358)）。它在锁内快照出「该销毁的」列表（按 `_last_activity` / 暖池 release_ts 判超时），然后**销毁前在锁内再验一次**仍空闲：

```python
for sandbox_id in active_to_destroy:
    with self._lock:
        last_activity = self._last_activity.get(sandbox_id)
        if last_activity is None: continue                         # 已被 release/destroy
        if (time.time() - last_activity) < idle_timeout: continue  # 快照到现在期间被 re-acquire
    self.destroy(sandbox_id)                                       # 锁外真销毁
```

为什么再验？因为「快照」到「动手」之间有时间窗，沙箱可能已被 re-acquire（`last_activity` 更新）或已 release/destroy。不验就会误杀刚被重新启用的沙箱。

#### 启动收养孤儿（reconcile）

进程重启 / 崩溃 / SIGKILL 后，内存状态丢失，但 Docker 容器还跑着（成了「孤儿」）。`_reconcile_orphans`（[aio_sandbox_provider.py:242](../backend/packages/harness/deerflow/community/aio_sandbox/aio_sandbox_provider.py#L242)）调 `backend.list_running()` 枚举所有匹配前缀的运行中容器，**全收养**进暖池：

```python
for info in running:
    with self._lock:                                               # 每容器单次锁：原子 check-and-insert
        if info.sandbox_id in self._sandboxes or info.sandbox_id in self._warm_pool:
            continue
        self._warm_pool[info.sandbox_id] = (info, current_time)
```

**无条件全收养**是因为光凭 age 分不清「孤儿」与「另一进程正在用」——`idle_timeout` 表「不活跃」非「uptime」。收养进暖池让 idle 检查器决定，避免误毁并发进程正用的容器。

#### 优雅关闭（shutdown）

注册 `SIGTERM` / `SIGINT` / `SIGINT` / `SIGHUP`（终端关闭）+ `atexit`（[aio_sandbox_provider.py:406](../backend/packages/harness/deerflow/community/aio_sandbox/aio_sandbox_provider.py#L406)）。`shutdown()`（[aio_sandbox_provider.py:878](../backend/packages/harness/deerflow/community/aio_sandbox/aio_sandbox_provider.py#L878)）幂等：锁内置 `_shutdown_called` 标志防重入 → 停 idle 检查线程（`join(timeout=5)`）→ 逐个 `destroy` 活跃 + 暖池沙箱。信号 handler 调完 `shutdown` 后链回原信号处理器（或恢复 `SIG_DFL` 重抛），确保用户的 Ctrl-C 仍能终止进程。

### 5.2 backend：怎么把容器弄起来

#### LocalContainerBackend（Docker / Apple Container CLI）

**runtime 探测**（[local_backend.py:224](../backend/packages/harness/deerflow/community/aio_sandbox/local_backend.py#L224)）：macOS 优先 Apple Container（`container --version` 探一下），没有回退 Docker；其它平台直接 Docker。

**create**（[local_backend.py:246](../backend/packages/harness/deerflow/community/aio_sandbox/local_backend.py#L246)）有两个重试逻辑：

```python
for _attempt in range(10):
    port = get_free_port(start_port=_next_start)
    try:
        container_id = self._start_container(container_name, port, extra_mounts)
        break
    except RuntimeError as exc:
        release_port(port)
        if "port is already allocated" in err:        # Docker 释端口异步 → 换下一个端口重试
            _next_start = port + 1; continue
        if "is already in use by container" in err:   # 容器名冲突 → 另一进程已起，discover 收养
            existing = self.discover(sandbox_id)
            if existing is not None: return existing
        raise
```

- **端口冲突重试**：`get_free_port`（[utils/network.py](../backend/packages/harness/deerflow/utils/network.py)）用 socket bind 检查镜像 Docker 的 `0.0.0.0` 绑定，但 Docker 释端口有微秒级异步——socket bind 通过、Docker 实际还占着。故最多换 10 个端口重试。
- **容器名冲突 → 发现**：报「container name already in use」说明另一进程已起同名容器，走 `discover` 收养而非报错。这是跨进程发现的设计意图。

**destroy**（[local_backend.py:290](../backend/packages/harness/deerflow/community/aio_sandbox/local_backend.py#L290)）：`docker stop`（`--rm` 确保自动移除）+ 从 `sandbox_url` 抽端口 `release_port`。

**is_alive**（[local_backend.py:305](../backend/packages/harness/deerflow/community/aio_sandbox/local_backend.py#L305)）：轻量 `docker inspect -f {{.State.Running}}`，不打 HTTP。

**discover**（[local_backend.py:311](../backend/packages/harness/deerflow/community/aio_sandbox/local_backend.py#L311)）：按确定性名查同名容器是否在跑 → 取端口 → `wait_for_sandbox_ready` 健康检查（5s 超时）通过才返回 `SandboxInfo`。

**list_running**（[local_backend.py:349](../backend/packages/harness/deerflow/community/aio_sandbox/local_backend.py#L349)）：**2 次 subprocess** 搞定——① `docker ps --filter name={prefix}-` 列名（`--filter` 是子串匹配，故二次 `startswith` 精确过滤前缀）；② 单次批量 `docker inspect` 取所有容器的创建时间 + 端口映射（[local_backend.py:418](../backend/packages/harness/deerflow/community/aio_sandbox/local_backend.py#L418)）。朴素做法是每容器 2 次（2N+1），容器多了慢。

**环境变量脱敏**（[local_backend.py:93](../backend/packages/harness/deerflow/community/aio_sandbox/local_backend.py#L93)）：日志里的 `-e KEY=value`，value 脱敏成 `KEY=<redacted>`，防密钥泄露（容器实际注入的值不变）。

**Docker 端口绑定收窄**（[local_backend.py:145](../backend/packages/harness/deerflow/community/aio_sandbox/local_backend.py#L145)）：裸机/本地经 localhost 访问，绑 `127.0.0.1`（不暴露到所有网卡）；DooD 从另一容器经 `host.docker.internal` 访问，保留宽绑定除非用 `DEER_FLOW_SANDBOX_BIND_HOST` 收窄。避免把沙箱 HTTP API 暴露到公网。

#### RemoteSandboxBackend（K8s provisioner 薄 HTTP client）

5 个方法全是对 provisioner 的 HTTP 调用（[remote_backend.py](../backend/packages/harness/deerflow/community/aio_sandbox/remote_backend.py)）：

| 方法 | provisioner API | 干嘛 |
|------|-----------------|------|
| `create` | `POST /api/sandboxes` | 建 Pod + NodePort Service（带 `thread_id` + `user_id`，[remote_backend.py:121](../backend/packages/harness/deerflow/community/aio_sandbox/remote_backend.py#L121)） |
| `destroy` | `DELETE /api/sandboxes/{id}` | 销毁 Pod + Service |
| `is_alive` | `GET /api/sandboxes/{id}` | 查 Pod phase（`status == "Running"`） |
| `discover` | `GET /api/sandboxes/{id}` | 发现已存在沙箱（404 → `None`） |
| `list_running` | `GET /api/sandboxes` | 列所有运行中沙箱（reconcile 用） |

provisioner 在 k3s 里按 `sandbox_id` 动态建 Pod + NodePort Service，本 backend 直接经 `k3s:{NodePort}` 访问沙箱 pod。本地不持有容器句柄、不管端口。

### 5.3 AioSandbox：经 HTTP API 操作容器

`AioSandbox`（[aio_sandbox.py:63](../backend/packages/harness/deerflow/community/aio_sandbox/aio_sandbox.py#L63)）继承 `Sandbox`，实现 8 个抽象方法的 HTTP 版本。它不自己起容器，拿一个 `base_url` 连到已就绪的容器。

#### 命令串行化 + ErrorObservation 重试

AIO 容器维护**单个**持久 shell session。并发 `exec_command` 会把 session 搞坏，返回 `ErrorObservation`（非真输出，源码注释标 #1433）。故 `execute_command`（[aio_sandbox.py:145](../backend/packages/harness/deerflow/community/aio_sandbox/aio_sandbox.py#L145)）用 `self._lock` 串行化；即便加了锁仍检测到 `ErrorObservation` 签名（如多进程共享同一沙箱），就在**新 session** 上重试一次：

```python
with self._lock:
    result = self._client.shell.exec_command(command=command, no_change_timeout=600)
    output = result.data.output if result.data else ""
    if output and _ERROR_OBSERVATION_SIGNATURE in output:        # aio_sandbox.py:54
        fresh_id = str(uuid.uuid4())
        self._client.shell.create_session(id=fresh_id)           # 显式建恢复 session
        try:
            result = self._client.shell.exec_command(command=command, id=fresh_id, ...)
            output = result.data.output if result.data else ""
        finally:
            try: self._client.shell.cleanup_session(fresh_id)    # 显式拆，清理失败只 warning
            except Exception as cleanup_error: logger.warning(...)
    return output if output else "(no output)"
```

> **恢复 session 显式建/拆**：重试不是直接 `exec_command(id=fresh_id)` 了事——那样 fresh_id 对应的 session 从未被显式创建、事后也没人清理，每次错误恢复都泄漏一个 session。修法是先 `create_session`、`try` 里跑、`finally` 里 `cleanup_session`（清理本身失败只 warning 不抛，免得掩盖原异常）。

#### close()：沿属性链关 httpx

`agent_sandbox` SDK 是 Fern 生成的，没暴露 `close()` / `__exit__`。`close()`（[aio_sandbox.py:92](../backend/packages/harness/deerflow/community/aio_sandbox/aio_sandbox.py#L92)，源码注释标 #2872）沿属性链摸到真正的 `httpx.Client`（socket 持有者）显式关：

```
Sandbox._client_wrapper     -> SyncClientWrapper
    .httpx_client           -> Fern HttpClient（wrapper，非 httpx.Client）
        .httpx_client       -> httpx.Client     <- 真正的 socket 持有者
```

取「第一个暴露 `close()` 的对象」（[aio_sandbox.py:120](../backend/packages/harness/deerflow/community/aio_sandbox/aio_sandbox.py#L120)），未来 SDK 加了顶层 `close()` 也自动用上。幂等（`_closed` 标志）、线程安全（锁内丢引用做 use-after-close 安全）、非致命（拆解失败吞日志）。

#### download_file：显式防穿越 + 100MB 上限

AIO 把路径**原样**转发给容器 API（不像 `LocalSandbox` 经 `_resolve_path` 隐式翻译防穿越）。故 `download_file`（[aio_sandbox.py:184](../backend/packages/harness/deerflow/community/aio_sandbox/aio_sandbox.py#L184)）先显式查 `..` 段 + 校验在 `/mnt/user-data` 前缀内，再流式分块读：

```python
for segment in normalised.split("/"):
    if segment == "..": raise PermissionError(...)               # 防 .. 穿越
if not stripped_path.startswith(f"{allowed_prefix}/"): raise PermissionError(...)  # 防逃出挂载根
for chunk in self._client.file.download_file(path=path):
    total += len(chunk)
    if total > _MAX_DOWNLOAD_SIZE: raise OSError(errno.EFBIG, ...) # 100MB 上限（aio_sandbox.py:51）
```

#### 其余 6 个方法

- `read_file` / `write_file`（[aio_sandbox.py:175](../backend/packages/harness/deerflow/community/aio_sandbox/aio_sandbox.py#L175) / [238](../backend/packages/harness/deerflow/community/aio_sandbox/aio_sandbox.py#L238)）：直接调容器 `file.read_file` / `file.write_file`，`append=True` 先读旧内容再拼接。
- `list_dir`（[aio_sandbox.py:225](../backend/packages/harness/deerflow/community/aio_sandbox/aio_sandbox.py#L225)）：经 `find -maxdepth` 列目录。
- `glob` / `grep`（[aio_sandbox.py:251](../backend/packages/harness/deerflow/community/aio_sandbox/aio_sandbox.py#L251) / [277](../backend/packages/harness/deerflow/community/aio_sandbox/aio_sandbox.py#L277)）：**远端搜本端滤**——候选文件用容器 API（`find_files` / `list_path` / `search_in_file`）拿，本端用 [search.py](../backend/packages/harness/deerflow/sandbox/search.py) 的 `should_ignore_path` / `path_matches` / `truncate_line` 过滤噪音 + 匹配 + 截断。
- `update_file`（[aio_sandbox.py:332](../backend/packages/harness/deerflow/community/aio_sandbox/aio_sandbox.py#L332)）：二进制内容 base64 编码经 HTTP 写（容器 API 要文本传输）。

### 5.4 soft-load：缺包不炸模块

```python
try:
    from agent_sandbox import Sandbox as AioSandboxClient
    _HAS_AGENT_SANDBOX = True
except ImportError:                              # aio_sandbox.py:45
    AioSandboxClient = None
    _HAS_AGENT_SANDBOX = False
```

`agent_sandbox` SDK 缺包时 `AioSandboxClient = None`，但**类定义不依赖 SDK**，故模块能正常 import、`AioSandbox` / `AioSandboxProvider` 能被引用。只有真正实例化 `AioSandbox` 时（[aio_sandbox.py:57](../backend/packages/harness/deerflow/community/aio_sandbox/aio_sandbox.py#L57) 的 `_require_agent_sandbox`）才抛带可操作安装提示的 `ImportError`。这让「没装 AIO extra 的用户」完全不受影响——provider 的 backend 选择 / acquire 才触发。

### 5.5 数据流：agent 调一次 AIO `bash` 端到端怎么走完

把上面三层（provider / backend / AioSandbox）串起来——agent 第一次在线程 T 里跑 `python3 main.py`、且尚无任何容器：

```
agent tool_call: bash(command="python3 /mnt/user-data/workspace/main.py")
   │
   ① AioSandboxProvider.acquire(thread_id="T")              [aio_sandbox_provider.py:631]
        _acquire_internal 四级缓存依次 miss（进程内活跃空 / 暖池空 / 文件锁内 discover 无）
        → _create_sandbox：backend.create() 起 Docker 容器 + wait_for_sandbox_ready 轮询健康端点
        → 缓存 sandbox_id + 建 AioSandbox client 连容器
   │
   ② AioSandbox.execute_command(command)                     [aio_sandbox.py:145]
        with self._lock:   ← 串行化（容器只有单个持久 shell session）
        client.shell.exec_command(command, no_change_timeout=600)
        若返回 ErrorObservation → create_session + 重试 + finally cleanup_session
   │
   ③ 输出原样返回（AIO 路径原样转发，不经 Local 的 _resolve_path 翻译；
        虚拟路径 /mnt/user-data 经 Docker Volume 真实挂进容器，本就是容器内真路径）
   │
   ④ release（线程结束）→ 容器不停，停进暖池；下次同 thread acquire 走第②级缓存秒回（免冷启动）
```

**三个关键点**：① 首次的昂贵在「起容器 + 等就绪」（冷启动），故四级缓存 + 暖池把后续访问压到秒级；② AIO 不像 Local 翻译路径——虚拟路径经 Volume 直接挂进容器，所以路径原样转发（这也是 `download_file` 要显式查 `..` 的原因，§5.3）；③ `self._lock` 串行化保容器的单 session 不被并发撞坏，撞坏了还有 ErrorObservation 重试兜底。

---

## §6 设计动机分析（为什么这么设计 / 作用 / 好处）

### 6.0 核心设计动机（先看这五个「为什么」）

**① 为什么上容器隔离（AIO），不就用本地沙箱？**
- **作用**：把每个沙箱关进 Docker / Apple Container，agent 的破坏性命令只作用在容器内。
- **好处**：真正的 OS 级隔离（namespace）——能安全跑 untrusted 代码、多租户、生产部署。host bash 因此对 AIO 自动放行（容器内 `rm -rf /` 只删容器）。
- **不这么设计会怎样**：本地沙箱非安全边界（[#13 §6.0 ④](sandbox.md)），跑 untrusted 代码一次穿越就逃逸；多租户互相能看到对方文件。

**② 为什么用暖池（warm pool），release 不停容器？**
- **作用**：release 时把容器「停进暖池」（继续跑、只是没人用），下次同 thread 秒级回收。
- **好处**：**免冷启动**——起容器要几秒到几十秒（拉镜像+启动+等就绪），暖池让重复访问秒回。
- **不这么设计会怎样**：每次 release 就 destroy 容器 → 下次 acquire 又冷启动，用户每轮对话等十几秒。

**③ 为什么确定性 sandbox_id + fcntl.flock 配合？**
- **作用**：`sandbox_id = sha256(thread_id)[:8]` 让所有进程对同 thread 算出同一 id / 同名容器；建容器前抢 `.lock` 文件锁防撞名。
- **好处**：**跨进程共享容器**——进程 A 起的容器进程 B 能 discover 复用，不重复起。光有确定性 id 两进程同时建会撞名，光有锁没确定性 id 后到进程不知该 discover 什么——两者缺一不可。
- **不这么设计会怎样**：两进程同时为同 thread 各起一个容器（撞名冲突 / 浪费资源）；或各进程独立起容器无法共享。

**④ 为什么四级缓存（活跃→暖池→锁内 discover→create），越快越先试？**
- **作用**：acquire 依次试 4 级命中，每级先查 `is_alive` 健康检查，明确死了才丢弃重建。
- **好处**：最快的最先试——进程内命中零开销，暖池免冷启动，discover 免建容器，create 是最后手段。
- **不这么设计会怎样**：每次都 create 新容器 → 全是冷启动；或复用不查健康 → 复用一个已死容器，调用全失败。

**⑤ 为什么 soft-load `agent_sandbox` SDK？**
- **作用**：import 时试装 SDK，缺了设 `None` 不报错，真正实例化 `AioSandbox` 时才报带安装提示的错。
- **好处**：没装 AIO extra 的用户零影响（模块能 import、其它路径不受影响）；CI 不装 extra 也能跑 soft-load 分支测试。
- **不这么设计会怎样**：SDK 必装 → 没装的用户 import 整个 community 模块就崩，或 CI 必须装 Docker/SDK 这类重依赖。

---

| 权衡 | 选择 | 理由 |
|------|------|------|
| **缓存层级** | 四级，越快越先试 | 进程内命中免一切开销；暖池免冷启动；discover 免建容器；create 是最后手段 |
| **每级命中前查 `is_alive`** | fail-open（查不出当活着） | 免瞬时 daemon 故障误杀健康容器；明确死了才丢弃重建 |
| **replicas 是软上限** | 超限淘汰**暖池**最老的，不强停活跃 | 守预算但不能掐断正在跑的任务；所有槽被活跃占就照建并 warning |
| **idle 回收销毁前锁内再验** | 再查一次 `last_activity` | 快照到动手有时间窗，防误杀刚 re-acquire 的沙箱 |
| **启动无条件全收养孤儿** | 进暖池让 idle 决定 | 光凭 age 分不清孤儿与「另一进程在用」；idle 表不活跃非 uptime |
| **release 关旧 HTTP client** | 暖池只存 `SandboxInfo` | 防长跑 gateway 累积未关 httpx client → 套接字泄漏 |
| **host bash 对 AIO 自动放行** | `is_host_bash_allowed` 对非 local 返回 True（[security.py:66](../backend/packages/harness/deerflow/sandbox/security.py#L66)） | AIO 有真隔离，host bash 安全；`allow_host_bash` 只对 Local 生效 |
| **soft-load SDK** | 缺包不炸模块，只实例化时报 | 没装 AIO extra 的用户零影响；CI 不装 extra 也能跑 soft-load 分支测试 |
| **环境变量脱敏 + 端口绑定收窄** | 日志脱敏、localhost 绑 127.0.0.1 | 防密钥进日志、防沙箱 API 暴露公网 |

---

## §7 配置用法

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
    API_KEY: $MY_API_KEY
```

> `$` 开头的环境变量值会从宿主环境解析（[aio_sandbox_provider.py:228](../backend/packages/harness/deerflow/community/aio_sandbox/aio_sandbox_provider.py#L228)）。线程级挂载（workspace/uploads/outputs）+ skills 挂载由 provider 自动加（[aio_sandbox_provider.py:287](../backend/packages/harness/deerflow/community/aio_sandbox/aio_sandbox_provider.py#L287)），不用手配。

### 配置 —— 远端 K8s provisioner 模式

```yaml
sandbox:
  use: deerflow.community.aio_sandbox:AioSandboxProvider
  provisioner_url: http://provisioner:8002   # 设了 → RemoteSandboxBackend，否则 → 本地
```

### 安装 SDK

```bash
uv pip install 'deerflow-harness[aio_sandbox]'   # 装 agent-sandbox + requests（pyproject.toml 定义）
```

### 跑测试

```bash
cd backend && make test    # 含 test/test_aio_sandbox.py（64 个 hermetic 测试，全 mock）
```

测试约定（[test_aio_sandbox.py](../test/test_aio_sandbox.py)）：`agent_sandbox` SDK 未装（CI 不装 extra）→ soft-load 分支天然可测；带 SDK 的行为用 fake client（`AioSandboxClient` 替身）测；`LocalContainerBackend` 的 `subprocess.run`、`RemoteSandboxBackend` 的 `requests`、provider 的 backend 全 mock；provider 构造的信号注册 + idle 线程 monkeypatch 成 no-op + `idle_timeout=0`，避免干扰 pytest。

---

## §8 与其它模块的关系

```
config (sandbox_config: use/image/port/replicas/idle_timeout/mounts/environment/provisioner_url)
  │
config/paths (VIRTUAL_PATH_PREFIX=/mnt/user-data)
runtime/user_context (get_effective_user_id → per-user 隔离路径，见 #18 memory 的 user_id 契约)
  │
sandbox (#13：Sandbox 抽象 + SandboxProvider ABC + security + search)  ← AIO 复用
  │   ▲
  │   └── AioSandbox 继承 Sandbox（8 抽象方法），AioSandboxProvider 继承 SandboxProvider
  │
community/aio_sandbox ◄── AioSandboxProvider
  │   ├── LocalContainerBackend ── Docker/Apple Container CLI（subprocess）
  │   └── RemoteSandboxBackend ─── K8s provisioner（requests HTTP）
  │
utils/network (#4：get_free_port/release_port)  ← LocalContainerBackend 端口分配
  │
tools (#22：7 工具 bash/ls/glob/grep/read_file/write_file/str_replace)
  │   └── 对 Local / AIO provider 透明——都经 get_sandbox_provider() 取实例
middlewares (#24：SandboxMiddleware 延迟初始化 + Command 写回)
```

- **上游依赖 [#13 sandbox.md](sandbox.md)**：`Sandbox` 抽象基类、`SandboxProvider` ABC、`security.is_host_bash_allowed`（[security.py:53](../backend/packages/harness/deerflow/sandbox/security.py#L53)）、`search.py`（glob/grep 本端过滤）、`local_sandbox.ensure_thread_dirs`（建线程目录，复用）。
- **下游**：与 `LocalSandboxProvider` 二选一（`config.sandbox.use` 切换）。工具层（[#22 tools.md](tools.md) 的 7 工具）和中间件（[#24 middlewares.md](middlewares.md) 的 `SandboxMiddleware`）对两者透明——都经 `get_sandbox_provider()` 取实例，agent 代码无感。
- **同级社区扩展 [#21 community.md](community.md)**：`community/` 还会放联网 provider（ddg/tavily/jina），与 AIO 共用「soft-load 外部 SDK」约定。
- **user_id 隔离**：挂载用 `get_effective_user_id()`（[aio_sandbox_provider.py:309](../backend/packages/harness/deerflow/community/aio_sandbox/aio_sandbox_provider.py#L309)）按用户隔离路径，契约同 [#18 memory.md](memory.md)。

---

## §9 实现差异（vs 上游 deer-flow 源码）

> 对照基线 = `deer-flow/backend/packages/harness/deerflow/community/aio_sandbox/`（与 mini 同布局、同 7 文件，`sandbox_info.py` 0 行差异）。已**剥 docstring/comment 后**判逻辑差。结论：**mini 的 AIO 沙箱是上游的忠实移植**——§5 讲的四层缓存 / flock 跨进程锁 / 孤儿收养 / 优雅关闭 / ErrorObservation 重试 / `close()` 防 socket 泄漏，**上游源码全有**（`aio_sandbox_provider.py` 同名方法、`aio_sandbox.py` 同名常量与 close 链一一对应）。真差异很小：

### 9.1 一致的部分（先放心）

| 维度 | 上游 deer-flow | mini |
|---|---|---|
| 四级缓存（活跃→暖池→锁内 discover→create） | 有（`_reuse_in_process_sandbox`/`_reclaim_warm_pool_sandbox`/`_discover_or_create_with_lock`） | **完全相同** |
| 跨进程 `fcntl.flock` 文件锁 | 有（`_lock_file_exclusive` + fcntl） | **相同** |
| 孤儿收养 `_reconcile_orphans` | 有 | **相同** |
| idle 回收 `_cleanup_idle_sandboxes`（销毁前锁内再验） | 有 | **相同** |
| 优雅关闭（SIGTERM/SIGINT/SIGHUP + atexit + 幂等 shutdown） | 有（`_register_signal_handlers`/`shutdown`） | **相同** |
| ErrorObservation 检测 + 新 session 重试（建/拆） | 有（`_ERROR_OBSERVATION_SIGNATURE`/`create_session`/`cleanup_session`） | **相同** |
| `close()` 沿属性链关 httpx | 有（`httpx_client`/`_closed`/`def close`） | **相同** |
| `SandboxInfo` dataclass | 有 | **`sandbox_info.py` 0 行差异** |
| backend 三文件方法面 | 有 | **公开方法清单完全一致**（仅实码细节差） |

### 9.2 mini 简化的

- **user_id 不显式穿参（同 [#13 sandbox.md](sandbox.md) §9.3）**：上游 `AioSandboxProvider` 有 `_effective_acquire_user_id` / `_thread_key`，把 `user_id` 显式穿进 acquire、按 `(thread_id, user_id)` 建沙箱；mini 删掉这两个 helper，`user_id` 下沉到路径层（建挂载目录时从 `user_context` 取，见 [#5](user_context.md)）。隔离效果一致，mini 把 user 感知收拢到路径层、provider 不感知 user——与 #13 的 LocalSandboxProvider 同一手法。

### 9.3 mini 新增的小 helper（上游无）

| mini 新增 | 干什么 | 行 |
|---|---|---|
| `needs_upload_permission_adjustment` | 返回 `isinstance(self._backend, LocalContainerBackend)`——本地容器 backend 才需调 upload 目录权限（mini uploads 读写策略相关） | [aio_sandbox_provider.py:184](../backend/packages/harness/deerflow/community/aio_sandbox/aio_sandbox_provider.py#L184) |
| `reset` | provider 重置（上游 provider 只有 `shutdown`，无 `reset`） | [aio_sandbox_provider.py:909](../backend/packages/harness/deerflow/community/aio_sandbox/aio_sandbox_provider.py#L909) |
| `_thread_lock_dir` | 线程锁文件目录计算 helper（上游内联） | [aio_sandbox_provider.py:703](../backend/packages/harness/deerflow/community/aio_sandbox/aio_sandbox_provider.py#L703) |
| `_require_agent_sandbox`（aio_sandbox.py） | soft-load 报错逻辑抽成方法（上游内联），缺包时抛带安装提示的 `ImportError` | [aio_sandbox.py:57](../backend/packages/harness/deerflow/community/aio_sandbox/aio_sandbox.py#L57) |

### 9.4 一句话总结

mini AIO 的设计原则是「**忠实移植、微调收口**」：核心生产护栏（四层缓存 / flock / reconcile / shutdown / ErrorObservation / close）与上游 deer-flow **一一对应**，不是 mini 自创；差异只有两处——把 user_id 隔离从 provider 显式穿参下沉到路径层（与 #13 sandbox 同一手法），以及加了几个小 helper（upload 权限判断 / reset / 锁目录 / soft-load 报错）。读完 mini 这篇，迁到上游 AIO 主要是「多一层 user_id 显式穿参」，核心心智模型完全不变。

---

## §10 常见问题 / 排错

**Q：配了 AIO provider，但 acquire 报 `AIO 沙箱需要 agent-sandbox SDK`？**
A：`agent_sandbox` 没装（soft-load 触发，[aio_sandbox.py:57](../backend/packages/harness/deerflow/community/aio_sandbox/aio_sandbox.py#L57)）。`uv pip install 'deerflow-harness[aio_sandbox]'`。不想用 AIO 就把 `config.sandbox.use` 改回 `deerflow.sandbox.local:LocalSandboxProvider`（见 [sandbox.md](sandbox.md)）。

**Q：容器起不来，报 `failed to become ready within timeout`？**
A：`wait_for_sandbox_ready`（[backend.py:31](../backend/packages/harness/deerflow/community/aio_sandbox/backend.py#L31)）轮询容器 `/v1/sandbox` 健康端点 60s 没就绪（[aio_sandbox_provider.py:796](../backend/packages/harness/deerflow/community/aio_sandbox/aio_sandbox_provider.py#L796)）。常见原因：镜像拉不下来（网络）、端口被占（`get_free_port` 找的端口 Docker 释放异步，已重试 10 个都不行）、镜像启动慢。看日志里 `Starting container using docker: ...`（环境变量已脱敏）确认命令对。

**Q：日志里 `port is already allocated, retrying with next port`？**
A：正常——Docker 释端口有微秒级异步，`get_free_port` 的 socket bind 检查通过但 Docker 实际还占着。backend 会自动换下一个端口重试（最多 10 个，[local_backend.py:257](../backend/packages/harness/deerflow/community/aio_sandbox/local_backend.py#L257)）。若 10 个都不行才报错（极少见，通常是 Docker daemon 卡了）。

**Q：`Container name X already in use, attempting to discover`？**
A：正常——另一进程已为同 thread 起了同名容器（确定性命名）。backend 走 `discover` 收养那个容器而不是报错（[local_backend.py:272](../backend/packages/harness/deerflow/community/aio_sandbox/local_backend.py#L272)）。这是跨进程发现的设计意图。

**Q：进程重启后，旧的容器怎么处理？**
A：`__init__` 的 `_reconcile_orphans` 会 `list_running` 枚举所有匹配前缀的运行中容器，全收养进暖池（[aio_sandbox_provider.py:242](../backend/packages/harness/deerflow/community/aio_sandbox/aio_sandbox_provider.py#L242)）。idle 检查器会在 `idle_timeout`（默认 10 分钟）后回收没人用的。所以重启不会留永久孤儿。

**Q：`replicas=3` 但我有 5 个活跃线程，会怎样？**
A：前 3 个各起一个容器。第 4、5 个 acquire 时，若暖池有空（有人 release 过）就回收暖池；否则**软上限**——照建新容器并记 warning（`beyond the soft limit`，[aio_sandbox_provider.py:620](../backend/packages/harness/deerflow/community/aio_sandbox/aio_sandbox_provider.py#L620)）。不会强停正在服务线程的容器。真正受限的是暖池：超 replicas 时淘汰暖池最老的腾位。

**Q：本地和 AIO 的工具行为一样吗？**
A：一样。两者都实现 `Sandbox` 的 8 个抽象方法，agent 用的 7 工具（[#22 tools.md](tools.md)）对两者透明。区别只在隔离强度：本地宿主机进程（非边界），AIO 容器（真隔离）。`is_host_bash_allowed` 对 AIO 自动 True（有隔离），对本地默认 False。

**Q：为什么 AIO 的 `download_file` 要显式查 `..`，Local 不用？**
A：Local 经 `_resolve_path` 翻译虚拟路径时隐式防了穿越（翻译后逃出挂载根就拒）。AIO 把路径原样转发给容器 API，没翻译步骤，故 `download_file` 显式查 `..` 段 + 校验在 `/mnt/user-data` 前缀内（[aio_sandbox.py:193](../backend/packages/harness/deerflow/community/aio_sandbox/aio_sandbox.py#L193)）。两层防穿越语义一致，实现位置不同。

**Q：日志里 `ErrorObservation detected, retrying on a fresh session`？**
A：通常是多进程共享同一沙箱、并发撞坏了容器的单持久 session。`execute_command` 检测到后会建新 session 重试一次（[aio_sandbox.py:156](../backend/packages/harness/deerflow/community/aio_sandbox/aio_sandbox.py#L156)）。偶发可忽略；频繁出现说明并发太重，考虑调大 `replicas`。

---

## §11 小结

AIO 沙箱是 [sandbox.md](sandbox.md) 里「本地模式非安全边界」的**真正解药**。它把每个沙箱关进 Docker / K8s 容器，agent 的破坏性命令只作用在容器内。核心是三层分工：

- **provider**（[aio_sandbox_provider.py](../backend/packages/harness/deerflow/community/aio_sandbox/aio_sandbox_provider.py)）管「谁在用哪个容器」——四级缓存让重复访问免冷启动，暖池让 release 的容器秒级回收，跨进程文件锁 + 确定性 id 让多进程共享容器不撞车，idle 回收 + 孤儿收养 + 优雅关闭保证长跑不泄漏。
- **backend**（[local_backend.py](../backend/packages/harness/deerflow/community/aio_sandbox/local_backend.py) / [remote_backend.py](../backend/packages/harness/deerflow/community/aio_sandbox/remote_backend.py)）管「怎么把容器弄起来」——本地 Docker/Apple Container CLI 编排（端口重试 + 名冲突发现 + 2-subprocess 枚举），或远端 K8s provisioner 薄 HTTP client。
- **AioSandbox**（[aio_sandbox.py](../backend/packages/harness/deerflow/community/aio_sandbox/aio_sandbox.py)）管「怎么操作容器」——经 HTTP API 实现 `Sandbox` 的 8 个抽象方法，命令串行化 + ErrorObservation 重试保 session 不坏，`close()` 防 socket 泄漏。

整个设计的关键不变量是**与 Local 同接口**：切换部署模式只改 `config.sandbox.use` 一行，agent 代码、工具层、中间件全部无感。这正是 [sandbox.md](sandbox.md) §4「`Sandbox` ABC + 8 抽象方法」抽象的价值兑现。

> 上一篇：[#13 sandbox.md](sandbox.md)（沙箱——虚拟路径翻译 + LocalSandbox + provider 单例 + 命令审计；本篇的「本地模式非安全边界」前提）
> 下一篇：[#15 subagents.md](subagents.md)（子代理——委派 + 单 scheduler pool + 持久隔离事件循环 + 5 状态契约 + token 回灌）
