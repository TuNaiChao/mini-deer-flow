# 21. community.md — 联网能力（搜索 / 抓取 provider 框架）

> **M21 六维重审（2026-06-28）**：12 个 provider 逐个 diff 最新上游（aio_sandbox/brave/browserless/
> ddg_search/exa/firecrawl/image_search/infoquest/jina_ai/searxng/serper/tavily），剥 docstring 后核对。
> 结论：**所有 fix mini 均已含**——**#3423** DDG Wikipedia region 推断（7 个 Unicode 码点范围 → jp-ja/kr-ko/
> cn-zh/ru-ru/gr-el/il-he/xa-ar，**已含**）/ **#3418** jina reader proxy 支持（`coerce_proxy` + `crawl(proxy=...,
> trust_env=...)`，**已含**）/ 各 provider 入参强转与 fail-closed（搜索/抓取失败返空不拖垮 agent）。
> 差异**全是 mini 的重构 + 文档措辞**：mini 把共享 helper 抽进 [`_common.py`](../backend/packages/harness/deerflow/community/_common.py)
> （`get_tool_extras`——返 `{}` 而非 None、调用方免判空；`coerce_bool`/`coerce_timeout`/`coerce_proxy`/
> `normalize_search_result`/`truncate_content`），上游等价逻辑内联在各 provider 里；mini 用 `%-format` 日志、
> 中文注释。**defer（3 个 additive 特性，非 fix）**：**#3585 fastCRW**（88 行，`firecrawl` 库的薄封装变体）、
> **#3675 GroundRoute**（166 行，纯 `httpx`、最易补）、**#3575 Serper Google Images**（`image_search` 经 Serper，
> 含其专属 SSRF 守卫 `_safe_public_url` 过滤返回的图片 URL）——mini 已有 9 个搜索 provider + DDG image_search，
> 这三个是可选新增变体，归后续按需补（§3.1-A「视需求，低」）。properly 接线需 config.example + 文档 + 测试，
> 不零碎半挂。

> **一句话定位**：本模块给 agent 装「眼睛和手」——能**搜网页**（web_search）找最新信息、能**抓网页**
> （web_fetch）读某个 URL 的内容。这是 agent「知道训练数据之外的事」的核心来源（训练数据有截止日期，
> 联网让它能答「今天天气」「最新论文」这类问题）。

读完 [mcp.md](mcp.md)（懂了「外部工具协议」）再看本篇最省事——MCP 是「**调别人写好的工具服务器**」，
community 是「**直接调搜索引擎 / 抓网页的 API**」。两者都扩展 agent 能力，但 community 专门管「联网」。

---

## 为什么需要联网（训练数据的局限）

LLM 的知识来自训练数据，有**截止日期**（cutoff）。问它「2024 年诺贝尔物理学奖得主」，若训练数据
截止在 2023，它要么瞎编要么说不知道。联网工具让 agent 在**回答前**先去网上查，拿到最新事实再组织答案。

deer-flow 把联网能力拆成两类工具，community 模块就是它们的实现集合：

| 工具 | 干什么 | 类比 |
|------|--------|------|
| `web_search` | 输入关键词，返回一堆网页结果（标题/链接/摘要） | 你在 Google 搜索框敲词 |
| `web_fetch` | 输入一个 URL，返回那页的正文内容（markdown） | 你点开一个搜索结果读全文 |
| `image_search` | 输入关键词，返回一堆图片 URL | Google 图片搜索 |

注意 `web_search` 和 `web_fetch` 是**两步**：先 search 找到候选 URL，再 fetch 读具体那页。
agent 自己决定要不要 fetch（看摘要够不够）。

---

## provider 框架（为什么有 12 个）

「搜网页」背后是真正的搜索引擎。市面上能用的搜索引擎 API 很多，各有取舍：

| provider | 类型 | 要 key？ | 特点 |
|----------|------|----------|------|
| **ddg_search** | web_search | ❌ 不用 | DuckDuckGo，**免费开箱即用**，教学首选 |
| **tavily** | search + fetch | ✅ 要 | 专为 AI 设计，结果质量高，自带 fetch |
| **jina_ai** | web_fetch | 可选 | Jina Reader，把网页转成干净 markdown，免费额度 |
| image_search | image_search | ❌ 不用 | DuckDuckGo 图搜 |
| brave | web_search | ✅ 要 | 独立搜索索引，REST API |
| serper | web_search | ✅ 要 | 经 Serper 调 Google |
| searxng | web_search | 自托管 | 聚合多个引擎，自己部署 |
| browserless | web_fetch | 自托管 | headless Chrome 渲染（抓 JS 页面） |
| firecrawl | search + fetch | ✅ 要 | 强力抓取 |
| exa | search + fetch | ✅ 要 | 语义搜索 |
| infoquest | 全能 | ✅ 要 | BytePlus，search+fetch+图搜 |

**为什么这么多？** 因为没有「唯一最好的搜索引擎」——免费的 ddg 限流、要钱的 tavily 质量好、
自托管的 searxng 隐私强。mini **全量对齐** deer 的 12 个 provider，但你**不需要全装**——按需在
config.yaml 里启用一两个就够（下面讲怎么选）。

mini 按落地深度分三档：

- **核心 3 个（完整实现）**：`ddg_search`（免费，教学默认）、`tavily`（生产推荐）、`jina_ai`（抓取）。
- **全量移植 5 个**（httpx/ddgs 驱动）：`image_search`、`brave`、`serper`、`searxng`、`browserless`。
- **软加载占位 3 个**（SDK 缺包返可操作错误）：`firecrawl`、`exa`、`infoquest`。

---

## 怎么用（config.yaml 的 `tools[].use:` 加载机制）

community 工具**不**像 MCP 那样自动发现——它们是**配置驱动**的：你在 `config.yaml` 的 `tools[]`
里写一行，agent 才有这个工具。加载靠 [reflection](reflection)（动态 import）：

```yaml
tools:
  - name: web_search                    # 工具名（get_tool_config 按它查配置）
    group: search
    use: "deerflow.community.ddg_search.tools:web_search_tool"   # 模块路径:变量名
    max_results: 5                       # ↓ 这些是「额外字段」，传给 provider 当参数
    region: wt-wt
    safesearch: moderate
  - name: web_fetch
    group: search
    use: "deerflow.community.jina_ai.tools:web_fetch_tool"
    timeout: 10
