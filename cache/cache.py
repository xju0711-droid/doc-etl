"""
缓存层。

作用：
  同一份文档 + 同一套 Schema → 只调用一次 LLM。
  之后命中缓存直接返回，省钱且速度从 10 秒降到毫秒级。

缓存 Key 的设计：
  key = hash(文档内容) + hash(Schema 定义)
  这样：
    - 文档内容变了 → 缓存失效（应该重新抽取）
    - Schema 变了（加了字段）→ 缓存失效（旧结果字段不全）
    - 两者都没变 → 命中缓存

为什么用 diskcache 而不是 Redis：
  - diskcache 是纯文件，无需安装任何服务，本机直接跑
  - 性能足够：10 万次/秒读写，LLM 场景完全够用
  - 生产环境要跨机器共享缓存，换 Redis 只需改这一个文件
"""

import hashlib
import json
import logging
from typing import Optional

import diskcache

from config import cfg
from extractor.extract import ExtractionSchema, ExtractionResult

logger = logging.getLogger(__name__)


_cache_instance: Optional["DocumentCache"] = None


def get_cache() -> "DocumentCache":
    """全局单例，避免重复打开磁盘文件。"""
    global _cache_instance
    if _cache_instance is None:
        _cache_instance = DocumentCache(cfg.CACHE_DIR)
    return _cache_instance


class DocumentCache:
    def __init__(self, cache_dir: str):
        self._disk = diskcache.Cache(cache_dir)

    def _make_key(self, text: str, schema: ExtractionSchema) -> str:
        """生成缓存 key：文档内容 + Schema 定义的组合哈希。"""
        content_hash = hashlib.sha256(text.encode()).hexdigest()[:20]
        schema_hash = hashlib.sha256(
            json.dumps(schema.model_dump(), sort_keys=True).encode()
        ).hexdigest()[:12]
        return f"doc:{content_hash}:{schema_hash}"

    def get(self, text: str, schema: ExtractionSchema) -> Optional[ExtractionResult]:
        """查缓存，命中返回 ExtractionResult，未命中返回 None。"""
        key = self._make_key(text, schema)
        raw = self._disk.get(key)
        if raw is None:
            logger.debug("缓存未命中: %s", key)
            return None
        logger.debug("缓存命中: %s", key)
        return ExtractionResult.model_validate(raw)

    def set(self, text: str, schema: ExtractionSchema, result: ExtractionResult):
        """写入缓存，永不过期（文档不变就一直有效）。"""
        key = self._make_key(text, schema)
        self._disk.set(key, result.model_dump())
        logger.debug("写入缓存: %s", key)

    def clear(self):
        self._disk.clear()

    def __len__(self):
        return len(self._disk)