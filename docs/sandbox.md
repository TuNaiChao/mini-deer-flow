# 13. sandbox.md — 沙箱（虚拟路径 + 7 工具 + 命令审计）

> **一句话定位**：沙箱是「让 agent 在受控环境里跑 bash、读写文件、搜索代码」的子系统——
> 它把 agent 看到的**虚拟路径**（`/mnt/user-data/...`）翻译成宿主机上按用户/线程隔离的真实目录，
> 再交给真实 shell / 文件系统执行，并审计每条 bash 命令。

小白第一次读，先把下面三个名词记住，后面的设计都是围绕它们展开的。

---

## 为什么需要沙箱（痛点）

agent 要能「写代码、跑代码、改文件」才有用。但 agent 跑的命令会**真实作用在宿主机上**：
如果它执行 `rm -rf /` 或 `cat /etc/passwd`，没有隔离就会真把宿主搞坏 / 把敏感文件读走。
所以需要一套机制：

1. **限定 agent 只能碰它自己的数据**——不能读写 `/etc`、`/root`、别的用户的数据。
2. **让 agent 看到统一的路径视图**——不管底层是本地进程还是 Docker 容器，agent 都用
   `/mnt/user-data/workspace/...` 这样的虚拟路径，代码不用为「本地/容器」写两套。
3. **审计危险命令**——`rm -rf /` 这类直接拦下；`pip install` 这类照跑但留个警告。
4. **串行化同文件的并发写**——两个工具调用同时改一个文件不能互相覆盖。

沙箱子系统（M10）解决 1-4 中的「本地模式」部分；真正的容器隔离（Docker）是 **M10b AIO 沙箱**，
本文末尾会讲为什么本地模式不够、何时该上 AIO。

---

## 核心概念（名词 + 类比）

### ① 虚拟路径（virtual path）

agent **永远只看到**这几类虚拟路径（容器视角）：

| 虚拟路径 | 对应什么 | 读/写 |
|----------|----------|-------|
| `/mnt/user-data/workspace/...` | 当前线程的工作目录（agent 写代码、跑代码的地方） | 读写 |
| `/mnt/user-data/uploads/...` | 用户上传的文件（见 M23） | 读写 |
| `/mnt/user-data/outputs/...` | agent 产物（可 `present_file` 给用户看，见 M15） | 读写 |
| `/mnt/skills/...` | 技能目录（SKILL.md，见 M14） | **只读** |

**类比**：虚拟路径就像酒店房间的「门牌号」——你只知道「301 房」，但 301 房在几楼、哪个
物理位置，是酒店（沙箱）内部的事。agent 拿着 `/mnt/user-data/workspace/app.py`，沙箱
内部把它翻译成宿主机上 `…/users/{user_id}/threads/{thread_id}/user-data/workspace/app.py`。

### ② PathMapping（路径映射）

一条 `PathMapping` 把「虚拟前缀 ↔ 宿主目录」对应起来，带只读标志：

```python
PathMapping(container_path="/mnt/user-data/workspace",
            local_path="/Users/.../users/u1/threads/t1/user-data/workspace",
            read_only=False)
```

一个 `LocalSandbox` 持有一组 `PathMapping`。**最长前缀匹配**：`/mnt/user-data/workspace`
的映射比 `/mnt/user-data` 更具体，所以对 `/mnt/user-data/workspace/a.py` 会胜出。

### ③ SandboxProvider（provider = 工厂 + 生命周期）

为什么不直接 `LocalSandbox()` 而要 provider？因为同一个线程的多次工具调用要**复用**
同一个沙箱（否则每条命令都重建目录、丢失缓存）。provider 是进程级单例，按 `thread_id`
缓存沙箱（`local:{thread_id}`），`acquire` / `get` / `release` 管生命周期，LRU 封顶防泄漏。

---

## 设计原理（权衡 / 不变量 / 踩坑）

### 「本地模式不是安全边界」是头号不变量

`LocalSandbox.execute_command` 直接调 `subprocess.run` 在**宿主机**跑 bash。隔离**完全靠**
虚拟路径翻译 + 路径穿越防御——这两层都是 **defense-in-depth（纵深防御）的 best-effort 守卫**，
不是真正的安全沙箱。所以：

- **host bash 默认禁用**（`sandbox.allow_host_bash: false`）：`bash` 工具的准入闸
  `is_host_bash_allowed()` 返回 False 时直接返回禁用提示，不执行任何命令（见 `security.py`）。
- 只有用户**显式**设 `sandbox.allow_host_bash: true`（且自认是「完全可信的本地环境」）才放行。
- 真要跑 untrusted 代码 / 多租户 → 上 **M10b AIO 沙箱**（Docker 容器隔离）。

