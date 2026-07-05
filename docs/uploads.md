# 23. uploads.md — 文件上传 + markitdown 转换（路径安全 + symlink 防御 + soft-load）

> **M23 六维重审（2026-06-28）**：逐文件 diff 最新上游（`uploads/__init__`/`manager`/`conversion` +
> `agents/middlewares/uploads_middleware` + `config/uploads_config`）。结论：**核心函数逐个对齐，零逻辑漂移**。
> 关注点核对：**#3311** UploadsMiddleware 扫描经 `run_in_executor` 卸出事件循环（**已含**，且 `run_in_executor`
> 拷 contextvar、`get_effective_user_id` 得以保留）/ **#2623** 拒 symlink 上传目标 + **#2794** Windows
> 适配（mini 用 `open_upload_file_no_symlink`/`write_upload_file_no_symlink` + `UnsafeUploadPathError` 显式
> 封装，**比上游 harness 层更完整**——上游的等价 symlink 防御在 Gateway `app/` 层，不在 harness uploads
> 模块）/ per-thread 目录布局（`users/{user_id}/threads/{thread_id}/user-data/uploads`，**已含**）/
> `claim_unique_filename` 重复名 `_N` 改名 / 拒目录输入 / markitdown 转换（`convert_file_to_markdown`，
> soft-load + 转换 worker 池 `convert_with_pool`，活跃循环里复用单 worker）。
> **结构性差异（非漂移）**：mini 把转换逻辑放 `uploads/conversion.py`、上游放 `utils/file_conversion.py`
> （组织选择）；mini 多了 `convert_with_pool`/`make_conversion_pool`（转换 worker 池）+ `*_no_symlink` 封装。
> **不 port**：上游 `get_uploads_dir`/`ensure_uploads_dir` 加了可选 `user_id` kwarg（**#3579** IM channel
> owner-scoping——channel worker 经 `_channel_storage_user_id` 透传连接 owner 的 user_id）。mini 无 `app/channels/`
> （§2.3 设计不 port），HTTP/embedded 调用方用 `get_effective_user_id()` 已正确，故不 port（同 task_tool 用户上下文）。

> **一句话定位**：本模块给 agent 装「文件柜」——用户上传 PDF / PPT / Word / Excel 后，本模块**安全地**
> 存到 per-user per-thread 隔离的目录，再用 `markitdown` 把这些二进制文档**转成 markdown**（agent 只读
> 得懂文本），让 agent 能「看懂」用户传来的文件。核心是**安全**：路径穿越防御 + symlink 防御（防沙箱逃逸）。

读完 [tools.md](tools.md)（懂了「9 内置工具怎么汇总」）再看本篇最省事——本篇是 agent 工具之外的**另一条
输入通道**：用户不是靠「打字」给 agent 信息，而是**传文件**。本模块管「文件怎么安全落地、怎么变成 agent
读得懂的文本」。它为 [middlewares.md](middlewares.md)（M16）里的 `UploadsMiddleware` 铺路——中间件负责
「把上传清单注入对话」，本模块负责「文件本身的存取与转换」。

---

## 0. 这个模块解决什么问题

用户在聊天界面拖一个 PDF 进来，接下来会发生什么？

1. **文件要存哪？** 不能存到一个全局共享目录（A 用户能看 B 用户的文件 = 安全事故）。要按 **用户 + 对话线程**
   隔离。
2. **agent 看不了 PDF 字节流。** agent 是语言模型，只懂文本。要把 PDF 转成 markdown。
3. **用户传的文件名可能藏雷。** `../../etc/passwd` 这种文件名如果直接拼路径，会把系统文件写穿。
4. **沙箱可能被预置陷阱。** 上传目录挂进沙箱后，沙箱里的恶意进程能在一个「未来会上传的文件名」处放一个
   **symlink** 指向敏感文件。普通写文件会**跟随 symlink**，于是用「上传服务的权限」覆盖了敏感文件——这叫
   **symlink 攻击 / 沙箱逃逸**。

本模块（`uploads/`）逐个解决这四点。**纯业务逻辑**，不依赖 FastAPI / HTTP——Gateway router 和未来的
Client 都委托本模块的函数（这样「存取逻辑」只有一份真相源）。

## 1. 文件结构

