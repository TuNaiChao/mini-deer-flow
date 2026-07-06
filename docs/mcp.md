# 20. mcp.md — MCP 集成（外部工具协议 / 三传输 / 会话池 / OAuth / mtime 缓存失效）

> **重写日期**：2026-07-05。**对照代码**：`backend/packages/harness/deerflow/mcp/`（6 文件，1457 行）。

> **一句话定位**：MCP（Model Context Protocol）是一个「让 agent 调用外部工具」的开放协议——别人写好的工具服务器（文件系统、数据库、浏览器、Git……）按 MCP 规范暴露，agent 不用改代码就能用。本模块负责**发现**这些外部工具、**按需调用**它们，并处理好**有状态会话**、**OAuth 鉴权**、**配置热更新**这些工程问题。

> **先读谁最省事**：[skills.md](skills.md)（懂「外部内容怎么注入」）。技能是「给人看的指南」（SKILL.md 注入提示），MCP 是「给 agent 调的远程工具」（可执行的函数）。两者都是「agent 能力的外部扩展」，但机制不同。

---

## §1 学完这篇你能回答什么（learning outcomes · 面试视角）

1. **「什么是 MCP？为什么不每个工具自己实现？」** —— 一个让 agent 复用第三方工具服务器的开放协议（像 USB）。能讲清 server/client 分工、为什么协议化比各自实现好。
2. **「有状态的工具调用怎么跨多次调用保活？难点在哪？」** —— stdio 子进程有状态，要会话池按 `(server, thread)` 复用。能讲清 anyio「cancel scope 必须由进入它的同一个 task 退出」约束、为什么需要 owner-task 模型。
3. **「异步服务里一个会话被多个事件循环/线程碰，会有什么问题？怎么干净地关闭？」** —— 跨 task/跨循环关闭崩溃。能讲清 owner 循环追踪、跨循环路由关闭、`run_coroutine_threadsafe`。
4. **「OAuth token 刷新怎么避免多协程并发各刷一次？」** —— 双重检查锁（锁外快查 + 进锁复查）+ 提前刷新（refresh_skew）。能讲清为什么不直接加锁（快路径不该付费）。
5. **「另一个进程改了配置文件，当前进程怎么不重启就生效？」** —— mtime 检测（每次 stat 一下，改过就重载）。能讲清为什么不跨进程事件通知（复杂、要 IPC）。
6. **「依赖包是可选的，怎么做到没装也不崩？」** —— 软加载（函数内懒 import + `try/except ImportError` 返 `[]`）。

---

## §2 零基础先读：名词解释

### §2.1 计算机基础层（不熟这些先看这段）

| 名词 | 一句话解释 |
|---|---|
| **协议（protocol）** | 两方通信前约定好的「规矩」（消息格式、谁先发、怎么应答）。MCP 就是「工具服务器 ↔ agent 客户端」的规矩，像 USB 是「设备 ↔ 电脑」的规矩。 |
| **stdio** | 「标准输入/输出」——进程默认的两个通道（键盘进、屏幕出）。stdio 传输=agent 启动一个子进程，靠这两个通道跟它通信。 |
| **子进程** | 一个进程（agent）启动的另一个进程（MCP 服务器）。stdio 传输下，子进程跨工具调用一直活着，所以能保留状态（如浏览器页面）。 |
| **Server-Sent Events（SSE）** | 服务器单向往客户端推消息流的 HTTP 技术名。MCP 的 sse 传输用它。 |
| **OAuth** | 一种「用 token 代替密码」的授权协议。本模块里，agent 拿到 access_token 后在每次请求加 `Authorization: Bearer <token>` 头；token 过期要刷新。 |
| **事件循环 / task / 协程** | 事件循环（event loop）在一个线程里轮流推进很多等待中的任务；**task**是事件循环里跑的一个协程单位；**协程**是 `async def` 定义的、可暂停恢复的函数。本篇会反复用。 |
| **anyio cancel scope** | anyio 库（跨 async/sync 的并发抽象）的一个「可取消区域」。关键约束：**进入它的 task 必须和退出它的 task 是同一个**，否则崩。这是会话池设计的核心难点。 |
| **上下文管理器**（`__aenter__`/`__aexit__`） | 「进入-退出」成对的资源管理协议。`async with x:` 会调 `x.__aenter__()` 进、`x.__aexit__()` 退。MCP `ClientSession` 就是个异步上下文管理器。 |
| **LRU 淘汰** | 「最近最少使用」缓存满了时，丢掉最久没用的。本模块会话池上限 256，满了 LRU 淘汰。 |
| **mtime** | 文件「最后修改时间」。本模块拿它做配置热更新——改过就重载。 |
| **软加载（soft-load）** | 把 `import` 放在函数内部、用 `try/except ImportError` 兜住。依赖包没装时不崩，降级成「不可用」。 |
| **拦截器（interceptor）** | 夹在「请求」和「真正处理」之间的函数，能改请求再放行。本模块用 OAuth 拦截器在每次工具调用前往请求注入鉴权头。 |
| **`run_coroutine_threadsafe`** | 把一个协程提交到**另一个线程的事件循环**上跑、拿到 future。本模块用它跨循环关闭会话。 |

