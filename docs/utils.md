# 4. utils.md — 公共工具（时间戳 + 消息抽取 + 端口分配 + HTML 可读性）

> 📝 重写于 2026-07-05 · 对照代码 commit `ffc5e5d`（函数 / 行号以此为准）。

> **一句话定位**：utils 是一个「装零碎但人人都要用的小工具」的抽屉——目前装了四类：**时间戳**（`now_iso` / `coerce_iso`）、**消息内容抽取**（`message_content_to_text` / `message_to_text` / `get_original_user_content_text`）、**网络端口分配**（`PortAllocator` / `get_free_port`）、**HTML 可读性提取**（`ReadabilityExtractor` / `Article`）。它们本身不是功能，而是很多业务模块反复用到的**底层零件**，单独拎出来是为了「同一操作只写一份、全项目复用」。

> 配套代码：[utils/time.py](../backend/packages/harness/deerflow/utils/time.py) · [utils/messages.py](../backend/packages/harness/deerflow/utils/messages.py) · [utils/network.py](../backend/packages/harness/deerflow/utils/network.py) · [utils/readability.py](../backend/packages/harness/deerflow/utils/readability.py) · [utils/__init__.py](../backend/packages/harness/deerflow/utils/__init__.py)。

## 学完这篇你能回答什么（learning outcomes）

- 为什么时间戳要统一存 **UTC + ISO 8601**？读历史数据时 `coerce_iso` 怎么兼容遗留的 unix 数字 / 字符串（以及为什么 `bool` 必须在 `int` 之前判断）？
- LangChain 消息的 `content` 有哪三种形态？怎么可靠地抽成纯文本（多模态块怎么跳过）？
- 并发起多个容器 / 服务时，怎么**线程安全地分配端口**不撞车？为什么端口探测要 bind `0.0.0.0` 而不是 `localhost`？
- 抓网页时怎么把 HTML 提取成干净文本？**软加载（soft import）三级降级**（readabilipy → 纯 Python 模式 → regex 兜底）是怎么设计的？

> 这些都是后端工程的基础功——时间归一、消息解析、并发资源分配、优雅降级。

---

## 1. 为什么需要它

utils 里每个工具都解决一类「**很多模块都要做、且容易写错**」的小操作。把实现收口到一处，避免每个模块各写一套、各错一遍。

| 工具 | 解决的痛点 | 谁在用 |
|------|-----------|--------|
| `time.py` | 时间戳「写」要统一格式、「读」要兼容历史脏数据 | persistence / memory / events（→ #7 / #18 / #9） |
| `messages.py` | LangChain 消息 `content` 有三种形态，到处手写判断又长又漏 | memory / journal / title 中间件 / skills（→ #18 / #10 / #24 / #19） |
| `network.py` | 并发分配端口会撞同一个 | AIO 沙箱容器 backend（→ #14） |
| `readability.py` | 抓到的 HTML 要提取成干净文本喂给 agent | community 联网 provider（jina/browserless，→ #21） |

> utils 排在 **Phase 0 地基**——它几乎没有依赖（time/messages 是纯标准库；network 用 socket；readability 软加载），但后面大半模块都依赖它，必须最先就位。

---

## 2. 零基础先读：这些名词是什么

> 这些概念后面所有模块都会反复出现，先在这里建立直觉。

### 2.1 ISO 8601 / UTC / unix 时间戳

- **ISO 8601** 是国际标准化的「日期时间字符串写法」：`2026-04-27T03:19:46+00:00`。日期和时间用 **`T`** 分隔（不是空格），末尾 `+00:00` 是相对 UTC 的偏移。
- **UTC**（协调世界时）是「世界基准时间」（零时区时钟），没有夏令时、不跳变。所有时间**先存 UTC**，用时再转本地时区——服务器搬到任何国家、用户在任何时区，存的数据都一致。
- **unix 时间戳** = 「从 1970-01-01 00:00 UTC 到现在过了多少秒」，纯数字（如 `1745724000.5`），没时区但人类看不懂。旧版曾用 `str(time.time())` 存成字符串 `"1745724000.5"`。

`now_iso()` / `coerce_iso()` 坚持输出 `T` 分隔的 UTC ISO；`coerce_iso` 还负责把历史遗留的 unix 数字 / 字符串翻译回 ISO，不用手动迁移旧数据。

### 2.2 LangChain 消息的三种 `content` 形态

LangChain（mini 依赖的框架）里，一条消息的 `content` 不是固定格式：

