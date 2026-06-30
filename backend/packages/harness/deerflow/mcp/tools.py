"""经 ``langchain-mcp-adapters`` 加载 MCP 工具，stdio 会话用池复用。

``get_mcp_tools`` 是入口：读 extensions_config → 组装 servers_config → 注入初始
OAuth 头 → 构造 ``MultiServerMCPClient`` → 发现工具 → **仅 stdio 工具**包一层
持久会话复用（``_make_session_pool_tool``）→ 给纯协程工具补同步入口。

软加载：``langchain-mcp-adapters`` 缺包返回 ``[]`` 并记可操作安装提示（红线 #24）。
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from langchain_core.tools import BaseTool, StructuredTool
from langgraph.config import get_config

from deerflow.config.extensions_config import ExtensionsConfig
from deerflow.config.paths import VIRTUAL_PATH_PREFIX, get_paths
from deerflow.mcp.client import build_servers_config
from deerflow.mcp.oauth import build_oauth_tool_interceptor, get_initial_oauth_headers
from deerflow.mcp.session_pool import get_session_pool
from deerflow.reflection import resolve_variable
from deerflow.runtime.user_context import resolve_runtime_user_id
from deerflow.tools.sync import make_sync_tool_wrapper
from deerflow.tools.types import Runtime

logger = logging.getLogger(__name__)


def _extract_thread_id(runtime: Runtime | None) -> str:
    """从注入的工具 runtime 或 LangGraph config 提取 thread_id（会话池的 scope_key）。"""
    if runtime is not None:
        tid = runtime.context.get("thread_id") if runtime.context else None
        if tid is not None:
            return str(tid)
        config = runtime.config or {}
        tid = config.get("configurable", {}).get("thread_id")
        if tid is not None:
            return str(tid)

    try:
        tid = get_config().get("configurable", {}).get("thread_id")
        return str(tid) if tid is not None else "default"
    except RuntimeError:
        return "default"


# ---------------------------------------------------------------------------
# #3597：stdio MCP 产物文件的虚拟路径翻译（host → /mnt/user-data/...）
#
# stdio MCP 服务器（如 Playwright）把文件写到宿主路径，沙箱/artifact API 只认
# ``/mnt/user-data`` 下的虚拟路径。**先把 stdio 子进程的 cwd / TMPDIR 钉在该线程的
# user-data 树里**（见 ``_make_session_pool_tool``），让产物落在可服务的目录；再在结果里把
# 这些宿主路径**确定性映射**回虚拟前缀（``_local_uri_to_virtual_path``）。**不拷贝文件**——
# cwd 已钉好，文件本来就在该在的地方。
#
# 安全：只在文件确实落在**本线程 user-data 树内**时才映射（``relative_to(user_data_root)``）。
# 树外路径（如 ``/etc/passwd``）原样保留、不映射——agent 看不到它，也不会被服务出去。
# ---------------------------------------------------------------------------

#: stdio 子进程的私有 tmp 子目录（钉在 user-data 树内，让默认走 OS tmp 的工具也写进来）。
_MCP_TMP_SUBDIR = ".mcp/tmp"
#: 在自由文本里抓「可能是本地路径」的 token（保守——抓到的再交给 _local_uri_to_virtual_path 验）。
_LOCAL_PATH_IN_TEXT_RE = re.compile(r"(?:file://)?/[^\s'\"<>|*?]+|(?:\.{0,2}/|[\w.-]+/)[^\s'\"<>|*?]+")
#: 路径尾部的标点 / 标记（不属于路径本身，重写时剥掉再粘回）。
_TEXT_PATH_TRAILING_CHARS = ".,;:!?)]}>\"'`"
#: 工作区文件快照：``{path: (mtime_ns, size)}``。
_FILE_SNAPSHOT = dict[Path, tuple[int, int]]


def _local_path_from_uri(uri: str, *, base_dir: Path | None = None) -> Path | None:
    """uri 指向本地文件时返回绝对 ``Path``，否则 ``None``。

    接受裸路径与 ``file://`` URI。远程 URI（``http``/``https``/``data``…）返 ``None``，让调用方
    原样保留。相对路径仅当给了 *base_dir* 时才解析。
    """
    if not uri:
        return None
    parsed = urlparse(uri)
    if parsed.scheme == "file":
        raw = unquote(parsed.path)
    elif parsed.scheme == "":
        raw = uri
    else:
        return None
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        if base_dir is None:
            return None
        path = base_dir / path
    return path


def _local_uri_to_virtual_path(
    uri: str,
    *,
    thread_id: str,
    user_id: str,
    source_base_dir: Path | None = None,
) -> str | None:
    """把本地文件引用翻成 ``/mnt/user-data/...`` 虚拟路径（纯确定性映射，不拷贝）。

    stdio MCP 服务器的 cwd / tmp 钉在线程的 user-data 树内，所以产物已经在沙箱/artifact API
    能服务的位置——只差其它 DeerFlow 部件寻址它用的虚拟前缀。本函数就做这个 host→virtual 映射。

    URI 是远程 / 解析不了 / 指向**本线程 user-data 树外** / 不是已存在文件时返 ``None``（调用方
    原样保留引用）。相对引用相对 *source_base_dir*（服务器 cwd）解析。
    """
    src = _local_path_from_uri(uri, base_dir=source_base_dir)
    if src is None:
        return None
    try:
        real = src.resolve()
    except OSError:
        return None
    if not real.is_file():
        return None
    try:
        # mini paths：thread_user_data_dir(user_id, thread_id)（注意 user_id 在前，与上游关键字序不同）。
        user_data_root = get_paths().thread_user_data_dir(user_id, thread_id).resolve()
    except OSError:
        return None
    try:
        relative = real.relative_to(user_data_root)
    except ValueError:
        # 文件在本线程 user-data 挂载之外——表达不成虚拟路径，原样保留。
        logger.debug("MCP path rewrite skipped outside user-data tree: %s", real)
        return None
    virtual_path = f"{VIRTUAL_PATH_PREFIX}/{relative.as_posix()}"
    logger.debug("MCP path rewrite: %s -> %s", real, virtual_path)
    return virtual_path


def _snapshot_workspace_files(root: Path) -> _FILE_SNAPSHOT:
    """root 下常规文件的轻量快照（``{path: (mtime_ns, size)}``）。"""
    snapshot: _FILE_SNAPSHOT = {}
    if not root.exists():
        return snapshot
    try:
        for path in root.rglob("*"):
            try:
                stat = path.stat()
            except OSError:
                continue
            if path.is_file():
                snapshot[path] = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        return snapshot
    return snapshot


def _changed_workspace_files(root: Path, before: _FILE_SNAPSHOT) -> list[Path]:
    """返回 *before* 之后新建 / 改动的文件。"""
    after = _snapshot_workspace_files(root)
    return [path for path, signature in after.items() if before.get(path) != signature]


def _prepare_stdio_workspace(paths: Any, *, thread_id: str, user_id: str) -> tuple[Path, Path, _FILE_SNAPSHOT]:
    """为一次钉定的 stdio MCP 子进程调用准备线程工作区。

    把同步文件系统活（建目录、备 tmp、调前快照）捆在一起，让调用方用 ``asyncio.to_thread``
    卸到事件循环外。返回工作区 cwd、钉定的 tmp 目录、调前文件快照。
    """
    paths.ensure_thread_dirs(thread_id, user_id=user_id)
    source_base_dir = paths.sandbox_work_dir(thread_id, user_id=user_id)
    tmp_dir = source_base_dir / _MCP_TMP_SUBDIR
    try:
        tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp_dir.chmod(0o700)
    except OSError:
        logger.warning("Failed to prepare MCP temp dir: %s", tmp_dir, exc_info=True)
    before_files = _snapshot_workspace_files(source_base_dir)
    return source_base_dir, tmp_dir, before_files


def _result_has_text_content(call_tool_result: Any) -> bool:
    """MCP 结果是否带任何文本内容（决定调后要不要做 bare-filename 关联重扫）。"""
    from mcp.types import EmbeddedResource, TextContent, TextResourceContents

    content = getattr(call_tool_result, "content", None)
    if not content:
        return False
    for item in content:
        if isinstance(item, TextContent):
            return True
        if isinstance(item, EmbeddedResource) and isinstance(item.resource, TextResourceContents):
            return True
    return False


def _rewrite_unique_bare_filenames(
    text: str,
    *,
    changed_files: Iterable[Path],
    thread_id: str,
    user_id: str,
    source_base_dir: Path | None = None,
) -> str:
    """仅当本次调用产出了**唯一匹配**的裸文件名时才重写它。

    像 ``Saved as page-2026.yml`` 这种回复结构上不是路径。唯一安全的解读是把文件名与
    **本次工具调用**新建 / 改的文件关联，仅当 basename 在本线程 user-data 树内映射到**恰好一个**
    文件时才重写。
    """
    candidates: dict[str, list[str]] = {}
    for path in changed_files:
        virtual_path = _local_uri_to_virtual_path(
            str(path),
            thread_id=thread_id,
            user_id=user_id,
            source_base_dir=source_base_dir,
        )
        if virtual_path is None:
            continue
        candidates.setdefault(path.name, []).append(virtual_path)

    unique = {name: vpaths[0] for name, vpaths in candidates.items() if len(set(vpaths)) == 1}
    if not unique:
        if candidates:
            logger.debug("MCP bare filename rewrite skipped: no unique candidate in %s", sorted(candidates))
        else:
            logger.debug("MCP bare filename rewrite skipped: no snapshot candidates")
        return text

    rewritten = text
    for name in sorted(unique, key=len, reverse=True):
        # 不在更长的路径 / 单词内重写。允许句末句号，但不允许 ``.bak`` 或另一段路径。
        pattern = re.compile(rf"(?<![\w./-]){re.escape(name)}(?!(?:[\w/-]|\.[\w]))")
        rewritten_text, count = pattern.subn(unique[name], rewritten)
        if count:
            logger.debug("MCP bare filename rewrite: %s -> %s", name, unique[name])
        rewritten = rewritten_text
    return rewritten


def _rewrite_local_paths_in_text(
    text: str,
    *,
    thread_id: str,
    user_id: str,
    source_base_dir: Path | None = None,
    changed_files: Iterable[Path] | None = None,
) -> str:
    """自由文本里本地文件引用的 best-effort 重写。

    有些 MCP 服务器（如 Playwright 的 ``browser_take_screenshot``）把保存的文件仅作为自由文本
    报出（``saved it as temp/page-2026.png``）而非 ``ResourceLink``。自由文本不是可靠协议，故本函数
    刻意保守：每个候选 token 交给 ``_local_uri_to_virtual_path``，**只有**解析成本线程 user-data 树内
    已存在文件时才重写。不是真路径 / 指向别处的 token 原样保留——过度匹配也无害。
    """
    translated_by_source: dict[str, str | None] = {}

    def _replace(match: re.Match[str]) -> str:
        token = match.group(0)
        # 路径可能结束一句（``saved as temp/a.png.``）：剥尾标点、重写后再粘回。
        stripped = token.rstrip(_TEXT_PATH_TRAILING_CHARS)
        trailing = token[len(stripped) :]
        if stripped not in translated_by_source:
            translated_by_source[stripped] = _local_uri_to_virtual_path(
                stripped,
                thread_id=thread_id,
                user_id=user_id,
                source_base_dir=source_base_dir,
            )
        rewritten = translated_by_source[stripped]
        if rewritten is None:
            return token
        return f"{rewritten}{trailing}"

    rewritten = _LOCAL_PATH_IN_TEXT_RE.sub(_replace, text)
    if changed_files is None:
        return rewritten
    return _rewrite_unique_bare_filenames(
        rewritten,
        changed_files=changed_files,
        thread_id=thread_id,
        user_id=user_id,
        source_base_dir=source_base_dir,
    )


def _convert_call_tool_result(
    call_tool_result: Any,
    *,
    thread_id: str | None = None,
    user_id: str | None = None,
    source_base_dir: Path | None = None,
    changed_files: list[Path] | None = None,
) -> Any:
    """把 MCP ``CallToolResult`` 转成 LangChain ``content_and_artifact`` 格式。

    复刻 adapter 的转换逻辑，不依赖私有 ``langchain_mcp_adapters.tools._convert_call_tool_result``。
    """
    from langchain_core.messages import ToolMessage
    from langchain_core.messages.content import (
        create_file_block,
        create_image_block,
        create_text_block,
    )
    from langchain_core.tools import ToolException
    from mcp.types import (
        BlobResourceContents,
        EmbeddedResource,
        ImageContent,
        ResourceLink,
        TextContent,
        TextResourceContents,
    )

    # 拦截器短路：ToolMessage 直接透传。
    if isinstance(call_tool_result, ToolMessage):
        return call_tool_result, None

    # langgraph 装好时 Command 直接透传。
    try:
        from langgraph.types import Command

        if isinstance(call_tool_result, Command):
            return call_tool_result, None
    except ImportError:
        # langgraph 可选；不可用时走标准 MCP content 转换。
        pass

    # 把 MCP content block 转成 LangChain content block。
    # #3597：stdio 调用给全了 thread_id + user_id 时，把结果里的本地文件引用（ResourceLink URI +
    # 文本里的路径 / 裸文件名）确定性映射成 ``/mnt/user-data/...`` 虚拟路径（仅树内文件）。
    rewrite = thread_id is not None and user_id is not None
    lc_content = []
    for item in call_tool_result.content:
        if isinstance(item, TextContent):
            text = item.text
            if rewrite:
                text = _rewrite_local_paths_in_text(
                    text,
                    thread_id=thread_id,  # type: ignore[arg-type]
                    user_id=user_id,  # type: ignore[arg-type]
                    source_base_dir=source_base_dir,
                    changed_files=changed_files,
                )
            lc_content.append(create_text_block(text=text))
        elif isinstance(item, ImageContent):
            lc_content.append(create_image_block(base64=item.data, mime_type=item.mimeType))
        elif isinstance(item, ResourceLink):
            mime = item.mimeType or None
            uri = str(item.uri)
            if rewrite:
                virtual = _local_uri_to_virtual_path(
                    uri,
                    thread_id=thread_id,  # type: ignore[arg-type]
                    user_id=user_id,  # type: ignore[arg-type]
                    source_base_dir=source_base_dir,
                )
                if virtual is not None:
                    uri = virtual
            if mime and mime.startswith("image/"):
                lc_content.append(create_image_block(url=uri, mime_type=mime))
            else:
                lc_content.append(create_file_block(url=uri, mime_type=mime))
        elif isinstance(item, EmbeddedResource):
            res = item.resource
            if isinstance(res, TextResourceContents):
                text = res.text
                if rewrite:
                    text = _rewrite_local_paths_in_text(
                        text,
                        thread_id=thread_id,  # type: ignore[arg-type]
                        user_id=user_id,  # type: ignore[arg-type]
                        source_base_dir=source_base_dir,
                        changed_files=changed_files,
                    )
                lc_content.append(create_text_block(text=text))
            elif isinstance(res, BlobResourceContents):
                mime = res.mimeType or None
                if mime and mime.startswith("image/"):
                    lc_content.append(create_image_block(base64=res.blob, mime_type=mime))
                else:
                    lc_content.append(create_file_block(base64=res.blob, mime_type=mime))
            else:
                lc_content.append(create_text_block(text=str(res)))
        else:
            lc_content.append(create_text_block(text=str(item)))

    if call_tool_result.isError:
        error_parts = [item["text"] for item in lc_content if isinstance(item, dict) and item.get("type") == "text"]
        raise ToolException("\n".join(error_parts) if error_parts else str(lc_content))

    artifact = None
    if call_tool_result.structuredContent is not None:
        artifact = {"structured_content": call_tool_result.structuredContent}

    return lc_content, artifact


def _make_session_pool_tool(
    tool: BaseTool,
    server_name: str,
    connection: dict[str, Any],
    tool_interceptors: list[Any] | None = None,
) -> BaseTool:
    """把一个 MCP 工具包成「复用池中持久会话」的版本。

    以 ``(server_name, thread_id)`` 为 scope 复用会话，保证有状态 MCP 服务器
    （如 Playwright）在同一线程的工具调用间保活状态。配置的 ``tool_interceptors``
    （OAuth、自定义）在每次调用前保留并应用。
    """
    # 剥掉 server-name 前缀，恢复原始 MCP 工具名。
    original_name = tool.name
    prefix = f"{server_name}_"
    if original_name.startswith(prefix):
        original_name = original_name[len(prefix) :]

    pool = get_session_pool()

    async def call_with_persistent_session(
        runtime: Runtime | None = None,
        **arguments: Any,
    ) -> Any:
        thread_id = _extract_thread_id(runtime)
        user_id = resolve_runtime_user_id(runtime)
        # #3597：stdio 子进程的 cwd / TMPDIR 钉在该线程 user-data 树里，产物落在沙箱/artifact
        # API 能服务的位置；调前快照工作区，调后 diff 出新建文件供结果重写。SSE/HTTP 无本地 cwd
        # 可钉，跳过这些文件系统活。
        session_connection = dict(connection)
        is_stdio = session_connection.get("transport", "stdio") == "stdio"
        source_base_dir: Path | None = None
        process_cwd: Path | None = None
        before_files: _FILE_SNAPSHOT | None = None
        if is_stdio:
            paths = get_paths()
            # 把同步文件系统准备（建目录 / tmp / 调前快照）捆一起卸到事件循环外——快照要走整棵工作区。
            source_base_dir, tmp_dir, before_files = await asyncio.to_thread(_prepare_stdio_workspace, paths, thread_id=thread_id, user_id=user_id)
            # stdio MCP 服务器按进程 cwd 解析相对产物链接。把 cwd 钉在线程 user-data 树内，让
            # Playwright 等工具的产物落在沙箱/artifact API 能服务的位置、其引用能翻成虚拟路径。
            configured_cwd = session_connection.get("cwd", str(source_base_dir))
            session_connection["cwd"] = str(configured_cwd)
            process_cwd = Path(configured_cwd)
            # 把子进程 tmp 钉到同一棵树。默认走 OS tmp 的工具（Node os.tmpdir() / Python tempfile / 多数 CLI）
            # 于是写进 user-data 而非不可达的宿主路径。合并而非覆盖 operator 给的 env。
            session_env = dict(session_connection.get("env") or {})
            session_env.setdefault("TMPDIR", str(tmp_dir))
            session_env.setdefault("TMP", str(tmp_dir))
            session_env.setdefault("TEMP", str(tmp_dir))
            session_connection["env"] = session_env
        session = await pool.get_session(server_name, thread_id, session_connection)

        if tool_interceptors:
            from langchain_mcp_adapters.interceptors import MCPToolCallRequest

            async def base_handler(request: MCPToolCallRequest) -> Any:
                # 经 MCP call meta 保留拦截器注入的头，供 stdio MCP 调用透传。
                call_kwargs: dict[str, Any] = {}
                if request.headers:
                    if isinstance(request.headers, Mapping):
                        call_kwargs["meta"] = {"headers": dict(request.headers)}
                    else:
                        logger.warning("忽略类型不支持的 MCP 拦截器头: %s", type(request.headers).__name__)
                return await session.call_tool(request.name, request.args, **call_kwargs)

            handler: Any = base_handler
            for interceptor in reversed(tool_interceptors):
                outer = handler

                async def wrapped(req: Any, _i: Any = interceptor, _h: Any = outer) -> Any:
                    return await _i(req, _h)

                handler = wrapped

            request = MCPToolCallRequest(
                name=original_name,
                args=arguments,
                server_name=server_name,
                runtime=runtime,
            )
            call_tool_result = await handler(request)
        else:
            call_tool_result = await session.call_tool(original_name, arguments)

        # 调后 diff 只为给文本里的裸文件名做关联，所以结果无文本内容时跳过第二次递归扫。
        # diff 与 _convert_call_tool_result 里逐 token 的路径解析都触文件系统，卸到事件循环外。
        changed_files: list[Path] | None = None
        if is_stdio and before_files is not None and _result_has_text_content(call_tool_result):
            changed_files = await asyncio.to_thread(_changed_workspace_files, source_base_dir, before_files)
        return await asyncio.to_thread(
            _convert_call_tool_result,
            call_tool_result,
            thread_id=thread_id if is_stdio else None,
            user_id=user_id if is_stdio else None,
            source_base_dir=process_cwd,
            changed_files=changed_files,
        )

    return StructuredTool(
        name=tool.name,
        description=tool.description,
        args_schema=tool.args_schema,
        coroutine=call_with_persistent_session,
        response_format="content_and_artifact",
        metadata=tool.metadata,
    )


async def get_mcp_tools() -> list[BaseTool]:
    """从所有启用的 MCP 服务器获取工具。

    stdio 传输的工具被包上持久会话逻辑，使同线程连续调用复用同一 MCP 会话。
    HTTP/SSE 工具不包（避免跨 task TaskGroup 清理错误，issue #3203）。

    Returns:
        所有启用 MCP 服务器的 LangChain 工具列表。缺包/无配置返回 ``[]``。
    """
    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient
    except ImportError:
        logger.warning("未安装 langchain-mcp-adapters。装上以启用 MCP 工具: pip install langchain-mcp-adapters")
        return []

    # 用 ExtensionsConfig.from_file() 而非 get_extensions_config()，始终读盘最新配置。
    # 这样 Gateway API（在另一进程跑）的改动初始化 MCP 工具时立即生效。
    extensions_config = ExtensionsConfig.from_file()
    servers_config = build_servers_config(extensions_config)

    if not servers_config:
        logger.info("没有启用的 MCP 服务器配置")
        return []

    try:
        logger.info("用 %d 个服务器初始化 MCP client", len(servers_config))

        # 注入 server 连接的初始 OAuth 头（工具发现/会话初始化）。
        initial_oauth_headers = await get_initial_oauth_headers(extensions_config)
        for server_name, auth_header in initial_oauth_headers.items():
            if server_name not in servers_config:
                continue
            if servers_config[server_name].get("transport") in ("sse", "http"):
                existing_headers = dict(servers_config[server_name].get("headers", {}))
                existing_headers["Authorization"] = auth_header
                servers_config[server_name]["headers"] = existing_headers

        tool_interceptors: list[Any] = []
        oauth_interceptor = build_oauth_tool_interceptor(extensions_config)
        if oauth_interceptor is not None:
            tool_interceptors.append(oauth_interceptor)

        # 加载 extensions_config.json 声明的自定义拦截器。
        # 格式: "mcpInterceptors": ["pkg.module:builder_func", ...]
        for interceptor_path in extensions_config.mcp_interceptors:
            try:
                builder = resolve_variable(interceptor_path)
                interceptor = builder()
                if callable(interceptor):
                    tool_interceptors.append(interceptor)
                    logger.info("已加载 MCP 拦截器: %s", interceptor_path)
                elif interceptor is not None:
                    logger.warning(
                        "builder %s 返回了非可调用对象 %s；跳过",
                        interceptor_path,
                        type(interceptor).__name__,
                    )
            except Exception as e:
                logger.warning("加载 MCP 拦截器 %s 失败: %s", interceptor_path, e, exc_info=True)

        client = MultiServerMCPClient(
            servers_config,
            tool_interceptors=tool_interceptors,
            tool_name_prefix=True,
        )

        # 按服务器独立发现工具——单个坏 MCP 服务器不让健康服务器的工具一起丢（#3772）。
        async def load_server_tools(server_name: str) -> list[BaseTool]:
            try:
                return await client.get_tools(server_name=server_name)
            except Exception as e:
                logger.warning("MCP 服务器 '%s' 工具发现失败，跳过: %s", server_name, e, exc_info=True)
                return []

        tools_by_server = await asyncio.gather(*(load_server_tools(name) for name in servers_config))
        tools = [tool for server_tools in tools_by_server for tool in server_tools]
        logger.info("成功从 MCP 服务器加载 %d 个工具", len(tools))

        # 给每个工具包上持久会话逻辑。仅池化 stdio 会话。HTTP/SSE 传输内部用
        # anyio TaskGroup，无法从不同 async task 关闭，池化会在清理时 RuntimeError（issue #3203）。
        wrapped_tools: list[BaseTool] = []
        for tool in tools:
            tool_server: str | None = None
            for name in servers_config:
                if tool.name.startswith(f"{name}_"):
                    tool_server = name
                    break

            if tool_server is not None:
                transport = servers_config[tool_server].get("transport", "stdio")
                if transport == "stdio":
                    wrapped_tools.append(_make_session_pool_tool(tool, tool_server, servers_config[tool_server], tool_interceptors))
                else:
                    wrapped_tools.append(tool)
            else:
                wrapped_tools.append(tool)

        # 给工具补同步入口——deerflow client 同步流式需要 BaseTool.func。
        for tool in wrapped_tools:
            if getattr(tool, "func", None) is None and getattr(tool, "coroutine", None) is not None:
                tool.func = make_sync_tool_wrapper(tool.coroutine, tool.name)

        return wrapped_tools

    except Exception as e:
        logger.error("加载 MCP 工具失败: %s", e, exc_info=True)
        return []
