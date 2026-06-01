"""
LLM 客户端，含多模型降级 + 指数退避重试 + token 成本统计。

降级顺序：PRIMARY_MODEL → FALLBACK_MODELS，每个模型最多重试 MAX_RETRIES_PER_MODEL 次。
重试间隔：min(60, 1×2^attempt) + U(0,1) 秒（指数退避 + jitter）。
"""

import logging
import random
import time

import anthropic
import openai
from pydantic import BaseModel

from config import cfg

logger = logging.getLogger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_RETRIES_PER_MODEL = 3


# ── 定价表（每百万 token，美元）─────────────────────────────────
# 格式：模型名前缀 → (input_price, output_price)
_PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-4":     (15.0,  75.0),
    "claude-sonnet-4":   (3.0,   15.0),
    "claude-haiku-4":    (0.80,  4.0),
    "claude-3-5-sonnet": (3.0,   15.0),
    "claude-3-opus":     (15.0,  75.0),
    "claude-3-haiku":    (0.25,  1.25),
    "gpt-4o-mini":       (0.15,  0.60),
    "gpt-4o":            (2.5,   10.0),
    "gpt-3.5-turbo":     (0.50,  1.50),
    # Ollama 本地模型不在表中 → 成本为 0
}


def _calc_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    for prefix, (in_p, out_p) in _PRICING.items():
        if model.startswith(prefix):
            return (input_tokens * in_p + output_tokens * out_p) / 1_000_000
    return 0.0  # 本地模型


def _backoff_sleep(attempt: int) -> None:
    """指数退避 + jitter：min(60, 1×2^attempt) + U(0,1) 秒。"""
    delay = min(60.0, 1.0 * (2 ** attempt)) + random.random()
    logger.info("退避等待 %.1fs（第 %d 次重试）...", delay, attempt + 1)
    time.sleep(delay)


# ── Token 用量数据类 ──────────────────────────────────────────

class TokenUsage(BaseModel):
    input_tokens: int
    output_tokens: int
    model: str
    cost_usd: float

    def log(self) -> None:
        if self.cost_usd > 0:
            logger.info(
                "Token [%s]  输入: %d  输出: %d  成本: $%.4f",
                self.model, self.input_tokens, self.output_tokens, self.cost_usd,
            )
        else:
            logger.info(
                "Token [%s]  输入: %d  输出: %d  (本地模型，无 API 成本)",
                self.model, self.input_tokens, self.output_tokens,
            )

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        """合并两次调用的用量（用于修复调用叠加）。"""
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            model=self.model,
            cost_usd=self.cost_usd + other.cost_usd,
        )


# ── 异常 ────────────────────────────────────────────────────

class AllModelsFailedError(Exception):
    pass


# ── LLM 客户端 ────────────────────────────────────────────────