```python
content = "你好"                                    # 形态一：纯字符串
content = ["你好", "世界"]                           # 形态二：字符串列表
content = [{"type": "text", "text": "看这张图"},     # 形态三：字典列表（多模态）
           {"type": "image_url", "image_url": {...}}]
```

- 纯文本对话 → 通常是形态一；
- 带图片 / 文件的多模态消息 → 形态三（每个字典是一个「块」）。

抽取文本时要**只挑 `text` 块、跳过 `image_url`** 这类非文本块（记忆 / 标题只要文字）。

### 2.3 `additional_kwargs` 是什么

每条 LangChain 消息除了 `content`，还带一个 `additional_kwargs`（额外参数字典）。mini 的中间件在**改写**用户消息时（往开头注入记忆 / 日期），会把「改写前的原始用户输入」存进 `additional_kwargs["original_user_content"]`，方便后续（如记忆抽取）拿到**用户真正的原话**，而非改写后的版本。

### 2.4 端口分配 / `bind` 探测

- 一台机器有 65535 个**端口**，网络服务各占一个（如 web 服务占 80/8080）。AIO 沙箱给每个容器做 `-p 宿主端口:8080` 映射，要给容器挑一个空闲的宿主端口。
- **`bind` 探测**：程序试着「绑定」一个端口（`socket.bind`），绑得上说明空闲，绑不上（`OSError`）说明被占。
- **并发撞车**：两个线程同时挑端口，可能都以为同一个端口空闲 → 撞车。所以要**锁保护**（线程安全）。

### 2.5 HTML 可读性 / 软加载

