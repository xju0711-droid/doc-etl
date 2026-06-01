"""
解析层入口：根据文件类型或 URL 自动选择对应的解析器。
对外只暴露一个函数 parse()，调用方不需要关心内部用了哪个解析器。
"""

import logging
from pathlib import Path
from .pdf_parser import parse_pdf
from .web_parser import parse_web
from .text_parser import parse_text

logger = logging.getLogger(__name__)


def parse(source: str) -> str:
    """
    统一入口。
    source 可以是：
      - 本地文件路径（.pdf / .txt / .md / .html）
      - http/https URL
    返回干净的纯文本，调用方无感知格式差异。
    """
    if source.startswith("http://") or source.startswith("https://"):
        logger.debug("解析网页: %s", source)
        return parse_web(source)

    path = Path(source)
    if not path.exists():
        logger.error("文件不存在: %s", source)
        raise FileNotFoundError(f"文件不存在: {source}")

    suffix = path.suffix.lower()
    if suffix == ".pdf":
        logger.debug("解析 PDF: %s", source)
        return parse_pdf(str(path))
    elif suffix in {".txt", ".md", ".csv", ".json"}:
        logger.debug("解析文本文件: %s", source)
        return parse_text(str(path))
    elif suffix in {".html", ".htm"}:
        logger.debug("解析本地 HTML: %s", source)
        return parse_web(str(path))
    else:
        logger.debug("未知格式，按纯文本处理: %s", source)
        return parse_text(str(path))