### 两层路径防御（红线 #4 路径穿越防御）

- **provider 层**（`LocalSandbox`）：`_resolve_path` 翻译虚拟路径；翻译后若逃出挂载根
  （`..` 穿越）→ `PermissionError`；只读挂载写入 → `OSError(EROFS)`。
- **工具层**（`tools.py`）：`validate_local_tool_path` / `validate_local_bash_command_paths`
  再校验一遍——即便 provider 翻译出问题，`..` 段、越界绝对路径、不安全的 `cd /etc` 也会被拦。
  这是 belt-and-suspenders：两层都过才放心。

### 反解析（reverse resolve）防泄露宿主布局

agent 不该知道宿主目录长啥样（`/Users/tu/...` 泄露用户名/路径）。所以：
- `execute_command` 的输出、`ls` 的结果、`read_file`（仅 agent 自写文件）的内容里出现的
  **宿主绝对路径**会被 `_reverse_resolve_paths_in_output` 洗回虚拟路径。
- `_agent_written_paths` 只对「agent 自己 write_file 写的文件」做反解析——用户上传 / 外部
  产物原样返回，不悄悄改写内容（否则可能改坏用户的数据）。

### host-bash 放行时的命令路径校验

`allow_host_bash: true` 时，`validate_local_bash_command_paths` 用 shlex 拆 token，识别：
绝对路径必须落在 `/mnt/user-data` / `/mnt/skills` 或一小撮系统前缀（`/bin/`、`/dev/` 等）；
`cd`/`pushd` 的目标不能是 `~`、`$()`、`/`；拦 `file://` URL（绕过绝对路径正则却能本地读文件）。
**仍是 best-effort**——真隔离靠容器。

### write_file 的 80KB 单次上限（issue #3189）

单次**非追加** `write_file` 超 80KB 会被拒（`append=True` 不受限）。原因：过大的单次写与
LLM 流式 chunk 超时强相关。环境变量 `DEERFLOW_WRITE_FILE_MAX_BYTES` 可覆盖，设 0 禁用。
超大文档应：先 write 一段，再 `str_replace` 增量改；或分多次 `append=True`。

### 同路径写串行化（`file_operation_lock.py`）

`write_file` / `str_replace` 是「读-改-写」组合。按 `(sandbox_id, path)` 取一把
`threading.Lock`（`get_file_operation_lock`），让同一文件的并发写串行；不同沙箱/路径不争用。
锁用 `WeakValueDictionary` 存，无引用时自动回收，长跑进程不泄漏。

### `SandboxMiddleware` 的 lazy_init + 状态贴回

`SandboxMiddleware(lazy_init=True)`（默认）不在 `before_agent` 就 acquire 沙箱，而是推迟到
首次工具调用（`ensure_sandbox_initialized` 懒 acquire）。但工具内部直接改 `runtime.state["sandbox"]`
是**局部**修改，不会被 LangGraph 的 channel reducer 捕获——后续图步看不到 sandbox_id。
所以 `wrap_tool_call` 比对调用前后的 state 快照，发现「首次懒初始化」就用 `Command(update=...)`
把 `sandbox.sandbox_id` 正式写回图状态（红线 #15：wrap 不吞 `GraphBubbleUp`）。

### `SandboxAuditMiddleware` 三档分级

每条 `bash` 命令分 **block / warn / pass**：

- **block**（高危，不执行）：`rm -rf /`、`curl url | bash`、`dd if=`、`mkfs`、fork bomb、
  覆盖系统二进制/shell 启动文件、`LD_PRELOAD` 劫持、`/dev/tcp/`…
- **warn**（中危，照跑附警告）：`pip install`、`chmod 777`、`sudo/su`、改 `PATH`…
- **pass**（安全）：放行。

复合命令（`cmd1 && cmd2 ; cmd3`）quote-aware 拆开逐条分级，取最严档；但跨语句的结构性攻击
（`while true; do bash & done`）先整串扫再拆。输入消毒：空 / 超长（>10KB）/含 NUL 直接 block。

---

## 文件结构

