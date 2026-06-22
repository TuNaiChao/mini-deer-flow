"""文档 → Markdown 转换工具（M23 uploads）。

把 PDF / PPT / Excel / Word 等二进制文档转成 markdown，供 agent 直接阅读
（agent 只懂文本，看不了 PDF 字节流）。**纯函数 + soft-load**，无 FastAPI / HTTP 依赖。

两个转换器都是 **soft-load**（函数内 ``import``，缺包即回退）：
- ``markitdown``：微软的多格式转 markdown（PDF/PPT/Excel/Word 通吃）。
- ``pymupdf4llm``：PDF 专用，标题检测更好、更快，但纯图片 / 加密 PDF 会输出接近空白。

PDF 双转换策略（``auto`` 模式，默认）：
  1. 装了 ``pymupdf4llm`` 就先试它——更好。
  2. 输出太稀疏（< 50 字/页，或页数不可得时 < 200 字）→ 判定为图片 / 加密 PDF，
     回退 ``markitdown``（后者走 OCR，能啃图片）。
  3. 没装 ``pymupdf4llm`` → 直接用 ``markitdown``。

大文件（> 1MB）用 ``asyncio.to_thread`` 卸载到线程池，避免阻塞事件循环
（转换是 CPU/IO 重活，#1569）。两个库都缺包时抛 ``ImportError`` →
:func:`convert_file_to_markdown` 捕获后返回 ``None``，调用方（uploads）**保留原文件**，
上传本身不受影响——这就是「soft-load：缺包跳过转换但上传仍可用」。
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from deerflow.config.app_config import get_app_config

logger = logging.getLogger(__name__)

# 应被转换成 markdown 的文件扩展名（小写，含点）。
# 这些是 agent 无法直接阅读的二进制 / 富文档格式。
CONVERTIBLE_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".pdf",
        ".ppt",
        ".pptx",
        ".xls",
        ".xlsx",
        ".doc",
        ".docx",
    }
)

# 大于此阈值（字节）的文件在后台线程转换。
# 小文件同步转完通常 < 1s，起线程反而白白增加调度开销。
_ASYNC_THRESHOLD_BYTES = 1 * 1024 * 1024  # 1 MB

# 若 pymupdf4llm 输出的「每页字符数」低于此阈值，判定为图片 / 加密 PDF，回退 markitdown。
# 理由：正常文本 PDF 每页 200~2000 字；纯图片 PDF 接近 0。50 字/页留足安全余量。
# 页数不可得时退化为绝对阈值 200 字。
_MIN_CHARS_PER_PAGE = 50

# 允许的 PDF 转换器取值（与 UploadsConfig 对齐）。
_ALLOWED_PDF_CONVERTERS = frozenset({"auto", "pymupdf4llm", "markitdown"})


def _get_pdf_converter() -> str:
    """从 AppConfig 读 pdf_converter 设置，归一化为小写并校验，非法值回退 'auto'。

    防止 ``config.yaml`` 写成 ``AUTO`` / ``MarkItDown`` 等大小写 / 拼写变体时
    静默走到非预期分支。读配置失败也回退 'auto'（不抛错——转换是尽力而为）。
    """
    try:
        cfg = get_app_config()
        return cfg.uploads.normalized_pdf_converter()
    except Exception:
        return "auto"


def _pymupdf_output_too_sparse(text: str, file_path: Path) -> bool:
    """pymupdf4llm 输出是否「太稀疏」（图片 / 加密 PDF 的特征）。

    用「每页字符数」而非绝对阈值，这样短文档（页少字少）和长文档（页多字多）都能正确判定。
    """
    chars = len(text.strip())
    doc = None
    pages: int | None = None
    try:
        import pymupdf

        doc = pymupdf.open(str(file_path))
        pages = len(doc)
    except Exception:
        pass
    finally:
        if doc is not None:
            try:
                doc.close()
            except Exception:
                pass
    if pages is not None and pages > 0:
        return (chars / pages) < _MIN_CHARS_PER_PAGE
    # 页数不可得时退化为绝对阈值。
    return chars < 200


def _convert_pdf_with_pymupdf4llm(file_path: Path) -> str | None:
    """尝试用 pymupdf4llm 转 PDF。

    返回 markdown 文本；未安装或转换失败（如加密 / 损坏）时返回 ``None``（soft-load）。
    """
    try:
        import pymupdf4llm
    except ImportError:
        return None

    try:
        return pymupdf4llm.to_markdown(str(file_path))
    except Exception:
        logger.exception("pymupdf4llm 转换 %s 失败，回退 markitdown", file_path.name)
        return None


def _convert_with_markitdown(file_path: Path) -> str:
    """用 markitdown 把任意受支持文件转成 markdown 文本（soft-load）。"""
    from markitdown import MarkItDown

    md = MarkItDown()
    return md.convert(str(file_path)).text_content


def _do_convert(file_path: Path, pdf_converter: str) -> str:
    """同步转换（直接调用或经 asyncio.to_thread 卸载）。

    Args:
        file_path: 文件路径。
        pdf_converter: ``"auto"`` | ``"pymupdf4llm"`` | ``"markitdown"``。
    """
    is_pdf = file_path.suffix.lower() == ".pdf"

    if is_pdf and pdf_converter != "markitdown":
        # auto 或显式 pymupdf4llm：先试 pymupdf4llm。
        pymupdf_text = _convert_pdf_with_pymupdf4llm(file_path)

        if pymupdf_text is not None:
            # pymupdf4llm 已安装。
            if pdf_converter == "pymupdf4llm":
                # 显式指定：原样用，不管输出长短。
                return pymupdf_text
            # auto 模式：输出太稀疏（图片 PDF）就回退 markitdown。
            if not _pymupdf_output_too_sparse(pymupdf_text, file_path):
                return pymupdf_text
            logger.warning(
                "pymupdf4llm 对 %s 只输出 %d 字（疑似图片 PDF），回退 markitdown",
                file_path.name,
                len(pymupdf_text.strip()),
            )
        # pymupdf4llm 未装或已触发回退 → 用 markitdown。

    return _convert_with_markitdown(file_path)


async def convert_file_to_markdown(file_path: Path) -> Path | None:
    """把一个受支持的文档文件转成 Markdown。

    PDF 走双转换策略（见模块 docstring）。大文件（> 1MB）卸载到线程池避免阻塞事件循环。

    Args:
        file_path: 待转换文件路径。

    Returns:
        生成的 ``.md`` 文件路径；转换失败（如转换器缺包）返回 ``None``。
    """
    try:
        pdf_converter = _get_pdf_converter()
        file_size = file_path.stat().st_size

        if file_size > _ASYNC_THRESHOLD_BYTES:
            text = await asyncio.to_thread(_do_convert, file_path, pdf_converter)
        else:
            text = _do_convert(file_path, pdf_converter)

        md_path = file_path.with_suffix(".md")
        md_path.write_text(text, encoding="utf-8")

        logger.info("已转换 %s → markdown: %s（%d 字）", file_path.name, md_path.name, len(text))
        return md_path
    except Exception as e:
        logger.error("转换 %s 到 markdown 失败: %s", file_path.name, e)
        return None


# ---------------------------------------------------------------------------
# 文档大纲（heading）抽取 —— 给 agent 一个「这份文档讲了什么」的目录
# ---------------------------------------------------------------------------
# pymupdf4llm 对 PDF 的 heading 渲染有三种风格，本段识别它们，供 M16 UploadsMiddleware
# 把「文档大纲」注入 agent 系统提示（让 agent 知道文档结构，决定读哪一段）。
#
# 1. 标准 markdown 标题（``# ...``）。
# 2. **纯粗体结构性标题**：``**ITEM 1. BUSINESS**``、``**PART II**``。SEC 财报用粗体 +
#    全大写表示章节标题（字号同正文，pymupdf4llm 无法提升为 ``#``），故需此模式。
# 3. **拆分粗体标题**：``**1** **Introduction**`` / ``**3.2** **Attention``。pymupdf4llm 在
#    「章节号」与「标题文本」是分离 span 时会这样输出（学术论文常见）。
#
# 中文标题（第三节…）pymupdf4llm 已渲染为标准 ``#`` 标题，无需本段处理。

# 纯粗体结构性标题：整行是一个 ``**...**`` 块，且以 ITEM / PART / SECTION / SCHEDULE /
# EXHIBIT / APPENDIX / ANNEX / CHAPTER 开头。全大写地址 / 套话（"CURRENT REPORT"、
# "SIGNATURES"、"WASHINGTON, DC 20549"）不以这些词开头，被排除。
_BOLD_HEADING_RE = re.compile(r"^\*\*((ITEM|PART|SECTION|SCHEDULE|EXHIBIT|APPENDIX|ANNEX|CHAPTER)\b[A-Z0-9 .,\-]*)\*\*\s*$")

# 拆分粗体标题：``**1** **Introduction**``。
# 约束：① 整行只有 ``**...**`` 块（无散文）；② 首块是章节号（数字 + 点）；
# ③ 第二块非纯数字 / 标点（排除 ``**2023** **2022** **2021`` 这类财务表头）；
# ④ 最多再两个块（共 4），块内无 ``*``，保持正则线性，防 ReDoS。
_SPLIT_BOLD_HEADING_RE = re.compile(r"^\*\*[\dA-Z][\d\.]*\*\*\s+\*\*(?!\d[\d\s.,\-–—/:()%]*\*\*)[^*]+\*\*(?:\s+\*\*[^*]+\*\*){0,2}\s*$")

# 注入 agent 上下文的大纲条目上限——文档再长，提示也要有界。
MAX_OUTLINE_ENTRIES = 50


def _clean_bold_title(raw: str) -> str:
    """归一化可能带 pymupdf4llm 粗体伪影的标题文本。

    pymupdf4llm 有时把相邻粗体 span 输出成 ``**A** **B`` 而非单个 ``**A B**``。
    本 helper 先合并这些片段，再剥掉最外层 ``**...**``，返回纯文本。

    Examples::

        "**Overview**"                       → "Overview"
        "**UNITED STATES** **SECURITIES**"   → "UNITED STATES SECURITIES"
        "plain text"                         → "plain text"  (不变)
    """
    merged = re.sub(r"\*\*\s*\*\*", " ", raw).strip()
    if m := re.fullmatch(r"\*\*(.+?)\*\*", merged, re.DOTALL):
        return m.group(1).strip()
    return merged


def extract_outline(md_path: Path) -> list[dict]:
    """从 markdown 文件抽取文档大纲（heading 列表）。

    识别 pymupdf4llm 输出的三种标题风格（见上方模块 docstring）。

    Args:
        md_path: ``.md`` 文件路径。

    Returns:
        dict 列表，每项 ``{"title": str, "line": int(1-based)}``。超过 ``MAX_OUTLINE_ENTRIES``
        时末尾追加一个哨兵 ``{"truncated": True}``，调用方可据此渲染「仅显示前 N 个标题」提示，
        无需重新扫描文件。读不到或无标题返回空列表。
    """
    outline: list[dict] = []
    try:
        with md_path.open(encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):
                stripped = line.strip()
                if not stripped:
                    continue

                # 风格 1：标准 markdown 标题。
                if stripped.startswith("#"):
                    title = _clean_bold_title(stripped.lstrip("#").strip())
                    if title:
                        outline.append({"title": title, "line": lineno})

                # 风格 2：单个粗体块 + SEC 结构性关键词。
                elif m := _BOLD_HEADING_RE.match(stripped):
                    title = m.group(1).strip()
                    if title:
                        outline.append({"title": title, "line": lineno})

                # 风格 3：拆分粗体标题 —— **<编号>** **<标题>**。
                elif _SPLIT_BOLD_HEADING_RE.match(stripped):
                    title = " ".join(re.findall(r"\*\*([^*]+)\*\*", stripped))
                    if title:
                        outline.append({"title": title, "line": lineno})

                if len(outline) >= MAX_OUTLINE_ENTRIES:
                    outline.append({"truncated": True})
                    break
    except Exception:
        return []

    return outline