```

工作流（M15 `get_available_tools` 落地后自动生效）：

```
config.yaml 的 tools[]
   │  for cfg in config.tools:
   ▼
resolve_variable("deerflow.community.ddg_search.tools:web_search_tool", BaseTool)
   │   # = import 那个模块 + 取出 web_search_tool 变量（@tool 装饰过的 BaseTool）
   ▼
tools 列表里多一个 web_search 工具 → agent 能调它了
   │  调用时 provider 读 get_tool_extras("web_search") 拿 max_results/region/...
   ▼
provider 调搜索引擎 API → 归一成 {title,url,snippet} → 返 JSON 给 agent
```

**关键设计**：`use:` 路径指向 `模块:变量`。`resolve_variable` import 模块、取出变量。所以**换 provider
只需改 `use:` 一行**（比如 ddg 换 tavily），agent 代码零改动——这就是 provider 框架的价值（可插拔）。

### 选哪个 provider？

- **学习/开发**：`ddg_search`（免费，不用申请 key）。
- **生产搜索**：`tavily`（质量好，有 `max_results` 配置）。
- **抓取网页**：`jina_ai`（免费额度，把网页转干净 markdown）。
- **要 Google 结果**：`serper`（经 Serper 调 Google，要 key）。
- **自己掌控**：`searxng`（自托管聚合）+ `browserless`（自托管渲染）。

---

## 共享层 `_common.py`（为什么抽出它）

deer 把「归一结果」「4KB 截断」「读配置」这些重复逻辑散在每个 provider 里。mini 抽到
`community/_common.py`（outline 明确要求），12 个 provider 共用，减少重复：

| 函数 | 作用 |
|------|------|
| `normalize_search_result(title, url, snippet)` | 把一条原始结果归一成 `{title,url,snippet/content}` dict（不同 provider 字段名五花八门，归一后统一） |
| `truncate_content(text, limit=4096)` | 抓取内容截到 4KB（防 prompt 爆炸——网页正文可能几万字） |
| `coerce_bool` / `coerce_int` / `coerce_timeout` / `coerce_proxy` | config.yaml 里的值可能是字符串（`timeout: "10"`），安全强转 |
| `get_tool_extras(name)` | 读 `config.yaml` 的 `tools[].name == name` 条目，返回其额外字段 dict |
| `post_json(url, ...)` | async httpx 封装：POST JSON，失败归一成 `"Error: ..."` 字符串（jina 复用） |

**结果归一化**是核心：ddg 返回 `href`/`body`，tavily 返回 `url`/`content`，brave 返回 `url`/`description`……
归一后 agent 看到的永远是 `{title, url, snippet}`，换 provider 不影响 agent 解析。

### 4KB 截断为什么是 4096

网页正文动辄上万字，全塞进 LLM 上下文会**挤掉别的消息**（甚至超 token 上限）。4KB（约 1000 token）
够 agent 判断「这页讲什么」，要更多 agent 会自己再调 `web_fetch` 或分块。deer 各 fetch 工具都用 4096，
mini 在 `_common.MAX_FETCH_CHARS` 单源定义。

---

## 可读性提取 `utils/readability.py`（抓取的核心）

`web_fetch` 不是返原始 HTML（满是 `<div>` `<script>` 的乱码），而是**提取正文**转 markdown。
这靠 `ReadabilityExtractor`（对齐 deer `utils/readability.py`）：

```
原始 HTML（含导航/广告/script）
   │  ReadabilityExtractor.extract_article(html)
   ▼