### §2.2 本模块名词

| 名词 | 解释 |
|---|---|
| **MCP 服务器** | 按 MCP 规范暴露一组工具的进程（文件系统、Playwright、Postgres……）。 |
| **三传输** | stdio（本地子进程，有状态）/ sse（HTTP 流）/ http（HTTP 请求）。 |
| **会话池** | 按 `(server, thread_id)` 复用持久会话的池子，仅服务 stdio（http/sse 无状态且跨 task 关不干净）。 |
| **owner task** | 专责持有某个会话生命周期的 task：进入上下文 → 等关闭信号 → 退出上下文，保证进/出同 task（满足 anyio 约束）。 |
| **工具发现** | 连上服务器、查出它有哪些工具（名字、参数 schema），包成 LangChain `BaseTool`。 |
| **`tool_name_prefix`** | 每个 MCP 工具名加 `{server}_` 前缀，防多服务器间工具名撞车。 |

---

## §3 整体结构：它在系统里的位置

```
extensions_config.json (mcpServers + oauth + mcpInterceptors)
        │
        ▼
mcp/client.py  build_servers_config  ──→ {server: {transport, command/url, ...}}
        │                          （stdio→command/args/env；sse/http→url/headers）
        ▼
mcp/tools.py  get_mcp_tools()
   ├─ 注入初始 OAuth 头（连接初始化）
   ├─ MultiServerMCPClient(servers, tool_interceptors=[oauth, ...], tool_name_prefix=True)
   ├─ 按服务器独立发现工具（asyncio.gather + try/except，单个坏 server 不拖累其它）
   ├─ 仅 stdio 工具 → _make_session_pool_tool（每次调用复用池中持久会话）
   └─ 补同步入口（make_sync_tool_wrapper）
        │
        ▼
get_available_tools()（tools/tools.py 的 include_mcp 分支）拼进 agent 工具集
```