```
uploads/
├── __init__.py     # 导出所有公共函数 + CONVERTIBLE_EXTENSIONS + 异常类
├── manager.py      # 路径安全 + symlink 防御 + 列表/删除 + 转换编排（事件循环内复用 worker）
└── conversion.py   # markitdown / pymupdf4llm 文档→markdown 转换（soft-load）+ 大纲抽取
```

两个文件分工：
- **`manager.py`**：管「**文件在磁盘上怎么安全地存取**」——路径清洗、穿越校验、symlink 拒绝、列出、删除。
  以及「**转换怎么编排**」（在事件循环里复用 worker，见 §6）。
- **`conversion.py`**：管「**一个文件怎么变成 markdown**」——调哪个转换器、PDF 双策略、大纲抽取。
  这层是纯算法，不碰路径安全。

为什么分两个文件？因为「路径安全」和「文档解析」是两个**正交的关注点**：换一个转换器不影响安全逻辑，
加一条安全规则不影响解析。分开后各自聚焦，也和 deer-flow 的布局对应（deer 把转换放 `utils/file_conversion.py`，
mini 收拢进 `uploads/` 子包，更内聚）。

## 2. 虚拟路径 vs 物理路径

这是理解本模块的**关键心智模型**。

**agent 看到的（虚拟路径）**：
```
/mnt/user-data/uploads/report.pdf
```
这个 `/mnt/user-data` 是 [sandbox.md](sandbox.md) 讲的**虚拟路径前缀**——agent 永远只看到这个固定路径，
不知道文件在宿主机上的真实位置。

**物理上落在哪（物理路径）**：
```
{base_dir}/users/{user_id}/threads/{thread_id}/user-data/uploads/report.pdf
# 例如：.deer-flow/users/alice/threads/t-42/user-data/uploads/report.pdf
```

三层隔离：`base_dir`（运行时根）→ `users/{user_id}`（**用户隔离**）→ `threads/{thread_id}`（**对话隔离**）。
不同用户的文件物理上分开，同一用户不同对话也分开。

路径怎么算？集中在 [config/paths.py](../backend/packages/harness/deerflow/config/paths.py) 的 `Paths` 类：

```python
def sandbox_uploads_dir(self, thread_id: str, *, user_id: str) -> Path:
    return self.thread_user_data_dir(user_id, thread_id) / "uploads"
```

> **唯一真相源**：`sandbox/local/local_sandbox.py` 的 `_thread_user_data_root` **委托**这个方法，而不是
> 自己再拼一遍。这样 uploads 和 sandbox 共用同一套布局，不会「一个模块改了路径，另一个模块还按老路径找」。
> [test_uploads.py](../test/test_uploads.py) 有测试钉死这个一致性。

`get_uploads_dir(thread_id)` 是对外的便捷函数，`user_id` 从 `user_context`（当前请求的用户）自动取：

```python
def get_uploads_dir(thread_id: str) -> Path:
    validate_thread_id(thread_id)
    return get_paths().sandbox_uploads_dir(thread_id, user_id=get_effective_user_id())
```

- `validate_thread_id` 先校验 thread_id 只含 `[A-Za-z0-9._-]`——防 `../` 或路径分隔符混进 thread_id 拼出穿越路径。
- `get_effective_user_id()` 是 [user_context.md](user_context.md) 的三态用户 id（默认 `"default"`）。

## 3. 路径安全：normalize_filename + validate_path_traversal

两道防线，纵深防御。

### 第一道：normalize_filename（清洗文件名）

用户传的文件名可能是 `../../etc/passwd`。`normalize_filename` 做：

```python
def normalize_filename(filename: str) -> str:
    if not filename:
        raise ValueError("Filename is empty")
    safe = Path(filename).name          # ① 只取 basename，剥掉所有目录成分
    if not safe or safe in {".", ".."}:  # ② 拒 "."/".."
        raise ValueError(...)
    if "\\" in safe:                     # ③ 拒反斜杠（Windows 路径分隔符）
        raise ValueError(...)
    if len(safe.encode("utf-8")) > 255:  # ④ 255 UTF-8 字节上限（文件系统限制）
        raise ValueError(...)
    return safe
```

