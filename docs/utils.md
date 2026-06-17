# 4. utils.md — 公共工具（time + messages）

> 对应模块：**M1**（Phase 0，地基）
> 源码：`backend/packages/harness/deerflow/utils/time.py`、`utils/messages.py`、`utils/__init__.py`

---

## 1. 一句话定位

**utils 是一个「装零碎但人人都要用的小工具」的抽屉**——目前装了两类：**时间戳**（`now_iso` / `coerce_iso`）和**消息内容抽取**（`get_original_user_content_text` / `message_content_to_text`）。

它们本身不是功能，而是**很多业务模块要反复用到的底层零件**（persistence 存时间、memory 抽取用户原话、agent 拼标题都靠它们）。把它单独拎出来，是为了「同一个操作只写一份、全项目复用」，避免每个模块各写一套、各错一遍。

> 这份文档虽然是「最小模块」（只有 4 个函数），但会借机把几个**贯穿后续所有模块**的基础概念讲清楚：ISO 8601 / UTC / unix 时间戳是什么、LangChain 消息的 `content` 长什么样。新手建议从这里开始读。

---

## 2. 为什么需要它

### 2.1 时间：为什么不能随便 `str(datetime.now())`？

DeerFlow 会在数据库里、JSON 文件里、API 返回里**到处存时间**（线程创建时间、run 开始时间、事实记录时间……）。如果每个地方存法不一样，后患无穷：

| 场景 | 各写各的（坏） | 走 utils（好） |
|------|----------|--------|
| 存「现在」 | 有人存 `str(datetime.now())`（本地时区、空格分隔），有人存 `time.time()` | 统一 `now_iso()` → UTC ISO 字符串 |
| 从旧数据读回时间 | 旧版本存的 unix 数字读不回来，得手动判断类型 | `coerce_iso()` 自动识别并归一 |
| 跨时区 | 服务器在 UTC，但写代码的人本地时区不同，时间对不上 | 全程 UTC，读出来再按需转换 |

一句话：**时间戳「写」要统一格式，「读」要能兼容历史脏数据**——utils 把这两件事收口。

### 2.2 消息内容：为什么抽取文本这么麻烦？

LangChain（DeerFlow 依赖的框架）里，一条消息的「内容」字段 `content` **不是固定格式**，有三种长相：

```python
content = "你好"                                    # 形态一：纯字符串
content = ["你好", "世界"]                           # 形态二：字符串列表
content = [{"type": "text", "text": "看这张图"},     # 形态三：字典列表（多模态）
           {"type": "image_url", "image_url": {...}}]
```

很多模块（比如 memory 抽取用户说的话做记忆、title 生成读首条消息）都需要「**把这条消息的纯文本拿出来**」。如果到处手写 `if isinstance(content, str)... else...` 判断，又长又容易漏。

`message_content_to_text` 就是把这个判断写一次、到处用。

---

## 3. 核心概念（先把名词讲明白）

> 这些概念后面所有模块都会反复出现，先在这里建立直觉。

### 3.1 什么是 ISO 8601？

**ISO 8601** 是国际标准化的「日期时间字符串写法」，长这样：

```
2026-04-27T03:19:46.511479+00:00
└─日期─┘ T └──── 时间 ────┘ └偏移┘
```

- 日期和时间之间用 **`T`** 分隔（不是空格！）；
- 末尾 **`+00:00`** 是「相对 UTC 的偏移量」——`+00:00` 就表示「这就是 UTC 时间」。

为什么要 `T` 而不是空格？因为有些系统解析 `2026-04-27 03:19:46`（空格分隔）会出错，ISO 标准用 `T` 避免歧义。这也是 `now_iso()` 和 `coerce_iso()` 都坚持输出 `T` 的原因。

### 3.2 什么是 UTC？

**UTC**（协调世界时）是「世界基准时间」，相当于「零时区的时钟」。它没有夏令时、不会跳变。所有时间**先存成 UTC**，要用时再转成本地时区显示。这样服务器搬到任何国家、用户在任何时区，存的数据都一致、可比较。

`datetime.now(UTC)` 得到的就是带 UTC 时区的「当前时刻」。

### 3.3 什么是 unix 时间戳？

**unix 时间戳** = 「从 1970 年 1 月 1 日 0 点（UTC）到现在，过去了多少秒」，是一个纯数字，如 `1745724000.5`。

- 它**没有时区**（永远是 UTC 基准），所以跨时区不会乱；
- 但它**人类看不懂**，而且旧版 DeerFlow 曾用 `str(time.time())` 把它存成字符串 `"1745724000.5"`。

`coerce_iso` 的一个重要职责就是：**遇到这些历史遗留的 unix 字符串，自动翻译回 ISO**，不用你手动迁移旧数据。

### 3.4 LangChain 消息的三种 `content` 形态

见 §2.2 的三个例子。记住：
- **纯文本对话** → 通常是形态一（字符串）；
- **模型返回多块文本** → 可能是形态二（列表）；
- **带图片/文件的多模态消息** → 形态三（字典列表，每个字典是一个「块」）。

