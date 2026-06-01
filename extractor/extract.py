"""
LLM 抽取层核心逻辑。

JSON 修复状态机（当直接解析失败时进入 REPAIRING）：
  直接解析失败
    → 策略1: 正则提取第一个 {...} 块
    → 策略2: 把错误回喂给 LLM 让它修复
    → 策略3: ast.literal_eval / yaml 宽松解析
    → 全失败: parse_strategy="failed"，data={"_raw": ...}
"""

import ast
import json
import logging
import re
from typing import Any

from pydantic import BaseModel

from .llm_client import LLMClient, TokenUsage

logger = logging.getLogger(__name__)


# ── Schema 定义 ──────────────────────────────────────────────

class FieldSpec(BaseModel):
    description: str
    required: bool = False
    field_type: str = "str"


class ExtractionSchema(BaseModel):
    fields: dict[str, FieldSpec]


class ExtractionResult(BaseModel):
    data: dict[str, Any]
    model_used: str
    raw_response: str
    parse_success: bool
    parse_strategy: str = "direct"   # direct | regex | llm_repair | lenient | failed
    token_usage: TokenUsage | None = None


# ── Prompt 构造 ───────────────────────────────────────────────

def _build_prompt(text: str, schema: ExtractionSchema) -> str:
    fields_desc = "\n".join(
        f'  - "{name}": {spec.description}'
        f'{"（必填）" if spec.required else "（选填，没有就填 null）"}'
        f'，类型：{spec.field_type}'
        for name, spec in schema.fields.items()
    )
    return f"""你是一个结构化信息抽取助手。请从下方文档中提取指定字段，严格以 JSON 格式返回。

【需要提取的字段】
{fields_desc}

【要求】
- 只返回 JSON 对象，不要有任何解释或 markdown 代码块
- 找不到的字段填 null，不要编造
- list 类型字段返回数组，str 类型返回字符串

【文档内容】
{text[:8000]}

请直接返回 JSON："""


def _repair_prompt(raw: str, error: str, schema: ExtractionSchema) -> str:
    field_names = list(schema.fields.keys())
    return f"""你之前返回的 JSON 格式有误，请修复它。

【错误信息】
{error}

【你之前返回的内容（前 800 字符）】
{raw[:800]}

【要求】
- 只返回合法的 JSON 对象，不要任何解释
- 必须包含这些字段（找不到填 null）：{field_names}

请直接返回修复后的 JSON："""


# ── JSON 修复状态机 ───────────────────────────────────────────

def _try_parse(text: str) -> dict | None:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def _clean_markdown(raw: str) -> str:
    return raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()


def _strategy_regex(raw: str) -> dict | None:
    """策略1：正则提取最外层 {...} 块。"""
    # 先找嵌套较浅的完整 JSON 对象
    for pattern in [
        r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)?\}',   # 最多一层嵌套
        r'\{.*?\}',                              # 贪婪兜底
    ]:
        for m in reversed(re.findall(pattern, raw, re.DOTALL)):
            result = _try_parse(m)
            if isinstance(result, dict):
                return result
    return None


def _strategy_llm_repair(
    raw: str, parse_error: str, client: LLMClient, schema: ExtractionSchema
) -> tuple[dict | None, TokenUsage | None]:
    """策略2：把错误回喂给 LLM 让它修复，返回 (data_or_None, usage_or_None)。"""
    try:
        prompt = _repair_prompt(raw, parse_error, schema)
        repaired, _, usage = client.complete(prompt)
        result = _try_parse(_clean_markdown(repaired))
        if isinstance(result, dict):
            return result, usage
        return None, usage
    except Exception as e:
        logger.warning("策略2 (LLM 修复) 调用失败: %s", e)
        return None, None


def _strategy_lenient(raw: str) -> dict | None:
    """策略3：ast.literal_eval（处理单引号/Python dict）+ yaml（如已安装）。"""
    try:
        data = ast.literal_eval(raw.strip())
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    try:
        import yaml
        data = yaml.safe_load(raw)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return None


def _repair_json(
    raw: str,
    parse_error: str,
    client: LLMClient,
    schema: ExtractionSchema,
) -> tuple[dict, str, TokenUsage | None]:
    """
    依次尝试三种修复策略。
    返回 (data, strategy_name, extra_token_usage)。
    strategy_name: "regex" | "llm_repair" | "lenient" | "failed"
    """
    logger.info("JSON 解析失败，进入 REPAIRING 状态")

    logger.debug("REPAIRING → 策略1 (正则提取)")
    result = _strategy_regex(raw)
    if result is not None:
        logger.info("REPAIRING → 策略1 (正则) 成功")
        return result, "regex", None

    logger.debug("REPAIRING → 策略2 (LLM 回喂修复)")
    result, repair_usage = _strategy_llm_repair(raw, parse_error, client, schema)
    if result is not None:
        logger.info("REPAIRING → 策略2 (LLM 修复) 成功")
        return result, "llm_repair", repair_usage
    logger.warning("REPAIRING → 策略2 失败，repair_usage=%s", repair_usage)

    logger.debug("REPAIRING → 策略3 (宽松解析)")
    result = _strategy_lenient(raw)
    if result is not None:
        logger.info("REPAIRING → 策略3 (宽松解析) 成功")
        return result, "lenient", None

    logger.error("REPAIRING → 全部策略失败。原始输出前 300 字: %s", raw[:300])
    return {"_raw": raw}, "failed", None


# ── 主抽取函数 ────────────────────────────────────────────────

def extract_document(
    text: str,
    schema: ExtractionSchema,
    client: LLMClient,
) -> ExtractionResult:
    prompt = _build_prompt(text, schema)
    logger.debug("构建 prompt：%d 个字段，文档 %d 字符", len(schema.fields), len(text))

    raw, model_used, usage = client.complete(prompt)
    data = _try_parse(_clean_markdown(raw))

    if data is not None:
        logger.debug("JSON 直接解析成功，提取到 %d 个字段", len(data))
        return ExtractionResult(
            data=data,
            model_used=model_used,
            raw_response=raw,
            parse_success=True,
            parse_strategy="direct",
            token_usage=usage,
        )

    # 进入修复状态机
    logger.warning("JSON 直接解析失败，原始输出前 200 字: %s", raw[:200])
    repaired_data, strategy, repair_usage = _repair_json(raw, "JSONDecodeError", client, schema)

    # 合并 token 用量
    combined_usage = usage
    if repair_usage:
        combined_usage = usage + repair_usage

    return ExtractionResult(
        data=repaired_data,
        model_used=model_used,
        raw_response=raw,
        parse_success=(strategy != "failed"),
        parse_strategy=strategy,
        token_usage=combined_usage,
    )