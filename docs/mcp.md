# 20. mcp.md — MCP 集成（外部工具协议 / 三传输 / 会话池 / OAuth / mtime 缓存失效）

> **一句话定位**：MCP（Model Context Protocol）是一个「让 agent 调用外部工具」的开放协议——
> 别人写好的工具服务器（文件系统、数据库、浏览器、Git……）按 MCP 规范暴露，agent 不用改代码
> 就能用。本模块负责**发现**这些外部工具、**按需调用**它们，并处理好**有状态会话**、
> **OAuth 鉴权**、**配置热更新**这些工程问题。

读完 [skills.md](skills.md)（懂了「外部内容怎么注入」）再看本篇最省事——技能是「给人看的指南」，
MCP 是「给 agent 调的远程工具」。两者都是「agent 能力的外部扩展」，但机制不同。

---

## 什么是 MCP（为什么需要它）

agent 自带的内置工具（bash、read_file、web_search……）数量有限。但现实里有大量现成工具：
Playwright（浏览器自动化）、文件系统、Postgres、Slack、GitHub…… 如果每个都要 agent 框架自己实现，
成本高且重复造轮子。

MCP（Model Context Protocol）是 Anthropic 提出的**开放协议**，定义了「工具服务器 ↔ agent 客户端」
怎么通信：

- **工具服务器**（MCP server）：按 MCP 规范暴露一组工具（每个工具有名字、参数 schema、实现）。
- **agent 客户端**（MCP client）：连上服务器，**发现**它有哪些工具，然后把这些工具**当成自己的工具**调用。

类比：MCP 像「USB 协议」——任何符合 USB 规范的设备（键盘/硬盘/摄像头）插上就能用，电脑不用为
每个设备写专用驱动。MCP 让 agent 插上任何 MCP 服务器就能用它的工具。

mini 经 [`langchain-mcp-adapters`](https://github.com/langchain-ai/langchain-mcp-adapters) 适配器
集成 MCP：适配器把 MCP 工具包成 LangChain `BaseTool`，agent 无感知地调用。

---

## 三种传输（stdio / sse / http）

MCP 服务器有三种「传输方式」（客户端怎么连服务器）：

| 传输 | 怎么连 | 典型场景 | 有状态？ |
|------|--------|----------|----------|
| **stdio** | agent 启动一个**本地子进程**，靠标准输入/输出通信 | 本地工具（Playwright、文件系统） | **是**（子进程跨调用保活） |
| **sse** | 连一个 HTTP 端点，靠 Server-Sent Events 流通信 | 远程工具，旧规范 | 否 |
| **http** | 普通 HTTP 请求/响应 | 远程工具，新规范 | 否 |

关键差异在 **stdio 有状态**：子进程起一次就一直活着，工具调用间的状态（如 Playwright 打开的浏览器
页面）保留。而 sse/http 每次调用是独立请求。这个差异决定了**会话池只服务 stdio**（见下）。

`extensions_config.json` 里这样配（deer 原生格式，mini 也兼容 mini 早期 list 格式）：

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
      "headers": {"X-Token": "..."},
      "oauth": {"token_url": "https://idp/token", "client_id": "...", "client_secret": "..."}
    }
  }
}
```

---

## 核心数据流（发现 → 调用）

```
extensions_config.json (mcpServers)
        │
        ▼
build_servers_config  ──→ {server: {transport, command/url, ...}}
        │                          （stdio→command/args/env；sse/http→url/headers）
        ▼
MultiServerMCPClient(servers_config, tool_interceptors=[oauth, ...], tool_name_prefix=True)
        │
        ▼  await client.get_tools()
发现工具（经临时会话）──→ [BaseTool, BaseTool, ...]
        │
        ▼  仅 stdio 工具
_make_session_pool_tool  ──→ StructuredTool（每次调用复用池中持久会话）
        │
        ▼