- **可读性提取**（readability）：把一坨 HTML 提取出「标题 + 正文」，剥掉导航 / 脚注 / 脚本 / 样式，喂给 agent 看个干净文本。
- **软加载**（soft import）：可选依赖用 `try: import X except ImportError:` 引入，缺包时不崩、降级到次优实现（详见 [build.md §3](build.md#3-核心概念) / [testing-setup.md §5.3 模式 C](testing-setup.md#53-三种可复用的-hermetic-模式)）。readability 的重依赖（`readabilipy` 经 Node 子进程跑 Mozilla Readability.js）就是软加载——缺包时降级到纯 Python 兜底。

---

## 3. 整体结构：它在系统里的位置

utils 是**叶子层**（几乎无依赖），被上层业务模块广泛复用：

```
utils/
├── time.py          now_iso / coerce_iso ─────┐
├── messages.py      message_content_to_text ──┤
│                    message_to_text           │   被几乎所有读写时间 /
│                    get_original_user_content │   读用户消息的业务模块用
├── network.py       PortAllocator / get_free_port ──→ AIO 沙箱容器（→ #14）
└── readability.py   ReadabilityExtractor / Article ──→ community 联网 provider（→ #21）
                                              │
            （time/messages 经 __init__ 重导出；network/readability 按需直接 import）
```

---

## 4. 核心概念（四类工具）

### 4.1 时间：统一 UTC ISO（`time.py`）

- **写**用 `now_iso()`：`datetime.now(UTC).isoformat()` → `"2026-04-27T03:19:46.511479+00:00"`。
- **读**用 `coerce_iso(value)`：不管 value 是什么形态，都吐出 ISO 字符串（详见 §5.1 的分支顺序）。

### 4.2 消息抽取（`messages.py`）

三个函数，按「粒度」递增：

| 函数 | 吃什么 | 干什么 |
|------|--------|--------|
| `message_content_to_text(content)` | 原始 `content` | 三种形态 → 纯文本，列表用**换行**拼，跳过非文本块 |
| `message_to_text(message, ...)` | **整条消息**（BaseMessage 或 dict 形态） | 先取 `content`，列表用**无分隔符**拼，支持嵌套 `{"content":...}` 块；可回退 `message.text` |
| `get_original_user_content_text(content, additional_kwargs)` | content + additional_kwargs | 优先取「中间件介入前的用户原话」，否则回退到 `message_content_to_text` |

> `message_content_to_text`（吃原始 content、换行拼）和 `message_to_text`（吃整条消息、无分隔拼、形态更宽）分工不同——后者是多个调用点各自重写的「整条消息→文本」逻辑的**合并版**。

### 4.3 端口分配（`network.py`）

`PortAllocator` —— 线程安全的端口分配器：锁保护的「已保留端口集合」+ `bind` 探测，分配后标记保留、显式 `release` 才回收。配一个进程级全局实例（`get_free_port` / `release_port`），AIO 沙箱并发起容器时共用，防撞端口。

### 4.4 HTML 可读性（`readability.py`）

`ReadabilityExtractor.extract_article(html)` → `Article`（标题 + 正文 HTML/文本）。`Article` 能 `to_markdown()`（喂 agent 文本）或 `to_message()`（切成多模态 text + image_url 块，喂视觉模型）。提取走**三级软加载降级**（§5.4）。

---

## 5. 代码走读：重要函数逐个讲

### 5.1 `coerce_iso` 的分支顺序（[time.py:34](../backend/packages/harness/deerflow/utils/time.py#L34)）

```python
def coerce_iso(value: object) -> str:
    if value is None or value == "":          # ① 空值 → ""
        return ""
    if isinstance(value, bool):               # ② bool 先于 int！
        return str(value)                     #    否则 True 会被当 unix 时间戳 1 → 1970 年
    if isinstance(value, datetime):           # ③ datetime 先于 int
        ...                                   #    归一到 UTC 后 isoformat()
    if isinstance(value, (int, float)):       # ④ unix 数字 → ISO
        return datetime.fromtimestamp(float(value), UTC).isoformat()
    if isinstance(value, str):
        if _UNIX_TIMESTAMP_PATTERN.match(value):   # ⑤ "1745724000" 这种 10 位串 → ISO
            return datetime.fromtimestamp(float(value), UTC).isoformat()
        return value                          # ⑥ 已是 ISO → 原样
    return str(value)                         # ⑦ 兜底
```

**两个关键坑**（顺序是故意的）：

1. **bool 先于 int**：Python 里 `isinstance(True, int)` 是 `True`。若不先拦，`True` 会被当 unix 时间戳 `1` 翻译成 `1970-01-01 00:00:01`。
2. **datetime 先于 int**：顺序写反会让 `str(datetime)`（空格分隔）混进流程，破坏 `T` 分隔的线格式。

`_UNIX_TIMESTAMP_PATTERN = re.compile(r"^\d{10}(?:\.\d+)?$")`（[第 20 行](../backend/packages/harness/deerflow/utils/time.py#L20)）—— 用 **10 位**做锚点识别 unix 秒时间戳。为什么不用「全数字就转」？因为年份串 `"2026"` 只有 4 位，当时间戳会翻译成 1970 年附近。10 位安全到 2286 年。

### 5.2 消息抽取三个函数（`messages.py`）

`message_content_to_text`（[第 16 行](../backend/packages/harness/deerflow/utils/messages.py#L16)）：str 直接返回；list 逐项取 str 或 dict 的 `"text"` 字段，**换行拼接**，跳过非文本块；兜底 `str(content)`。

`message_to_text`（[第 39 行](../backend/packages/harness/deerflow/utils/messages.py#L39)）：先从 `message`（`BaseMessage` 走属性、dict 走键）取 `content`；列表用**无分隔符** `"".join` 拼接，还认嵌套 `{"content": ...}` 块；抽不出时可 `text_attribute_fallback=True` 回退 `message.text`。**注意它不在 `__init__.py` 重导出**——调用方直接 `from deerflow.utils.messages import message_to_text`。

`get_original_user_content_text`（[第 80 行](../backend/packages/harness/deerflow/utils/messages.py#L80)）：优先返 `additional_kwargs["original_user_content"]`（若是 str），否则回退 `message_content_to_text(content)`。

### 5.3 `PortAllocator`（[network.py:22](../backend/packages/harness/deerflow/utils/network.py#L22)）

```python
class PortAllocator:
    def _is_port_available(self, port: int) -> bool:
        if port in self._reserved_ports:              # 先查保留集合
            return False
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("0.0.0.0", port))             # bind 0.0.0.0（镜像 Docker 的 wildcard）
                return True
            except OSError:
                return False

    def allocate(self, start_port=8080, max_range=100) -> int:
        with self._lock:                              # 锁保护，防并发撞车
            for port in range(start_port, start_port + max_range):
                if self._is_port_available(port):
                    self._reserved_ports.add(port)    # 标记保留
                    return port
            raise RuntimeError("No available port ...")
```

进程级全局实例 + 便捷函数：`get_free_port()` / `release_port()`（[第 91–101 行](../backend/packages/harness/deerflow/utils/network.py#L91)）；还有 `allocate_context` 上下文管理器（退出自动 release）。

### 5.4 `ReadabilityExtractor.extract_article` 的三级降级（[readability.py:146](../backend/packages/harness/deerflow/utils/readability.py#L146)）

```python
def extract_article(self, html: str) -> Article:
    try:
        from readabilipy import simple_json_from_html_string
        article = simple_json_from_html_string(html, use_readability=True)   # ① 最优：Readability.js（经 Node 子进程）
    except ImportError:                                                       # ② 缺包 → 纯 Python regex 兜底
        ...return Article(..., _fallback_extract(html))
    except (subprocess.CalledProcessError, FileNotFoundError):               # ③ Node/Readability.js 失败 → readabilipy 纯 Python 模式
        article = simple_json_from_html_string(html, use_readability=False)
        ...                                                                   #    再失败才落 _fallback_extract
    except Exception:                                                         # ④ 其它错误 → regex 兜底
        ...
```

降级链：**Readability.js（最优）→ readabilipy 纯 Python 模式（次优）→ 本模块 regex `_fallback_extract`（兜底）**。`_fallback_extract`（[第 52 行](../backend/packages/harness/deerflow/utils/readability.py#L52)）剥 `<script>/<style>/<nav>/...` + 抽 `<title>` + 折叠空白，质量不如 Readability.js 但**零依赖、可 hermetic 测试**，保证缺包时仍能产出可读文本。

`Article.to_markdown`（[第 86 行](../backend/packages/harness/deerflow/utils/readability.py#L86)）也软加载 `markdownify`（缺则用正文文本）；`Article.to_message`（[第 109 行](../backend/packages/harness/deerflow/utils/readability.py#L109)）把 markdown 按 `![alt](url)` 切成 text + image_url 交替的多模态块（用 `urljoin` 拼绝对 URL）。

---

## 6. 设计权衡与踩坑

### 6.1 为什么不全用 `str()` 一把梭存时间？

一把梭会产生 `"2026-04-27 03:19:46"`（空格分隔）或把 `True` 变时间戳，都是隐蔽 bug。`coerce_iso` 的分支顺序（bool 先于 int、datetime 先于 int）就是专门防这些（§5.1）。

### 6.2 unix 字符串为什么用「10 位」做锚点？

见 §5.1——避免把 `"2026"` 这类年份当时间戳翻译成 1970 年。10 位安全到 2286 年。

### 6.3 端口探测为什么 bind `0.0.0.0` 而不是 `localhost`？

Docker 也绑 `0.0.0.0:PORT`（wildcard）。只查 `127.0.0.1`（loopback）可能在 Docker 已占 wildcard 时**误报端口空闲**，导致 `docker run -p` 报「port is already allocated」。镜像 Docker 的绑定行为才能准确探测（[network.py 顶部注释](../backend/packages/harness/deerflow/utils/network.py#L10)）。

### 6.4 readability 为什么软加载 + 三级降级？

`readabilipy`（经 Node 跑 Mozilla Readability.js）提取质量最好，但**重依赖**（要装 Node、子进程调用）。mini 把它当可选：缺包时降级到纯 Python，保证「联网 provider 在没装重依赖时仍能跑」。这是和 [build.md](build.md) extras + 软加载策略一致的设计——核心功能不依赖可选重包。

---

## 7. 应用方法

### 7.1 时间戳

```python
from deerflow.utils import now_iso, coerce_iso

created_at = now_iso()                              # 写：存进 DB / JSON
coerce_iso("2026-04-27T03:19:46+00:00")             # '...'（原样）
coerce_iso(1745724000)                               # → ISO（历史数字时间戳）
coerce_iso("1745724000")                             # → ISO（历史字符串时间戳）
coerce_iso(None)                                     # ''（空值）
coerce_iso(True)                                     # 'True'（不会变成 1970 年！）
```

### 7.2 抽取消息文本

```python
from deerflow.utils import message_content_to_text, get_original_user_content_text

message_content_to_text("你好")                      # '你好'
message_content_to_text([{"type": "text", "text": "看这张图"},
                         {"type": "image_url", "image_url": {}}])   # '看张图'（跳过图片块）

# 取中间件介入前的用户原话（记忆抽取用）
get_original_user_content_text(
    "<memory>...</memory> 我是张三",                  # 改写后的 content
    {"original_user_content": "我是张三"},            # 原话存在 additional_kwargs
)   # '我是张三'
```

### 7.3 分配端口（AIO 沙箱用）

```python
from deerflow.utils.network import get_free_port, release_port

port = get_free_port(start_port=8080)               # 线程安全挑一个空闲端口
try:
    ...  # docker run -p {port}:8080 ...
finally:
    release_port(port)                               # 用完释放

# 或上下文管理器（推荐，退出自动释放）：
from deerflow.utils.network import PortAllocator
with PortAllocator().allocate_context(8080) as port:
    ...
```

### 7.4 HTML → 干净文本（community 联网 provider 用）

```python
from deerflow.utils.readability import ReadabilityExtractor

article = ReadabilityExtractor().extract_article("<html>...</html>")
md = article.to_markdown()                           # 标题 + 正文 markdown，喂给 agent（再 4KB 截断）
```

---

## 8. 与其它模块的关系

```
utils/time.py      ──→ persistence（→ #7）存/读 run、thread 时间戳
                   ──→ memory（→ #18）记录事实的 createdAt
                   ──→ events / journal（→ #9 / #10）事件时间戳

utils/messages.py  ──→ memory（→ #18）抽取「用户说了什么」做记忆
                   ──→ journal（→ #10）从首条 human 消息抽取
                   ──→ title 中间件（→ #24）读首条消息生成标题
                   ──→ skills（→ #19）/skill 激活时取用户文本

utils/network.py   ──→ AIO 沙箱 LocalContainerBackend（→ #14）并发分配容器端口

utils/readability.py ──→ community 联网 provider（jina/browserless/infoquest，→ #21）抓网页后提取正文
```

- **依赖**：time / messages 是纯标准库（最底层叶子）；network 用 `socket`；readability 软加载 `readabilipy` / `markdownify`。
- **被依赖**：几乎所有读写时间、读用户消息的业务模块 + AIO 沙箱 + 联网 provider。

---

## 9. 常见问题 / 排错

**Q: 存进去的时间读回来变成 `"None"` 字符串？**
你是不是直接 `str(some_datetime_or_none)` 了？空值要给 `coerce_iso(None)` → `""`，而不是 `str()` 出 `"None"`。

**Q: 时间突然全变成 1970 年？**
多半是 `coerce_iso` 拿到了 `bool` 或很小的整数。`coerce_iso` 已对 `bool` 做保护（§5.1），但如果你在调它**之前**自己 `int(x)` 了一道，`True` 就变 `1` 进了时间戳分支——别自己多做转换，直接把原值交给 `coerce_iso`。

**Q: 消息文本抽出来是空的？**
检查 `content` 是不是形态三但所有块都不是 `text` 类型（如全是图片块）。`message_content_to_text` 只认 `text` 块，纯图片消息抽出空串是**预期行为**。

**Q: `get_original_user_content_text` 返回的是改写后的内容？**
说明这条消息的 `additional_kwargs` 没有 `original_user_content`（没经过注入中间件，或中间件没设这键）。它回退到当前 `content`——设计内兜底，不是 bug。

**Q: `docker run -p` 报「port is already allocated」，但 `get_free_port` 说端口空闲？**
`PortAllocator._is_port_available` 已经 bind `0.0.0.0`（不是 loopback）来镜像 Docker 的 wildcard 绑定（§6.3）。若仍撞，检查是不是有**另一个进程**也绕过了这个全局分配器直接占端口——所有端口分配都应走 `get_free_port` 共用同一个全局实例。

**Q: 联网抓取时正文质量很差 / 只有零散文本？**
多半是 `readabilipy` 没装，走了纯 Python regex 兜底（§5.4 ③/④）。装上高质量依赖：`uv sync --extra community`（或单独装 `readabilipy` + `markdownify`），自动走 Readability.js 路径。

---

## 小结

utils 的精髓是「**把会反复用到、且容易写错的小操作，收口成一份正确实现**」。记四件事：

1. **时间统一 UTC ISO**：写用 `now_iso`，读用 `coerce_iso`（自动兼容历史 unix 时间戳与空值；bool/datetime 顺序是故意防坑的）。
2. **消息抽取**：`message_content_to_text`（原始 content、换行拼）/ `message_to_text`（整条消息、无分隔拼）/ `get_original_user_content_text`（用户原话）。
3. **端口分配**：`PortAllocator` 线程安全 + bind `0.0.0.0` 镜像 Docker；走全局 `get_free_port`。
4. **HTML 可读性**：`ReadabilityExtractor` 三级软加载降级（Readability.js → 纯 Python 模式 → regex 兜底），缺重依赖也能跑。

> 下一步：[#5 user_context.md](user_context.md)（用户隔离基石），Phase 0 地基收尾。