```
sandbox/
├── __init__.py                 # 导出 Sandbox / SandboxProvider / get_sandbox_provider
├── exceptions.py               # 7 个异常类（SandboxError 基 + 6 子类）
├── sandbox.py                  # 抽象 Sandbox（8 个抽象方法）+ 异常再导出
├── sandbox_provider.py         # SandboxProvider ABC + 进程级单例
├── security.py                 # is_host_bash_allowed / uses_local_sandbox_provider
├── file_operation_lock.py      # get_file_operation_lock（同路径写串行化）
├── search.py                   # GrepMatch + find_glob_matches + find_grep_matches + 57 忽略模式
├── tools.py                    # 7 工具 + 路径校验 + 输出截断
├── middleware.py               # SandboxMiddleware（生命周期 + lazy_init 贴回）
└── local/
    ├── __init__.py             # 导出 LocalSandbox / LocalSandboxProvider / PathMapping
    ├── list_dir.py             # list_dir（树形列出，复用 search 的忽略模式）
    ├── local_sandbox.py        # LocalSandbox（路径翻译/反解析/只读/glob/grep/download）+ PathMapping
    └── local_sandbox_provider.py  # LocalSandboxProvider（per-thread + LRU + reset/shutdown）

agents/middlewares/
└── sandbox_audit_middleware.py # SandboxAuditMiddleware（bash 命令分级审计）
```

> **为什么 `SandboxAuditMiddleware` 在 `agents/middlewares/` 而不在 `sandbox/`？**
> `SandboxMiddleware`（生命周期）属于沙箱子系统本身；而审计中间件是「中间件链的一环」
> （M16 的 23 步第 8 步），由 `build_middlewares` 装配，故放中间件目录，与其它中间件并列。

---

## 关键接口

### 抽象 `Sandbox`（`sandbox/sandbox.py`）

```python
class Sandbox(ABC):
    def execute_command(self, command: str) -> str: ...      # bash
    def read_file(self, path: str) -> str: ...               # 读文本
    def download_file(self, path: str) -> bytes: ...         # 读二进制（view_image 用）
    def list_dir(self, path: str, max_depth: int = 2) -> list[str]: ...  # ls
    def write_file(self, path: str, content: str, append: bool = False) -> None: ...
    def glob(self, path, pattern, *, include_dirs=False, max_results=200) -> tuple[list[str], bool]: ...
    def grep(self, path, pattern, *, glob=None, literal=False, case_sensitive=False, max_results=100) -> tuple[list[GrepMatch], bool]: ...
    def update_file(self, path: str, content: bytes) -> None: ...  # 二进制覆盖写
```

7 个工具只依赖这 8 个抽象方法（`str_replace` 由工具层用 `read_file`+`write_file` 组合实现）。

### provider 单例（`sandbox/sandbox_provider.py`）

```python
get_sandbox_provider(**kwargs) -> SandboxProvider   # 按 config.sandbox.use 反射实例化，缓存
reset_sandbox_provider()                             # 清缓存（让 config 改动生效）
shutdown_sandbox_provider()                          # 先 shutdown 再清（应用退出用）
set_sandbox_provider(provider)                       # 测试注入
```

### 7 个工具（`sandbox/tools.py`，都是 `@tool` 装饰的 `StructuredTool`）

| 工具 | 干什么 | 关键约束 |
|------|--------|----------|
| `bash` | 执行 bash | host-bash 闸（local 默认禁用）；输出 mask 宿主路径；中间截断 |
| `ls` | 树形列目录（max 2 层） | 忽略 .git/__pycache__ 等；mask 宿主路径 |
| `glob` | glob 模式找文件 | `max_results` 钳到 [200, 1000]；忽略噪音目录 |
| `grep` | 内容搜匹配行 | 二进制跳过；防 ReDoS（跳过长行）；支持 literal/case/glob 过滤 |
| `read_file` | 读文本 | 可选 start_line/end_line；头部截断（默认 50000 字符） |
| `write_file` | 写文本 | 非追加 80KB 上限；同路径写锁；只读挂载拒绝 |
| `str_replace` | 子串替换 | 默认替换第一个（须唯一）；`replace_all` 全换；同路径写锁 |

每个工具有同步版（`.func`）和异步版（`.coroutine`，经 `asyncio.to_thread` 卸载）。

---

## 应用方法

### 配置（`config.yaml`）

```yaml
sandbox:
  use: deerflow.sandbox.local:LocalSandboxProvider  # provider 类路径
  allow_host_bash: false        # 危险；仅完全可信本地环境设 true
  bash_output_max_chars: 20000  # bash 输出截断上限（中间截断）
  read_file_output_max_chars: 50000
  ls_output_max_chars: 20000
```

### 工具加载（`config.yaml` 的 `tools[]`）

沙箱工具不在内置列表里硬编码，而是经 `tools[].use` 反射加载（见 M15 `get_available_tools`）：

