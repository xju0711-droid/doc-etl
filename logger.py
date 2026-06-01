"""
集中日志配置。

策略：
  - 文件（logs/app.log）：记录 DEBUG 及以上，自动轮转（5MB × 3 个）
  - 终端：只显示 WARNING 及以上，不干扰正常输出
  - 第三方 HTTP 库的噪音日志单独压制

使用方法：
  在 main.py 启动时调用 setup_logging()，之后各模块直接用
  logging.getLogger(__name__) 即可，无需额外配置。
"""

import logging
import logging.handlers
from pathlib import Path


LOG_DIR = Path("logs")
LOG_FILE = LOG_DIR / "app.log"


def setup_logging() -> Path:
    """初始化日志系统，返回日志文件路径。幂等：重复调用无副作用。"""
    root = logging.getLogger()
    if root.handlers:
        return LOG_FILE  # 已初始化，跳过

    LOG_DIR.mkdir(exist_ok=True)

    fmt_file = logging.Formatter(
        "%(asctime)s [%(levelname)-8s] %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fmt_console = logging.Formatter("[%(levelname)s] %(message)s")

    # 文件：DEBUG+，5MB 自动轮转，保留最近 3 个文件
    fh = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt_file)

    # 终端：WARNING+，不干扰正常输出
    ch = logging.StreamHandler()
    ch.setLevel(logging.WARNING)
    ch.setFormatter(fmt_console)

    root.setLevel(logging.DEBUG)
    root.addHandler(fh)
    root.addHandler(ch)

    # 压制第三方库的噪音
    for name in ("httpx", "httpcore", "urllib3", "trafilatura", "fitz"):
        logging.getLogger(name).setLevel(logging.WARNING)

    logging.getLogger(__name__).debug("日志系统初始化完成，日志文件: %s", LOG_FILE)
    return LOG_FILE