class LLMClient:
    def __init__(self):
        self._anthropic = (
            anthropic.Anthropic(api_key=cfg.ANTHROPIC_API_KEY)
            if cfg.ANTHROPIC_API_KEY else None
        )
        self._openai = (
            openai.OpenAI(api_key=cfg.OPENAI_API_KEY)
            if cfg.OPENAI_API_KEY else None
        )
        # Ollama 走 OpenAI 兼容接口
        self._ollama = openai.OpenAI(
            base_url=cfg.OLLAMA_BASE_URL,
            api_key="ollama",
        )
        self.model_chain: list[str] = [cfg.PRIMARY_MODEL] + cfg.FALLBACK_MODELS

    def complete(self, prompt: str) -> tuple[str, str, TokenUsage]:
        """
        发送 prompt，返回 (响应文本, 实际使用的模型名, token 用量)。
        每个模型最多重试 MAX_RETRIES_PER_MODEL 次，失败后用指数退避。
        """
        errors: list[str] = []

        for model in self.model_chain:
            for attempt in range(MAX_RETRIES_PER_MODEL):
                try:
                    logger.debug("调用模型: %s (attempt %d/%d)", model, attempt + 1, MAX_RETRIES_PER_MODEL)
                    t0 = time.time()
                    response, usage = self._call_model(model, prompt)
                    elapsed = time.time() - t0
                    logger.debug("模型 %s 响应完成，耗时 %.1fs，返回 %d 字符", model, elapsed, len(response))
                    usage.log()
                    return response, model, usage

                except Exception as e:
                    if self._is_retryable(e):
                        errors.append(f"{model}[attempt={attempt}]: {e}")
                        logger.warning(
                            "模型 %s 可重试错误 (%d/%d): %s",
                            model, attempt + 1, MAX_RETRIES_PER_MODEL, e,
                        )
                        if attempt < MAX_RETRIES_PER_MODEL - 1:
                            _backoff_sleep(attempt)
                        # 最后一次重试失败 → 继续尝试下一个模型
                    else:
                        logger.error("模型 %s 不可重试错误: %s", model, e, exc_info=True)
                        raise

        logger.error("所有模型均失败: %s", errors)
        raise AllModelsFailedError("所有模型均失败:\n" + "\n".join(errors))

    # ── 各平台调用 ────────────────────────────────────────────

    def _call_model(self, model: str, prompt: str) -> tuple[str, TokenUsage]:
        if model.startswith("claude"):
            return self._call_claude(model, prompt)
        elif model.startswith("gpt"):
            return self._call_openai(model, prompt)
        else:
            return self._call_ollama(model, prompt)

    def _call_claude(self, model: str, prompt: str) -> tuple[str, TokenUsage]:
        if not self._anthropic:
            raise ValueError("未配置 ANTHROPIC_API_KEY")
        msg = self._anthropic.messages.create(
            model=model,
            max_tokens=2048,
            timeout=cfg.LLM_TIMEOUT,
            messages=[{"role": "user", "content": prompt}],
        )
        in_tok, out_tok = msg.usage.input_tokens, msg.usage.output_tokens
        usage = TokenUsage(
            input_tokens=in_tok, output_tokens=out_tok,
            model=model, cost_usd=_calc_cost(model, in_tok, out_tok),
        )
        return msg.content[0].text, usage

    def _call_openai(self, model: str, prompt: str) -> tuple[str, TokenUsage]:
        if not self._openai:
            raise ValueError("未配置 OPENAI_API_KEY")
        resp = self._openai.chat.completions.create(
            model=model,
            max_tokens=2048,
            timeout=cfg.LLM_TIMEOUT,
            messages=[{"role": "user", "content": prompt}],
        )
        in_tok = resp.usage.prompt_tokens
        out_tok = resp.usage.completion_tokens
        usage = TokenUsage(
            input_tokens=in_tok, output_tokens=out_tok,
            model=model, cost_usd=_calc_cost(model, in_tok, out_tok),
        )
        return resp.choices[0].message.content, usage

    def _call_ollama(self, model: str, prompt: str) -> tuple[str, TokenUsage]:
        resp = self._ollama.chat.completions.create(
            model=model,
            max_tokens=2048,
            timeout=cfg.LLM_TIMEOUT,
            messages=[{"role": "user", "content": prompt}],
        )
        in_tok = getattr(resp.usage, "prompt_tokens", 0) or 0
        out_tok = getattr(resp.usage, "completion_tokens", 0) or 0
        usage = TokenUsage(input_tokens=in_tok, output_tokens=out_tok, model=model, cost_usd=0.0)
        return resp.choices[0].message.content, usage

    # ── 可重试判断 ────────────────────────────────────────────

    @staticmethod
    def _is_retryable(error: Exception) -> bool:
        if isinstance(error, anthropic.RateLimitError):
            return True
        if isinstance(error, anthropic.APIStatusError):
            return error.status_code in RETRYABLE_STATUS
        if isinstance(error, openai.RateLimitError):
            return True
        if isinstance(error, openai.APIStatusError):
            return error.status_code in RETRYABLE_STATUS
        if isinstance(error, (TimeoutError, ConnectionError)):
            return True
        return False