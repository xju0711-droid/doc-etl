"""
PDF 解析器。

为什么用 PyMuPDF（fitz）：
  - 速度快（C 底层），支持扫描件检测
  - 能按页读取，方便分块处理大文件
  - 比 pdfplumber 对复杂排版的容错更好
"""

import logging
import fitz  # PyMuPDF

logger = logging.getLogger(__name__)


def parse_pdf(path: str) -> str:
    """提取 PDF 全文，保留段落换行，去除多余空白行。"""
    doc = fitz.open(path)
    pages: list[str] = []

    for page in doc:
        text = page.get_text("text")  # "text" 模式保留自然段落
        text = text.strip()
        if text:
            pages.append(text)

    page_count = len(doc)
    doc.close()

    logger.debug("PDF %s：共 %d 页，提取到 %d 页有效文本", path, page_count, len(pages))

    if not pages:
        logger.error("PDF 无可读文本（可能是纯图片扫描件）: %s", path)
        raise ValueError(f"PDF 无可读文本（可能是纯图片扫描件）: {path}")

    return "\n\n".join(pages)