get_available_tools() 拼进 agent 工具集
```

`tool_name_prefix=True`：每个 MCP 工具名加 `{server_name}_` 前缀（如 `filesystem_read_file`），
防多服务器间工具名撞车（红线 #18 工具按 name 去重的延伸）。

---

## 四大工程问题（本模块的全部复杂度）

MCP 「发现+调用」本身不复杂，复杂的是四个工程问题：

### ① 会话池（session_pool）—— stdio 有状态工具的跨调用保活

**问题**：Playwright 这类 stdio 服务器**有状态**——打开的浏览器页面、填好的表单要跨工具调用保留。
但 `langchain-mcp-adapters` 默认每次调用建新会话（`session=None`），状态全丢。

**解法**：`MCPSessionPool` 按 `(server_name, thread_id)` 维护持久会话：

- 同一线程（thread_id）调同一服务器的工具 → 复用同一会话 → 状态保活；
- 不同线程 → 不同会话（隔离）；
- 容量上限 256，LRU 淘汰（防泄漏，红线 #29）；
- **仅 stdio 入池**——http/sse 用 anyio TaskGroup，无法跨 task 关闭，入池会崩（issue #3203）。

**最难的部分——anyio cancel-scope 同 task 约束**：

MCP `ClientSession` 建在 anyio task group 上，anyio 强制「cancel scope 必须由**进入它的同一个 task**
退出」。而同步工具路径（`make_sync_tool_wrapper`）每次调用走一个全新 `asyncio.run` 循环——
于是「调用 A 进入的会话」会在「调用 B 的 task」里退出，跨 task，崩（issue #3379）。

**解法（owner task 模型）**：每个池中会话由**专属 `_run_session` task 持有**：

1. 该 task 进入上下文管理器（`__aenter__`）→ 初始化会话 → 经 future 把活会话交回调用方；
2. 然后该 task **阻塞等一个 close 事件**；
3. 所有关闭路径**只信号该事件**，绝不直接调 `__aexit__`；
4. owner task 自己跑 `__aexit__`（在它 `__aenter__` 的同一 task）——满足 anyio 约束。

```
调用方 task:  get_session() ──创建 owner task──→ 等 ready future ──→ 拿到 session ──→ 用
                                                                  │
owner task:   __aenter__ → initialize → 发布 ready → 等 close_evt ──┘
                                                              │ (收到 close 信号)
                                                              ▼
                                                         __aexit__（同 task！）
```

### ② OAuth —— HTTP/SSE 服务器的鉴权头注入

**问题**：远程 MCP 服务器常要 OAuth 鉴权。token 会过期，要提前刷新；还要在每次工具调用时
注入 `Authorization` 头。

**解法**：

- `OAuthTokenManager`：每个 server 一把 `asyncio.Lock` + 双检锁防并发刷新；token 过期前
  `refresh_skew_seconds` 秒（默认 60）提前刷新（红线 #30）；
- `build_oauth_tool_interceptor`：构造工具拦截器，在每次工具调用前往请求注入 `Authorization` 头
  （经 `request.override(headers=...)`）；
- `get_initial_oauth_headers`：为 server **连接初始化**（工具发现/会话建立）提供初始鉴权头。

支持两种授权类型：`client_credentials`（服务间，需 client_id/secret）和 `refresh_token`
（需 refresh_token）。token 响应字段名可覆写（不同 IdP 字段名不同）。

### ③ mtime 缓存失效 —— 改配置不用重启

**问题**：Gateway API（在另一个进程）改了 `extensions_config.json`（启用/禁用服务器、改 OAuth），
Gateway 内嵌的 LangGraph runtime 要**不重启**就看到新配置。

**解法**：缓存记录初始化时的配置文件 mtime。`get_cached_mcp_tools()` 每次调用检测 mtime 是否
变了——变了就 `reset_mcp_tools_cache()`（关闭所有持久会话，它们持有旧连接配置）→ 重新初始化。

```
首次:  get_cached_mcp_tools() → 懒加载 → 记录 mtime=T1
改配置后: get_cached_mcp_tools() → 检测 mtime=T2 > T1 → reset → 重新初始化
```

### ④ 软加载 —— 缺包不崩

**问题**：`langchain-mcp-adapters` 是可选依赖，可能没装。没装时 agent 其它工具还要正常用。

**解法**：所有 `langchain_mcp_adapters` / `mcp` 的 import 都在**函数内部懒加载**（非模块顶层），
缺包 `ImportError` → `get_mcp_tools()` 返回 `[]` + 记可操作安装提示（红线 #24）。`get_available_tools`
用 `try/except Exception` 兜底，MCP 不可用不影响其它工具组装。

---

## 文件结构

```
mcp/
├── __init__.py            # 导出公共 API
├── client.py              # build_server_params（单服务器参数）+ build_servers_config（全服务器）
├── oauth.py               # OAuthTokenManager（缓存+提前刷新+双检锁）+ build_oauth_tool_interceptor + get_initial_oauth_headers
├── session_pool.py        # MCPSessionPool（owner-task 生命周期 + LRU 256 + 跨循环关闭）+ 单例
├── tools.py               # get_mcp_tools（发现+stdio 包会话池+补同步入口）+ _convert_call_tool_result + _make_session_pool_tool
└── cache.py               # _mcp_tools_cache + mtime 失效 + initialize/get_cached/reset

tools/
└── sync.py                # make_sync_tool_wrapper（异步工具→同步入口；M20 引入，M15 扩展 RunnableConfig 注入）

config/extensions_config.py  # M20 扩展：McpOAuthConfig + oauth/description + resolve_config_path + resolve_env_variables + mcp_interceptors
tools/tools.py               # get_available_tools 接 MCP（include_mcp 分支）
```

---

## 关键接口

```python
# client
def build_server_params(server_name: str, config: McpServerConfig) -> dict[str, Any]: ...
def build_servers_config(extensions_config: ExtensionsConfig) -> dict[str, dict[str, Any]]: ...

