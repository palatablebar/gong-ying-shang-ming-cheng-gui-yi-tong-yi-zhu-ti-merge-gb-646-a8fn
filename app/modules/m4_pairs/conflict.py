"""判定优先级：税号硬冲突 > 税号相同 > 城市不一致 > 名称得分。

规则集合按 rank 取最小者，因此调整规则书写顺序不改变结论。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ...config import AUTO_THRESHOLD
from ...reasons import (
    P_AUTO,
    P_CITY_MISMATCH,
    P_CODE_REUSE,
    P_REJECT,
    P_REVIEW,
    P_TAX_CONFLICT,
    P_TAX_MATCH,
)


@dataclass(frozen=True)
class Rule:
    name: str
    rank: int
    code: str
    verdict: str


RULES = (
    Rule("code_reuse", 5, P_CODE_REUSE, "reject"),
    Rule("tax_conflict", 10, P_TAX_CONFLICT, "reject"),
    Rule("tax_match", 20, P_TAX_MATCH, "auto"),
    Rule("city_mismatch", 30, P_CITY_MISMATCH, "review"),
    Rule("score_auto", 40, P_AUTO, "auto"),
    Rule("score_review", 50, P_REVIEW, "review"),
    Rule("score_reject", 60, P_REJECT, "reject"),
)


def _rule(name: str) -> Rule:
    for rule in RULES:
        if rule.name == name:
            return rule
    raise KeyError(name)


def is_code_reuse(left: dict, right: dict) -> bool:
    """同一集团编码下的两个不同记录号，且不是同一税号：编码被重新分配。

    编码复用必须先经接替复核，不能让名称相似度把它并入旧主体，也不能让
    普通合并把旧账直接带给新主体。税号完全相同的两条属于同主体补录，
    仍走普通合并。
    """
    left_code = (left.get("supplier_no") or "").strip()
    right_code = (right.get("supplier_no") or "").strip()
    if not left_code or left_code != right_code:
        return False
    if left.get("supplier_id") == right.get("supplier_id"):
        return False
    left_tax, right_tax = left.get("tax_id", ""), right.get("tax_id", "")
    return not (left_tax and left_tax == right_tax)


def applicable(left: dict, right: dict, score: Decimal) -> list:
    value = Decimal(str(score))
    left_tax, right_tax = left.get("tax_id", ""), right.get("tax_id", "")
    left_city, right_city = left.get("city", ""), right.get("city", "")
    hits = []
    if is_code_reuse(left, right):
        hits.append(_rule("code_reuse"))
    if left_tax and right_tax and left_tax != right_tax:
        hits.append(_rule("tax_conflict"))
    if left_tax and left_tax == right_tax:
        hits.append(_rule("tax_match"))
    if left_city and right_city and left_city != right_city and value >= AUTO_THRESHOLD:
        hits.append(_rule("city_mismatch"))
    if value >= AUTO_THRESHOLD:
        hits.append(_rule("score_auto"))
    elif value >= Decimal("0.80"):
        hits.append(_rule("score_review"))
    else:
        hits.append(_rule("score_reject"))
    return hits


def decide(left: dict, right: dict, score: Decimal) -> dict:
    hits = applicable(left, right, score)
    winner = min(hits, key=lambda rule: rule.rank)
    detail = " ".join([
        f"score={Decimal(str(score)):.4f}",
        f"tax={left.get('tax_id', '') or '-'}/{right.get('tax_id', '') or '-'}",
        f"city={left.get('city', '') or '-'}/{right.get('city', '') or '-'}",
    ])
    if is_code_reuse(left, right):
        detail += (f" code={left.get('supplier_no', '') or '-'}"
                   f" reused={left.get('supplier_id', '')}/{right.get('supplier_id', '')}")
    return {
        "verdict": winner.verdict,
        "reason_code": winner.code,
        "rule": winner.name,
        "rules": [rule.name for rule in sorted(hits, key=lambda item: item.rank)],
        "detail": detail,
    }
