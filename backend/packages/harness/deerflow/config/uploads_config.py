"""文件上传配置（M23 uploads）。

控制上传时的**自动文档转换**：把 PDF/PPT/Excel/Word 等二进制文档用
``markitdown``（可选 pymupdf4llm）转成 markdown，供 agent 直接阅读。

- ``auto_convert_documents``：上传后是否自动转换（默认开）。关掉后只存原文件。
- ``pdf_converter``：PDF 用哪个转换器——``"auto"``（先试 pymupdf4llm，输出太稀疏再
  回退 markitdown）/ ``"pymupdf4llm"``（强制用，不回退）/ ``"markitdown"``（跳过 pymupdf4llm）。
  两个库都是 **soft-load**：没装就回退 / 跳过，上传本身不受影响。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# 允许的 PDF 转换器取值（与 conversion._ALLOWED_PDF_CONVERTERS 对齐）。
_ALLOWED_PDF_CONVERTERS = frozenset({"auto", "pymupdf4llm", "markitdown"})


class UploadsConfig(BaseModel):
    """文件上传子系统配置。

    所有字段都有默认值，因此 ``config.yaml`` 不写 ``uploads:`` 段时也能跑——
    这对「教学起步」场景很重要（少配置即可用）。
    """

    auto_convert_documents: bool = Field(
        default=True,
        description=("上传后是否自动把 PDF/PPT/Excel/Word 转成 markdown。关闭则只存原文件。转换器（markitdown/pymupdf4llm）缺包时自动跳过，不影响上传本身。"),
    )

    pdf_converter: str = Field(
        default="auto",
        description=("PDF 转换器：'auto'（先 pymupdf4llm，太稀疏回退 markitdown）/ 'pymupdf4llm'（强制，不回退）/ 'markitdown'（跳过 pymupdf4llm）。"),
    )

    def normalized_pdf_converter(self) -> str:
        """返回归一化（小写）并校验过的 pdf_converter，非法值回退 'auto'。

        防止 ``config.yaml`` 写成 ``AUTO`` / ``MarkItDown`` 等大小写/拼写变体时
        静默走到非预期分支。
        """
        raw = (self.pdf_converter or "auto").strip().lower()
        if raw not in _ALLOWED_PDF_CONVERTERS:
            return "auto"
        return raw