# oauth
class OAuthTokenManager:
    @classmethod
    def from_extensions_config(cls, extensions_config) -> OAuthTokenManager: ...
    async def get_authorization_header(self, server_name: str) -> str | None: ...
def build_oauth_tool_interceptor(extensions_config) -> Any | None: ...
async def get_initial_oauth_headers(extensions_config) -> dict[str, str]: ...

# session_pool
class MCPSessionPool:
    MAX_SESSIONS = 256
    async def get_session(self, server_name, scope_key, connection) -> ClientSession: ...
    async def close_scope(self, scope_key: str) -> None: ...   # 关某 thread 的所有会话
    async def close_server(self, server_name: str) -> None: ...
    async def close_all(self) -> None: ...
    def close_all_sync(self) -> None: ...                      # 同步关闭（测试/进程退出）
def get_session_pool() -> MCPSessionPool: ...
def reset_session_pool() -> None: ...

# tools
async def get_mcp_tools() -> list[BaseTool]: ...

# cache
async def initialize_mcp_tools() -> list[BaseTool]: ...
def get_cached_mcp_tools() -> list[BaseTool]: ...              # 懒加载 + mtime 失效
def reset_mcp_tools_cache() -> None: ...                        # 关会话池 + 清缓存
```

---

## 设计原理（权衡 / 不变量 / 踩坑）

### 为什么会话池只服务 stdio

http/sse 传输内部用 **anyio TaskGroup** 管理连接生命周期。TaskGroup 要求「从进入它的 task 关闭」，
而会话池的关闭路径可能来自不同 task（别的工具调用、缓存重置、进程退出）。跨 task 关 TaskGroup 会抛
`RuntimeError`（issue #3203）。所以 http/sse 工具**不包会话池**，每次调用走适配器自己的临时会话。
代价：http/sse 工具无状态——但它们本来就无状态（每次请求独立），所以无损失。

### 为什么 owner task 不能被任意 cancel

owner task 在 `__aenter__` 之后、`__aexit__` 之前，**必须**自己跑完 `__aexit__`。如果外部
`task.cancel()` 打断它在 `initialize()` 中途，它的 `finally` 块仍会跑 `__aexit__`（在自己 task）——
这正是我们想要的。但**在建会话**（inflight）的 owner 可能卡在 `initialize()` 里，`close_evt` 唤不醒它，
所以淘汰/关闭 inflight 时用 `cancel=True` 强制解除。普通已注册会话用温和的 `close_evt.set()`。

### 为什么 mtime 失效而不是事件通知

Gateway API 和 LangGraph runtime 是**不同进程**（Gateway 改配置，runtime 读配置）。跨进程事件通知
复杂（要 IPC / 文件锁 / 消息队列）。mtime 检测零基础设施——每次读缓存时 `stat` 一下文件，改过就重载。
代价：每次 `get_cached_mcp_tools` 多一次 `stat`（微秒级，可忽略）。收益：极简、可靠、无需重启。

### 为什么双检锁刷新 token

多协程同时发现 token 过期，会同时进 `_fetch_token`，各自发一次 token 请求（浪费 + 可能触发 IdP 限流）。
双检锁：锁外先看缓存（快路径），过期才进锁；**进锁后再看一次**（慢路径），防止多协程排队各刷一次。
第二个进锁的协程看到第一个刷好的新 token，直接用。

### `get_event_loop` 的 Python 3.14 陷阱

deer 原版 `cache.py` 用 `asyncio.get_event_loop()`——在 Python 3.14 已废弃（会 DeprecationWarning
且行为变）。mini 改用 `asyncio.get_running_loop()`（只在有运行循环时返，否则 `RuntimeError`）精确检测：
有运行循环 → 卸线程开新循环跑；无 → 直接 `asyncio.run`。避免废弃 API + 行为更确定。

---

## 与其它模块的关系

```
config/extensions_config (mcpServers + oauth + mcpInterceptors + resolve_config_path)
   │
mcp
   ├── client (build_servers_config：extensions_config → MultiServerMCPClient 参数)
   ├── oauth (HTTP/SSE 鉴权：token 刷新 + 头注入)
   ├── session_pool (stdio 有状态会话：owner-task + LRU 256)
   ├── tools (get_mcp_tools：发现 + stdio 包池 + 补同步入口 + soft-load)
   │      ↑ soft-load langchain-mcp-adapters（缺包返 []）
   └── cache (mtime 失效 + 懒加载 + reset 关池)
          │
tools/tools.py (get_available_tools：include_mcp 分支拼进工具集)
tools/sync (make_sync_tool_wrapper：异步工具→同步入口)
   │