- `Path("../../etc/passwd").name` → `"passwd"`：目录成分被剥光，穿越被中和。
- 为什么拒反斜杠？Linux 上 `Path.name` 把 `\` 当**字面字符**保留（`a\b.txt` 的 name 还是 `a\b.txt`），
  但它暗示 Windows 风格路径，应拒绝以免在不同 OS 上行为分裂。
- 255 **字节**不是字符：中文每字 3 字节 UTF-8，所以 85 个中文字才到上限。

### 第二道：validate_path_traversal（校验越界）

万一有路径绕过了第一道（比如某处用拼接而非 normalize），这层兜底：

```python
def validate_path_traversal(path: Path, base: Path) -> None:
    try:
        path.resolve().relative_to(base.resolve())
    except ValueError:
        raise PathTraversalError("Path traversal detected") from None
```

`.resolve()` 把符号链接和 `..` 都展开成绝对真实路径，再 `.relative_to(base)` 检查「真实路径是否在 base 内」。
不在就抛 `PathTraversalError`。**这层是 symlink 也能识别的**——一个指向 base 外的 symlink，resolve 后会跑到
外面，被抓住。

## 4. symlink 防御：open_upload_file_no_symlink（核心红线）

这是本模块**最微妙、也最关键**的安全逻辑。先说清攻击场景：

**威胁模型**：上传目录（`.../uploads/`）会被挂进沙箱（让 agent 能读上传的文件）。沙箱里的进程因此能在
上传目录里**创建文件**——包括在一个「未来会上传的文件名」处放一个 **symlink**，指向宿主机上的敏感文件
（比如 `/etc/shadow` 或别的用户的文件）。然后用户上传同名文件时，普通 `Path.write_bytes` 会**跟随 symlink**，
用**上传服务的权限**（通常比沙箱进程高）去覆盖那个敏感文件。这就是**沙箱逃逸 + 提权写**。

**防御**：`open_upload_file_no_symlink` 用 POSIX `O_NOFOLLOW` 打开——如果目标是 symlink，`open()` 直接
以 `ELOOP` 失败，**根本不跟随**：

```python
flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW
if hasattr(os, "O_NONBLOCK"):
    flags |= os.O_NONBLOCK
fd = os.open(dest, flags, 0o600)
```

打开后还要 `fstat` 复核：必须是普通文件（`S_ISREG`）且**硬链接数 == 1**（`st_nlink == 1`，防有人故意多建
硬链接指向敏感文件）。

> **为什么 O_NONBLOCK？** 开特殊文件（FIFO、设备文件）时普通 open 会阻塞，加 NONBLOCK 让它立即失败
> （`EAGAIN`/`ENXIO`），把这些「不该上传的目标」也挡掉。

**Windows 没有 `O_NOFOLLOW`**，退化为「open 前 `lstat` + open 后 `fstat`」双校验，缩小 **TOCTOU**（time-of-check
to time-of-use）竞态窗口——不能完全消除（攻击者可能在 lstat 之后、open 之前原子地把文件换成 symlink），但
显著提高利用难度。注释里**诚实标注了**这个残留风险。

`write_upload_file_no_symlink(base, name, data)` 是它的便捷封装：开 + 写 + 关，一步到位。

> **红线 #29**：uploads 写入必须拒绝 symlink 目标，防沙箱逃逸。本函数是这条红线的落地。

## 5. markitdown 转换（soft-load 是核心）

agent 读不懂 PDF。`convert_file_to_markdown` 把二进制文档转 markdown：

```python
async def convert_file_to_markdown(file_path: Path) -> Path | None:
    try:
        text = _do_convert(file_path, _get_pdf_converter())
        md_path = file_path.with_suffix(".md")
        md_path.write_text(text, encoding="utf-8")
        return md_path
    except Exception:
        return None  # 转换失败返回 None，原文件保留——上传不受影响