Article(title, html_content)   # 只剩标题 + 正文 HTML
   │  .to_markdown()
   ▼
# 标题\n\n正文 markdown...   → 4KB 截断 → 返给 agent
```

**软加载**（红线 #24）：优先用 `readabilipy`（包 Mozilla Readability.js，质量最好，但要装 Node）+
`markdownify`（HTML→markdown）。两个都缺时，mini 走**纯 Python 兜底**：剥 `<script>`/`<style>`/`<nav>`
等噪音标签 + 抽 `<title>` + 去标签 + 折叠空白。质量不如 Readability.js，但**零依赖**，保证没装重包时
jina/browserless 仍能产出可读文本。装上 `readabilipy` + `markdownify` 后自动走高质量路径。

---

## 核心数据流（以 ddg_search 为例）

```
agent 调 web_search(query="量子计算 最新进展")
   │
   ▼
web_search_tool(query, max_results=5)
   │  extras = get_tool_extras("web_search")  # 读 config 的 region/safesearch/backend
   │  effective_region = _resolve_ddgs_region(query, region, backend)  # CJK 推断（见下）
   ▼
_search_text(query, max_results, region, safesearch, backend)
   │  from ddgs import DDGS   # ← 软加载：缺包返 []
   │  ddgs.text(query, region=, safesearch=, max_results=, backend=)
   ▼
[{"title":..., "href":..., "body":...}, ...]   # ddgs 原始结果
   │  normalize_search_result(title, url=href, snippet=body)
   ▼
{"query":..., "total_results":N, "results":[{title,url,content},...]}   # JSON 字符串给 agent
```

---

## CJK region 自动推断（ddg_search 的精巧之处）

DuckDuckGo 的 `wikipedia` backend 把 region 的第二段当 **Wikipedia 子域名语言**。它的全球 region
`wt-wt` 会变成 `wt.wikipedia.org`（无效）。所以用 wikipedia backend 时，得按查询语言选个合理的 region。

mini 照搬 deer 的推断：扫查询里的 Unicode 字符块——

| 字符块 | 推断 region | 语言 |
|--------|------------|------|
| 平假名/片假名（こんにちは） | `jp-ja` | 日语 |
| 韩文（안녕） | `kr-ko` | 韩语 |
| CJK 统一表意（你好） | `cn-zh` | 中文 |
| 西里尔（привет） | `ru-ru` | 俄语 |
| 希腊（γειά） | `gr-el` | 希腊语 |
| 希伯来（שלום） | `il-he` | 希伯来语 |
| 阿拉伯（مرحبا） | `xa-ar` | 阿拉伯语 |
| 拉丁/其它 | `us-en` | 英语（默认） |

这样查「量子计算」会去中文 Wikipedia，查「quantum computing」去英文——结果相关性更高。
**只在用 wikipedia backend 且 region 是全球 wt-wt 时才推断**，否则尊重用户配的 region。

---

## 软加载（缺包不崩，红线 #24）

每个 provider 的外部 SDK（`ddgs` / `tavily` / `firecrawl` / `exa_py`）都是**可选依赖**，可能没装。
设计铁律：**模块顶层绝不 import SDK**——SDK 的 import 放在**工具函数体里**，用 `try/except ImportError`：

```python
# ddg_search/tools.py
def _search_text(query, ...):
    try:
        from ddgs import DDGS          # ← 函数内部 import
    except ImportError:
        logger.error("ddgs library not installed. Run: pip install ddgs")
        return []                       # ← 缺包返空，不崩
