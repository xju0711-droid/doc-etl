"""
网页解析器。

为什么用 trafilatura：
  - 专门做"正文提取"，自动丢弃导航栏/广告/页脚
  - 比 BeautifulSoup 手动提取干净得多
  - 既支持 URL（自动请求），也支持本地 HTML 文件
"""

import logging
import trafilatura

logger = logging.getLogger(__name__)


def parse_web(source: str) -> str:
    """
    source 可以是 URL 或本地 HTML 文件路径。
    返回网页正文纯文本。
    """
    if source.startswith("http://") or source.startswith("https://"):
        logger.debug("抓取网页: %s", source)
        html = trafilatura.fetch_url(source)
        if not html:
            logger.error("无法获取网页内容: %s", source)
            raise ValueError(f"无法获取网页内容: {source}")
    else:
        with open(source, "r", encoding="utf-8", errors="ignore") as f:
            html = f.read()

    text = trafilatura.extract(
        html,
        include_comments=False,
        include_tables=True,
        no_fallback=False,
    )

    if not text:
        logger.error("无法从页面提取正文: %s", source)
        raise ValueError(f"无法从页面提取正文: {source}")

    logger.debug("网页 %s 提取正文 %d 字符", source, len(text))
    return text.strip()