```

**两个转换器都是 soft-load**（函数内 `import`，缺包即回退）：

| 转换器 | 能力 | soft-load 行为 |
|--------|------|---------------|
| `markitdown` | 微软出品，PDF/PPT/Excel/Word 通吃，图片 PDF 走 OCR | `from markitdown import MarkItDown` 缺包 → `ImportError` → 回退 |
| `pymupdf4llm` | PDF 专用，标题检测更好、更快，但纯图片 PDF 输出接近空白 | 缺包 → 返回 `None` → 走 markitdown |

**关键契约**：两个都缺包时，`convert_file_to_markdown` 返回 `None`，**原文件照常保留**。上传功能不依赖
转换器——这是「**soft-load：缺包跳过转换但上传仍可用**」的精确含义。装上 `pip install markitdown` 后转换
自动生效，无需改代码。

### PDF 双转换策略（auto 模式，默认）

为什么 PDF 要两步？因为 pymupdf4llm 对**纯图片 PDF**（扫描件、加密 PDF）会输出接近空白：

1. 装了 pymupdf4llm → 先试它（更好）。
2. 输出**太稀疏**（< 50 字/页，或页数不可得时 < 200 字）→ 判定为图片 PDF，**回退 markitdown**（后者走 OCR）。
3. 没装 pymupdf4llm → 直接 markitdown。

「太稀疏」用**每页字符数**而非绝对阈值——这样 1 页短文档（字少）和 100 页长文档（字多）都能正确判定，
不会误把「本来就短的合法文档」当图片 PDF。

可在 `config.yaml` 强制选转换器：
```yaml
uploads:
  auto_convert_documents: true   # 关掉则只存原文件不转换
  pdf_converter: auto            # auto / pymupdf4llm / markitdown
```

### 大文件卸载到线程

转换是 CPU/IO 重活。> 1MB 的文件用 `asyncio.to_thread` 卸载，避免阻塞事件循环（这是 deer #1569 的修复）。
小文件同步转（< 1s，起线程反而白白增加调度开销）。

## 6. 事件循环内复用 worker（make_conversion_pool）

上传流程（未来的 `UploadsMiddleware.abefore_agent`）跑在**活动事件循环**里。如果逐个文件
`asyncio.run(convert_file_to_markdown(path))`，有两个问题：

1. **在已有事件循环里调 `asyncio.run` 直接抛 `RuntimeError`**（一个线程只能有一个运行中的循环）。
2. 即使不抛，每文件建+拆一个事件循环，开销大。

解决：`make_conversion_pool()` 检测「是否在活动循环里」——是就返回一个**单 worker 线程池**，转换提交到
worker 线程（worker 线程里没有事件循环，可以安全 `asyncio.run`）；不在循环里就返回 `None`（直接 `asyncio.run`）：

```python
def make_conversion_pool():
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return None  # 不在循环里
    return ThreadPoolExecutor(max_workers=1)  # 在循环里，复用单 worker

def convert_with_pool(pool, path):
    if pool is not None:
        return pool.submit(_convert_in_worker, path).result()  # worker 内 asyncio.run
    return asyncio.run(convert_file_to_markdown(path))
```

`max_workers=1`：转换是顺序的，且单文件转换内部已有线程卸载（>1MB 走 to_thread），多 worker 收益小还抢资源。
测试验证：在循环里用 pool 连转 3 个文件，**全部跑在同一个 worker 线程**（复用生效）。

## 7. 列表 / 删除 / 虚拟路径补全

- **`list_files_in_dir(dir)`**：只列**文件**（`os.scandir` + `is_file(follow_symlinks=False)`——不跟随 symlink，
  上传目录里的 symlink 不算合法上传文件）。每项含 `filename/size/path/extension/modified`。
- **`delete_file_safe(base, name, *, convertible_extensions=)`**：穿越校验后删文件。若传了
  `convertible_extensions` 且文件扩展名命中，**顺带删伴随 `.md`**（上传时转换生成的）——防删原文留个孤儿 markdown。
  `.md` 不存在也不报错（`missing_ok=True`，因为转换可能失败没生成）。
- **`enrich_file_listing(result, thread_id)`**：给 list 结果的每项补 `virtual_path`（agent 看的
  `/mnt/user-data/uploads/x`）和 `artifact_url`（下载 URL，文件名 percent-encoded，让空格/`#`/`?` 安全）。

## 8. 一次完整上传的流程（把上面的串起来）

用户拖 3 个文件（含 2 个重名的 `report.pdf`）上传：

```
ensure_uploads_dir(thread_id)          # 建目录（per-user per-thread）
seen = set()
for file in files:
    name = claim_unique_filename(file.name, seen)   # report.pdf → report_1.pdf 防互相覆盖
    dest, fh = open_upload_file_no_symlink(dir, name)  # O_NOFOLLOW 防 symlink
    with fh: fh.write(file.bytes)                     # 流式写
    if name 末尾 in CONVERTIBLE_EXTENSIONS:
        md = convert_with_pool(pool, dest)            # 转 markdown（事件循环内复用 worker）
# enrich_file_listing 给每项补 virtual_path/artifact_url
# → 返回给前端 / 注入对话（M16 UploadsMiddleware 的活）
```