**六个文件的职责切分**（为什么这么拆见 [§9 设计动机](#9-设计动机分析为什么这么设计作用好处)）：

```
mcp/
├── __init__.py            # 导出公共 API
├── client.py              # build_server_params（单服务器参数）+ build_servers_config（全服务器）
├── oauth.py               # OAuthTokenManager（缓存+提前刷新+双检锁）+ build_oauth_tool_interceptor + get_initial_oauth_headers
├── session_pool.py        # MCPSessionPool（owner-task 生命周期 + LRU 256 + 跨循环关闭）+ 单例
├── tools.py               # get_mcp_tools（发现+stdio 包会话池+#3597 虚拟路径翻译+补同步入口）+ _convert_call_tool_result
└── cache.py               # _mcp_tools_cache + mtime 失效 + initialize/get_cached/reset

（接入点，不在本包内）
config/extensions_config.py  # McpServerConfig + McpOAuthConfig + get_enabled_mcp_servers + get_oauth_servers + resolve_config_path + resolve_env_variables
tools/tools.py               # get_available_tools 接 MCP（include_mcp 分支）
tools/sync.py                # make_sync_tool_wrapper（异步工具→同步入口）
```

**面试概念地图**：本篇对应「外部工具集成 / MCP 协议」「异步并发设计（anyio cancel scope / 跨循环）」「可插拔架构（软加载 / 拦截器）」三个面试常考点。`deerflow-book` 的 `16-mcp-extensions.md` 是可选概念预读。

---

## §4 核心概念：MCP 是什么

agent 自带的内置工具（bash、read_file、web_search……）数量有限。但现实里有大量现成工具：Playwright（浏览器自动化）、文件系统、Postgres、Slack、GitHub…… 如果每个都要 agent 框架自己实现，成本高且重复造轮子。

MCP（Model Context Protocol）是 Anthropic 提出的**开放协议**，定义了「工具服务器 ↔ agent 客户端」怎么通信：
- **工具服务器**（MCP server）：按 MCP 规范暴露一组工具（每个工具有名字、参数 schema、实现）。
- **agent 客户端**（MCP client）：连上服务器，**发现**它有哪些工具，然后把这些工具**当成自己的工具**调用。

**类比**：MCP 像「USB 协议」——任何符合 USB 规范的设备（键盘/硬盘/摄像头）插上就能用，电脑不用为每个设备写专用驱动。mini 经 [`langchain-mcp-adapters`](https://github.com/langchain-ai/langchain-mcp-adapters) 适配器集成：适配器把 MCP 工具包成 LangChain `BaseTool`，agent 无感知调用。

**三种传输**：

| 传输 | 怎么连 | 典型场景 | 有状态？ |
|------|--------|----------|----------|
| **stdio** | agent 启动一个本地子进程，靠标准输入/输出通信 | 本地工具（Playwright、文件系统） | **是**（子进程跨调用保活） |
| **sse** | 连一个 HTTP 端点，靠 Server-Sent Events 流通信 | 远程工具，旧规范 | 否 |
| **http** | 普通 HTTP 请求/响应 | 远程工具，新规范 | 否 |

关键差异在 **stdio 有状态**：子进程起一次就一直活着，工具调用间的状态（如 Playwright 打开的浏览器页面）保留。这个差异决定了**会话池只服务 stdio**（见 §5.3）。

`extensions_config.json` 里这样配：

```json
{
  "mcpServers": {
    "filesystem": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
    },
    "my-api": {
      "type": "http",
      "url": "https://my-api.example.com/mcp",
      "oauth": {"token_url": "https://idp/token", "client_id": "...", "client_secret": "..."}
    }
  }
}
```

---

## §5 代码走读：重要函数逐个讲

### §5.1 client.py —— 配置 → 客户端参数

**`build_server_params()`** [client.py:17](../backend/packages/harness/deerflow/mcp/client.py#L17)：组装单个服务器的传输参数——stdio→`command/args/env`，sse/http→`url/headers`，校验必填字段（stdio 缺 command / sse-http 缺 url 抛 ValueError）。**`build_servers_config()`** [client.py:52](../backend/packages/harness/deerflow/mcp/client.py#L52)：遍历所有启用服务器，坏配置记 warning 跳过（不拖垮其它）。

### §5.2 oauth.py —— OAuth token 管理 + 头注入

**`OAuthTokenManager`** [oauth.py:31](../backend/packages/harness/deerflow/mcp/oauth.py#L31)：每个 server 一把 `asyncio.Lock` + 双检锁防并发刷新。**`get_authorization_header()`** [oauth.py:54](../backend/packages/harness/deerflow/mcp/oauth.py#L54)：

```python
token = self._tokens.get(server_name)
if token and not self._is_expiring(token, oauth):       # 锁外快路径
    return f"{token.token_type} {token.access_token}"
lock = self._locks[server_name]
async with lock:                                          # 进锁
    token = self._tokens.get(server_name)                # 再查一次（防多协程排队）
    if token and not self._is_expiring(token, oauth):
        return f"{token.token_type} {token.access_token}"
    fresh = await self._fetch_token(oauth)               # 只刷一次
    self._tokens[server_name] = fresh
    return f"{fresh.token_type} {fresh.access_token}"
```

`_is_expiring()` [oauth.py:79](../backend/packages/harness/deerflow/mcp/oauth.py#L79)：token 过期前 `refresh_skew_seconds`（默认 60s）提前刷新。`_fetch_token()` [oauth.py:85](../backend/packages/harness/deerflow/mcp/oauth.py#L85) 支持 `client_credentials`（需 client_id/secret）和 `refresh_token`（需 refresh_token）两种授权类型，token 响应字段名可覆写（不同 IdP 字段名不同）。

**`build_oauth_tool_interceptor()`** [oauth.py:140](../backend/packages/harness/deerflow/mcp/oauth.py#L140)：构造工具拦截器——每次工具调用前往请求注入 `Authorization` 头（`request.override(headers=...)`，[oauth.py:153](../backend/packages/harness/deerflow/mcp/oauth.py#L153)）。`get_initial_oauth_headers()` [oauth.py:165](../backend/packages/harness/deerflow/mcp/oauth.py#L165) 为连接初始化（工具发现/会话建立）提供初始鉴权头。

### §5.3 session_pool.py —— 有状态会话池（owner-task 模型，最难的部分）

`MCPSessionPool` [session_pool.py:39](../backend/packages/harness/deerflow/mcp/session_pool.py#L39) 按 `(server_name, scope_key)` 维护持久会话，`MAX_SESSIONS = 256` [session_pool.py:42](../backend/packages/harness/deerflow/mcp/session_pool.py#L42) LRU 淘汰。

**为什么需要 owner task？** MCP `ClientSession` 建在 anyio task group 上，anyio 强制「cancel scope 必须由**进入它的同一个 task** 退出」。而同步工具路径（`make_sync_tool_wrapper`）每次调用走一个全新 `asyncio.run` 循环——于是「调用 A 进入的会话」会在「调用 B 的 task」里退出，跨 task，崩。

**解法** [session_pool.py:74](../backend/packages/harness/deerflow/mcp/session_pool.py#L74)：每个池中会话由**专属 `_run_session` task 持有**——

```
调用方 task:  get_session() ──创建 owner task──→ 等 ready future ──→ 拿到 session ──→ 用
                                                                  │
owner task:   __aenter__ → initialize → 发布 ready → 等 close_evt ──┘
                                                              │ (收到 close 信号)
                                                              ▼
                                                         __aexit__（同 task！）
```

1. 该 task 进入上下文管理器（`__aenter__`）→ 初始化会话 → 经 future 把活会话交回调用方；
2. 然后该 task **阻塞等一个 close 事件**；
3. 所有关闭路径**只信号该事件**，绝不直接调 `__aexit__`；
4. owner task 自己跑 `__aexit__`（在它 `__aenter__` 的同一 task）——满足 anyio 约束。

**`get_session()`** [session_pool.py:112](../backend/packages/harness/deerflow/mcp/session_pool.py#L112) 四阶段：① 线程锁下检查/修改注册表（无 await）；② 关闭被淘汰的会话（同循环 await / 跨循环路由 / 空循环信号）；③ 等.owner 发布会话；④ 提升为注册项。**`close_all_sync()`** [session_pool.py:334](../backend/packages/harness/deerflow/mcp/session_pool.py#L334) 按 owner 循环选对关闭策略（避免死锁）。

### §5.4 tools.py —— 工具发现 + #3597 虚拟路径翻译

**`get_mcp_tools()`** [tools.py:510](../backend/packages/harness/deerflow/mcp/tools.py#L510) 是入口：读 config → 组装 servers_config → 注入初始 OAuth 头 → 构造 `MultiServerMCPClient(tool_name_prefix=True)` → 发现工具 → 仅 stdio 包会话池 → 补同步入口。

**按服务器独立发现**（[tools.py:576-585](../backend/packages/harness/deerflow/mcp/tools.py#L576)，上游 issue #3772）——单个坏 server 不拖累其它：

```python
async def load_server_tools(server_name: str) -> list[BaseTool]:
    try:
        return await client.get_tools(server_name=server_name)
    except Exception as e:
        logger.warning("MCP 服务器 '%s' 工具发现失败，跳过: %s", server_name, e)
        return []                                    # 坏 server 只丢自己
tools_by_server = await asyncio.gather(*(load_server_tools(name) for name in servers_config))
```

旧版 `client.get_tools()` 一把梭——任何一个 server 发现抛错，整个调用被外层 except 吞成 `[]`，**所有** MCP 工具一起丢。现在按 server 独立 + gather 并发 + 每 server try/except：坏 server 返回 `[]` 只丢自己，健康 server 照常贡献。

**`_make_session_pool_tool()`** [tools.py:402](../backend/packages/harness/deerflow/mcp/tools.py#L402)：把一个 stdio MCP 工具包成「复用池中持久会话」的版本——以 `(server, thread_id)` 为 scope 复用会话，剥掉 server 前缀恢复原名发给服务器，应用拦截器链。

**#3597 stdio 虚拟路径翻译**（[tools.py:54-65](../backend/packages/harness/deerflow/mcp/tools.py#L54)）：stdio MCP 服务器（如 Playwright）把文件写到宿主路径，但沙箱/artifact API 只认 `/mnt/user-data` 下的虚拟路径。解法：① 把 stdio 子进程的 cwd/TMPDIR 钉在该线程的 user-data 树里（`_prepare_stdio_workspace` [tools.py:166](../backend/packages/harness/deerflow/mcp/tools.py#L166)），让产物落在可服务目录；② 在结果里把宿主路径**确定性映射**回虚拟前缀（`_local_uri_to_virtual_path` [tools.py:102](../backend/packages/harness/deerflow/mcp/tools.py#L102)）。**不拷贝文件**——cwd 已钉好。安全：只在文件确实落在**本线程 user-data 树内**时才映射（`relative_to`），树外路径原样保留。

### §5.5 cache.py —— mtime 失效 + 懒加载

`get_cached_mcp_tools()` [cache.py:81](../backend/packages/harness/deerflow/mcp/cache.py#L81)：未初始化时自动初始化（懒加载），每次检测配置 mtime 是否变了——变了就 `reset_mcp_tools_cache()` 重置。`_is_cache_stale()` [cache.py:36](../backend/packages/harness/deerflow/mcp/cache.py#L36) 比对当前 mtime 与缓存时记录的 mtime。

```python
# Python 3.14：get_event_loop() 已废弃，改用 get_running_loop 检测
try:
    running = asyncio.get_running_loop()
except RuntimeError:
    running = None
if running is not None and running.is_running():
    # 循环在跑（如 LangGraph Studio）—— 在线程里开新循环跑
    with concurrent.futures.ThreadPoolExecutor() as executor:
        future = executor.submit(asyncio.run, initialize_mcp_tools())
        future.result()
else:
    asyncio.run(initialize_mcp_tools())
```

`reset_mcp_tools_cache()` [cache.py:124](../backend/packages/harness/deerflow/mcp/cache.py#L124)：清缓存 + 关所有持久会话（它们持有旧连接配置）。

---

## §6 数据流：一次调用怎么走完

### §6.1 数据流 A：agent 启动 → 发现所有 MCP 工具

```
① agent 装配 → get_available_tools(include_mcp=True)
② get_cached_mcp_tools() → 懒加载（首次）或 mtime 命中（后续）
   └─ initialize_mcp_tools() → get_mcp_tools()
        ├─ ExtensionsConfig.from_file() → build_servers_config → {server: params}
        ├─ get_initial_oauth_headers → 注入连接初始 Authorization 头
        ├─ build_oauth_tool_interceptor → OAuth 拦截器
        ├─ resolve_variable 加载 mcpInterceptors 自定义拦截器
        ├─ MultiServerMCPClient(servers, interceptors, tool_name_prefix=True)
        ├─ 按服务器独立 client.get_tools(server_name=...) + asyncio.gather  ← #3772 隔离
        │      坏 server 返 []，健康 server 贡献工具
        ├─ 仅 stdio 工具 → _make_session_pool_tool（包持久会话）
        └─ 补同步入口（make_sync_tool_wrapper）
③ 工具集拼进 agent（含 filesystem_read_file 等带 server 前缀的 MCP 工具）
```

### §6.2 数据流 B：用户让 agent 用 Playwright 截图 → 跨调用保活

```
① 第 1 轮：agent 调 playwright_browser_navigate("https://example.com")
   └─ _make_session_pool_tool 的 call_with_persistent_session
        ├─ thread_id = _extract_thread_id(runtime) = "thread-42"
        ├─ _prepare_stdio_workspace：cwd/TMPDIR 钉到 thread-42 的 user-data 树
        ├─ pool.get_session("playwright", "thread-42", connection)
        │    └─ 首次：创建 owner task → __aenter__ → initialize → 发布 ready → 等 close_evt
        ├─ session.call_tool("browser_navigate", {...}) → 浏览器打开页面（状态保留在子进程）
        └─ _convert_call_tool_result：#3597 把结果里的本地路径映射成 /mnt/user-data/...
② 第 2 轮：agent 调 playwright_browser_take_screenshot
   └─ pool.get_session("playwright", "thread-42", ...) → 命中池 → 复用同一会话
        └─ 子进程没重启，之前打开的页面还在 → 截图成功
③ 第 4 轮（不同线程 thread-99）：复用不了 thread-42 的会话 → 建新会话（隔离）
```

---

## §7 配置与用法

### §7.1 配置（`extensions_config.json`）

```json
{
  "mcpServers": {
    "filesystem": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
      "description": "本地文件系统访问"
    },
    "my-api": {
      "type": "http",
      "url": "https://api.example.com/mcp",
      "oauth": {
        "token_url": "https://idp.example.com/oauth/token",
        "grant_type": "client_credentials",
        "client_id": "$MY_CLIENT_ID",
        "client_secret": "$MY_CLIENT_SECRET",
        "scope": "tools:read",
        "refresh_skew_seconds": 120
      }
    }
  },
  "mcpInterceptors": ["myorg.audit:build_audit_interceptor"]
}
```

`$MY_CLIENT_ID` 自动从环境变量展开（`resolve_env_variables`）。mini 兼容 deer 原生格式（`mcpServers` dict）和 mini 早期格式（`mcp_servers` list），内部统一成 `list[McpServerConfig]`。`McpOAuthConfig` [extensions_config.py:29](../backend/packages/harness/deerflow/config/extensions_config.py#L29) 字段：token_url / grant_type / client_id / client_secret / refresh_token / scope / audience / token_field / token_type_field / expires_in_field / refresh_skew_seconds / extra_token_params。

### §7.2 跑测试

```bash
cd backend && make test    # 含 test/test_mcp.py（109 个 hermetic 测试）
```

测试约定：`langchain-mcp-adapters` / `mcp` / `httpx` 均**未安装**——用 `sys.modules` 注入 fake 模块 + monkeypatch，零网络零子进程。conftest autouse 每测前后 `reset_mcp_tools_cache`（清缓存 + 会话池单例）。

---

## §8 与其它模块的关系

```
config/extensions_config (mcpServers + oauth + mcpInterceptors + resolve_config_path + resolve_env_variables)
   │
mcp
   ├── client (build_servers_config：extensions_config → MultiServerMCPClient 参数)
   ├── oauth (HTTP/SSE 鉴权：token 刷新 + 头注入)
   ├── session_pool (stdio 有状态会话：owner-task + LRU 256)
   ├── tools (get_mcp_tools：发现 + stdio 包池 + #3597 虚拟路径 + 补同步入口 + soft-load)
   └── cache (mtime 失效 + 懒加载 + reset 关池)
          │
tools/tools.py (get_available_tools：include_mcp 分支拼进工具集)
tools/sync (make_sync_tool_wrapper：异步工具→同步入口)
   │
▼ 消费者：tool_search（延迟工具装配，MCP 工具体量大默认不绑定）
          DeferredToolFilterMiddleware（按 thread 提升延迟工具）
```

- **上游**：[config](config.md) extensions_config（mcpServers/oauth/mcpInterceptors/resolve_config_path）、`reflection.resolve_variable`（自定义 interceptor builder）、`langchain-mcp-adapters`（soft-load）。
- **下游消费者**：[tools](tools.md)（`get_available_tools` include_mcp 分支）、`tool_search`（延迟工具装配——MCP 工具体量大，默认不绑定，agent 用 tool_search 按需发现）、DeferredToolFilterMiddleware（按 thread 提升延迟工具）。

---

## §9 设计动机分析（为什么这么设计 / 作用 / 好处）

### §9.0 核心设计动机一览

| 关键机制 | 为什么这么设计 | 作用 / 好处 | 不这么设计会怎样 |
|---|---|---|---|
| **会话池仅服务 stdio** | http/sse 的 anyio TaskGroup 无法跨 task 关闭 | stdio 有状态保活，http/sse 本就无状态无损失 | http/sse 入池 → 清理时跨 task 关 TaskGroup 崩 |
| **owner-task 模型** | anyio 要求 cancel scope 进/出同 task | 每个会话由专属 task 持有，进/出永远同 task | 同步路径每次新 asyncio.run → 会话跨 task 退出崩 |
| **按 server 独立发现（#3772）** | 一个坏 server 不该连累其它 | gather 并发 + 每 server try/except，坏 server 只丢自己 | 一把梭 → 一个坏 server 全丢 |
| **OAuth 双检锁刷新** | 多协程同时发现过期会各刷一次 | 锁外快查 + 进锁复查，只刷一次 | 并发刷多次 → 浪费 + IdP 限流 |
| **mtime 失效** | Gateway（另一进程）改配置要立即生效 | 每次 stat 一下，改过重载，零基础设施 | 跨进程事件通知复杂（IPC/消息队列）；不检测要重启 |
| **软加载** | mcp 是可选依赖 | 缺包返 [] + 安装提示，不影响其它工具 | 模块顶层 import → 缺包整个 agent 起不来 |
| **tool_name_prefix** | 多服务器工具名可能撞车 | `{server}_` 前缀防撞 | 同名工具按 name 去重时丢一个 |
| **cwd/TMPDIR 钉 user-data（#3597）** | stdio 产物要被沙箱/artifact API 服务 | 产物落在可服务目录，结果里确定性映射成虚拟路径 | 产物写在不可达宿主路径，agent 看不到 |

### §9.1 为什么会话池只服务 stdio

**动机**：http/sse 传输内部用 anyio TaskGroup 管理连接生命周期。TaskGroup 要求「从进入它的 task 关闭」，而会话池的关闭路径可能来自不同 task（别的工具调用、缓存重置、进程退出）。跨 task 关 TaskGroup 会抛 `RuntimeError`。

**作用 / 好处**：http/sse 工具**不包会话池**，每次调用走适配器自己的临时会话。代价是 http/sse 工具无状态——但它们本来就无状态（每次请求独立），所以无损失。

**不这么设计会怎样**：http/sse 入池 → 清理时跨 task 关 TaskGroup 崩。

### §9.2 为什么需要 owner-task 模型（最难的点）

**动机**：anyio 强制「cancel scope 必须由进入它的同一个 task 退出」。在非 owner task 上调 `cm.__aexit__` 会抛 `RuntimeError: Attempted to exit cancel scope in a different task`。而同步工具路径（`make_sync_tool_wrapper`）每次调用走一个全新 `asyncio.run` 循环——于是「调用 A 进入的会话」会在「调用 B 的 task」里退出，跨 task，崩。

**作用 / 好处**：每个池中会话由专属 `_run_session` task 持有：进入上下文 → 把活会话交回调用方 → 等关闭事件。所有关闭路径只**信号**该事件，owner task 自己跑 `__aexit__`，保证进/出永远同 task。

**不这么设计会怎样**：同步工具路径每次新 `asyncio.run` → 会话跨 task 退出 → anyio 崩 + 泄漏会话/子进程。

**为什么 inflight owner 用 cancel=True**：在建会话（inflight）的 owner 可能卡在 `initialize()` 里，`close_evt` 唤不醒它，所以淘汰/关闭 inflight 时用 `cancel=True` 强制解除；它的 `finally` 仍在自己 task 跑 `__aexit__`，满足 anyio 要求。普通已注册会话用温和的 `close_evt.set()`。

### §9.3 为什么按 server 独立发现（#3772）

**动机**：旧版 `client.get_tools()` 一把梭——任何一个 server 的工具发现抛错，整个调用抛异常被外层 except 吞成 `[]`，**所有** MCP 工具一起丢。

**作用 / 好处**：按 server 独立 `get_tools(server_name=...)` + `asyncio.gather` 并发 + 每 server try/except：坏 server 返回 `[]` 只丢自己，健康 server 照常贡献工具。`asyncio.gather` 并发发起（不串行等慢 server）。

**不这么设计会怎样**：一个坏 server → 所有 MCP 工具消失（用户配了 5 个 server，一个挂了，全没了）。

### §9.4 为什么 mtime 失效而不是事件通知

**动机**：Gateway API 和 LangGraph runtime 是**不同进程**（Gateway 改配置，runtime 读配置）。

**作用 / 好处**：mtime 检测零基础设施——每次读缓存时 `stat` 一下文件（微秒级），改过就重载。重置时关所有持久会话（它们持有旧连接配置），下次按新配置重建。

**不这么设计会怎样**：跨进程事件通知复杂（要 IPC / 文件锁 / 消息队列）；不检测要重启进程才生效。

### §9.5 为什么 OAuth 用双检锁刷新

**动机**：多协程同时发现 token 过期，会同时进 `_fetch_token`，各自发一次 token 请求（浪费 + 可能触发 IdP 限流）。

**作用 / 好处**：双检锁——锁外先看缓存（快路径，未过期直接返，不付费）；过期才进锁；**进锁后再看一次**（慢路径），防止多协程排队各刷一次。第二个进锁的协程看到第一个刷好的新 token，直接用。加上 `refresh_skew_seconds`（默认 60s）提前刷新，避免请求途中 token 突然失效。

**不这么设计会怎样**：每次都加锁 → 快路径也付费，性能差；不加锁 → 并发刷多次浪费 + 限流。

### §9.6 为什么软加载 + 六文件拆分

**动机**：`langchain-mcp-adapters` 是可选依赖，可能没装。没装时 agent 其它工具还要正常用。

**作用 / 好处**：所有 `langchain_mcp_adapters` / `mcp` 的 import 都在**函数内部懒加载**，缺包 `ImportError` → `get_mcp_tools()` 返回 `[]` + 记可操作安装提示。六文件按「传输配置 / 鉴权 / 会话池 / 发现 / 缓存」切分责任——改一处不牵连另一处（见 [§3 文件结构](#3-整体结构它在系统里的位置)）。

**不这么设计会怎样**：模块顶层 import → 缺包整个 agent 起不来；一锅文件 → 改 OAuth 误伤会话池。

---

## §10 实现差异（vs 上游 deer-flow 源码）

> 对照 `deer-flow/backend/packages/harness/deerflow/mcp/`（与 mini 同布局，6 文件）。**先剥 docstring/comment 再判逻辑差**。

**总结论：高度忠实移植，且 mini 在一处（cache.py）比上游更优。** 剥 docstring 后逐文件比对：

| 文件 | 剥后 mini/up | 逻辑差 |
|---|---|---|
| `client.py` | 36 / 35 | **0 逻辑差**（docstring/log 中英） |
| `session_pool.py` | 243 / 244 | **0 逻辑差**——owner-task 模型 / LRU 256 / 四阶段 get_session / 跨循环关闭 / `SESSION_CLOSE_TIMEOUT=5.0` 全一致。唯一差：上游 import `ClientSession` 类型用在注解上（`Future[ClientSession]`、`-> ClientSession`），mini 用 `Any`——**软加载适配**（mcp 是可选包，类型 import 缺包会崩，故用 `Any`）。无行为差 |
| `oauth.py` | 104 / 108 | **0 逻辑差**——双检锁 / 提前刷新 / grant_type 两类 / 拦截器头注入全一致。唯一差：mini 的 `from_extensions_config` 调 `extensions_config.get_oauth_servers()` helper，上游内联同一段 for 循环——mini 提取了 helper，**等价重构** |
| `cache.py` | 79 / 77 | **1 处 mini 改进**——mini 用 `asyncio.get_running_loop()`（Python 3.14 安全），上游用已废弃的 `asyncio.get_event_loop()`（3.14 会 DeprecationWarning 且行为变）。mini 的懒加载分支更简洁（有运行循环→线程池跑 `asyncio.run`；无→直接 `asyncio.run`），上游多一层 `loop.run_until_complete + RuntimeError` 兜底。**mini 更正确**。其余全是 docstring/log 中英 |
| `tools.py` | 438 / 426 | **核心逻辑忠实，两边都有 #3597 + #3772**。差异：① paths.py API 名——mini `thread_user_data_dir(user_id, thread_id)`、上游 `sandbox_user_data_dir(thread_id, user_id=...)`（mini 合并 paths.py 时方法名不同，[config.md](config.md) §9）；② 资源链接解析——上游抽 `_resolve_link_url` helper，mini 内联在 `_convert_call_tool_result` 里（结构差，结果一致）；③ 类型注解 mini 用 `Any`/`Iterable`、上游强类型；④ 函数顺序（`_extract_thread_id` 位置）；⑤ import 单行/多行格式。**无行为差** |
| `__init__.py` | 26 / 14 | mini **多导出公共符号**（`build_server_params` 等）——API 面差异 |

**两个旧版「defer / 不 port」的纠正**（按不复读旧版本原则，直接讲当前真相）：
1. **#3597 stdio 虚拟路径翻译**——旧文档称「归后续专项、不 port」。**当前 mini tools.py 已完整实现**（~370 行：cwd/TMPDIR 钉 user-data + 调前快照 + 调后 diff + 宿主→虚拟路径映射 + 裸文件名关联重写），与上游一致。
2. **测试数**——旧文档称「91 个」。**实际 109 个**。

**为什么这么干净？** MCP 模块是**纯集成逻辑**——它把 `langchain-mcp-adapters` 的能力适配进 mini 的工具系统，输入（extensions_config）和输出（BaseTool 列表）都不依赖 Gateway/IM/auth。靠**软加载**（函数内 import）、**单例工厂**（session_pool/cache）、**拦截器链**解耦，砍 Gateway 一行不改。mini 的两处「非忠实」都是有意识的改进/适配：cache.py 的 Python 3.14 适配（更优）、session_pool/tools 的 `Any` 注解（软加载必需）。

---

## §11 常见问题 / 排错

**Q：MCP 和技能（skill）有什么区别？**
A：技能是「**给人看的操作指南**」（SKILL.md 正文注入提示）；MCP 是「**给 agent 调的远程工具**」（可执行的函数）。技能影响 agent「怎么做」，MCP 给 agent「能做什么」。技能靠激活注入，MCP 靠发现加载。

**Q：没装 `langchain-mcp-adapters` 会怎样？**
A：`get_mcp_tools()` 返回 `[]` + 记一条可操作安装提示。其它工具（bash、内置工具等）正常用。装上即可：`pip install langchain-mcp-adapters`（mini 的 `mcp` extra）。

**Q：stdio 工具为什么能跨调用保活？**
A：会话池为每个 `(server, thread_id)` 维护一个**专属 owner task** 持有的持久会话。同一线程再调同一服务器的工具，复用那个会话——子进程没重启，状态（如 Playwright 浏览器页面）还在。

**Q：http/sse 工具为什么不保活？**
A：它们的 anyio TaskGroup 无法跨 task 关闭，入池会在清理时崩。而 http/sse 本就无状态（每次请求独立），不保活无损失。

**Q：改了 extensions_config.json 要重启吗？**
A：不用。`get_cached_mcp_tools()` 检测文件 mtime 变化，自动 reset 重新初始化。所有持久会话被关闭（它们持有旧连接配置），下次按新配置重建。

**Q：OAuth token 会突然失效吗？**
A：不会。token 过期前 `refresh_skew_seconds`（默认 60s）就提前刷新。双检锁保证多协程不会并发刷多次。

**Q：会话池会无限增长吗？**
A：不会。上限 256（`MAX_SESSIONS`），LRU 淘汰最久未用的。淘汰时按 owner 循环选对关闭策略（同循环 await / 跨循环路由 / 空循环信号），保证不泄漏会话或 owner task。

**Q：会话池的 owner task 是什么？为什么这么设计？**
A：anyio 要求 cancel scope 进出同 task。owner task 专责「进入会话上下文 → 等关闭信号 → 退出上下文」，保证 `__aenter__` 和 `__aexit__` 永远在同一 task。没有这个设计，同步工具路径（每次调用一个新 `asyncio.run` 循环）会让会话跨 task 退出而崩。

**Q：工具名为什么有前缀？**
A：`tool_name_prefix=True` 给每个 MCP 工具名加 `{server_name}_` 前缀（如 `filesystem_read_file`），防多服务器间工具名撞车。`_make_session_pool_tool` 调用时再剥掉前缀恢复原名发给服务器。

**Q：一个 MCP 服务器挂了，会影响其它服务器吗？**
A：不会。工具发现按服务器独立（`get_tools(server_name=...)` + `asyncio.gather` + 每 server try/except）：坏 server 只丢自己的工具，健康 server 照常贡献。

**Q：stdio MCP 工具写的文件，agent 能在沙箱里看到吗？**
A：能。#3597 把 stdio 子进程的 cwd/TMPDIR 钉在该线程的 user-data 树内，产物落在沙箱/artifact API 能服务的位置；结果里的本地路径确定性映射成 `/mnt/user-data/...` 虚拟路径（仅树内文件映射，树外路径原样保留不动）。
