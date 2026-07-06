# 23. uploads.md — 文件上传 + markitdown 转换（路径安全 + symlink 防御 + soft-load）

> **重写日期**：2026-07-05。**对照代码**：`backend/packages/harness/deerflow/uploads/`（3 文件，706 行）。

> **一句话定位**：本模块给 agent 装「文件柜」——用户上传 PDF / PPT / Word / Excel 后，本模块**安全地**存到 per-user per-thread 隔离的目录，再用 `markitdown` 把这些二进制文档**转成 markdown**（agent 只读得懂文本），让 agent 能「看懂」用户传来的文件。核心是**安全**：路径穿越防御 + symlink 防御（防沙箱逃逸）。

> **先读谁最省事**：[tools.md](tools.md)（懂「9 内置工具怎么汇总」）+ [sandbox.md](sandbox.md)（懂虚拟路径前缀 `/mnt/user-data`）。本篇是 agent 工具之外的**另一条输入通道**：用户不靠「打字」给 agent 信息，而是**传文件**。它为 [middlewares.md](middlewares.md) 里的 `UploadsMiddleware` 铺路——中间件负责「把上传清单注入对话」，本模块负责「文件本身的存取与转换」。

---

## §1 学完这篇你能回答什么（learning outcomes · 面试视角）

1. **「用户上传的文件怎么安全落地？」** —— per-user per-thread 物理隔离 + 路径穿越两道防线。能讲清 normalize_filename（剥目录成分）+ validate_path_traversal（resolve+relative_to 兜底）。
2. **「上传目录挂进沙箱后，恶意进程能怎么攻击？怎么防？」** —— symlink 攻击（预置 symlink 让上传覆盖敏感文件 = 沙箱逃逸/提权写）。能讲清 `O_NOFOLLOW` 拒跟随 + fstat 复核硬链接数 + Windows 无 O_NOFOLLOW 的 TOCTOU 残留风险。
3. **「agent 读不懂 PDF 字节流，怎么办？」** —— markitdown/pymupdf4llm 转 markdown，soft-load（缺包跳过转换但上传仍可用）。
4. **「PDF 为什么用两个转换器？」** —— pymupdf4llm 标题检测好但纯图片 PDF 输出空白，按每页字符数判定后回退 markitdown（OCR）。
5. **「在已有事件循环里逐文件 `asyncio.run` 转换会怎样？」** —— RuntimeError（不能嵌套循环）。能讲清「事件循环内复用 worker」模式（单 worker 线程池）。
6. **「虚拟路径 vs 物理路径为什么要分开？」** —— agent 永远看 `/mnt/user-data/uploads/x`（固定），物理按 user+thread 三段隔离。

---

## §2 零基础先读：名词解释

### §2.1 计算机基础层（不熟这些先看这段）

| 名词 | 一句话解释 |
|---|---|
| **symlink（符号链接）** | 一种「快捷方式」文件，指向另一个路径。危险：上传写入若跟随 symlink，可能覆盖链接指向的敏感文件。 |
| **硬链接（hard link）** | 同一个文件有多个目录入口（共享 inode）。多硬链接可能被故意指向敏感文件，故上传要校验 `st_nlink == 1`。 |
| **`O_NOFOLLOW`** | POSIX open() 标志：目标若是 symlink，open 直接以 `ELOOP` 失败，**根本不跟随**。防 symlink 攻击的核心。 |
| **`O_NONBLOCK`** | open() 标志：开特殊文件（FIFO/设备）时不阻塞、立即失败。挡掉「不该上传的目标」。 |
| **TOCTOU** | "time-of-check to time-of-use"——检查和实际使用之间有竞态窗口（攻击者可在 lstat 之后、open 之前原子换 symlink）。Windows 无 O_NOFOLLOW 的双校验不能完全消除。 |
| **路径穿越（path traversal）** | 用 `../` 或绝对路径逃出限定目录。`../../etc/passwd` 当文件名直接拼路径会写穿系统文件。 |
| **basename** | 路径的「文件名部分」（`a/b/c.txt` 的 basename 是 `c.txt`）。`normalize_filename` 只取 basename 剥光目录成分。 |
| **`resolve()`** | 把路径展开成绝对真实路径（展开 `..` 和 symlink）。`validate_path_traversal` 靠它 + `relative_to` 校验越界。 |
| **`lstat` vs `stat`** | `lstat` 不跟随 symlink（看链接本身）；`stat` 跟随（看指向的文件）。上传校验用 lstat。 |
| **事件循环 / `asyncio.run` 嵌套** | 一个事件循环正在跑时不能再 `asyncio.run`（报 "loop already running"）。所以转换要卸到新线程。 |
| **soft-load（软加载）** | import 放函数内 + `try/except ImportError`，依赖包没装时不崩、降级。 |
| **percent-encoding** | URL 里把特殊字符（空格/`#`/`?`）编码成 `%20` 等，让 URL 安全。`artifact_url` 用它。 |
| **OCR** | 光学字符识别——从图片里认文字。markitdown 对图片 PDF 走 OCR。 |