删一个文件：`delete_file_safe(dir, "report.pdf", convertible_extensions=CONVERTIBLE_EXTENSIONS)`——删原文 + 伴随 `.md`。

## 9. 配置（UploadsConfig）

[config/uploads_config.py](../backend/packages/harness/deerflow/config/uploads_config.py) 定义 `UploadsConfig`：

| 字段 | 默认 | 作用 |
|------|------|------|
| `auto_convert_documents` | `true` | 上传后是否自动转 markdown。关掉只存原文件。 |
| `pdf_converter` | `"auto"` | PDF 用哪个转换器：`auto`/`pymupdf4llm`/`markitdown`。 |

`normalized_pdf_converter()` 把值归一为小写并校验——防 `config.yaml` 写成 `AUTO`/`MarkItDown` 时静默走错分支。
`AppConfig.uploads` 默认就是这个配置（不写 `uploads:` 段也能跑，对教学起步友好）。

## 10. 与其它模块的关系

| 模块 | 关系 |
|------|------|
| **config/paths** | `Paths.sandbox_uploads_dir` 是上传目录的**唯一路径来源** |
| **runtime/user_context** | `get_uploads_dir` 从这里取当前 `user_id` 实现 per-user 隔离 |
| **sandbox** | 上传目录挂进沙箱（`/mnt/user-data/uploads`），sandbox 的 `_thread_user_data_root` 与本模块共享布局 |
| **M16 UploadsMiddleware** | 调用方——把「上传清单 + 文档大纲」注入对话，让 agent 知道有这些文件 |
| **M17 lead_agent** | agent 通过沙箱虚拟路径读上传的文件（`/mnt/user-data/uploads/x.md`） |

## 11. 设计要点回顾

1. **纯业务逻辑，无 HTTP 依赖**——router 和 client 共用一份存取逻辑。
2. **路径安全两道防线**：normalize_filename（剥目录成分）+ validate_path_traversal（resolve + relative_to 兜底，含 symlink）。
3. **symlink 防御是核心红线**（#29）：`O_NOFOLLOW` 拒绝跟随 symlink 目标，防沙箱逃逸/提权写。Windows 无 O_NOFOLLOW 退化为 lstat+fstat 双校验，诚实标注残留 TOCTOU。
4. **soft-load 贯穿**：markitdown / pymupdf4llm 缺包 → 转换跳过但上传仍可用（红线 #24 同款思路）。
5. **PDF 双策略**：pymupdf4llm 优先，图片 PDF 回退 markitdown（OCR），按每页字符数判定。
6. **事件循环内复用 worker**：活动循环里用单 worker 线程池跑 `asyncio.run`，避免反复建拆循环。
7. **per-user per-thread 物理隔离**：虚拟路径统一 `/mnt/user-data/uploads`，物理三段隔离。
8. **伴随 .md 清理**：删原文顺带删转换生成的 markdown，防孤儿文件。

## 12. 排错 FAQ

- **「上传成功但没生成 .md」**：`markitdown` 没装。`pip install markitdown`（PDF 想更好再装 `pymupdf4llm`）。原文件已存，agent 可通过沙箱读原文件（只是看不懂 PDF）。
- **`UnsafeUploadPathError`**：上传目标处有 symlink / 目录 / 多硬链接。通常是沙箱进程预置了 symlink（安全告警，非 bug）。
- **`PathTraversalError`**：某处用拼接而非 normalize_filename 构造了越界路径——查调用方是否绕过了 normalize_filename。
- **转换很慢卡住对话**：文件 > 1MB 会走 `to_thread` 不阻塞循环；若仍慢，考虑关 `auto_convert_documents` 或换更快的转换器。
- **PDF 转出来是空的**：图片 PDF，pymupdf4llm 啃不动。确认 markitdown 已装（auto 模式会自动回退到它走 OCR）。

---

**下一篇**：[README.md](README.md) 的待写表里，M23 完成后，下一个是 [middlewares.md](middlewares.md)——
本模块的 `convert_file_to_markdown` / `list_files_in_dir` / `enrich_file_listing` 会被 `UploadsMiddleware` 调用，
把上传清单注入对话。