```

为什么这样？因为 `tools[].use: "deerflow.community.ddg_search.tools:web_search_tool"` 路径要经
`resolve_variable` import 模块。如果模块顶层 `import ddgs`，缺包时**整个模块 import 崩**，agent 连
别的工具都用不了。把 import 放函数里，模块永远能 import，工具永远能 resolve，**真正调用时**才检测 SDK——
缺包就返可操作安装提示（`pip install ddgs`）。

`community/__init__.py` 也**不 eager import** 子模块——否则缺任一 SDK 就让 `import deerflow.community`
炸。子模块由消费者（M15）按需 import。

---

## 文件结构

```
community/
├── __init__.py            # 不 eager import 子模块（缺 SDK 不崩）
├── _common.py             # 共享层：normalize + truncate + coerce + get_tool_extras + post_json
├── ddg_search/            # 【核心】web_search（DuckDuckGo，免费，CJK 推断）
│   ├── __init__.py
│   └── tools.py           #   _search_text + _resolve_ddgs_region + _infer_wikipedia_region
├── tavily/                # 【核心】web_search + web_fetch（Tavily，需 key）
│   ├── __init__.py
│   └── tools.py
├── jina_ai/               # 【核心】web_fetch（Jina Reader，async，可选 key）
│   ├── __init__.py
│   ├── tools.py
│   └── jina_client.py     #   JinaClient.crawl（走 _common.post_json）
├── image_search/          # image_search（DuckDuckGo 图搜）
├── brave/                 # web_search（Brave REST API，需 key）
├── serper/                # web_search（Google 经 Serper，需 key）
├── searxng/               # web_search（async，自托管）
│   ├── tools.py
│   └── searxng_client.py  #   SearxngClient
├── browserless/           # web_fetch（async，headless Chrome 渲染）
│   ├── tools.py
│   └── browserless_client.py
├── firecrawl/             # 【占位】search+fetch（软加载 firecrawl SDK）
├── exa/                   # 【占位】search+fetch（软加载 exa_py SDK）
└── infoquest/             # 【占位】search+fetch+图搜（compact InfoQuestClient，需 key）

utils/
└── readability.py         # ReadabilityExtractor + Article（soft-load readabilipy/markdownify + 纯 Python 兜底）

config/
└── app_config.py          # + get_tool_config(name)（按 tools[].name 查配置 dict）
```

---

## 关键接口

```python
# _common
MAX_FETCH_CHARS = 4096
def normalize_search_result(title, url, snippet, *, content_key="content") -> dict: ...
def truncate_content(text, limit=MAX_FETCH_CHARS) -> str: ...
def coerce_bool(value, default) -> bool: ...
def coerce_int(value, default) -> int: ...
def coerce_timeout(value, default) -> int: ...      # coerce_int 别名
def coerce_proxy(value) -> str | None: ...
def get_tool_extras(name: str) -> dict: ...          # 读 config tools[].name，无则 {}
async def post_json(url, *, headers, json_body, timeout=10, proxy=None, trust_env=True) -> str: ...

# readability
class Article:
    def to_markdown(self, including_title=True) -> str: ...
class ReadabilityExtractor:
    def extract_article(self, html: str) -> Article: ...

# config
class AppConfig:
    def get_tool_config(self, name: str) -> dict | None: ...   # mini 新增（deer 用 pydantic ToolConfig.model_extra）