### §2.2 本模块名词

| 名词 | 解释 |
|---|---|
| **虚拟路径** | agent 在沙箱里看到的固定路径 `/mnt/user-data/uploads/<file>`。 |
| **物理路径** | 文件在宿主机的真实位置 `{base_dir}/users/{uid}/threads/{tid}/user-data/uploads/<file>`，三段隔离。 |
| **markitdown / pymupdf4llm** | 两个文档→markdown 转换器（soft-load）。markitdown 多格式通吃；pymupdf4llm PDF 专用更好。 |
| **伴随 `.md`** | 上传时转换生成的同名 markdown（`report.pdf` → `report.md`）。删原文时顺带清掉。 |
| **文档大纲（outline）** | 从转换出的 markdown 抽 heading 列表，给 agent「这份文档讲了什么」的目录。 |

---

## §3 整体结构：它在系统里的位置

```
用户拖文件上传（UploadsMiddleware / API）
   │
   ▼
uploads/manager.py（路径安全 + symlink 防御 + 列表/删除 + 转换编排）
   ├─ validate_thread_id / normalize_filename / validate_path_traversal（路径安全）
   ├─ open_upload_file_no_symlink / write_upload_file_no_symlink（O_NOFOLLOW 防 symlink）
   ├─ list_files_in_dir / delete_file_safe / enrich_file_listing（列表/删除/虚拟路径补全）
   └─ make_conversion_pool / convert_with_pool（事件循环内复用 worker）
        │
        ▼
uploads/conversion.py（文档 → markdown 转换，soft-load）
   ├─ convert_file_to_markdown（PDF 双策略 + 大文件 to_thread）
   └─ extract_outline（heading 抽取，给 agent 文档目录）
        │
        ▼
UploadsMiddleware（middlewares）：把「上传清单 + 文档大纲」注入对话
```