▼ 消费者：M15 tool_search（延迟工具装配，标记 MCP 工具供 tool_search 发现）
          M16 DeferredToolFilterMiddleware（按 thread 提升延迟工具，依赖 MCP 工具被标记）
```

- **上游**：`config/extensions_config`（mcpServers/oauth/mcpInterceptors/resolve_config_path）、
  `reflection`（自定义 interceptor builder 的 `resolve_variable`）、`langchain-mcp-adapters`（soft-load）。
- **下游消费者**：M15 `tool_search`（延迟工具装配——MCP 工具体量大，默认不绑定，agent 用
  `tool_search` 按需发现）；M16 `DeferredToolFilterMiddleware`（按 thread 提升延迟工具）。
  这就是为什么 M20 **必须先于 M15**（outline 依赖图）。
- **配置来源**：`extensions_config.json` 的 `mcpServers`（deer 格式 dict）/ `mcp_servers`（mini 早期 list），
  mini `from_file` 兼容两种。

---

## 常见问题 / 排错

**Q：MCP 和技能（skill）有什么区别？**
A：技能是「**给人看的操作指南**」（SKILL.md 正文注入提示）；MCP 是「**给 agent 调的远程工具**」
（可执行的函数）。技能影响 agent「怎么做」，MCP 给 agent「能做什么」。技能靠激活注入，MCP 靠发现加载。

**Q：没装 `langchain-mcp-adapters` 会怎样？**
A：`get_mcp_tools()` 返回 `[]` + 记一条可操作安装提示。其它工具（bash、内置工具等）正常用。
装上即可：`pip install langchain-mcp-adapters`（mini 的 `mcp` extra）。

**Q：stdio 工具为什么能跨调用保活？**
A：会话池为每个 `(server, thread_id)` 维护一个**专属 owner task** 持有的持久会话。同一线程再调
同一服务器的工具，复用那个会话——子进程没重启，状态（如 Playwright 浏览器页面）还在。

**Q：http/sse 工具为什么不保活？**
A：它们的 anyio TaskGroup 无法跨 task 关闭，入池会在清理时崩（issue #3203）。而 http/sse 本就无状态
（每次请求独立），不保活无损失。

**Q：改了 extensions_config.json 要重启吗？**
A：不用。`get_cached_mcp_tools()` 检测文件 mtime 变化，自动 reset 重新初始化。所有持久会话被关闭
（它们持有旧连接配置），下次按新配置重建。

**Q：OAuth token 会突然失效吗？**
A：不会。token 过期前 `refresh_skew_seconds`（默认 60s）就提前刷新。双检锁保证多协程不会并发刷多次。

**Q：会话池会无限增长吗？**
A：不会。上限 256（`MAX_SESSIONS`），LRU 淘汰最久未用的。淘汰时按 owner 循环选对关闭策略
（同循环 await / 跨循环路由 / 空循环信号），保证不泄漏会话或 owner task。

**Q：会话池的 owner task 是什么？为什么这么设计？**
A：anyio 要求 cancel scope 进出同 task。owner task 专责「进入会话上下文 → 等关闭信号 → 退出上下文」，
保证 `__aenter__` 和 `__aexit__` 永远在同一 task。没有这个设计，同步工具路径（每次调用一个新
`asyncio.run` 循环）会让会话跨 task 退出而崩（issue #3379）。

**Q：工具名为什么有前缀？**
A：`tool_name_prefix=True` 给每个 MCP 工具名加 `{server_name}_` 前缀（如 `fs_read_file`），
防多服务器间工具名撞车。`_make_session_pool_tool` 调用时再剥掉前缀恢复原名发给服务器。

---

## 应用方法

### 配置一个 stdio MCP 服务器

`extensions_config.json`：

```json
{
  "mcpServers": {
    "filesystem": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
      "description": "本地文件系统访问"
    }
  }
}
```

### 配置一个带 OAuth 的 http 服务器

```json
{
  "mcpServers": {
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
  }
}
```

`$MY_CLIENT_ID` 自动从环境变量展开（`resolve_env_variables`）。

### 加载自定义工具拦截器

```json
{
  "mcpInterceptors": ["myorg.audit:build_audit_interceptor"]
}
```

`build_audit_interceptor()` 返回一个 `async def interceptor(request, handler)`，会在每次工具调用前跑。
按 `pkg.module:func` 路径经 `resolve_variable` 加载。

### 跑测试

```bash
cd backend && make test    # 含 test/test_mcp.py（91 个 hermetic 测试）
```

测试约定：`langchain-mcp-adapters` / `mcp` / `httpx` 均**未安装**——用 `sys.modules` 注入 fake 模块
+ monkeypatch，零网络零子进程。conftest autouse 每测前后 `reset_mcp_tools_cache`（清缓存 + 会话池单例）。
