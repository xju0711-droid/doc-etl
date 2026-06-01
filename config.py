"""
全局配置中心。
所有模块从这里读配置，避免在各处散落 os.getenv()。
"""

import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")

    # Ollama 本地模型（兼容 OpenAI 格式）
    OLLAMA_BASE_URL: str = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")

    PRIMARY_MODEL: str = os.getenv("PRIMARY_MODEL", "claude-sonnet-4-6")
    FALLBACK_MODELS: list[str] = [
        m.strip()
        for m in os.getenv("FALLBACK_MODELS", "").split(",")
        if m.strip()
    ]

    CACHE_DIR: str = os.getenv("CACHE_DIR", ".cache")
    OUTPUT_DIR: str = os.getenv("OUTPUT_DIR", "output")
    WORKER_THREADS: int = int(os.getenv("WORKER_THREADS", "4"))
    LLM_TIMEOUT: int = int(os.getenv("LLM_TIMEOUT", "120"))


cfg = Config()