`message_content_to_text` 只抽 `text` 块，**跳过** `image_url` 这类非文本块（因为记忆/标题只要文字）。

### 3.5 什么是 `additional_kwargs`？

每条 LangChain 消息除了 `content`，还带一个 `additional_kwargs`（额外参数字典）。DeerFlow 的中间件在**改写**用户消息时（比如往开头注入记忆、日期），会把「改写前的原始用户输入」存进 `additional_kwargs["original_user_content"]`，方便后续（如记忆抽取）能拿到**用户真正的原话**，而不是被改写后的版本。

---

## 4. 设计原理（讲清楚每个「为什么」）

### 4.1 `coerce_iso` 为什么这么多分支？

`coerce_iso(value)` 的任务是「不管 `value` 是什么形态，都吐出一个 ISO 字符串」。它按顺序判断：

| 输入形态 | 处理 | 为什么这样 |
|----------|------|-----------|
| `None` / `""` | 返回 `""` | 空就是空，不要变成 `"None"` 字符串 |
| `bool`（`True`/`False`） | `str(value)` | ⚠️ **bool 是 int 的子类**，若不先拦，`True` 会被当成 unix 时间戳 `1` 翻译成 1970 年！ |
| `datetime` 对象 | 归一到 UTC 后 `isoformat()` | 无时区视为 UTC；**必须在 int/float 之前判断** |
| `int` / `float` | 当 unix 时间戳转 ISO | 历史遗留的数字时间戳 |
| `"1745724000"` 这种 10 位数字串 | 当 unix 时间戳转 ISO | 历史遗留 `str(time.time())` |
| 其它字符串 | 原样返回 | 已经是 ISO 的直接用 |
| 其它类型（对象等） | 兜底 `str(value)` | 实在认不出就转字符串，不崩溃 |

两个关键「坑」点：
1. **bool 先于 int**：Python 里 `isinstance(True, int)` 是 `True`，所以 `True` 必须在 int 分支**之前**被拦下，否则 `True==1` 会被翻译成「1970-01-01 00:00:01」。
2. **datetime 先于 int**：`datetime` 不是 int，但顺序写反会让 `str(datetime)`（空格分隔）混进流程，破坏 `T` 分隔的线格式。

> 这正是「为什么不全用 `str()` 一把梭」——一把梭会产生 `"2026-04-27 03:19:46"`（空格分隔）或把 `True` 变时间戳，都是隐蔽 bug。

### 4.2 unix 字符串为什么用「10 位」做锚点？

历史遗留的 unix 秒时间戳是 **10 位数字**（如 `1745724000`）。`coerce_iso` 用正则 `^\d{10}(?:\.\d+)?$` 判断「这是不是个 unix 时间戳字符串」。

- **为什么不用「全数字就转」**？因为年份字符串 `"2026"` 只有 4 位，如果一律当时间戳会被翻译成 1970 年 1 月 1 日附近——完全乱套。
- **10 位安全到 2286 年**（那时 unix 秒才变 11 位），足够用。

### 4.3 消息抽取为什么「跳过非文本块」？

形态三的字典列表里，可能有 `{"type": "image_url", ...}`（图片块）。如果你要做「**统计这条消息的字数**」或「**把用户说的话喂给记忆模型**」，图片块没文字、塞进去只会捣乱。所以 `message_content_to_text` **只挑 `text` 字段**，非文本块静默跳过。

### 4.4 `original_user_content` 为什么是「字符串才认」？

中间件注入记忆后，`additional_kwargs["original_user_content"]` 理论上应该是个字符串（原话）。但为了健壮，`get_original_user_content_text` 只有在它是**字符串**时才用；如果是 `None`、字典、或压根没这个键，就回退到从当前 `content` 抽文本。这样即使某条消息没经过注入中间件、或字段格式异常，也不会崩。

---

## 5. 文件结构

```
utils/
├── __init__.py     # 导出全部 4 个公开函数 + ORIGINAL_USER_CONTENT_KEY 常量
├── time.py         # now_iso / coerce_iso（时间戳）
└── messages.py     # message_content_to_text / get_original_user_content_text（消息抽取）
```

`utils/__init__.py` 把子模块的符号提到顶层，所以业务代码直接 `from deerflow.utils import now_iso, coerce_iso` 即可，不用关心文件在 `time.py` 还是 `messages.py`。

---

## 6. 关键接口 / 签名

### 时间戳（`utils/time.py`）

```python
def now_iso() -> str:
    """当前 UTC 时间的 ISO 8601 字符串，如 '2026-04-27T03:19:46.511479+00:00'。"""

def coerce_iso(value: object) -> str:
    """把任意形态的时间值归一成 ISO 8601 字符串（空值→''，历史 unix 数字/串→ISO，
    datetime→UTC ISO，已是 ISO→原样，无法识别→str()）。"""
```

### 消息抽取（`utils/messages.py`）

