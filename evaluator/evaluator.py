"""
质量评估层。

作用：
  LLM 抽取完之后，自动给结果打分，回答三个问题：
    1. JSON 解析成功了吗？（基础合法性）
    2. 必填字段都有值吗？（完整性）
    3. 整体填充率多少？（覆盖率）

  最终给出 0-100 的综合分 + pass/fail 判定。
  分数低的任务会被标记为"需要人工复查"。

为什么不用 LLM-as-Judge：
  基础校验用规则即可，速度快、零成本。
  LLM-as-Judge 适合评估语义质量（比如"摘要准不准确"），
  那是更高阶的需求，当前用规则已经覆盖最常见的失败场景。
"""

from dataclasses import dataclass
from typing import Any

from extractor.extract import ExtractionSchema, ExtractionResult


@dataclass
class QualityReport:
    score: float             # 0-100 综合分
    passed: bool             # True = 可信，False = 需要复查
    parse_ok: bool           # JSON 解析是否成功
    missing_required: list[str]   # 缺失的必填字段
    missing_optional: list[str]   # 缺失的选填字段
    fill_rate: float         # 已填字段占总字段的比例（0-1）
    notes: list[str]         # 可读的问题说明


def evaluate(result: ExtractionResult, schema: ExtractionSchema) -> QualityReport:
    """
    对一次抽取结果打分。
    返回 QualityReport，score >= 60 视为通过。
    """
    notes: list[str] = []
    score = 100.0

    # ── 1. JSON 解析检查 ─────────────────────────────────────
    if not result.parse_success:
        notes.append("LLM 返回内容无法解析为 JSON")
        return QualityReport(
            score=0, passed=False, parse_ok=False,
            missing_required=[], missing_optional=[],
            fill_rate=0.0, notes=notes
        )

    data = result.data

    # ── 2. 必填字段检查 ──────────────────────────────────────
    missing_required: list[str] = []
    for name, spec in schema.fields.items():
        if spec.required and (data.get(name) is None or data.get(name) == ""):
            missing_required.append(name)

    if missing_required:
        penalty = len(missing_required) * 25  # 每缺一个必填扣 25 分
        score -= penalty
        notes.append(f"缺失必填字段: {', '.join(missing_required)}")

    # ── 3. 选填字段覆盖率 ────────────────────────────────────
    all_fields = list(schema.fields.keys())
    filled = [f for f in all_fields if data.get(f) is not None and data.get(f) != ""]
    fill_rate = len(filled) / len(all_fields) if all_fields else 1.0

    missing_optional = [
        f for f in all_fields
        if f not in missing_required and data.get(f) is None
    ]

    if fill_rate < 0.5:
        score -= 20
        notes.append(f"字段填充率偏低: {fill_rate:.0%}")

    # ── 4. 使用了降级模型 ─────────────────────────────────────
    from config import cfg
    if result.model_used != cfg.PRIMARY_MODEL:
        score -= 5
        notes.append(f"使用了降级模型: {result.model_used}")

    score = max(0.0, min(100.0, score))

    return QualityReport(
        score=round(score, 1),
        passed=score >= 60,
        parse_ok=True,
        missing_required=missing_required,
        missing_optional=missing_optional,
        fill_rate=round(fill_rate, 3),
        notes=notes,
    )