```yaml
tools:
  - use: deerflow.sandbox.tools:bash_tool
    group: sandbox
  - use: deerflow.sandbox.tools:ls_tool
    group: sandbox
  - use: deerflow.sandbox.tools:glob_tool
    group: sandbox
  - use: deerflow.sandbox.tools:grep_tool
    group: sandbox
  - use: deerflow.sandbox.tools:read_file_tool
    group: sandbox
  - use: deerflow.sandbox.tools:write_file_tool
    group: sandbox
  - use: deerflow.sandbox.tools:str_replace_tool
    group: sandbox
```

### 跑测试

```bash
cd backend && make test    # 含 test/test_sandbox.py（97 个 hermetic 测试）
```

测试约定：`DEER_FLOW_HOME` 指向 `tmp_path`（per-thread 目录建临时盘，不碰宿主真实数据）；
`is_host_bash_allowed` / skills 路径缓存用 monkeypatch 控制，不读全局 config.yaml。

---

## 与其它模块的关系（依赖图）

```
config (sandbox_config: use/allow_host_bash/输出上限)
  │
config/paths (VIRTUAL_PATH_PREFIX=/mnt/user-data, runtime_home→base_dir)
  │
runtime/user_context (get_effective_user_id → per-user 隔离)
  │
sandbox ◄── tools (7 工具经 Runtime 读 state 里的 sandbox/thread_data)
  │   ▲
  │   └── agents/middlewares/sandbox_audit_middleware (bash 分级审计)
  │   └── sandbox/middleware (SandboxMiddleware lazy_init)
  │
  └── (未来 M10b) community/aio_sandbox ◄── AioSandboxProvider (Docker 隔离)
```

- **上游**：`config`（provider 类路径 + 各输出上限）、`config/paths`（虚拟前缀 + base_dir）、
  `runtime/user_context`（per-user 隔离的用户 id）。
- **下游消费者**：
  - `agents/middlewares`（M16）：`SandboxMiddleware` 第 4 步、`SandboxAuditMiddleware` 第 8 步。
  - `tools`（M15）：7 工具是 agent 文件操作/命令执行的唯一入口。
  - `subagents`（M11）：bash 子代理只用 sandbox 的 5 个 bash/文件工具。
  - `agents/thread_state`（M17）：`sandbox` / `thread_data` 字段存沙箱 id 与线程目录。

---

## 常见问题 / 排错

**Q：agent 说 `Host bash execution is disabled`，但我想要它能跑命令？**
A：这是 `is_host_bash_allowed()` 返回 False。本地模式默认禁用（不是安全边界）。两个选择：
① 仅在完全可信的本地环境设 `sandbox.allow_host_bash: true`；② 用 M10b AIO 沙箱（容器隔离，
host bash 自动放行）。

**Q：`bash` 报 `Unsafe absolute paths in command`？**
A：`validate_local_bash_command_paths` 拦下了宿主绝对路径。本地模式要求用 `/mnt/user-data/...`
虚拟路径访问用户数据；只有 `/bin/`、`/dev/` 等系统前缀放行。

**Q：`write_file` 报 `exceeds the 80KB single-call limit`？**
A：见「设计原理」的 80KB 上限。先写第一段，后续用 `str_replace` 增量改；或多次 `append=True`。

**Q：`read_file` 读用户上传的文件，里面的路径没被翻译？**
A：这是**故意的**。`_agent_written_paths` 只对 agent 自己 write_file 写的文件做反解析，
用户上传 / 外部产物原样返回（避免改坏用户数据）。

**Q：`SandboxAuditMiddleware` 拦了我的 `pip install`？**
A：不会——`pip install` 是 **warn** 档（中危），照常执行，只往结果追加一条警告。被 **block**
（不执行）的是高危命令。想看分级日志，看 `[SandboxAudit]` 开头的 logger 输出。

**Q：本地模式和 AIO 模式的区别？什么时候该上 AIO？**
A：本地模式（M10）在宿主机进程直接跑，隔离靠虚拟路径翻译（**不是**安全边界）。AIO（M10b）
用 Docker 容器隔离，agent 的 bash 真在容器里跑，逃不出容器。规则：跑自己/可信代码用本地；
跑 untrusted 代码、多租户、生产环境 → 上 AIO。两者的公开 `Sandbox` API 一致（agent 代码不用改），
因为 AIO 把 `/mnt/user-data/...` bind-mount 进容器，路径视图相同。

**Q：`LocalSandboxProvider` 缓存了 256 个沙箱，会被淘汰吗？**
A：会。`_thread_sandboxes` 是 LRU，超 256 淘汰最久未用的（`acquire` 和 `get` 都提升顺序）。
被淘汰的线程下次 `acquire` 会重建——只丢失 `_agent_written_paths` 反解析提示（优雅降级，
read_file 不再反解析该线程的旧文件，与新线程行为一致）。