```python
ORIGINAL_USER_CONTENT_KEY = "original_user_content"
# additional_kwargs 里存「原始用户输入」的键名。

def message_content_to_text(content: Any) -> str:
    """从消息 content 的三种形态（str / str 列表 / dict 列表）抽出纯文本，跳过非文本块。"""

def get_original_user_content_text(
    content: Any,
    additional_kwargs: Mapping[str, Any] | None,
) -> str:
    """优先返回 additional_kwargs['original_user_content']（若是 str），
    否则回退到 message_content_to_text(content)。"""
```

---

## 7. 应用方法

### 7.1 生成「现在」的时间戳

```python
from deerflow.utils import now_iso

created_at = now_iso()   # 存进数据库 / JSON / API 返回
```

### 7.2 读回历史时间（兼容脏数据）

```python
from deerflow.utils import coerce_iso

coerce_iso("2026-04-27T03:19:46+00:00")   # '2026-04-27T03:19:46+00:00'（原样）
coerce_iso(1745724000)                       # → ISO（历史数字时间戳）
coerce_iso("1745724000")                     # → ISO（历史字符串时间戳）
coerce_iso(None)                             # ''（空值）
coerce_iso(True)                             # 'True'（不会变成 1970 年！）
```

### 7.3 抽取消息文本

```python
from deerflow.utils import message_content_to_text

message_content_to_text("你好")                              # '你好'
message_content_to_text([{"type": "text", "text": "看这张图"},
                          {"type": "image_url", "image_url": {}}])  # '看这张图'（跳过图片块）
```

### 7.4 取「中间件介入前的用户原话」（记忆抽取用）

```python
from deerflow.utils import get_original_user_content_text

# 假设 DynamicContext 把用户消息从 "我是张三" 改写成 "<memory>...</memory> 我是张三"，
# 并把原话存进 additional_kwargs：
get_original_user_content_text(
    "<memory>...</memory> 我是张三",
    {"original_user_content": "我是张三"},
)   # '我是张三'（拿到原话，而非改写版）
```

---

## 8. 与其它模块的关系

```
utils/time.py     ──┐
                    ├─→ persistence (M4)：存/读 run、thread 的时间戳
                    ├─→ memory (M13)：记录事实的 createdAt
                    └─→ events/journal (M6/M7)：事件时间戳

utils/messages.py ──┐
                    ├─→ memory (M13)：抽取「用户说了什么」做记忆
                    ├─→ journal (M7)：从首条 human 消息抽取
                    ├─→ title 中间件 (M16)：读首条消息生成标题
                    └─→ skills (M14)：/skill 激活时取用户文本
```

- **依赖**：无（最底层叶子模块，纯标准库）。
- **被依赖**：几乎所有读写时间、读用户消息的业务模块（persistence / memory / journal / title / skills）。

> 这就是为什么 utils 排在 **Phase 0 地基**——它没有依赖，但后面大半模块都依赖它，必须最先就位。

---

## 9. 常见问题 / 排错

### Q1：存进去的时间读回来变成了 `None` 字符串 `"None"`？

你是不是直接 `str(some_datetime_or_none)` 了？空值要给 `coerce_iso(None)` → 返回 `""`，而不是 `str()` 出 `"None"`。

### Q2：时间突然全变成 1970 年？

十有八九是 `coerce_iso` 拿到了 `bool`（`True`/`False`）或一个很小的整数。检查是不是把某个布尔字段/计数器当时间传进来了。`coerce_iso` 已对 `bool` 做了保护（返回 `str(value)`），但如果在调用 `coerce_iso` **之前**就 `int(x)` 了一道，`True` 就变成 `1` 进了时间戳分支——别自己多做转换，直接把原值交给 `coerce_iso`。

### Q3：消息文本抽出来是空的？

检查 `content` 是不是形态三但所有块都不是 `text` 类型（比如全是图片块）。`message_content_to_text` 只认 `text` 块，纯图片消息抽出空字符串是**预期行为**。

### Q4：`get_original_user_content_text` 返回的是改写后的内容？

说明这条消息的 `additional_kwargs` 里没有 `original_user_content`（可能没经过注入中间件，或中间件没设这个键）。它会回退到当前 `content`——这是设计内的兜底，不是 bug。

---

## 小结

utils 的精髓是「**把会反复用到、且容易写错的小操作，收口成一份正确实现**」。记住三件事：

1. **时间统一走 UTC ISO**：写用 `now_iso`，读用 `coerce_iso`（自动兼容历史 unix 时间戳与空值）。
2. **消息抽取只取文本**：`message_content_to_text` 处理三种形态、跳过非文本块；要用户原话用 `get_original_user_content_text`。
3. **bool/datetime 的坑**：`coerce_iso` 分支顺序是故意设计的（bool 先于 int、datetime 先于 int），别用 `str()` 一把梭。

下一个要读的文档：`docs/user_context.md`（用户隔离基石）→ `docs/config.md`（配置类型化）。
