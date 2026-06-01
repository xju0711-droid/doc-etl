"""
字段格式校验器。

根据字段名关键词自动推断要校验的格式：
  邮箱、电话、日期、金额 → 对应的 regex 校验
  其他字段 → 只检查是否为空

不依赖任何 LLM，纯规则，毫秒级完成。
"""

import re
from dataclasses import dataclass


@dataclass
class CheckResult:
    ok: bool
    status: str   # "ok" | "empty" | "fmt_error"
    note: str     # 空字符串表示没问题


_EMAIL_KEYS  = ("email", "mail", "邮箱", "邮件")
_PHONE_KEYS  = ("phone", "tel", "mobile", "电话", "手机", "联系方式", "联系电话")
_DATE_KEYS   = ("date", "time", "日期", "时间", "年份", "year", "month")
_AMOUNT_KEYS = ("amount", "price", "money", "fee", "cost", "salary",
                "金额", "价格", "费用", "工资", "薪资", "总价", "单价", "报价")


def _is_empty(value) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and value.strip() in ("", "null", "None", "—", "N/A", "无", "暂无"):
        return True
    if isinstance(value, (list, dict)) and len(value) == 0:
        return True
    return False


def validate(field_name: str, value) -> CheckResult:
    """校验单个字段，返回 CheckResult。"""
    if _is_empty(value):
        return CheckResult(False, "empty", "空值")

    name = field_name.lower()
    val  = str(value).strip()

    if any(k in name for k in _EMAIL_KEYS):
        if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", val):
            return CheckResult(True, "ok", "")
        return CheckResult(False, "fmt_error", "邮箱格式错误")

    if any(k in name for k in _PHONE_KEYS):
        digits = re.sub(r"[\s\-\(\)\+\.]", "", val)
        # 大陆手机号 / 固话 / 7-8位短号
        if re.fullmatch(r"(1[3-9]\d{9}|0\d{9,11}|\d{7,8})", digits):
            return CheckResult(True, "ok", "")
        return CheckResult(False, "fmt_error", "电话格式可疑")

    if any(k in name for k in _DATE_KEYS):
        patterns = [
            r"\d{4}[-/年]\d{1,2}([-/月]\d{1,2})?",
            r"\d{4}年\d{1,2}月",
            r"\d{4}[-/]\d{1,2}",
        ]
        if any(re.search(p, val) for p in patterns):
            return CheckResult(True, "ok", "")
        return CheckResult(False, "fmt_error", "日期格式可疑")

    if any(k in name for k in _AMOUNT_KEYS):
        clean = re.sub(r"[¥$￥,，\s元万亿千百]+", "", val)
        try:
            float(clean)
            return CheckResult(True, "ok", "")
        except ValueError:
            return CheckResult(False, "fmt_error", "金额格式可疑")

    return CheckResult(True, "ok", "")