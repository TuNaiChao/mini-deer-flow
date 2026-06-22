"""``UploadsMiddleware``：把上传文件清单 + 文档大纲注入对话（M16，接 M23 uploads）。

用户上传 PDF / PPT / Excel 后，本中间件在 ``before_agent`` 把「这次上传的文件 + 历史
上传文件」连同各自的**文档大纲**（``extract_outline`` 从转换出的 ``.md`` 抽 heading）
格式化进一个 ``<uploaded_files>`` 块，**前置**到当前 HumanMessage，让模型知道有哪些
文件可用、每份文件的结构是什么。

新文件来自当前消息的 ``additional_kwargs.files``（前端上传后塞入）；历史文件从该线程
的 uploads 目录扫描（排除本次新增）。文件存在性校验用 per-thread 物理目录（M23 的
``sandbox_uploads_dir``）。

``abefore_agent`` 把同步扫描卸到 executor 线程——目录枚举 / ``stat`` / 读 ``.md``
大纲都是阻塞 IO，绝不能在事件循环上跑（红线 #1）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import NotRequired, override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage
from langchain_core.runnables import run_in_executor
from langgraph.runtime import Runtime

from deerflow.config.paths import Paths, get_paths
from deerflow.runtime.user_context import get_effective_user_id
from deerflow.uploads.conversion import extract_outline
from deerflow.utils.messages import ORIGINAL_USER_CONTENT_KEY, message_content_to_text

logger = logging.getLogger(__name__)

_OUTLINE_PREVIEW_LINES = 5


def _extract_outline_for_file(file_path: Path) -> tuple[list[dict], list[str]]:
    """返回 (大纲, 预览)。大纲非空时预览为空；大纲空时读 ``.md`` 前几行做锚点。

    找的 ``.md`` 是上传转换管线（M23 ``convert_file_to_markdown``）生成的同名 markdown。
    """
    md_path = file_path.with_suffix(".md")
    if not md_path.is_file():
        return [], []

    outline = extract_outline(md_path)
    if outline:
        logger.debug("Extracted %d outline entries from %s", len(outline), file_path.name)
        return outline, []

    # 大纲空 → 读前几行非空行作内容预览，给模型一个锚点。
    preview: list[str] = []
    try:
        with md_path.open(encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped:
                    preview.append(stripped)
                if len(preview) >= _OUTLINE_PREVIEW_LINES:
                    break
    except Exception:
        logger.debug("Failed to read preview lines from %s", md_path, exc_info=True)
    return [], preview


class UploadsMiddlewareState(AgentState):
    uploaded_files: NotRequired[list[dict] | None]


class UploadsMiddleware(AgentMiddleware[UploadsMiddlewareState]):
    """把上传文件信息注入 agent 上下文。

    从当前消息的 ``additional_kwargs.files``（前端上传后塞入）读新文件元数据，给最后一条
    HumanMessage 前置 ``<uploaded_files>`` 块。原 ``additional_kwargs``（含 files）保留，
    前端可从流里读结构化文件信息。
    """

    state_schema = UploadsMiddlewareState

    def __init__(self, base_dir: str | None = None):
        super().__init__()
        self._paths = Paths(base_dir) if base_dir else get_paths()

    def _format_file_entry(self, file: dict, lines: list[str]) -> None:
        size_kb = file["size"] / 1024
        size_str = f"{size_kb:.1f} KB" if size_kb < 1024 else f"{size_kb / 1024:.1f} MB"
        lines.append(f"- {file['filename']} ({size_str})")
        lines.append(f"  Path: {file['path']}")
        outline = file.get("outline") or []
        if outline:
            truncated = outline[-1].get("truncated", False)
            visible = [e for e in outline if not e.get("truncated")]
            lines.append("  Document outline (use `read_file` with line ranges to read sections):")
            for entry in visible:
                lines.append(f"    L{entry['line']}: {entry['title']}")
            if truncated:
                lines.append(f"    ... (showing first {len(visible)} headings; use `read_file` to explore further)")
        else:
            preview = file.get("outline_preview") or []
            if preview:
                lines.append("  No structural headings detected. Document begins with:")
                for text in preview:
                    lines.append(f"    > {text}")
            lines.append("  Use `grep` to search for keywords (e.g. `grep(pattern='keyword', path='/mnt/user-data/uploads/')`).")
        lines.append("")

    def _create_files_message(self, new_files: list[dict], historical_files: list[dict]) -> str:
        lines = ["<uploaded_files>"]

        lines.append("The following files were uploaded in this message:")
        lines.append("")
        if new_files:
            for file in new_files:
                self._format_file_entry(file, lines)
        else:
            lines.append("(empty)")
            lines.append("")

        if historical_files:
            lines.append("The following files were uploaded in previous messages and are still available:")
            lines.append("")
            for file in historical_files:
                self._format_file_entry(file, lines)

        lines.append("To work with these files:")
        lines.append("- Read from the file first — use the outline line numbers and `read_file` to locate relevant sections.")
        lines.append("- Use `grep` to search for keywords when you are not sure which section to look at")
        lines.append("  (e.g. `grep(pattern='revenue', path='/mnt/user-data/uploads/')`).")
        lines.append("- Use `glob` to find files by name pattern")
        lines.append("  (e.g. `glob(pattern='**/*.md', path='/mnt/user-data/uploads/')`).")
        lines.append("- Only fall back to web search if the file content is clearly insufficient to answer the question.")
        lines.append("</uploaded_files>")

        return "\n".join(lines)

    def _files_from_kwargs(self, message: HumanMessage, uploads_dir: Path | None = None) -> list[dict] | None:
        """从 message.additional_kwargs.files 抽文件信息。

        前端上传后塞 files 元数据：filename / size / path(虚拟) / status。
        ``uploads_dir`` 给定时校验物理存在性——文件已删的条目跳过。
        """
        kwargs_files = (message.additional_kwargs or {}).get("files")
        if not isinstance(kwargs_files, list) or not kwargs_files:
            return None

        files = []
        for f in kwargs_files:
            if not isinstance(f, dict):
                continue
            filename = f.get("filename") or ""
            if not filename or Path(filename).name != filename:
                continue
            if uploads_dir is not None and not (uploads_dir / filename).is_file():
                continue
            files.append(
                {
                    "filename": filename,
                    "size": int(f.get("size") or 0),
                    "path": f"/mnt/user-data/uploads/{filename}",
                    "extension": Path(filename).suffix,
                }
            )
        return files if files else None

    @override
    def before_agent(self, state: UploadsMiddlewareState, runtime: Runtime) -> dict | None:
        messages = list(state.get("messages", []))
        if not messages:
            return None

        last_message_index = len(messages) - 1
        last_message = messages[last_message_index]

        if not isinstance(last_message, HumanMessage):
            return None

        thread_id = (runtime.context or {}).get("thread_id")
        if thread_id is None:
            try:
                from langgraph.config import get_config

                thread_id = get_config().get("configurable", {}).get("thread_id")
            except RuntimeError:
                pass  # get_config() 在非 runnable 上下文（如单测）抛 RuntimeError
        uploads_dir = self._paths.sandbox_uploads_dir(thread_id, user_id=get_effective_user_id()) if thread_id else None

        new_files = self._files_from_kwargs(last_message, uploads_dir) or []

        # 历史文件 = uploads 目录里除本次新增外的文件。
        new_filenames = {f["filename"] for f in new_files}
        historical_files: list[dict] = []
        if uploads_dir and uploads_dir.exists():
            for file_path in sorted(uploads_dir.iterdir()):
                if file_path.is_file() and file_path.name not in new_filenames:
                    stat = file_path.stat()
                    outline, preview = _extract_outline_for_file(file_path)
                    historical_files.append(
                        {
                            "filename": file_path.name,
                            "size": stat.st_size,
                            "path": f"/mnt/user-data/uploads/{file_path.name}",
                            "extension": file_path.suffix,
                            "outline": outline,
                            "outline_preview": preview,
                        }
                    )

        # 给新文件也附大纲。
        if uploads_dir:
            for file in new_files:
                phys_path = uploads_dir / file["filename"]
                outline, preview = _extract_outline_for_file(phys_path)
                file["outline"] = outline
                file["outline_preview"] = preview

        if not new_files and not historical_files:
            return None

        logger.debug(
            "New files: %s, historical: %s",
            [f["filename"] for f in new_files],
            [f["filename"] for f in historical_files],
        )

        files_message = self._create_files_message(new_files, historical_files)

        original_content = last_message.content
        additional_kwargs = dict(last_message.additional_kwargs or {})
        # 存原始内容供 DynamicContext 的 ID-swap 复用（防二次注入时丢原文）。
        additional_kwargs.setdefault(ORIGINAL_USER_CONTENT_KEY, message_content_to_text(original_content))
        if isinstance(original_content, str):
            updated_content = f"{files_message}\n\n{original_content}"
        elif isinstance(original_content, list):
            # 多模态：前置一个文本块，保留所有原 block（含图片）。
            files_block = {"type": "text", "text": f"{files_message}\n\n"}
            updated_content = [files_block, *original_content]
        else:
            updated_content = original_content

        updated_message = HumanMessage(
            content=updated_content,
            id=last_message.id,
            name=last_message.name,
            additional_kwargs=additional_kwargs,
        )

        messages[last_message_index] = updated_message

        return {"uploaded_files": new_files, "messages": messages}

    @override
    async def abefore_agent(self, state: UploadsMiddlewareState, runtime: Runtime) -> dict | None:
        """异步钩子：把同步 uploads 扫描卸到 worker 线程。

        ``before_agent`` 做阻塞文件 IO（目录枚举 / ``stat`` / 读 ``.md`` 大纲）。图跑异步时
        langgraph 会把同步钩子直接跑在事件循环上，故经 ``run_in_executor`` 卸到线程。
        ``run_in_executor`` 拷当前 context，``get_effective_user_id`` 读的 contextvar 得以保留。
        """
        return await run_in_executor(None, self.before_agent, state, runtime)
