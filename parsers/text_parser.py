"""
纯文本解析器。
处理 .txt / .md / .csv / .json 等文本格式。
作用是统一编码处理，去除多余空行，保持接口一致。
"""


import logging
import re

logger = logging.getLogger(__name__)


def parse_text(path: str) -> str:
    """读取文本文件，自动处理编码问题。"""
    for encoding in ("utf-8", "gbk", "latin-1"):
        try:
            with open(path, "r", encoding=encoding) as f:
                text = f.read()
            logger.debug("文本文件 %s 使用编码 %s 读取成功", path, encoding)
            break
        except UnicodeDecodeError:
            logger.debug("编码 %s 读取失败，尝试下一个: %s", encoding, path)
            continue
    else:
        logger.error("无法识别文件编码: %s", path)
        raise ValueError(f"无法识别文件编码: {path}")

    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()