**三个文件的职责切分**（为什么这么拆见 [§9 设计动机](#9-设计动机分析为什么这么设计作用好处)）：

```
uploads/
├── __init__.py     # 导出公共函数 + CONVERTIBLE_EXTENSIONS + 异常类
├── manager.py      # 文件在磁盘上怎么安全地存取 + 转换怎么编排（事件循环内复用 worker）
└── conversion.py   # 一个文件怎么变成 markdown（调哪个转换器 + PDF 双策略 + 大纲抽取）
```

**面试概念地图**：本篇对应「文件安全（穿越/symlink/TOCTOU）」「soft-load 降级」「异步并发（事件循环内复用 worker）」面试常考点。无 deerflow-book 对应章节（内部模块）。

---

## §4 核心概念：虚拟路径 vs 物理路径 + 这个模块解决什么

用户拖一个 PDF 进来，会发生四件事，本模块逐个解决：

1. **文件存哪？** 不能存全局共享目录（A 能看 B 的文件 = 安全事故）。要按**用户 + 对话线程**隔离。
2. **agent 看不了 PDF 字节流。** agent 是语言模型，只懂文本。要转成 markdown。
3. **文件名可能藏雷。** `../../etc/passwd` 直接拼路径会写穿系统文件。
4. **沙箱可能被预置陷阱。** 上传目录挂进沙箱后，沙箱里的恶意进程能在「未来会上传的文件名」处放一个 **symlink** 指向敏感文件。普通写文件会**跟随 symlink**，用「上传服务的权限」覆盖敏感文件——这叫 **symlink 攻击 / 沙箱逃逸**。

**关键心智模型——虚拟路径 vs 物理路径**：

```
agent 看到的（虚拟路径）：
/mnt/user-data/uploads/report.pdf

物理上落在哪（物理路径）：
{base_dir}/users/{user_id}/threads/{thread_id}/user-data/uploads/report.pdf
# 例如：.deer-flow/users/alice/threads/t-42/user-data/uploads/report.pdf
```

三层隔离：`base_dir`（运行时根）→ `users/{user_id}`（**用户隔离**）→ `threads/{thread_id}`（**对话隔离**）。路径集中在 `Paths.sandbox_uploads_dir`，sandbox 的 `_thread_user_data_root` **委托**这个方法（唯一真相源，uploads 和 sandbox 共用布局）。

---

## §5 代码走读：重要函数逐个讲

### §5.1 manager.py —— 路径安全两道防线

**第一道：`normalize_filename`** [manager.py:72](../backend/packages/harness/deerflow/uploads/manager.py#L72) 清洗文件名：

```python
safe = Path(filename).name          # ① 只取 basename，剥掉所有目录成分
if not safe or safe in {".", ".."}: # ② 拒 "."/".."
    raise ValueError(...)
if "\\" in safe:                    # ③ 拒反斜杠（Windows 路径分隔符）
    raise ValueError(...)
if len(safe.encode("utf-8")) > 255: # ④ 255 UTF-8 字节上限（文件系统限制）
    raise ValueError(...)
```

`Path("../../etc/passwd").name` → `"passwd"`：目录成分被剥光，穿越被中和。255 **字节**不是字符（中文每字 3 字节，85 个中文字才到上限）。

**第二道：`validate_path_traversal`** [manager.py:128](../backend/packages/harness/deerflow/uploads/manager.py#L128) 兜底校验越界：

```python
path.resolve().relative_to(base.resolve())  # resolve 展开 .. 和 symlink，relative_to 查是否在 base 内
```

`.resolve()` 把符号链接和 `..` 都展开成绝对真实路径——**这层 symlink 也能识别**（指向 base 外的 symlink resolve 后会跑到外面）。`validate_thread_id` [manager.py:49](../backend/packages/harness/deerflow/uploads/manager.py#L49) 校验 thread_id 只含 `[A-Za-z0-9._-]`，防 `../` 注入。

### §5.2 manager.py —— symlink 防御（核心安全逻辑）

**`open_upload_file_no_symlink`** [manager.py:140](../backend/packages/harness/deerflow/uploads/manager.py#L140) 用 POSIX `O_NOFOLLOW` 拒绝 symlink 目标：

```python
flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW    # symlink → open() 以 ELOOP 失败，不跟随
if hasattr(os, "O_NONBLOCK"): flags |= os.O_NONBLOCK
fd = os.open(dest, flags, 0o600)
# fstat 复核：必须是普通文件（S_ISREG）且硬链接数 == 1（st_nlink == 1）
if not stat.S_ISREG(opened_stat.st_mode) or opened_stat.st_nlink != 1:
    raise UnsafeUploadPathError(...)
```

打开后 `fstat` 复核：必须 `S_ISREG`（普通文件）且 `st_nlink == 1`（防多硬链接指向敏感文件）。**Windows 无 `O_NOFOLLOW`**，退化为「open 前 lstat + open 后 fstat」双校验，缩小 TOCTOU 窗口（注释诚实标注残留风险）。`write_upload_file_no_symlink` [manager.py:237](../backend/packages/harness/deerflow/uploads/manager.py#L237) 是它的便捷封装。

### §5.3 manager.py —— 列表 / 删除 / 虚拟路径补全

- **`list_files_in_dir`** [manager.py:245](../backend/packages/harness/deerflow/uploads/manager.py#L245)：`os.scandir` + `is_file(follow_symlinks=False)` 只列文件（上传目录里的 symlink 不算合法上传）。`claim_unique_filename` [manager.py:101](../backend/packages/harness/deerflow/uploads/manager.py#L101) 重复名 `_N` 改名。
- **`delete_file_safe`** [manager.py:277](../backend/packages/harness/deerflow/uploads/manager.py#L277)：穿越校验后删文件，若扩展名命中 `convertible_extensions` 顺带删伴随 `.md`（`missing_ok=True`，转换可能失败没生成）。
- **`enrich_file_listing`** [manager.py:324](../backend/packages/harness/deerflow/uploads/manager.py#L324)：给 list 结果补 `virtual_path`（agent 看的 `/mnt/user-data/uploads/x`）和 `artifact_url`（下载 URL，文件名 percent-encoded）。

### §5.4 conversion.py —— 文档转 markdown（soft-load）

**`convert_file_to_markdown`** [conversion.py:156](../backend/packages/harness/deerflow/uploads/conversion.py#L156)：两个转换器都 soft-load（函数内 import，缺包即回退）。**关键契约**：两个都缺包时返回 `None`，**原文件照常保留**——上传不依赖转换器。

**PDF 双转换策略**（`_do_convert` [conversion.py:125](../backend/packages/harness/deerflow/uploads/conversion.py#L125)）：① 装了 pymupdf4llm 先试它（更好）；② 输出太稀疏（`_pymupdf_output_too_sparse` [conversion.py:73](../backend/packages/harness/deerflow/uploads/conversion.py#L73)，< 50 字/页）判定图片 PDF，回退 markitdown（OCR）；③ 没装 pymupdf4llm 直接 markitdown。「太稀疏」用**每页字符数**而非绝对阈值——1 页短文档和 100 页长文档都能正确判定。

**`extract_outline`** [conversion.py:233](../backend/packages/harness/deerflow/uploads/conversion.py#L233)：从转换出的 markdown 抽 heading 列表，识别 pymupdf4llm 的三种标题风格（标准 `#` / 纯粗体结构性标题 `**ITEM 1. BUSINESS**` / 拆分粗体标题 `**1** **Introduction**`），给 agent 文档目录（`MAX_OUTLINE_ENTRIES = 50` 上限）。

### §5.5 manager.py —— 事件循环内复用 worker

**`make_conversion_pool`** [manager.py:344](../backend/packages/harness/deerflow/uploads/manager.py#L344)：检测「是否在活动事件循环里」——是就返回单 worker 线程池（`max_workers=1`），否则返回 None。**`convert_with_pool`** [manager.py:365](../backend/packages/harness/deerflow/uploads/manager.py#L365)：有池就提交到 worker 线程（worker 内 `asyncio.run`），无池直接 `asyncio.run`。

---

## §6 数据流：一次完整上传怎么走完

用户拖 3 个文件（含 2 个重名的 `report.pdf`）上传：

```
① ensure_uploads_dir(thread_id)                         # 建目录（per-user per-thread）
② pool = make_conversion_pool()                         # 检测事件循环，活动则返单 worker 池
③ seen = set()
   for file in files:
     name = claim_unique_filename(file.name, seen)      # report.pdf → report_1.pdf 防互相覆盖
     dest, fh = open_upload_file_no_symlink(dir, name)  # O_NOFOLLOW 防 symlink
     with fh: fh.write(file.bytes)                      # 流式写
     if name 后缀 in CONVERTIBLE_EXTENSIONS:
       md = convert_with_pool(pool, dest)               # 转 markdown（事件循环内复用 worker）
④ enrich_file_listing 给每项补 virtual_path / artifact_url
⑤ → 返回给前端 / UploadsMiddleware 注入对话
```

删一个文件：`delete_file_safe(dir, "report.pdf", convertible_extensions=CONVERTIBLE_EXTENSIONS)`——删原文 + 伴随 `.md`。

---

## §7 配置与用法

### §7.1 配置（`UploadsConfig`，[config/uploads_config.py](../backend/packages/harness/deerflow/config/uploads_config.py)）

| 字段 | 默认 | 作用 |
|------|------|------|
| `auto_convert_documents` | `true` | 上传后是否自动转 markdown。关掉只存原文件。 |
| `pdf_converter` | `"auto"` | PDF 用哪个转换器：`auto`/`pymupdf4llm`/`markitdown`。 |

`normalized_pdf_converter()` 把值归一小写并校验——防 `config.yaml` 写成 `AUTO`/`MarkItDown` 时静默走错分支。

### §7.2 跑测试

```bash
cd backend && make test    # 含 test/test_uploads.py（89 个 hermetic 测试）
```

测试约定：转换器缺包用 monkeypatch；路径安全/穿越/symlink 用 tmp_path 隔离；转换池测试验证「循环内连转 3 文件全跑同一 worker 线程」。

---

## §8 与其它模块的关系

| 模块 | 关系 |
|------|------|
| **config/paths** | `Paths.sandbox_uploads_dir` 是上传目录的**唯一路径来源** |
| **runtime/user_context** | `get_uploads_dir` 从这里取当前 `user_id` 实现 per-user 隔离 |
| **sandbox** | 上传目录挂进沙箱（`/mnt/user-data/uploads`），sandbox 的 `_thread_user_data_root` 与本模块共享布局 |
| **UploadsMiddleware**（[middlewares](middlewares.md)） | 调用方——把「上传清单 + 文档大纲」注入对话，让 agent 知道有这些文件 |
| **lead_agent**（[agents](agents.md)） | agent 经沙箱虚拟路径读上传文件（`/mnt/user-data/uploads/x.md`） |

- **上游**：[config](config.md) paths（路径布局）+ uploads_config、[runtime/user_context](user_context.md)（per-user user_id）。
- **下游消费者**：[middlewares](middlewares.md) UploadsMiddleware（扫描上传目录 + `extract_outline` 注入对话，经 `run_in_executor` 卸出事件循环并拷 contextvar）。

---

## §9 设计动机分析（为什么这么设计 / 作用 / 好处）

### §9.0 核心设计动机一览

| 关键机制 | 为什么这么设计 | 作用 / 好处 | 不这么设计会怎样 |
|---|---|---|---|
| **per-user per-thread 物理隔离** | 多用户共用一目录 = 安全事故 | 三段隔离，物理分开 | A 能看 B 的文件 |
| **路径安全两道防线** | normalize + validate 纵深防御 | basename 剥目录 + resolve+relative_to 兜底 | 一道被绕过就穿越 |
| **symlink 防御 `O_NOFOLLOW`** | 沙箱进程能预置 symlink | open 不跟随 symlink + fstat 复核硬链接 | 上传覆盖敏感文件（沙箱逃逸/提权） |
| **soft-load 转换器** | markitdown/pymupdf4llm 可选 | 缺包跳过转换但上传仍可用 | 缺包 → 上传整个崩 |
| **PDF 双策略** | pymupdf4llm 图片 PDF 输出空白 | 按每页字符数判定回退 markitdown OCR | 图片 PDF 转出空文档 |
| **事件循环内复用 worker** | 活动循环里 `asyncio.run` 会 RuntimeError | 单 worker 线程池复用，避免反复建拆循环 | RuntimeError + 开销大 |
| **伴随 .md 清理** | 删原文留孤儿 markdown | delete 顺带删转换生成的 .md | 孤儿文件堆积 |
| **conversion 单独成文件** | 路径安全 vs 文档解析正交 | 换转换器不影响安全逻辑 | 一锅改一处误伤另一处 |

### §9.1 为什么 symlink 防御是最关键的安全逻辑

**动机**：上传目录挂进沙箱（让 agent 能读上传文件），沙箱进程因此能在上传目录里**创建文件**——包括在一个「未来会上传的文件名」处放一个 **symlink** 指向宿主机敏感文件（`/etc/shadow`、别的用户的文件）。用户上传同名文件时，普通 `Path.write_bytes` 会**跟随 symlink**，用**上传服务的权限**（通常比沙箱进程高）去覆盖那个敏感文件——**沙箱逃逸 + 提权写**。

**作用 / 好处**：`open_upload_file_no_symlink` 用 `O_NOFOLLOW` 打开——目标是 symlink 时 `open()` 以 `ELOOP` 失败，**根本不跟随**。打开后 `fstat` 复核必须是普通文件且硬链接数 == 1（防故意多建硬链接指向敏感文件）。加 `O_NONBLOCK` 让开特殊文件（FIFO/设备）也立即失败。

**不这么设计会怎样**：普通 write 跟随 symlink → 上传覆盖敏感文件 → 沙箱逃逸。

**Windows 的诚实权衡**：无 `O_NOFOLLOW`，退化为 lstat+fstat 双校验，**不能完全消除 TOCTOU**（攻击者可能在 lstat 后、open 前原子换 symlink）——注释诚实标注这个残留风险，靠路径穿越校验缓解。

### §9.2 为什么路径安全要两道防线（纵深防御）

**动机**：万一某处用拼接而非 `normalize_filename` 构造路径，绕过了第一道。

**作用 / 好处**：第一道 `normalize_filename` 在入口剥光目录成分（`../../etc/passwd` → `passwd`）；第二道 `validate_path_traversal` 用 `resolve().relative_to()` 兜底（含 symlink——指向 base 外的 symlink resolve 后跑到外面被抓住）。纵深防御：一道被绕过，另一道还挡着。

**不这么设计会怎样**：只一道 → 被绕过就穿越写任意文件。

### §9.3 为什么转换器要 soft-load + PDF 双策略

**动机**：agent 读不懂 PDF 字节流，但 markitdown/pymupdf4llm 是可选重依赖。

**作用 / 好处**：① soft-load（函数内 import + try/except）——两个都缺包时 `convert_file_to_markdown` 返回 `None`，**原文件照常保留**，上传不依赖转换器；装上 `pip install markitdown` 后转换自动生效。② PDF 双策略：pymupdf4llm 先试（标题检测好、快），输出太稀疏（< 50 字/页）判定图片 PDF 回退 markitdown（OCR 能啃图片）。大文件 > 1MB 用 `asyncio.to_thread` 卸载不阻塞循环。

**不这么设计会怎样**：硬依赖转换器 → 缺包上传整个崩；单策略 → 图片 PDF 转出空文档。

### §9.4 为什么事件循环内要复用 worker

**动机**：上传流程（UploadsMiddleware）跑在活动事件循环里。逐文件 `asyncio.run(convert_file_to_markdown)`：① 在已有循环里调 `asyncio.run` 直接抛 `RuntimeError`；② 每文件建+拆一个循环开销大。

**作用 / 好处**：`make_conversion_pool` 检测活动循环——是就返回单 worker 线程池（`max_workers=1`），转换提交到 worker 线程（worker 内无循环，可安全 `asyncio.run`）；不在循环里返回 None 直接 `asyncio.run`。单 worker：转换顺序、单文件内部已有线程卸载，多 worker 收益小还抢资源。

**不这么设计会怎样**：直接 `asyncio.run` → RuntimeError 崩；每文件新循环 → 开销大。

### §9.5 为什么 conversion 单独成文件

**动机**：「路径安全」和「文档解析」是两个**正交关注点**。

**作用 / 好处**：`manager.py` 管「文件怎么安全存取」+ 转换编排；`conversion.py` 管「文件怎么变成 markdown」（纯算法，不碰路径安全）。换转换器不影响安全逻辑，加安全规则不影响解析。mini 把转换收拢进 `uploads/` 子包（上游放 `utils/file_conversion.py`）——更内聚。

**不这么设计会怎样**：一锅 → 改转换器误伤安全逻辑。

---

## §10 实现差异（vs 上游 deer-flow 源码）

> 对照 `deer-flow/backend/packages/harness/deerflow/`（uploads/ + utils/file_conversion.py）。**先剥 docstring/comment 再判逻辑差**。

**总结论：高度忠实移植，mini 把转换收拢进 uploads/ 子包并加了「事件循环内复用 worker」编排。**

| 文件 | 剥后 mini/up | 逻辑差 |
|---|---|---|
| `manager.py` | 169 / 153 | **核心安全逻辑 0 逻辑差**——`normalize_filename`/`validate_path_traversal`/`open_upload_file_no_symlink`（O_NOFOLLOW + fstat 复核 nlink）/`write_upload_file_no_symlink`/`list_files_in_dir`/`delete_file_safe`/`enrich_file_listing`/`claim_unique_filename` 全一致（**勘误**：旧文档称「mini 的 symlink 防御比上游 harness 层更完整，上游等价防御在 Gateway app/ 层」——**不准**：上游 `uploads/manager.py` 同样有 `open_upload_file_no_symlink`/`UnsafeUploadPathError`）。mini 两处差异：① **mini 新增** `make_conversion_pool`/`_convert_in_worker`/`convert_with_pool`（事件循环内复用 worker 编排，上游 uploads 无此模式）；② `get_uploads_dir`/`ensure_uploads_dir`：上游有可选 `user_id: str \| None = None` kwarg（IM channel owner-scoping，channel worker 透传连接 owner 的 user_id），mini 无此 kwarg（mini 无 IM/channel，HTTP/embedded 调用方用 `get_effective_user_id()` 已正确） |
| `conversion.py`（mini）vs `utils/file_conversion.py`（上游） | 123 / 130 | **0 逻辑差（组织迁移）**——mini 把转换逻辑从 `utils/file_conversion.py` 收拢进 `uploads/conversion.py`（更内聚）。PDF 双策略（pymupdf4llm 优先 + 稀疏回退 markitdown）/ soft-load / `extract_outline` 三种标题风格 / 大文件 to_thread **全一致**。唯一差：mini `CONVERTIBLE_EXTENSIONS: frozenset[str]`（带泛型注解）vs 上游 untyped set 字面量——等价 |
| `__init__.py` | 55 / 29 | mini **多导出**（`convert_file_to_markdown`/`extract_outline`/`CONVERTIBLE_EXTENSIONS` 等 conversion 符号）——API 面差异 |

**为什么这样？** uploads 是**纯业务逻辑**（路径安全 + 文档转换），不依赖 Gateway/IM/auth。安全核心（normalize/traverse/symlink）靠**纯函数 + OS 原语**（`O_NOFOLLOW`/`lstat`/`fstat`）解耦，砍 Gateway 一行不改，故忠实。两处真差异都有据：① mini **新增**转换池编排（事件循环内复用 worker，是 mini 的工程改进）；② mini **不 port** IM channel 的 `user_id` kwarg（无 IM/channel，同 [#15 subagents](subagents.md) / [#22 tools](tools.md) 的 Gateway-auth-不-port 结论）。转换文件的位置迁移是组织选择（mini 更内聚），无行为差。

---

## §11 常见问题 / 排错

**Q：上传成功但没生成 .md？**
A：`markitdown` 没装。`pip install markitdown`（PDF 想更好再装 `pymupdf4llm`）。原文件已存，agent 可通过沙箱读原文件（只是看不懂 PDF）。

**Q：`UnsafeUploadPathError`？**
A：上传目标处有 symlink / 目录 / 多硬链接。通常是沙箱进程预置了 symlink（安全告警，非 bug）——`O_NOFOLLOW` 拒绝了跟随。

**Q：`PathTraversalError`？**
A：某处用拼接而非 `normalize_filename` 构造了越界路径——查调用方是否绕过了 normalize。

**Q：转换很慢卡住对话？**
A：文件 > 1MB 会走 `to_thread` 不阻塞循环；若仍慢，考虑关 `auto_convert_documents` 或换更快的转换器。

**Q：PDF 转出来是空的？**
A：图片 PDF，pymupdf4llm 啃不动。确认 markitdown 已装（auto 模式会按每页字符数判定自动回退到它走 OCR）。

**Q：为什么 agent 看到的路径是 `/mnt/user-data/uploads/...` 不是真实路径？**
A：那是**虚拟路径前缀**（[sandbox.md](sandbox.md)）。agent 永远只看到这个固定路径，物理上落在 per-user per-thread 隔离目录——既安全（agent 不知道真实位置）又一致（虚拟路径固定）。

**Q：删了原文怎么还有个 `.md`？**
A：不会。`delete_file_safe` 若扩展名命中 `convertible_extensions` 会顺带删伴随 `.md`（`missing_ok=True`，转换失败没生成也不报错）。

**Q：上传目录里的 symlink 会被当上传文件列出吗？**
A：不会。`list_files_in_dir` 用 `is_file(follow_symlinks=False)` 不跟随 symlink——上传目录里的 symlink 不算合法上传文件。