# 各 provider 工具（@tool 装饰，经 tools[].use: 加载）
#   ddg_search.tools:web_search_tool(query, max_results=5)
#   tavily.tools:web_search_tool(query) / web_fetch_tool(url)
#   jina_ai.tools:web_fetch_tool(url)              # async
#   image_search.tools:image_search_tool(query, ...)
#   brave.tools:web_search_tool(query, max_results=5)
#   serper.tools:web_search_tool(query, max_results=5)
#   searxng.tools:web_search_tool(query)           # async
#   browserless.tools:web_fetch_tool(url)          # async
#   firecrawl.tools:web_search_tool(query) / web_fetch_tool(url)
#   exa.tools:web_search_tool(query) / web_fetch_tool(url)
#   infoquest.tools:web_search_tool(query) / web_fetch_tool(url) / image_search_tool(query)
```

---

## 设计原理（权衡 / 不变量）

### 为什么 mini 抽 `_common.py` 而 deer 没有

deer 每个 provider 重复写「归一 + 截断 + 读 config」。mini 把它们集中到 `_common.py`，12 个 provider
共用——改一处（比如截断上限从 4096 调到 8192）只改一个常量。代价：多一层函数调用（可忽略）。收益：
DRY + 单源真相。

### 为什么 `get_tool_extras` 返回 dict 而不是 pydantic model

deer 的 `tools` 是 `list[ToolConfig]`（pydantic，`extra="allow"`），额外字段存 `model_extra`。
mini 的 `tools` 当前是 `list[dict]`（M15 才类型化）。所以 mini 的 `get_tool_config(name)` 返回**原始
dict**，调用方直接 `.get(key, default)`——等价于 deer 的 `config.model_extra.get(...)`。M15 类型化后
这个 dict 会变成 ToolConfig，到时再补 `model_extra` 适配（向前兼容）。

### 为什么 readability 要纯 Python 兜底

deer 的 `ReadabilityExtractor` 强依赖 `readabilipy`（经 Node 子进程跑 Readability.js）。没装就抛异常。
mini 教学/开发场景往往不装 Node——若照搬，jina/browserless 在没装 readabilipy 时**直接崩**。mini 加纯
Python 兜底（剥噪音标签 + 抽 title + 去标签），质量降级但不崩。装上 readabilipy 自动升级到高质量路径。

### 为什么 jina 的 crawl 把异常归一成 `"Error: ..."` 字符串

jina_ai 的 `web_fetch_tool` 是 agent 调的工具。工具**不该抛异常打断 agent**（agent 不知道怎么处理网络错误）。
所以 `JinaClient.crawl`（经 `_common.post_json`）把所有异常（非 200、空响应、超时、网络错）归一成
`"Error: <可读消息>"` 前缀字符串返回。工具检查前缀判断成败——失败时 agent 看到「Error: ...」能决定
要不要换 URL 或换 provider。这是 deer 的 convention，mini 沿用。

### 为什么参数要 `coerce_*` 强转

config.yaml 是 YAML，用户可能写 `timeout: 10`（int）或 `timeout: "10"`（str）或 `trust_env: yes`
（YAML 的 yes）。直接传给 httpx 会类型错。`coerce_bool`/`coerce_int`/`coerce_timeout`/`coerce_proxy`
把这些值安全转成期望类型，非法值回退默认——配置宽容，不因笔误崩。

---

## 与其它模块的关系

```
config/app_config (tools[] + get_tool_config)
   │
community/_common (归一 + 截断 + 强转 + post_json + get_tool_extras)
   │
community/<provider>/tools.py  ←  @tool 装饰的 BaseTool
   │   ↑ 各 SDK 软加载（ddgs/tavily/httpx/firecrawl/exa_py/requests）
   │
   ▼ 经 reflection.resolve_variable 加载
tools/tools.py (get_available_tools：M15 落地后遍历 config.tools[] 调 resolve_variable)
   │
   ▼
