# 21. community.md — 联网能力（搜索 / 抓取 provider 框架）

> **重写日期**：2026-07-05。**对照代码**：`backend/packages/harness/deerflow/community/`（12 个 provider 子包 + `_common.py`，约 2000 行；`aio_sandbox/` 子包由 [#14 aio_sandbox.md](aio_sandbox.md) 讲）。

> **一句话定位**：本模块给 agent 装「眼睛和手」——能**搜网页**（web_search）找最新信息、能**抓网页**（web_fetch）读某个 URL 的内容。这是 agent「知道训练数据之外的事」的核心来源（训练数据有截止日期，联网让它能答「今天天气」「最新论文」这类问题）。

> **先读谁最省事**：[mcp.md](mcp.md)（懂「外部工具协议」）。MCP 是「**调别人写好的工具服务器**」（协议层），community 是「**本进程内直接调搜索/抓取 API**」（工具在本仓库实现）。两者都扩展 agent 能力，但 community 专门管「联网」。

---

## §1 学完这篇你能回答什么（learning outcomes · 面试视角）

1. **「agent 怎么知道训练数据之外的事？」** —— 联网工具（web_search + web_fetch 两步）。能讲清为什么是两步（先找 URL 再读全文）、agent 自己决定要不要 fetch。
2. **「为什么要做 provider 框架？换搜索引擎要改代码吗？」** —— 不同搜索 API 各有取舍（免费/要钱/自托管）。能讲清结果归一化 + 配置驱动加载（换 provider 只改 `use:` 一行，agent 零改动）。
3. **「依赖的 SDK 是可选的，怎么做到缺包也不崩？」** —— 软加载（函数内 import + try/except，模块顶层绝不 import SDK）。能讲清为什么必须在函数内 import（否则 resolve_variable 加载模块就崩）。
4. **「把网页正文塞进 LLM 上下文，怎么不爆 token？」** —— 4KB 截断 + 可读性提取（剥 HTML 噪音、转 markdown）。能讲清为什么是 4096。
5. **「不同搜索 API 返回的字段名五花八门，怎么统一？」** —— `normalize_search_result` 归一成 `{title,url,snippet}`。
6. **「CJK 查询怎么让搜索引擎去对应语言的源？」** —— ddg wikipedia backend 按 Unicode 字符块推断 region（中文→cn-zh……）。

---

## §2 零基础先读：名词解释

### §2.1 计算机基础层（不熟这些先看这段）

| 名词 | 一句话解释 |
|---|---|
| **HTTP 请求 / REST API** | 程序间通信的标准方式。你的程序发一个 HTTP 请求到某 URL（带参数/头），服务器返响应。**REST API**是约定俗成的一组 HTTP 调用规矩。搜索引擎都通过 HTTP API 提供服务。 |
| **API key** | 一串密码字符串，证明「我有权调这个 API」。要钱的搜索引擎（tavily/brave/serper）都靠它计费鉴权。 |
| **SDK** | 「软件开发包」——某服务官方提供的客户端库（如 `ddgs`、`tavily-python`、`firecrawl`），把 HTTP 调用包成 Python 函数，省得你手写请求。 |
| **URL** | 网页地址（`https://example.com/page`）。web_search 返回一堆 URL，web_fetch 读具体某个。 |
| **HTML / markdown** | HTML 是网页的标记语言（满屏 `<div>` `<script>`）；**markdown**是简洁的纯文本格式（`# 标题`、`**粗体**`）。web_fetch 把 HTML 正文转成 markdown 喂给 agent。 |
| **headless browser** | 「无头浏览器」——没有界面的 Chrome，能执行 JS 渲染页面（普通 HTTP 抓不下来的 SPA 单页应用）。browserless 用它。 |
| **region** | 搜索的「地区/语言」参数（如 `cn-zh`=中国中文、`us-en`=美国英文）。影响结果相关性。 |
| **Unicode 码点（codepoint）** | 每个字符在 Unicode 里的编号（如「你」=U+4F60）。CJK 推断靠「字符落在哪个码点范围」判断语言。 |
| **proxy（代理）** | 一个中间服务器，你的请求先发给它、它转发给目标。公司网络/翻墙常用。jina 支持 `proxy` + `trust_env`（读 `HTTP_PROXY` 环境变量）。 |
| **SSRF（服务器端请求伪造）** | 一种攻击：让服务器去请求它本不该访问的内网地址（如 `http://localhost/admin`、`http://10.0.0.1`）。serper 的图片 URL 经 `_safe_public_url` 守卫拒掉这类。 |
| **async / await** | 异步编程关键字。网络请求要等（IO），`async def` 让等待时事件循环能干别的。本模块的网络调用多是 async。 |
| **软加载（soft-load）** | 把 `import` 放函数内部 + `try/except ImportError`，依赖包没装时不崩、降级。 |
| **JSON** | 文本格式的嵌套数据。搜索引擎 API 的响应都是 JSON。 |

### §2.2 本模块名词

| 名词 | 解释 |
|---|---|
| **provider** | 一个联网服务的实现（ddg_search、tavily、jina_ai……）。每个 provider 是一个子包。 |
| **web_search** | 输入关键词，返回一堆网页结果（标题/链接/摘要）。 |
| **web_fetch** | 输入一个 URL，返回那页的正文（markdown）。 |
| **image_search** | 输入关键词，返回一堆图片 URL。 |
| **结果归一化** | 把不同 provider 五花八门的字段名统一成 `{title,url,snippet}`。 |
| **可读性提取（readability）** | 从原始 HTML 抽出正文、剥掉导航/广告/script，转成干净 markdown。 |
| **CJK region 推断** | 按查询里的 Unicode 字符块（中日韩俄希腊等）推断搜索 region。 |

---

## §3 整体结构：它在系统里的位置

```
config.yaml 的 tools[]  ──每条 {name, use: "deerflow.community.<provider>.tools:<tool>_tool"}
        │
        ▼
tools/tools.py  get_available_tools()
   └─ for cfg in config.tools: resolve_variable(cfg["use"], BaseTool)   ← 动态加载
        │   # import 那个模块 + 取出 @tool 装饰的 BaseTool 变量
        ▼
community/<provider>/tools.py  （@tool 装饰的 web_search_tool / web_fetch_tool）
   ├─ 读 get_tool_extras(name) 拿 config 的额外字段（max_results/region/...）
   ├─ 软加载 SDK（ddgs/tavily/httpx...，函数内 import，缺包返可操作错误）
   ├─ 调搜索/抓取 API
   ├─ normalize_search_result 归一 / ReadabilityExtractor 提取正文
   └─ truncate_content 4KB 截断 → 返 JSON 给 agent
        │
        ▼
agent 工具集（agent 调 web_search / web_fetch）
```

**community 的 12 个 provider 子包**（`aio_sandbox/` 见 [#14](aio_sandbox.md)）：

```
community/
├── __init__.py            # 不 eager import 子模块（缺 SDK 不崩）—— mini 新增
├── _common.py             # 共享层：归一+截断+强转+post_json+get_tool_extras —— mini 新增
├── ddg_search/            # web_search（DuckDuckGo，免费，CJK 推断）★教学默认
├── tavily/                # web_search + web_fetch（Tavily，需 key）★生产推荐
├── jina_ai/               # web_fetch（Jina Reader，转干净 markdown，免费额度）★抓取
│   ├── tools.py
│   └── jina_client.py     #   JinaClient.crawl（走 _common.post_json）
├── image_search/          # image_search（DuckDuckGo 图搜）
├── brave/                 # web_search（Brave REST API，需 key）
├── serper/                # web_search + image_search（Google 经 Serper，需 key，含 SSRF 守卫）
├── searxng/               # web_search（自托管聚合）
├── browserless/           # web_fetch（headless Chrome 渲染，自托管）
├── firecrawl/             # search+fetch（软加载 firecrawl SDK）
├── exa/                   # search+fetch（软加载 exa_py SDK）
├── fastcrw/               # search+fetch（firecrawl 库的薄封装变体）
├── groundroute/           # search+fetch（纯 httpx）
└── infoquest/             # web_search + web_fetch（BytePlus，需 key）★mini 精简版
```

按落地深度分三档（见 [§7 选哪个](#7-配置与用法)）：核心 3 个（ddg/tavily/jina，完整实现）+ 全量移植（httpx/ddgs 驱动）+ 软加载占位（SDK 缺包返可操作错误）。

**面试概念地图**：本篇对应「工具集成 / provider 框架」「软加载与降级」「联网工具设计」面试常考点。`deerflow-book` 的 `15-builtin-tools.md`（web 部分）是可选概念预读。

---

## §4 核心概念：为什么需要联网 + provider 框架

LLM 的知识来自训练数据，有**截止日期**（cutoff）。问它「2024 年诺贝尔物理学奖得主」，若训练数据截止在 2023，它要么瞎编要么说不知道。联网工具让 agent 在**回答前**先去网上查，拿到最新事实再组织答案。

deer-flow 把联网拆成几类工具：

| 工具 | 干什么 | 类比 |
|------|--------|------|
| `web_search` | 输入关键词，返回一堆网页结果（标题/链接/摘要） | Google 搜索框敲词 |
| `web_fetch` | 输入一个 URL，返回那页的正文内容（markdown） | 点开一个搜索结果读全文 |
| `image_search` | 输入关键词，返回一堆图片 URL | Google 图片搜索 |

注意 `web_search` 和 `web_fetch` 是**两步**：先 search 找候选 URL，再 fetch 读具体那页。agent 自己决定要不要 fetch（看摘要够不够）。

**为什么有这么多 provider？** 因为没有「唯一最好的搜索引擎」：

| provider | 类型 | 要 key？ | 特点 |
|----------|------|----------|------|
| **ddg_search** | web_search | ❌ | DuckDuckGo，**免费开箱即用**，教学首选 |
| **tavily** | search+fetch | ✅ | 专为 AI 设计，质量高，自带 fetch |
| **jina_ai** | web_fetch | 可选 | Jina Reader，网页转干净 markdown，免费额度 |
| image_search | image_search | ❌ | DuckDuckGo 图搜 |
| brave | web_search | ✅ | 独立搜索索引 |
| serper | search+image | ✅ | 经 Serper 调 Google |
| searxng | web_search | 自托管 | 聚合多引擎，自己部署 |
| browserless | web_fetch | 自托管 | headless Chrome 渲染 JS 页面 |
| firecrawl / fastcrw / groundroute / exa | search+fetch | ✅ | 各类抓取/语义搜索 |
| infoquest | search+fetch | ✅ | BytePlus（mini 精简版） |

你**不需要全装**——按需在 config.yaml 里启用一两个。换 provider 只改 `use:` 一行，结果都归一成 `{title,url,snippet}`，agent 无感知——这就是 provider 框架的价值（可插拔）。

---

## §5 代码走读：重要函数逐个讲

### §5.1 _common.py —— 共享层（mini 抽出，上游没有）

mini 把散在各 provider 里的重复逻辑集中到 [`_common.py`](../backend/packages/harness/deerflow/community/_common.py)：

| 函数 | 作用 |
|------|------|
| [`normalize_search_result`](../backend/packages/harness/deerflow/community/_common.py#L46)(title, url, snippet) | 把一条原始结果归一成 `{title,url,snippet/content}`（不同 provider 字段名五花八门：ddg 的 `href`/`body`、tavily 的 `url`/`content`、brave 的 `url`/`description`，归一后统一） |
| [`truncate_content`](../backend/packages/harness/deerflow/community/_common.py#L66)(text, limit=4096) | 抓取内容截到 4KB（`MAX_FETCH_CHARS` [L29](../backend/packages/harness/deerflow/community/_common.py#L29)，防 prompt 爆炸） |
| [`coerce_bool`](../backend/packages/harness/deerflow/community/_common.py#L79) / [`coerce_int`](../backend/packages/harness/deerflow/community/_common.py#L95) / `coerce_timeout` / [`coerce_proxy`](../backend/packages/harness/deerflow/community/_common.py#L116) | config.yaml 值可能是字符串（`timeout: "10"`、`trust_env: yes`），安全强转 |
| [`get_tool_extras`](../backend/packages/harness/deerflow/community/_common.py#L129)(name) | 读 `config.yaml` 的 `tools[].name == name` 条目，返回其额外字段 dict（无则 `{}`，调用方免判空） |
| [`post_json`](../backend/packages/harness/deerflow/community/_common.py#L146) | async httpx 封装：POST JSON，失败归一成 `"Error: ..."` 字符串（jina 复用） |

**为什么抽 `_common` 而 upstream 没有**：upstream 每个 provider 重复写「归一+截断+读 config」。mini 集中到 `_common.py`，12 个 provider 共用——改一处（如截断上限）只改一个常量。代价是多一层函数调用（可忽略），收益是 DRY + 单源真相。

### §5.2 ddg_search —— 免费搜索 + CJK region 推断（教学默认）

**`web_search_tool`** [ddg_search/tools.py:131](../backend/packages/harness/deerflow/community/ddg_search/tools.py#L131)：读 extras → `_resolve_ddgs_region` 推断 region → `_search_text` 调 ddgs → 归一结果。

**CJK region 推断**（`_infer_wikipedia_region` [ddg_search/tools.py:58](../backend/packages/harness/deerflow/community/ddg_search/tools.py#L58)）：DuckDuckGo 的 `wikipedia` backend 把 region 第二段当 Wikipedia 子域名语言，全球 region `wt-wt` 会变成 `wt.wikipedia.org`（无效）。所以用 wikipedia backend 时，按查询里的 Unicode 字符块推断：

| 字符块 | region | 语言 |
|--------|--------|------|
| 平假名/片假名 | `jp-ja` | 日语 |
| 韩文 | `kr-ko` | 韩语 |
| CJK 统一表意 | `cn-zh` | 中文 |
| 西里尔 | `ru-ru` | 俄语 |
| 希腊 | `gr-el` | 希腊语 |
| 希伯来 | `il-he` | 希伯来语 |
| 阿拉伯 | `xa-ar` | 阿拉伯语 |
| 拉丁/其它 | `us-en` | 英语（默认） |

**只在用 wikipedia backend 且 region 是全球 `wt-wt` 时才推断**（`_resolve_ddgs_region` [L81](../backend/packages/harness/deerflow/community/ddg_search/tools.py#L81)），否则尊重用户配的 region。

**软加载**：`_search_text` [L97](../backend/packages/harness/deerflow/community/ddg_search/tools.py#L97) 函数内 `from ddgs import DDGS`，缺包返 `[]` + 记 `pip install ddgs` 提示。

### §5.3 jina_ai —— 网页转 markdown 抓取

**`web_fetch_tool`** [jina_ai/tools.py:28](../backend/packages/harness/deerflow/community/jina_ai/tools.py#L28) → **`JinaClient.crawl`** [jina_client.py:27](../backend/packages/harness/deerflow/community/jina_ai/jina_client.py#L27)：走 `_common.post_json`，支持 `proxy`（经 `coerce_proxy` 强转）+ `trust_env`（读 `HTTP_PROXY` 环境变量）。所有异常归一成 `"Error: <可读消息>"` 字符串返回——工具不该抛异常打断 agent。

### §5.4 tavily / serper —— 要 key 的搜索

**`tavily.web_search_tool`** [tavily/tools.py:34](../backend/packages/harness/deerflow/community/tavily/tools.py#L34) / **`web_fetch_tool`** [L66](../backend/packages/harness/deerflow/community/tavily/tools.py#L66)：软加载 `tavily-python`，`_get_tavily_client` [L25](../backend/packages/harness/deerflow/community/tavily/tools.py#L25) 按 extras 的 `api_key` 建客户端。**serper** 的 `image_search` 工具查 Google Images，返回的图片 URL 经 `_safe_public_url` 过一遍 **SSRF 守卫**（拒非 http(s) scheme、localhost、私有/非全局 IP——含十进制/十六进制/八进制编码的 IPv4，`_decode_ipv4` 解码后再判）。

### §5.5 加载机制 —— 配置驱动（reflection）

community 工具**不**像 MCP 那样自动发现——它们是**配置驱动**的：你在 `config.yaml` 的 `tools[]` 里写一行，agent 才有这个工具。`get_available_tools` [tools/tools.py:64](../backend/packages/harness/deerflow/tools/tools.py#L64) 遍历 `config.tools`，对每条调 `resolve_variable(cfg["use"], BaseTool)` [L94](../backend/packages/harness/deerflow/tools/tools.py#L94)——`use:` 路径指向 `模块:变量`，import 模块 + 取出 `@tool` 装饰的变量。**换 provider 只改 `use:` 一行**，agent 代码零改动。

---

## §6 数据流：一次调用怎么走完

### §6.1 数据流 A：agent 启动 → 加载 web_search 工具

```
① agent 装配 → get_available_tools(include_mcp=True)
② for cfg in config.tools:
     resolve_variable("deerflow.community.ddg_search.tools:web_search_tool", BaseTool)
       └─ import ddg_search.tools 模块 + 取 web_search_tool 变量（@tool 装饰的 BaseTool）
③ 工具集多一个 web_search → agent 能调它了
```

### §6.2 数据流 B：agent 调 web_search("量子计算 最新进展")

```
① agent 调 web_search(query="量子计算 最新进展")
② web_search_tool(query, max_results=5)
   ├─ extras = get_tool_extras("web_search")          # 读 config 的 region/safesearch/backend
   ├─ _resolve_ddgs_region(query, region, backend)    # CJK 推断 → "cn-zh"
   └─ _search_text(query, max_results, region="cn-zh", ...)
        ├─ from ddgs import DDGS                       # 软加载，缺包返 []
        └─ ddgs.text(query, region="cn-zt", safesearch=, max_results=, backend=)
③ [{"title":..., "href":..., "body":...}, ...]         # ddgs 原始结果
   └─ normalize_search_result(title, url=href, snippet=body)
④ {"query":..., "total_results":N, "results":[{title,url,content},...]}  # JSON 给 agent
⑤ agent 看摘要够不够 → 够就答；不够再调 web_fetch(某 URL) 读全文
```

---

## §7 配置与用法

### §7.1 配置（`config.yaml` 的 `tools[]`）

```yaml
tools:
  - name: web_search                    # 工具名（get_tool_extras 按它查配置）
    group: search
    use: "deerflow.community.ddg_search.tools:web_search_tool"   # 模块:变量
    max_results: 5                      # ↓ 额外字段，传给 provider 当参数
    region: wt-wt                       # 全球；用 wikipedia backend 时自动按 CJK 推断
    safesearch: moderate
    backend: auto                       # auto/duckduckgo/wikipedia
  - name: web_fetch
    group: search
    use: "deerflow.community.jina_ai.tools:web_fetch_tool"
    timeout: 10
    trust_env: true                     # 读 HTTP_PROXY 环境变量
```

### §7.2 选哪个 provider？

- **学习/开发**：`ddg_search`（免费，不用申请 key）。
- **生产搜索**：`tavily`（质量好，要 key）。
- **抓取网页**：`jina_ai`（免费额度，转干净 markdown）。
- **要 Google 结果**：`serper`（经 Serper 调 Google，要 key）。
- **自己掌控**：`searxng`（自托管聚合）+ `browserless`（自托管渲染）。

### §7.3 跑测试

```bash
cd backend && make test    # 含 test/test_community.py（87 个 hermetic 测试）
```

测试约定：`ddgs` / `tavily` / `firecrawl` / `exa_py` 均**未安装**——用 `sys.modules` 注入 fake 模块；`httpx` / `requests` 已安装——monkeypatch 替 `httpx.Client` / `httpx.AsyncClient`。零网络零子进程。config 经 monkeypatch `_common.get_app_config` 注入假配置。

---

## §8 与其它模块的关系

```
config/app_config (tools[] + get_tool_config)
   │
community/_common (归一 + 截断 + 强转 + post_json + get_tool_extras)
   │
community/<provider>/tools.py  ←  @tool 装饰的 BaseTool
   │   ↑ 各 SDK 软加载（ddgs/tavily/httpx/firecrawl/exa_py）
   │
   ▼ 经 reflection.resolve_variable 加载
tools/tools.py (get_available_tools：遍历 config.tools[] 调 resolve_variable)
   │
   ▼
agents/lead_agent (工具集拼进 agent)
```

- **上游**：[config](config.md) app_config（`get_tool_config`）、`reflection.resolve_variable`（加载 `tools[].use:` 路径）、[utils](utils.md) readability（jina/browserless 的可读性提取）。
- **下游消费者**：[tools](tools.md) `get_available_tools`（community 工具**唯一**挂载点）。
- **与 MCP 的区别**：MCP 是「调外部工具**服务器**」（协议层，工具由别人实现，体量大需延迟加载）；community 是「**本进程内**调搜索/抓取 API」（工具在本仓库实现，轻量直接绑定）。`aio_sandbox/` 子包见 [#14](aio_sandbox.md)。

---

## §9 设计动机分析（为什么这么设计 / 作用 / 好处）

### §9.0 核心设计动机一览

| 关键机制 | 为什么这么设计 | 作用 / 好处 | 不这么设计会怎样 |
|---|---|---|---|
| **provider 框架 + 结果归一化** | 没有唯一最好的搜索引擎 | 换 provider 只改 `use:` 一行，agent 无感知 | 每个搜索引擎写死 → 切换要改 agent 代码 |
| **`_common.py` 共享层** | 各 provider 重复写归一/截断/读 config | DRY，改一处全生效（mini 新增，上游内联） | 重复代码，改截断上限要改 12 处 |
| **软加载（函数内 import）** | SDK 是可选依赖 | 缺包返可操作错误，模块永远能 import | 模块顶层 import → 缺包 resolve_variable 崩，全工具挂 |
| **4KB 截断** | 网页正文几万字会爆 token | 够 agent 判断「这页讲什么」，要更多再 fetch | 全塞 → 挤掉别的消息/超 token 上限 |
| **`__init__.py` 不 eager import** | 任一 SDK 缺都会让 `import community` 炸 | 子模块按需 import，缺 SDK 不影响别的 | eager import → 一个 provider 缺包整包崩 |
| **异常归一 `"Error: ..."`** | 工具不该抛异常打断 agent | 失败返可读字符串，agent 据此决策 | 抛异常 → agent 不知怎么处理网络错误 |
| **CJK region 推断** | wikipedia backend 的全球 region 无效 | 中文查询去中文源，结果更相关 | 全球 region → wt.wikipedia.org 无效 |
| **SSRF 守卫（serper 图片）** | 图片 URL 是不可信输入 | 拒 localhost/私有 IP（含编码绕过） | agent 能让服务器探内网 |

### §9.1 为什么做 provider 框架 + 结果归一化

**动机**：市面上能用的搜索 API 很多，各有取舍（免费的 ddg 限流、要钱的 tavily 质量好、自托管的 searxng 隐私强）。而且字段名五花八门（ddg 的 `href`/`body`、tavily 的 `url`/`content`、brave 的 `url`/`description`）。

**作用 / 好处**：`normalize_search_result` 归一成统一 `{title,url,snippet}`，agent 看到的永远一样。换 provider 只改 config.yaml 的 `use:` 一行，agent 代码零改动——这就是可插拔的价值。

**不这么设计会怎样**：每个搜索引擎写死在 agent 里 → 想换要改 agent 代码；不归一 → 换 provider agent 解析就坏。

### §9.2 为什么软加载必须在「函数内」import

**动机**：每个 provider 的外部 SDK（`ddgs`/`tavily`/`firecrawl`/`exa_py`）都是**可选依赖**。而加载机制是 `resolve_variable("...tools:web_search_tool")` 要 import 模块。

**作用 / 好处**：SDK 的 import 放在**工具函数体里** + `try/except ImportError`。模块顶层绝不 import SDK——这样模块永远能 import，工具永远能 resolve，**真正调用时**才检测 SDK，缺包返可操作安装提示。`community/__init__.py` 也不 eager import 子模块。

**不这么设计会怎样**：模块顶层 `import ddgs` → 缺包时**整个模块 import 崩** → `resolve_variable` 抛错 → agent 连别的工具都用不了。

### §9.3 为什么 4KB 截断 + 可读性提取

**动机**：web_fetch 抓回来的是原始 HTML（满屏 `<div>` `<script>`，正文可能几万字）。全塞进 LLM 上下文会挤掉别的消息甚至超 token 上限。

**作用 / 好处**：① `ReadabilityExtractor`（[utils/readability.py](utils.md)）剥导航/广告/script、抽正文、转 markdown；② `truncate_content` 截到 `MAX_FETCH_CHARS = 4096`（约 1000 token）。4KB 够 agent 判断「这页讲什么」，要更多 agent 会自己再 fetch 或分块。**软加载**：优先用 `readabilipy`（包 Mozilla Readability.js，质量最好但要装 Node）+ `markdownify`，都缺时走**纯 Python 兜底**（剥噪音标签 + 抽 title + 去标签）——质量降级但不崩。

**不这么设计会怎样**：返原始 HTML → agent 看不懂 + 浪费 token；不截断 → 几万字正文爆上下文。

### §9.4 为什么异常归一成 `"Error: ..."` 字符串

**动机**：`web_fetch_tool` 是 agent 调的工具。工具**不该抛异常打断 agent**（agent 不知道怎么处理网络错误）。

**作用 / 好处**：`post_json` 把所有异常（非 200、空响应、超时、网络错）归一成 `"Error: <可读消息>"` 前缀字符串返回。工具检查前缀判断成败——失败时 agent 看到「Error: ...」能决定要不要换 URL 或换 provider。

**不这么设计会怎样**：工具抛异常 → LangGraph 捕获 → agent 流程被打断或重试浪费。

### §9.5 为什么参数要 `coerce_*` 强转

**动机**：config.yaml 是 YAML，用户可能写 `timeout: 10`（int）或 `timeout: "10"`（str）或 `trust_env: yes`（YAML 的 yes）。

**作用 / 好处**：`coerce_bool`/`coerce_int`/`coerce_timeout`/`coerce_proxy` 把这些值安全转成期望类型，非法值回退默认——配置宽容，不因笔误崩。

**不这么设计会怎样**：直接传 httpx → 类型错（`timeout: "10"` 字符串 httpx 不认）→ 崩。

---

## §10 实现差异（vs 上游 deer-flow 源码）

> 对照 `deer-flow/backend/packages/harness/deerflow/community/`（12 个 provider 子包）。**先剥 docstring/comment 再判逻辑差**。

**总结论：多数 provider 忠实移植（0 逻辑差），mini 额外抽了 `_common.py` 共享层，infoquest 是诚实的精简子集。**

| provider | 剥后 mini/up | 逻辑差 |
|---|---|---|
| `ddg_search` | 125 / 124 | **0 逻辑差**——CJK region 推断的 7 个 Unicode 码点范围（jp-ja/kr-ko/cn-zh/ru-ru/gr-el/il-he/xa-ar）逐行一致。差异：mini 调 `_common` 的 `normalize_search_result`/`get_tool_extras`，上游内联等价逻辑（mini 的 `_common` 重构）；变量名 `backend_str`/`text` vs `backend`/`query`；注释中英 |
| `tavily` | 64 / 38 | **0 逻辑差**——3 个函数同。mini 用 `# type: ignore`（软加载 `TavilyClient` 缺包），上游强类型注解。mini 调 `_common` helper |
| `jina_ai` | 70 / 85 | **0 逻辑差**——mini 把 `crawl` 的 timeout/proxy/trust_env/异常归一抽进 `_common.post_json`，上游在 `JinaClient` 内联（等价重构）。proxy 支持（`coerce_proxy` + `crawl(proxy=, trust_env=)`）两边都有 |
| `brave` | 64 / 91 | 忠实——差异是 mini 调 `_common` helper + 上游内联更多强转逻辑（等价） |
| `serper` | 218 / 216 | **0 逻辑差**——`image_search`（Google Images）+ `_safe_public_url` SSRF 守卫（`_decode_ipv4` 解码十进制/十六进制/八进制 IPv4 再判私有 IP）两边都有 |
| `searxng` / `browserless` / `exa` / `firecrawl` / `image_search` / `fastcrw` / `groundroute` | ≈±5 行 | **0 逻辑差**——diff 全是 docstring 中英 + `_common` helper 重构 + 软加载注解 |
| `infoquest` | 187 / 355 | **诚实精简子集**——upstream 有 `image_search`/`image_search_raw_results`/`clean_results_with_image_search`/`_prepare_crawl_request_data` + 更丰富 `__init__`（fetch_time/fetch_timeout/fetch_navigation_timeout/search_time_range/image_size 等时间/尺寸配置）。mini 只保留核心 `web_search` + `web_fetch`，砍掉 image_search/crawl/时间范围配置——教学简化 |
| **mini 新增（上游无）** | — | 顶层 [`_common.py`](../backend/packages/harness/deerflow/community/_common.py)（共享层）+ [`__init__.py`](../backend/packages/harness/deerflow/community/__init__.py)（不 eager import）。上游两个都没有，等价逻辑散在各 provider 内联 |

**旧版「defer / 不 port」的纠正**（按不复读原则，直接讲当前真相）：
1. **fastCRW / GroundRoute / Serper Google Images**——旧文档称「3 个 additive 特性归后续专项、不 port」。**当前 mini 全部已实现**（fastcrw/groundroute/image_search 子包都在，serper 的 image_search + SSRF 守卫也在）。
2. **测试数**——旧文档称「106 个」。**实际 87 个**。
3. **provider 数**——旧文档称「12 个」。**实际 12 个 provider 子包**（brave/browserless/ddg_search/exa/fastcrw/firecrawl/groundroute/image_search/infoquest/jina_ai/searxng/serper/tavily = 13；旧文档的「12」漏数了一个，含 aio_sandbox 则 14）。

**为什么 infoquest 精简、其余忠实？** community 是**纯集成逻辑**——把外部搜索/抓取 API 适配成 LangChain `BaseTool`，输入（config）和输出（JSON 结果）都不依赖 Gateway/IM/auth。多数 provider 靠**软加载**（函数内 import）+ **`_common` 共享层**解耦，砍 Gateway 一行不改，故忠实。infoquest 是唯一 mini 主动精简的（功能多但教学价值边际递减，保留核心 search+fetch 讲透 provider 模式即可）。mini 抽 `_common.py` 是有意识的可维护性改进（DRY），upstream 内联等价逻辑——无行为差。

---

## §11 常见问题 / 排错

**Q：装了 ddg_search 但 agent 搜不到东西？**
A：先确认 `config.yaml` 的 `tools[]` 里有 `use: "deerflow.community.ddg_search.tools:web_search_tool"` 这行（没配=没工具）。再确认装了 `ddgs`（`pip install ddgs`）。缺包时工具返 `{"error": "No results found"}` + 日志记安装提示。

**Q：换 provider 要改 agent 代码吗？**
A：不用。只改 `config.yaml` 的 `use:` 路径（如 ddg → tavily）。结果都归一成 `{title,url,snippet}`，agent 无感知。这是 provider 框架的核心价值（可插拔）。

**Q：web_fetch 抓回来的是乱码 HTML？**
A：不会。所有 fetch 工具都经 `ReadabilityExtractor` 提取正文 + 转 markdown + 4KB 截断。装了 `readabilipy`+`markdownify` 质量最好；没装走纯 Python 兜底（剥 `<script>`/`<nav>` 等）。

**Q：免费能用哪个？**
A：`ddg_search`（搜索）+ `jina_ai`（抓取，免费额度）+ `image_search`（图搜）都不用 API key，开箱即用。要更强就上 tavily（要 key）。

**Q：13 个 provider 都要装吗？**
A：不用。config.yaml 里只写你用的那个的 `tools[].use:`。没配的 provider 根本不会被 import（`resolve_variable` 只加载 config 里列的）。软加载保证没装的 provider 不影响其它工具。

**Q：为什么 firecrawl/exa 是「软加载占位」？**
A：它们的 SDK（`firecrawl`/`exa_py`）没装时，工具返可操作安装提示（不崩）。装上 SDK 后自动走真实逻辑。mini 已移植它们的完整逻辑，只是默认不装 SDK——需要时 `pip install firecrawl-py` 即可激活。

**Q：CJK 查询的 region 是怎么定的？**
A：ddg_search 用 wikipedia backend 且 region 是全球 wt-wt 时，按查询里的 Unicode 字符块推断（中文→cn-zh，日语→jp-ja……）。让中文查询去中文 Wikipedia，结果更相关。只在 wikipedia backend 生效，其它 backend 尊重你配的 region。

**Q：infoquest 和别的 provider 有什么不同？**
A：mini 的 infoquest 是精简版——只保留核心 `web_search` + `web_fetch`，砍掉了上游的 image_search/crawl/时间范围配置（功能多但教学价值边际递减）。核心 provider 模式（软加载+归一+配置驱动）和别的 provider 一致。

**Q：为什么 web_search 和 web_fetch 分两步？**
A：search 先找到候选 URL（返回标题/链接/摘要），fetch 再读具体某页全文。agent 自己决定要不要 fetch——摘要够答就省一次抓取（省时间省 token）。
