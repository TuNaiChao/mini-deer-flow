"""社区扩展模块（按需启用、soft-load 外部 SDK）。

本包放两类「按需启用」的扩展能力：

- ``aio_sandbox/`` —— AIO 容器沙箱（M10b，生产容器隔离）。soft-load ``agent_sandbox`` SDK。
- **联网 provider（M21）** —— 12 个 web 搜索/抓取 provider：
  - 核心（已完整实现）：``ddg_search``（无需 key）、``tavily``（search+fetch）、``jina_ai``（reader 抓取）。
  - 全量移植（httpx/ddgs）：``image_search``、``brave``、``serper``、``searxng``、``browserless``。
  - 软加载占位（SDK 缺包返可操作错误）：``firecrawl``、``exa``、``infoquest``。
  - 共享层 ``_common.py``（结果归一 + 4KB 截断 + async httpx 封装 + 通用参数强转）。

设计约定：每个 provider 的外部依赖（``ddgs`` / ``tavily`` / ``httpx`` / ``firecrawl`` / ``exa_py`` /
``requests`` …）一律 ``try/except ImportError`` 软加载——**模块顶层不 import SDK**，SDK import
放在工具函数体里，缺包时返可操作安装提示（红线 #24）。这样 ``tools[].use:
"deerflow.community.<provider>.tools:<tool>"`` 路径永远能经 ``resolve_variable`` resolve，
真正调用才检测 SDK。本包**不**在 ``__init__`` 里 eager import 子模块——否则缺任一 SDK 就让
``import deerflow.community`` 炸；子模块由消费者（M15 ``get_available_tools`` 经 config）按需 import。
"""
