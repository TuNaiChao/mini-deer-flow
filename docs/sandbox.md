# 13. sandbox.md — 沙箱（虚拟路径 + 7 工具 + provider 单例 + 命令审计）

> 📝 重写于 2026-07-05 · 对照代码 commit ffc5e5d

> **一句话定位**：沙箱是「让 agent 在受控环境里跑 bash、读写文件、搜索代码」的子系统——它把 agent 看到的**虚拟路径**（`/mnt/user-data/...`）翻译成宿主机上按 `(user_id, thread_id)` 隔离的真实目录，再交给真实 shell / 文件系统执行，并审计每条 bash 命令。**本地模式不是安全边界**，真正的容器隔离见 [#14 aio_sandbox.md](aio_sandbox.md)。

> **配套代码**：[sandbox/](../backend/packages/harness/deerflow/sandbox/)（13 个文件）+ [agents/middlewares/sandbox_audit_middleware.py](../backend/packages/harness/deerflow/agents/middlewares/sandbox_audit_middleware.py) + [config/sandbox_config.py](../backend/packages/harness/deerflow/config/sandbox_config.py)
> **配套测试**：[test/test_sandbox.py](../test/test_sandbox.py)（hermetic 测试套件，`DEER_FLOW_HOME` 指向 tmp_path 不碰宿主真实数据）
> **参考**：deerflow-book [13-sandbox-abstraction.md](../deerflow-book/chapters/13-sandbox-abstraction.md)（借「虚拟路径 + Sandbox ABC + 延迟初始化」框架；mini 实现细节与 book 有差异，见 §4.5）
> 本文面向「刚接触沙箱 / 容器隔离 / 虚拟文件系统的小白」。每个名词第一次出现都会解释。

---

## 学完能回答（learning outcomes）

1. 为什么 `LocalSandbox` 不是安全边界？host bash 为什么默认禁用？要跑 untrusted 代码 / 多租户该上什么？
2. agent 看到的虚拟路径 `/mnt/user-data/workspace/a.py` 怎么翻译成宿主路径？翻译后为什么还要**反解析**回去（不让 agent 看到宿主布局）？
3. `PathMapping` 的「最长前缀匹配」解决什么问题？（为什么 `/mnt/user-data/workspace` 要比 `/mnt/user-data` 更优先）
4. 为什么需要 `SandboxProvider` 单例 + per-thread LRU 缓存？`release()` 为什么是 no-op？这样设计的代价是什么？
5. provider 单例的 `_provider_lock` 为什么守 4 个位点（get/reset/shutdown/set）、回调（构造/reset/shutdown）却在锁**外**跑？（自死锁 + 慢回调阻塞并发 get）
6. `write_file` 的 80KB 单次上限（非追加）为什么存在？怎么绕过？`DEERFLOW_WRITE_FILE_MAX_BYTES=0` 干嘛？同路径写串行化锁为什么用 `WeakValueDictionary`？
7. `SandboxAuditMiddleware` 的 block/warn/pass 三档怎么分？复合命令（`a && b ; c`）怎么处理？合法 heredoc 在 `shlex.split` 失败时为什么不 block？
8. 「工具层 + provider 层」两层路径防御（belt-and-suspenders）各做什么？为什么本地模式仍然只是 best-effort？

---

## §1 为什么需要沙箱（痛点）

agent 要能「写代码、跑代码、改文件」才有用。但 agent 跑的命令会**真实作用在宿主机上**——如果它执行 `rm -rf /` 或 `cat /etc/passwd`，没有隔离就会真把宿主搞坏 / 把敏感文件读走。所以需要：

| 痛点 | 沙箱怎么解 |
|------|-----------|
| agent 不该碰 `/etc`、`/root`、别的用户数据 | **虚拟路径 + 按 (user_id, thread_id) 隔离**：agent 只看到 `/mnt/user-data/...`，翻译到自己的目录 |
| 本地 / 容器两套底层，agent 代码不该写两遍 | **统一虚拟路径视图**：不管底层是本地进程还是 Docker，agent 都用 `/mnt/user-data/...` |
| `rm -rf /` 这类危险命令 | **命令审计三档**：高危 block、中危 warn、安全 pass |
| 两个工具调用并发改同一文件互相覆盖 | **同路径写串行化锁** |
| host bash 在宿主真实执行（非安全边界） | **host bash 默认禁用**，需显式 opt-in |

本文讲「本地模式」沙箱（`LocalSandbox`）；真正的容器隔离（Docker）是 **[#14 aio_sandbox.md](aio_sandbox.md)**，文末会讲为什么本地模式不够、何时该上 AIO。

---

## §2 零基础名词（第一次出现都解释）

**沙箱（sandbox）**：让程序在「受控环境」里运行的机制。狭义的沙箱是操作系统级的强隔离（容器、虚拟机）；mini 的本地沙箱是**弱隔离**——靠路径翻译 + 穿越防御，不是 OS 级边界。

**虚拟路径（virtual path）**：agent **永远只看到**的路径前缀（容器视角），不感知底层物理位置。mini 的虚拟前缀是 `/mnt/user-data`（[config/paths.py](../backend/packages/harness/deerflow/config/paths.py) 的 `VIRTUAL_PATH_PREFIX`）：

| 虚拟路径 | 对应什么 | 读/写 |
|----------|----------|-------|
| `/mnt/user-data/workspace/...` | 当前线程的工作目录（agent 写代码、跑代码的地方） | 读写 |
| `/mnt/user-data/uploads/...` | 用户上传的文件（见 [#23 uploads.md](uploads.md)） | 读写 |
| `/mnt/user-data/outputs/...` | agent 产物 | 读写 |
| `/mnt/skills/...` | 技能目录（SKILL.md，见 [#19 skills.md](skills.md)） | **只读** |

**类比**：虚拟路径像酒店房间的「门牌号」——你只知道「301 房」，但它在几楼、哪个物理位置是酒店（沙箱）内部的事。agent 拿着 `/mnt/user-data/workspace/app.py`，沙箱内部翻译成 `…/users/{user_id}/threads/{thread_id}/user-data/workspace/app.py`。

> **为什么是 `/mnt/`？**（借 deerflow-book §13.7 的洞察）：`/mnt/` 是 Linux 传统的外部存储挂载点，这个惯例深植于 LLM 训练数据。当 agent 看到 `/mnt/user-data/workspace/`，LLM 会自然理解为「挂载进来的、持久的、真实的外部存储」——从而认真对待写入、不随意覆盖。命名是 prompt engineering 在文件系统层面的延伸。

**PathMapping（路径映射）**：一条 `PathMapping`（[local_sandbox.py:56-62](../backend/packages/harness/deerflow/sandbox/local/local_sandbox.py#L56-L62)）把「虚拟前缀 ↔ 宿主目录」对应起来，带 `read_only` 标志（skills 只读）。一个 `LocalSandbox` 持有一组映射，按**最长前缀匹配**解析（[local_sandbox.py:152-164](../backend/packages/harness/deerflow/sandbox/local/local_sandbox.py#L152-L164)）。

**SandboxProvider（provider = 工厂 + 生命周期管理器）**：负责 acquire（取沙箱）/ get（按 id 查）/ release（释放）。为什么不直接 `LocalSandbox()` 而要 provider？因为同一个线程的多次工具调用要**复用**同一个沙箱（否则每条命令重建目录、丢失缓存）。provider 是进程级单例，按 `thread_id` 缓存沙箱。

**反解析（reverse resolve）**：把宿主绝对路径「洗回」虚拟路径。agent 不该知道宿主目录长啥样（`/Users/tu/...` 泄露用户名）——所以命令输出、`ls` 结果、`read_file`（仅 agent 自写文件）内容里的宿主路径会被反解析。

**host bash**：直接在宿主机进程里跑 bash（`subprocess.run`）。**不是**安全边界——`rm -rf` 会真实生效在宿主上，所以默认禁用。

---

## §3 整体结构

```
sandbox/
├── __init__.py                 # 导出 Sandbox / SandboxProvider / get_sandbox_provider
├── exceptions.py               # 异常层次（SandboxError 基 + 6 子类，带 details）
├── sandbox.py                  # 抽象 Sandbox（8 个抽象方法）+ 异常再导出
├── sandbox_provider.py         # SandboxProvider ABC + 进程级单例（加锁）
├── security.py                 # is_host_bash_allowed / uses_local_sandbox_provider（host-bash 准入闸）
├── file_operation_lock.py      # get_file_operation_lock（同路径写串行化，WeakValueDictionary）
├── search.py                   # GrepMatch + find_glob/grep_matches + 57 忽略模式 + ReDoS/二进制防御
├── tools.py                    # 7 工具 + 路径校验 + 输出截断（1397 行）
├── middleware.py               # SandboxMiddleware（生命周期 + lazy_init 贴回状态）
└── local/
    ├── list_dir.py             # list_dir（树形列出，复用 search 的忽略模式）
    ├── local_sandbox.py        # LocalSandbox（路径翻译/反解析/只读/glob/grep/download）+ PathMapping
    └── local_sandbox_provider.py  # LocalSandboxProvider（per-thread + LRU 256 + reset/shutdown）

agents/middlewares/
└── sandbox_audit_middleware.py # SandboxAuditMiddleware（bash 命令 block/warn/pass 三档审计）
```

三层架构：
- **抽象层**（`sandbox.py` / `exceptions.py` / `sandbox_provider.py`）：定 `Sandbox` ABC 契约 + provider 单例机制，不碰具体实现。
- **实现层**（`local/`）：`LocalSandbox` + `LocalSandboxProvider`，宿主机本地实现。
- **工具/中间件层**（`tools.py` / `middleware.py` / `sandbox_audit_middleware.py`）：7 个 agent 工具 + 生命周期中间件 + 审计中间件。

> **为什么 `SandboxAuditMiddleware` 在 `agents/middlewares/` 而不在 `sandbox/`？** `SandboxMiddleware`（生命周期）属于沙箱子系统本身；审计中间件是「中间件链的一环」（[#24 middlewares.md](middlewares.md) 的生产链第 8 步），由 `build_middlewares` 装配，故放中间件目录与其它中间件并列。

---

## §4 核心概念

### 4.1 `Sandbox` ABC——8 个抽象方法

[sandbox.py:51-183](../backend/packages/harness/deerflow/sandbox/sandbox.py#L51-L183) 定义抽象基类，子类（Local / 未来的 AIO）填实现。工具层拿到的路径都是**容器视角**，子类负责内部翻译：

| 方法 | 干什么 | 行 |
|------|--------|----|
| `execute_command(command)` | 执行 bash，返回 stdout（失败附 stderr/exit code） | [:68](../backend/packages/harness/deerflow/sandbox/sandbox.py#L68) |
| `read_file(path)` | 读文本 | [:79](../backend/packages/harness/deerflow/sandbox/sandbox.py#L79) |
| `download_file(path)` | 读二进制（view_image 用，限 `/mnt/user-data` + 100MB） | [:95](../backend/packages/harness/deerflow/sandbox/sandbox.py#L95) |
| `list_dir(path, max_depth=2)` | 树形列目录 | [:110](../backend/packages/harness/deerflow/sandbox/sandbox.py#L110) |
| `write_file(path, content, append=False)` | 写文本（自动建父目录，只读挂载拒绝） | [:122](../backend/packages/harness/deerflow/sandbox/sandbox.py#L122) |
| `glob(path, pattern, ...)` | glob 模式找文件，返回 `(路径列表, 是否截断)` | [:134](../backend/packages/harness/deerflow/sandbox/sandbox.py#L134) |
| `grep(path, pattern, ...)` | 内容搜匹配行，返回 `(GrepMatch 列表, 是否截断)` | [:148](../backend/packages/harness/deerflow/sandbox/sandbox.py#L148) |
| `update_file(path, content: bytes)` | 二进制覆盖写 | [:173](../backend/packages/harness/deerflow/sandbox/sandbox.py#L173) |

7 个工具只依赖这 8 个方法（`str_replace` 由工具层用 `read_file`+`write_file` 组合实现，基类不单列）。

### 4.2 7 个工具（`tools.py`，都是 `@tool` 装饰）

| 工具 | 干什么 | 关键约束 |
|------|--------|----------|
| `bash` [:989](../backend/packages/harness/deerflow/sandbox/tools.py#L989) | 执行 bash | host-bash 闸（local 默认禁用）；命令路径校验；输出 mask 宿主路径；**中间**截断（默认 20000 字符） |
| `ls` [:1034](../backend/packages/harness/deerflow/sandbox/tools.py#L1034) | 树形列目录（max 2 层） | 忽略 .git/__pycache__ 等；mask 宿主路径；**头部**截断（默认 20000） |
| `glob` [:1079](../backend/packages/harness/deerflow/sandbox/tools.py#L1079) | glob 模式找文件 | `max_results` 钳到 [200, 1000]；忽略噪音目录 |
| `grep` [:1146](../backend/packages/harness/deerflow/sandbox/tools.py#L1146) | 内容搜匹配行 | 二进制跳过；防 ReDoS（跳过长行）；支持 literal/case/glob 过滤 |
| `read_file` [:1237](../backend/packages/harness/deerflow/sandbox/tools.py#L1237) | 读文本 | 可选 start_line/end_line；**头部**截断（默认 50000） |
| `write_file` [:1284](../backend/packages/harness/deerflow/sandbox/tools.py#L1284) | 写文本 | 非追加 80KB 上限；同路径写锁；只读挂载拒绝 |
| `str_replace` [:1351](../backend/packages/harness/deerflow/sandbox/tools.py#L1351) | 子串替换 | 默认替换第一个（须唯一）；`replace_all` 全换；同路径写锁 |

每个工具第一个参数都是 `description`——要求 agent 先用自然语言解释「为什么这么做」，提升可审计性 + 让 LLM 推理更谨慎。每个工具有同步版（`.func`）和异步版（`.coroutine`，经 `asyncio.to_thread` 卸载到线程，[tools.py:855-867](../backend/packages/harness/deerflow/sandbox/tools.py#L855-L867)）。

### 4.3 两层路径防御（belt-and-suspenders）

- **provider 层**（`LocalSandbox`）：`_resolve_path` 翻译虚拟路径；翻译后逃出挂载根（`..` 穿越）→ `PermissionError`；只读挂载写入 → `OSError(EROFS)`。
- **工具层**（`tools.py`）：`validate_local_tool_path`（[:375](../backend/packages/harness/deerflow/sandbox/tools.py#L375)）/ `validate_local_bash_command_paths`（[:684](../backend/packages/harness/deerflow/sandbox/tools.py#L684)）再校验一遍——即便 provider 翻译出问题，`..` 段、越界绝对路径、不安全的 `cd /etc` 也会被拦。**两层都过才放心**，但仍是 best-effort，真隔离靠容器。

### 4.4 异常层次（`exceptions.py`）

[exceptions.py:25-93](../backend/packages/harness/deerflow/sandbox/exceptions.py#L25-L93)，带结构化 `details` dict 方便排查：

```
SandboxError（基类，带 message + details）
├── SandboxNotFoundError         # 按 id 取不到沙箱
├── SandboxRuntimeError          # 运行时不可用 / 配置错（runtime/thread_id 缺失）
├── SandboxCommandError          # 命令执行失败（带 command/exit_code；command 截到 100 字）
└── SandboxFileError             # 文件操作失败（带 path/operation）
    ├── SandboxPermissionError   # 权限/穿越拒绝
    └── SandboxFileNotFoundError # 文件/目录不存在
```

集中在一个模块是为了「错误类型层次有单一真相源」——工具层/中间件/provider 都从这 import，不会出现两处各定义一个 `SandboxError` 导致 `isinstance` 漏判。`SandboxFileError` 是文件类错误的公共父类，调用方可 `except SandboxFileError` 一网打尽，也可精确 catch 子类。

### 4.5 与 deerflow-book 的差异（借框架非抄实现）

deerflow-book §13 把沙箱讲成「5 个工具 / 5 个抽象方法 / uploads 只读」。mini 的实现有意识地扩展/调整，诚实标注：

| | deerflow-book §13 | mini 实现 |
|---|---|---|
| 工具数 | 5（bash/ls/read_file/write_file/str_replace） | **7**（多 `glob`/`grep` 两个搜索工具） |
| 抽象方法 | 5 | **8**（多 `download_file` 二进制读 + `glob`/`grep`） |
| uploads 读写 | 只读 | **读写**（[local_sandbox_provider.py:129-132](../backend/packages/harness/deerflow/sandbox/local/local_sandbox_provider.py#L129-L132) 三个目录都 `read_only=False`） |
| 第三种实现 | Local / aio-sandbox / K8s 三种 | mini 只 port **Local**；AIO（[#14](aio_sandbox.md)）soft-load；K8s 不 port |

借的是「虚拟路径统一 + Sandbox ABC + 延迟初始化 + provider 单例」这套框架思想，实现细节按 mini 的需要来。

---

## §5 代码走读

### 5.1 虚拟路径翻译 + 反解析（`LocalSandbox` 核心）

`LocalSandbox` 通过 `path_mappings` 把容器路径翻译成宿主路径（[local_sandbox.py:75-127](../backend/packages/harness/deerflow/sandbox/local/local_sandbox.py#L75-L127)）。三个方向的翻译：

- **正解析** `_resolve_path`（[:183](../backend/packages/harness/deerflow/sandbox/local/local_sandbox.py#L183)）：容器路径 → 宿主路径。`_find_path_mapping`（[:152-164](../backend/packages/harness/deerflow/sandbox/local/local_sandbox.py#L152-L164)）按**最长容器前缀优先**（避免 `/mnt/user-data` 抢了 `/mnt/user-data/workspace`）；翻译后用 `relative_to(local_root)` 校验，逃出挂载根 → `PermissionError`（[:179](../backend/packages/harness/deerflow/sandbox/local/local_sandbox.py#L179)）。
- **反解析** `_reverse_resolve_path`（[:189-200](../backend/packages/harness/deerflow/sandbox/local/local_sandbox.py#L189-L200)）：宿主路径 → 容器路径（最长 local_path 优先）。
- **批量洗输出** `_reverse_resolve_paths_in_output`（[:202-219](../backend/packages/harness/deerflow/sandbox/local/local_sandbox.py#L202-L219)）：把命令输出里出现的宿主绝对路径正则匹配后批量洗回虚拟路径。

`_agent_written_paths`（[:127](../backend/packages/harness/deerflow/sandbox/local/local_sandbox.py#L127)）记录 agent 自己 `write_file` 写的路径——`read_file` 只对这些文件做反解析（[:333-334](../backend/packages/harness/deerflow/sandbox/local/local_sandbox.py#L333-L334)）；用户上传 / 外部产物原样返回，不悄悄改写（否则可能改坏用户数据）。

### 5.2 `execute_command`——bash 执行

[local_sandbox.py:287-315](../backend/packages/harness/deerflow/sandbox/local/local_sandbox.py#L287-L315)：

```python
def execute_command(self, command):
    resolved_command = self._resolve_paths_in_command(command)   # 先翻译命令里的容器路径
    shell = self._get_shell()                                     # 探测 zsh→bash→sh→Windows fallback
    ...
    result = subprocess.run(args, shell=False, capture_output=True, text=True, timeout=600)  # 600s 超时
    output = result.stdout
    if result.stderr:  output += ...\nStd Error:...               # 附 stderr
    if result.returncode != 0: output += ...\nExit Code:...        # 附 exit code
    final_output = output if output else "(no output)"
    return self._reverse_resolve_paths_in_output(final_output)    # 输出洗回虚拟路径
```

`shell=False`（不用 `shell=True`，避免一层 shell 注入面），600 秒超时。Windows 下区分 PowerShell/cmd/msys（[:292-302](../backend/packages/harness/deerflow/sandbox/local/local_sandbox.py#L292-L302)）。

### 5.3 `LocalSandboxProvider`——per-thread + LRU

[local_sandbox_provider.py:45-217](../backend/packages/harness/deerflow/sandbox/local/local_sandbox_provider.py#L45-L217)。核心是 `acquire`（[:135-171](../backend/packages/harness/deerflow/sandbox/local/local_sandbox_provider.py#L135-L171)）：

- `thread_id=None` → 返回通用单例（id `"local"`），供无线程上下文场景（[:145-150](../backend/packages/harness/deerflow/sandbox/local/local_sandbox_provider.py#L145-L150)）。
- `thread_id="abc"` → per-thread `LocalSandbox`（id `"local:abc"`），`path_mappings` 把 `/mnt/user-data/{workspace,uploads,outputs}` 绑到该线程宿主目录（[:118-133](../backend/packages/harness/deerflow/sandbox/local/local_sandbox_provider.py#L118-L133) `_build_thread_path_mappings`）。
- **LRU 缓存**：`_thread_sandboxes` 是 `OrderedDict`（默认 256 上限，[:48](../backend/packages/harness/deerflow/sandbox/local/local_sandbox.py#L48)），`acquire` 和 `get` 都 `move_to_end` 提升顺序，超限淘汰最久未用的（`_evict_until_within_cap_locked` [:173](../backend/packages/harness/deerflow/sandbox/local/local_sandbox_provider.py#L173)）。
- **锁内快路径 + 锁外 IO**：缓存命中在 `self._lock` 内；`ensure_thread_dirs` 触及文件系统，**I/O 期间释放锁**（[:159-160](../backend/packages/harness/deerflow/sandbox/local/local_sandbox_provider.py#L159-L160)），IO 后复查（[:162-171](../backend/packages/harness/deerflow/sandbox/local/local_sandbox_provider.py#L162-L171) 双重检查防重复建）。
- `release`（[:198-202](../backend/packages/harness/deerflow/sandbox/local/local_sandbox_provider.py#L198-L202)）是 **no-op**（`pass`）——保留缓存实例让 `_agent_written_paths` 跨轮次存活；只有 LRU 淘汰和显式 reset/shutdown 清缓存。

### 5.4 provider 单例加锁（`sandbox_provider.py`）

[sandbox_provider.py:76-157](../backend/packages/harness/deerflow/sandbox/sandbox_provider.py#L76-L157)。单例可被多个 OS 线程触达（主事件循环 + 跑自己循环的 IM channel 线程），一把模块级 `_provider_lock`（[:87](../backend/packages/harness/deerflow/sandbox/sandbox_provider.py#L87)）守卫 4 个位点（get/reset/shutdown/set）。关键是**回调在锁外跑**：

```
get_sandbox_provider():
  锁内快路径读（:99-101）→ 命中直接返回
  锁外冷启动：resolve_class 动态 import + 构造 provider（:103-107）  ← 插件代码，可能慢/重入
  锁内裁决：谁先装谁赢（:109-114）
  锁外：输家 shutdown 自己建的实例防泄漏（:118-119）
```

为什么回调必须在锁外？`config.sandbox.use` 指向任意类，`resolve_class` 的 import 和 provider 的 `__init__`/`reset`/`shutdown` 是**插件代码**——可能很慢，更糟的是可能**重入**这些生命周期函数。用非重入 `threading.Lock` 跨着它们会自死锁，还会在一次慢拆除期间挡住所有并发 `get()`。把回调挪到锁外，两个问题都避开。

### 5.5 `SandboxMiddleware`——生命周期 + lazy_init 贴回

[middleware.py](../backend/packages/harness/deerflow/sandbox/middleware.py)。`lazy_init=True`（默认）不在 `before_agent` 就 acquire，而是推迟到首次工具调用（`ensure_sandbox_initialized`，[tools.py:782-818](../backend/packages/harness/deerflow/sandbox/tools.py#L782-L818) 懒 acquire）。

但工具内部直接改 `runtime.state["sandbox"]` 是**局部**修改，不会被 LangGraph 的 channel reducer 捕获——后续图步看不到 sandbox_id。所以 `wrap_tool_call`（[middleware.py:181-201](../backend/packages/harness/deerflow/sandbox/middleware.py#L181-L201)）比对调用**前后**的 state 快照，发现「首次懒初始化」就用 `Command(update={"sandbox": {"sandbox_id": ...}})` 把 sandbox_id 正式写回图状态（`_attach_sandbox_update` [:155-172](../backend/packages/harness/deerflow/sandbox/middleware.py#L155-L172)）。`after_agent`（[:111-124](../backend/packages/harness/deerflow/sandbox/middleware.py#L111-L124)）调 `release`（对 local 是 no-op，所以沙箱跨轮复用）。

### 5.6 `SandboxAuditMiddleware`——bash 命令三档审计

[sandbox_audit_middleware.py](../backend/packages/harness/deerflow/agents/middlewares/sandbox_audit_middleware.py)。每个 `bash` 调用前分级：

- **block**（高危，不执行）：`rm -rf /`、`curl url | bash`、`dd if=`、`mkfs`、fork bomb、覆盖系统二进制/shell 启动文件、`LD_PRELOAD` 劫持、`/dev/tcp/`…（`_HIGH_RISK_PATTERNS` [:47-73](../backend/packages/harness/deerflow/agents/middlewares/sandbox_audit_middleware.py#L47-L73)）。
- **warn**（中危，照跑附警告）：`pip install`、`chmod 777`、`sudo/su`、改 `PATH`…（`_MEDIUM_RISK_PATTERNS` [:76-84](../backend/packages/harness/deerflow/agents/middlewares/sandbox_audit_middleware.py#L76-L84)）。
- **pass**（安全）：放行。

分级策略 `_classify_command`（[:184-207](../backend/packages/harness/deerflow/agents/middlewares/sandbox_audit_middleware.py#L184-L207)）两轮：① 先对**整串原始命令**扫高危（捕获 `while true; do bash & done`、`:(){ :|:& };:` 这类跨多语句的结构性攻击，`;` 拆分会丢模式上下文）；② 再拆复合命令（`_split_compound_command` [:87-154](../backend/packages/harness/deerflow/agents/middlewares/sandbox_audit_middleware.py#L87-L154)，quote-aware）逐条分级，取最严档。

**heredoc 处理**（[:172-175](../backend/packages/harness/deerflow/agents/middlewares/sandbox_audit_middleware.py#L172-L175)）：`_classify_single_command` 先对**原文**跑高危检查，再用 `shlex.split` 解析 token 复跑高危。`shlex.split` 对合法 heredoc（`python3 << 'EOF' ... EOF`）会抛 `ValueError`——旧版直接 `return 'block'`，把合法 heredoc 也挡了；现改成 `pass` 继续走中危检查。fail-closed 不变性保住（高危永远在原文阶段拦），合法用法不再误伤。

输入消毒（[:289-299](../backend/packages/harness/deerflow/agents/middlewares/sandbox_audit_middleware.py#L289-L299)）：空 / 超长（>10KB）/含 NUL 直接 block（几乎必是 payload 注入）。每条 bash 调用写一条结构化 JSON 审计日志（`[SandboxAudit]`，[:246-256](../backend/packages/harness/deerflow/agents/middlewares/sandbox_audit_middleware.py#L246-L256)）。

### 5.7 `file_operation_lock`——同路径写串行化

[file_operation_lock.py](../backend/packages/harness/deerflow/sandbox/file_operation_lock.py)。`write_file`/`str_replace` 是「读-改-写」组合，按 `(sandbox_id, path)` 取一把 `threading.Lock`（`get_file_operation_lock` [:38-46](../backend/packages/harness/deerflow/sandbox/file_operation_lock.py#L38-L46)），让同一文件的并发写串行；不同沙箱/路径互不争用。锁用 `WeakValueDictionary` 存（[:26](../backend/packages/harness/deerflow/sandbox/file_operation_lock.py#L26)），无引用时自动回收——长跑进程里 thread_id 无界增长，锁对象也会无界增长，弱引用避免内存泄漏。

### 5.8 search.py——glob/grep 算法

[search.py](../backend/packages/harness/deerflow/sandbox/search.py)。抽成独立模块让 `LocalSandbox.glob/grep` 与未来的 AIO 沙箱共用同一份过滤/截断语义（两端输出格式必须一致）。要点：
- **57 个忽略模式**（[:32-82](../backend/packages/harness/deerflow/sandbox/search.py#L32-L82)）：.git/__pycache__/.venv/node_modules…，预拆成精确名 frozenset（O(1) 查）+ 通配符编译正则（一次 match），优化 `os.walk` 热路径（[:91-93](../backend/packages/harness/deerflow/sandbox/search.py#L91-L93)）。
- **二进制检测**：grep 跳过含 NUL 字节的文件（[:143-149](../backend/packages/harness/deerflow/sandbox/search.py#L143-L149) `is_binary_file`）。
- **防 ReDoS**：grep 跳过过长的行（`line_summary_length * 10`，[:249](../backend/packages/harness/deerflow/sandbox/search.py#L249)），避免在压缩/无换行文件上被正则回溯拖死。
- **符号链接**：grep/list_dir 都校验 resolve 后仍在 root 内，防 symlink 逃出搜索根（search.py [:267-270](../backend/packages/harness/deerflow/sandbox/search.py#L267-L270)、list_dir.py [:56-65](../backend/packages/harness/deerflow/sandbox/local/list_dir.py#L56-L65)）。
- **上限截断**：`max_results` 防一次搜几万条撑爆上下文，超限返回 `truncated=True`。

---

## §6 设计权衡（不变量 / 踩坑）

### 6.1 「本地模式不是安全边界」是头号不变量

`LocalSandbox.execute_command` 直接调 `subprocess.run` 在**宿主机**跑 bash。隔离**完全靠**虚拟路径翻译 + 路径穿越防御——这两层都是 defense-in-depth 的 best-effort 守卫，不是真正的安全沙箱。所以 host bash 默认禁用（`security.py` 的 `is_host_bash_allowed`，[security.py:53-68](../backend/packages/harness/deerflow/sandbox/security.py#L53-L68)），只有用户**显式**设 `sandbox.allow_host_bash: true`（且自认完全可信本地环境）才放行；非 Local provider（未来的 Docker）则总是允许（它们有真正隔离）。

### 6.2 host-bash 放行时的命令路径校验

`allow_host_bash: true` 时，`validate_local_bash_command_paths`（[tools.py:684-714](../backend/packages/harness/deerflow/sandbox/tools.py#L684-L714)）用 shlex 拆 token（`_split_shell_tokens` [:538](../backend/packages/harness/deerflow/sandbox/tools.py#L538)），识别：绝对路径必须落在 `/mnt/user-data`/`/mnt/skills` 或一小撮系统前缀（`/bin/`、`/dev/` 等，[:85](../backend/packages/harness/deerflow/sandbox/tools.py#L85)）；`cd`/`pushd` 目标不能是 `~`、`$()`、`/`（`_validate_local_bash_cwd_target` [:599-609](../backend/packages/harness/deerflow/sandbox/tools.py#L599-L609)）；拦 `file://` URL（绕过绝对路径正则却能本地读文件，[:696-698](../backend/packages/harness/deerflow/sandbox/tools.py#L696-L698)）。**仍是 best-effort**——真隔离靠容器。

### 6.3 write_file 的 80KB 单次上限（非追加）

单次**非追加** `write_file` 超 80KB 被拒（[tools.py:1307-1319](../backend/packages/harness/deerflow/sandbox/tools.py#L1307-L1319)）；`append=True` 不受限。原因（代码里注明 issue #3189）：过大的单次写与 LLM 流式 chunk 超时强相关——80KB ≈ 20K token，在默认 240s `stream_chunk_timeout` 下留足余量（[tools.py:71-74](../backend/packages/harness/deerflow/sandbox/tools.py#L71-L74)）。绕法：① 先 write 一段，再 `str_replace` 增量改；② 分多次 `append=True`。`DEERFLOW_WRITE_FILE_MAX_BYTES` 可覆盖，设 0 禁用（`_effective_write_file_max_bytes` [:964-972](../backend/packages/harness/deerflow/sandbox/tools.py#L964-L972)）。

### 6.4 download_file 的两道限制

`download_file`（[local_sandbox.py:340-362](../backend/packages/harness/deerflow/sandbox/local/local_sandbox.py#L340-L362)）只允许 `/mnt/user-data` 下的路径（防越界下载宿主任意文件，[:348-350](../backend/packages/harness/deerflow/sandbox/local/local_sandbox.py#L348-L350)），上限 100MB（[:353](../backend/packages/harness/deerflow/sandbox/local/local_sandbox.py#L353)）。供 `view_image` 等读二进制场景。

### 6.5 输出截断三种策略

| 工具 | 策略 | 默认上限 | 配置项 |
|------|------|---------|--------|
| bash | **中间**截断（保留首尾，stderr/stdout 顺序不确定两端都可能有错） | 20000 | `bash_output_max_chars` |
| read_file | **头部**截断（源码/文档从头读最有上下文） | 50000 | `read_file_output_max_chars` |
| ls | **头部**截断（目录列表从头看结构最相关） | 20000 | `ls_output_max_chars` |

`_truncate_bash_output`（[:894-907](../backend/packages/harness/deerflow/sandbox/tools.py#L894-L907)）中间截断、`_truncate_read_file_output`（[:910](../backend/packages/harness/deerflow/sandbox/tools.py#L910)）/`_truncate_ls_output`（[:923](../backend/packages/harness/deerflow/sandbox/tools.py#L923)）头部截断。设 0 禁用截断。

### 6.6 LRU 淘汰的优雅降级

`_thread_sandboxes` LRU 超 256 淘汰最久未用的。被淘汰的线程下次 `acquire` 会重建——只丢失 `_agent_written_paths` 反解析提示（优雅降级：`read_file` 不再反解析该线程的旧文件，与新线程行为一致）。

---

## §7 配置与用法

### 7.1 SandboxConfig（[config/sandbox_config.py:22-81](../backend/packages/harness/deerflow/config/sandbox_config.py#L22-L81)）

| 字段 | 默认 | 含义 |
|------|------|------|
| `use` | （必填） | provider 类路径（如 `deerflow.sandbox.local:LocalSandboxProvider`） |
| `allow_host_bash` | `false` | Local 模式放行 host bash。**危险**，仅完全可信本地环境 |
| `bash_output_max_chars` | `20000` | bash 输出截断上限（中间截断）。0 禁用 |
| `read_file_output_max_chars` | `50000` | read_file 截断上限（头部） |
| `ls_output_max_chars` | `20000` | ls 截断上限（头部） |
| `mounts` | `[]` | 自定义卷挂载（host_path↔container_path + read_only） |
| `image`/`port`/`replicas`/... | None | AIO 专属（见 [#14](aio_sandbox.md)） |

`model_config = ConfigDict(extra="allow")`（[:81](../backend/packages/harness/deerflow/config/sandbox_config.py#L81)）——未知字段不报错，为扩展留空间。

### 7.2 config.yaml 示例

```yaml
sandbox:
  use: deerflow.sandbox.local:LocalSandboxProvider
  allow_host_bash: false        # 危险；仅完全可信本地环境设 true
  bash_output_max_chars: 20000
  read_file_output_max_chars: 50000
  ls_output_max_chars: 20000
```

### 7.3 工具加载（`tools[].use` 反射）

沙箱工具不在内置列表硬编码，而是经 `tools[].use` 反射加载（见 [#22 tools.md](tools.md) `get_available_tools`）：

```yaml
tools:
  - use: deerflow.sandbox.tools:bash_tool
    group: sandbox
  - use: deerflow.sandbox.tools:ls_tool
    group: sandbox
  # ... glob/grep/read_file/write_file/str_replace 同理
```

### 7.4 跑测试

```bash
cd backend && make test    # 含 test/test_sandbox.py（hermetic）
```

测试约定：`DEER_FLOW_HOME` 指向 `tmp_path`（per-thread 目录建临时盘，不碰宿主真实数据）；`is_host_bash_allowed`/skills 路径缓存用 monkeypatch 控制，不读全局 config.yaml。

---

## §8 与其它模块的关系

```
config (sandbox_config: use/allow_host_bash/输出上限/mounts)
  │
config/paths (VIRTUAL_PATH_PREFIX=/mnt/user-data, base_dir)
  │
runtime/user_context (get_effective_user_id → per-user 隔离)  —— 见 #5
  │
sandbox ◄── tools (7 工具经 Runtime 读 state 里的 sandbox/thread_data)
  │   ▲
  │   └── agents/middlewares/sandbox_audit_middleware (bash 分级审计，#24 第 8 步)
  │   └── sandbox/middleware (SandboxMiddleware lazy_init，#24 第 4 步)
  │
  └── [#14] community/aio_sandbox ◄── AioSandboxProvider (Docker 隔离，soft-load)
```

- **上游**：[#3 config.md](config.md)（provider 类路径 + 输出上限）、`config/paths`（虚拟前缀 + base_dir）、[#5 user_context.md](user_context.md)（`get_effective_user_id` 做 per-user 隔离）。
- **下游消费者**：
  - [#24 middlewares.md](middlewares.md)：`SandboxMiddleware`（生命周期，第 4 步）、`SandboxAuditMiddleware`（审计，第 8 步）。
  - [#22 tools.md](tools.md)：7 工具是 agent 文件操作/命令执行的唯一入口。
  - [#15 subagents.md](subagents.md)：bash 子代理只用 sandbox 的 bash/文件工具。
  - [#25 agents.md](agents.md) 的 `thread_state`：`sandbox`/`thread_data` 字段存沙箱 id 与线程目录。

### 与 AIO 沙箱的区别（预告 [#14](aio_sandbox.md)）

| | LocalSandbox（本篇） | AioSandbox（#14） |
|---|---|---|
| 隔离方式 | 宿主机进程直接跑，靠虚拟路径翻译（**非安全边界**） | Docker 容器隔离，bash 真在容器里 |
| host bash | 默认禁用 | 自动放行（有真正隔离） |
| 路径视图 | `/mnt/user-data/...` 翻译到宿主 | `/mnt/user-data/...` bind-mount 进容器（恒等映射） |
| 适用 | 跑自己/可信代码、本地开发 | untrusted 代码、多租户、生产 |

两者公开 `Sandbox` API 一致（agent 代码不用改），AIO 把 `/mnt/user-data/...` bind-mount 进容器，路径视图相同。

---

## §9 常见问题 / 排错

**Q：agent 说 `Host bash execution is disabled`，但我想要它能跑命令？**
A：`is_host_bash_allowed()` 返回 False。本地模式默认禁用（不是安全边界）。两个选择：① 仅在完全可信本地环境设 `sandbox.allow_host_bash: true`；② 用 [#14 AIO 沙箱](aio_sandbox.md)（容器隔离，host bash 自动放行）。

**Q：`bash` 报 `Unsafe absolute paths in command`？**
A：`validate_local_bash_command_paths` 拦下了宿主绝对路径。本地模式要求用 `/mnt/user-data/...` 虚拟路径；只有 `/bin/`、`/dev/` 等系统前缀放行。

**Q：`write_file` 报 `exceeds the 80KB single-call limit`？**
A：见 §6.3。先写第一段，后续用 `str_replace` 增量改；或多次 `append=True`（追加不受限）。

**Q：`read_file` 读用户上传的文件，里面的路径没被翻译？**
A：**故意的**。`_agent_written_paths` 只对 agent 自己 `write_file` 写的文件做反解析，用户上传/外部产物原样返回（避免改坏用户数据）。

**Q：`SandboxAuditMiddleware` 拦了我的 `pip install`？**
A：不会——`pip install` 是 **warn** 档（中危），照常执行，只往结果追加警告。被 **block**（不执行）的是高危命令。想看分级日志，看 `[SandboxAudit]` 开头的 logger 输出。

**Q：本地模式和 AIO 模式的区别？什么时候该上 AIO？**
A：见 §8 对比表。跑自己/可信代码用本地；跑 untrusted 代码、多租户、生产 → 上 AIO。两者 `Sandbox` API 一致。

**Q：`LocalSandboxProvider` 缓存了 256 个沙箱，会被淘汰吗？**
A：会。`_thread_sandboxes` 是 LRU，超 256 淘汰最久未用的（`acquire` 和 `get` 都提升顺序）。被淘汰的线程下次 `acquire` 重建——只丢 `_agent_written_paths` 反解析提示（优雅降级，§6.6）。

**Q：合法的 `python3 << 'EOF' ... EOF` heredoc 被审计挡了？**
A：不会。`shlex.split` 对 heredoc 抛 `ValueError` 时不再 block，而是继续走中危检查——高危模式在 shlex **之前**已对原文查过，所以 heredoc 体内的 `cat /etc/shadow` 仍会被 block；只有既不高危也不中危的合法 heredoc 才放行（§5.6）。

---

## §10 小结

沙箱子系统给 agent 「操作文件系统 + 跑命令」的能力，靠四件事立住：

1. **虚拟路径统一**：agent 只看 `/mnt/user-data/...`，底层 local/容器都不用改 agent 代码。命名 `/mnt/` 借 LLM 语义锚点。
2. **`Sandbox` ABC + 7 工具**：8 个抽象方法 → 7 个 agent 工具，两层（provider + 工具）路径防御。
3. **provider 单例 + per-thread LRU**：复用沙箱、按线程隔离、加锁防多线程双重初始化（回调锁外跑）。
4. **命令审计三档 + 本地非安全边界**：block/warn/pass 分级，host bash 默认禁用，真隔离靠 [#14 AIO](aio_sandbox.md)。

记三句就够：
- **本地不是安全边界**——靠路径翻译 + 穿越防御 + 审计，真隔离上容器。
- **虚拟路径进、宿主路径出、反解析洗回**——agent 永远看不到宿主布局。
- **provider 单例加锁、回调锁外**——多线程安全又不自死锁。

读完这篇，[#14 aio_sandbox.md](aio_sandbox.md) 讲「为什么本地不够、Docker 容器怎么补上真正的隔离」就顺了。

---

> 上一篇：[#12 serialization.md](serialization.md)（序列化——对象出进程的单一真相源，Phase 1 收官） · 下一篇：[#14 aio_sandbox.md](aio_sandbox.md)（AIO 沙箱——Docker/K8s 容器隔离 + 暖池 + 跨进程文件锁发现 + idle 回收；本地模式非安全边界的「真正解药」）
