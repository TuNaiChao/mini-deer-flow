"""HTML 可读性提取（标题 + 正文 → markdown）。

对齐 deer ``utils/readability.py``：把抓到的 HTML 提取成结构化 ``Article``（标题 + 正文），
供 jina_ai / browserless 等抓取 provider 调 ``to_markdown()`` 后再 4KB 截断喂给 agent。

依赖（**软加载**，红线 #24）：
- ``readabilipy`` —— Python 包装 Mozilla Readability.js（经 Node 子进程），提取质量最好但重；
- ``markdownify`` —— HTML→markdown 转换。

两者都缺时，回落到**纯 Python 兜底**（剥 script/style/nav + 去标签 + 抽 ``<title>`` + 折叠空白），
质量不如 Readability.js 但零依赖、可 hermetic 测试，保证缺包时 jina/browserless 仍能产出可读文本。
装上 ``readabilipy`` + ``markdownify`` 后自动走高质量路径（``extract_article`` 内部 try/except）。
"""

from __future__ import annotations

import logging
import re
import subprocess
from urllib.parse import urljoin

logger = logging.getLogger(__name__)

# 纯 Python 兜底用：这些标签的内容与正文无关，整块删（非贪婪，含标签自身）。
# head 整块删——它含 title/meta/link/script/style，title 已单独抽出，不能让它的文本漏进正文。
_DROP_TAG_PATTERN = re.compile(
    r"<(head|script|style|nav|footer|header|aside|noscript|template|iframe)\b[^>]*>.*?</\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
_TITLE_PATTERN = re.compile(r"<title[^>]*>(.*?)</title\s*>", re.IGNORECASE | re.DOTALL)
_TAG_PATTERN = re.compile(r"<[^>]+>")
_WHITESPACE_PATTERN = re.compile(r"[ \t\f\v]+")
_NEWLINE_PATTERN = re.compile(r"\n\s*\n\s*")
# 常见 HTML 实体（兜底；readabilipy 路径不走这里）。
_HTML_ENTITIES = {
    "&nbsp;": " ",
    "&amp;": "&",
    "&lt;": "<",
    "&gt;": ">",
    "&quot;": '"',
    "&#39;": "'",
    "&apos;": "'",
}


def _decode_entities(text: str) -> str:
    for entity, replacement in _HTML_ENTITIES.items():
        text = text.replace(entity, replacement)
    return text


def _fallback_extract(html: str) -> tuple[str, str]:
    """纯 Python 兜底提取：返回 (title, text)。

    非 Readability.js 级别，但足够给 agent 看个大概。用于 readabilipy 缺包时。
    """
    title_match = _TITLE_PATTERN.search(html)
    title = _decode_entities(title_match.group(1).strip()) if title_match else "Untitled"
    if not title:
        title = "Untitled"

    cleaned = _DROP_TAG_PATTERN.sub("", html)
    # <br> / </p> 等块级结束 → 换行，保留基本排版。
    cleaned = re.sub(r"</(p|div|li|h[1-6]|tr|br)\s*>", "\n", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"<br\s*/?>", "\n", cleaned, flags=re.IGNORECASE)
    cleaned = _TAG_PATTERN.sub("", cleaned)
    cleaned = _decode_entities(cleaned)
    cleaned = _WHITESPACE_PATTERN.sub(" ", cleaned)
    cleaned = _NEWLINE_PATTERN.sub("\n\n", cleaned)
    text = cleaned.strip()
    if not text:
        text = "No content could be extracted from this page"
    return title, text


class Article:
    """从 HTML 提取出的文章（标题 + 正文 HTML/文本）。对齐 deer Article。"""

    url: str = ""

    def __init__(self, title: str, html_content: str, *, url: str = "") -> None:
        self.title = title
        self.html_content = html_content
        self.url = url

    def to_markdown(self, including_title: bool = True) -> str:
        """转成 markdown。``markdownify`` 可用时把 HTML 转成 markdown；否则直接用正文文本。

        空正文 → ``*No content available*`` 占位（与 deer 一致）。
        """
        markdown = ""
        if including_title:
            markdown += f"# {self.title}\n\n"

        if self.html_content is None or not str(self.html_content).strip():
            markdown += "*No content available*\n"
            return markdown

        try:
            from markdownify import markdownify as md

            markdown += md(self.html_content)
        except ImportError:
            # 兜底：html_content 此时已经是兜底提取的纯文本（见 extract_article），原样用。
            markdown += str(self.html_content)

        return markdown

    def to_message(self) -> list[dict]:
        """把文章转成多模态 content 块列表（文本 + 图片 URL 交替）。

        对齐 deer ``Article.to_message``：把 ``to_markdown()`` 的输出按 markdown 图片语法
        （``![alt](url)``）切开——奇数段是图片 URL（用 ``urljoin`` 拼成绝对 URL，发 ``image_url``
        块），偶数段是文本（发 ``text`` 块）。这样支持视觉的模型能直接看到文章里的图片，
        而不是只看到 markdown 图片文本。空内容回退 ``[{"type": "text", "text": "No content available"}]``。
        """
        image_pattern = r"!\[.*?\]\((.*?)\)"

        markdown = self.to_markdown()
        if not markdown or not markdown.strip():
            return [{"type": "text", "text": "No content available"}]

        content: list[dict[str, str]] = []
        parts = re.split(image_pattern, markdown)
        for i, part in enumerate(parts):
            if i % 2 == 1:
                # 图片段：相对 URL 用文章自身 URL 拼成绝对。
                image_url = urljoin(self.url, part.strip())
                content.append({"type": "image_url", "image_url": {"url": image_url}})
            else:
                text_part = part.strip()
                if text_part:
                    content.append({"type": "text", "text": text_part})

        if not content:
            content = [{"type": "text", "text": "No content available"}]
        return content


class ReadabilityExtractor:
    """把 HTML 提取成 ``Article``。

    优先用 ``readabilipy``（Mozilla Readability.js，质量最好）；缺包则用纯 Python 兜底。
    """

    def extract_article(self, html: str) -> Article:
        try:
            from readabilipy import simple_json_from_html_string

            article = simple_json_from_html_string(html, use_readability=True)
        except ImportError:
            # readabilipy 未安装 —— 走纯 Python 兜底（不报错，降级提取）。
            logger.debug("readabilipy not installed; using pure-python fallback extractor")
            title, text = _fallback_extract(html)
            return Article(title=title, html_content=text)
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            # readabilipy 装了，但 Mozilla Readability.js（经 Node 子进程）失败/缺失——
            # 回退到 readabilipy 的纯 Python 模式（use_readability=False），质量仍好于本模块的
            # regex 兜底。再失败才落 _fallback_extract。对齐 deer。
            stderr = getattr(exc, "stderr", None)
            if isinstance(stderr, bytes):
                stderr = stderr.decode(errors="replace")
            stderr_info = f"; stderr={stderr.strip()}" if isinstance(stderr, str) and stderr.strip() else ""
            logger.warning(
                "Readability.js extraction failed with %s%s; falling back to pure-Python extraction",
                type(exc).__name__,
                stderr_info,
                exc_info=True,
            )
            try:
                article = simple_json_from_html_string(html, use_readability=False)
            except Exception:  # noqa: BLE001 — 纯 Python 模式也炸，落 regex 兜底
                title, text = _fallback_extract(html)
                return Article(title=title, html_content=text)
        except Exception as exc:  # noqa: BLE001 — readabilipy 其它内部错误
            stderr = getattr(exc, "stderr", None)
            if isinstance(stderr, bytes):
                stderr = stderr.decode(errors="replace")
            stderr_info = f"; stderr={stderr.strip()}" if isinstance(stderr, str) and stderr.strip() else ""
            logger.warning(
                "Readability.js extraction failed with %s%s; falling back to pure-python extraction",
                type(exc).__name__,
                stderr_info,
                exc_info=True,
            )
            title, text = _fallback_extract(html)
            return Article(title=title, html_content=text)

        html_content = article.get("content")
        if not html_content or not str(html_content).strip():
            html_content = "No content could be extracted from this page"

        title = article.get("title")
        if not title or not str(title).strip():
            title = "Untitled"

        return Article(title=title, html_content=html_content)