agents/lead_agent (工具集拼进 agent)
```

- **上游**：`config/app_config`（`get_tool_config`）、`reflection`（`resolve_variable` 加载 `tools[].use:` 路径）、
  `utils/readability`（jina/browserless 的可读性提取）。
- **下游消费者**：M15 `get_available_tools`（遍历 `config.tools[]`，对 community 工具调
  `resolve_variable(cfg["use"], BaseTool)` 加载——这是 community 工具**唯一**的挂载点，M15 落地后自动生效）。
- **与 MCP 的区别**：MCP 是「调外部工具**服务器**」（协议层，工具由别人实现）；community 是「**本进程内**
  调搜索/抓取 API」（工具在本仓库实现）。MCP 工具体量大需延迟加载（M15 tool_search），community 工具轻量直接绑定。

---

## 常见问题 / 排错

**Q：装了 ddg_search 但 agent 搜不到东西？**
A：先确认 `config.yaml` 的 `tools[]` 里有 `use: "deerflow.community.ddg_search.tools:web_search_tool"`
这行（没配 = 没工具）。再确认装了 `ddgs`（`pip install ddgs`）。缺包时工具返 `{"error": "No results found"}`
+ 日志记安装提示。

**Q：换 provider 要改 agent 代码吗？**
A：不用。只改 `config.yaml` 的 `use:` 路径（如 ddg → tavily）。结果都归一成 `{title,url,snippet}`，
agent 无感知。这是 provider 框架的核心价值（可插拔）。

**Q：web_fetch 抓回来的是乱码 HTML？**
A：不会。所有 fetch 工具都经 `ReadabilityExtractor` 提取正文 + 转 markdown + 4KB 截断。装了
`readabilipy`+`markdownify` 质量最好；没装走纯 Python 兜底（剥 `<script>`/`<nav>` 等）。

**Q：免费能用哪个？**
A：`ddg_search`（搜索）+ `jina_ai`（抓取，免费额度）+ `image_search`（图搜）都不用 API key，开箱即用。
要更强就上 tavily（要 key）。

**Q：12 个 provider 都要装吗？**
A：不用。config.yaml 里只写你用的那个的 `tools[].use:`。没配的 provider 根本不会被 import
（`resolve_variable` 只加载 config 里列的）。软加载保证没装的 provider 不影响其它工具。

**Q：为什么 firecrawl/exa/infoquest 是「占位」？**
A：它们的 SDK（`firecrawl`/`exa_py`）没装时，工具返可操作安装提示（不崩）。装上 SDK 后自动走真实逻辑。
mini 已移植它们的完整逻辑，只是默认不装 SDK——需要时 `pip install firecrawl-py` 即可激活。

**Q：CJK 查询的 region 是怎么定的？**
A：ddg_search 用 wikipedia backend 且 region 是全球 wt-wt 时，按查询里的 Unicode 字符块推断（中文→cn-zh，
日语→jp-ja……）。让中文查询去中文 Wikipedia，结果更相关。只在 wikipedia backend 生效，其它 backend
尊重你配的 region。

---

## 应用方法

### 启用免费搜索（ddg，推荐入门）

`config.yaml`：

```yaml
tools:
  - name: web_search
    group: search
    use: "deerflow.community.ddg_search.tools:web_search_tool"
    max_results: 5
    region: wt-wt            # 全球；用 wikipedia backend 时自动按 CJK 推断
    safesearch: moderate
    backend: auto            # auto/duckduckgo/wikipedia
```

`pip install ddgs` 即可（无 key）。

### 启用生产搜索 + 抓取（tavily + jina）

```yaml
tools:
  - name: web_search
    group: search
    use: "deerflow.community.tavily.tools:web_search_tool"
    api_key: "$TAVILY_API_KEY"     # $VAR 从环境变量展开
    max_results: 5
  - name: web_fetch
    group: search
    use: "deerflow.community.jina_ai.tools:web_fetch_tool"
    timeout: 10
    trust_env: true               # 读 HTTP_PROXY 环境变量
```

`pip install tavily-python`，设 `TAVILY_API_KEY` 环境变量。jina 可选设 `JINA_API_KEY` 提限额。

### 换成 Brave（要 key）

```yaml
tools:
  - name: web_search
    group: search
    use: "deerflow.community.brave.tools:web_search_tool"
    api_key: "$BRAVE_SEARCH_API_KEY"
    max_results: 5                # 上限 20（Brave API 单次最多 20）
```

### 跑测试

```bash
cd backend && make test    # 含 test/test_community.py（106 个 hermetic 测试）
```

测试约定：`ddgs` / `tavily` / `firecrawl` / `exa_py` 均**未安装**——用 `sys.modules` 注入 fake 模块；
`httpx` / `requests` 已安装——monkeypatch 替 `httpx.Client` / `httpx.AsyncClient`。零网络零子进程。
config 经 monkeypatch `_common.get_app_config` 注入假配置。
