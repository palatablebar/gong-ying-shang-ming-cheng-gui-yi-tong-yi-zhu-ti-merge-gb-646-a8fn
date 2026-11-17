"""编码复用判定：何时算主体接替，何时必须旧主体退役、新主体另行启用。

判定只看证据，不看名称相似度一个维度：

1. 税号相同 → 同一主体（改名或补录），不进接替流程，交普通合并。
2. 税号不同且新主体无接替证据 → 名称再像也只是“疑似”，默认另行启用；
   名称等于旧主体而税号不同，这是最典型的编码复用，必须阻断按名合并。
3. 税号不同、旧主体已注销/吊销/停业、且有接替证据（名称一致或接替类
   标记词）→ 判主体接替，旧主体退役、余额可结转、付款对象切到新主体。
4. 税号不同、旧主体仍存续、即使有接替证据 → 只判“疑似接替”，一律人工
   确认；不能仅凭一个编码把旧账划走。

证据不论来自名称、状态还是标记词，都逐条落进 evidence，供复核页面展示
“接替依据”。函数是纯函数：同一输入必然得到同一结论与同一 evidence 顺序。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from ...config import (
    DORMANT_STATUSES,
    SUCCESSION_MARKERS,
    SUCCESSION_NAME_THRESHOLD,
)
from ...reasons import (
    S_CODE_REUSED,
    S_NEW_SEPARATE,
    S_NEW_TAX_CHANGED,
    S_OLD_UNSETTLED,
    S_SAME_SUBJECT,
    S_SUCCESSOR_CANDIDATE,
    S_SUCCESSOR_CONFIRMED,
)

VERDICT_SUCCESSOR = "successor"
VERDICT_SEPARATE = "separate"


@dataclass
class Assessment:
    verdict: str
    reason_code: str
    evidence: dict = field(default_factory=dict)


def _has_marker(*texts) -> bool:
    return any(marker in (text or "") for text in texts for marker in SUCCESSION_MARKERS)


def _same_entity_name(left: str, right: str, score: Decimal) -> bool:
    return bool(left) and left == right and score >= SUCCESSION_NAME_THRESHOLD


def assess(old: dict, new: dict, score: Decimal, *, new_name: str = "",
           old_name: str = "") -> Assessment:
    """返回接替判定与证据。score 为归一名的相似得分（0-1）。"""
    old_tax = old.get("tax_id", "")
    new_tax = new.get("tax_id", "")
    old_status = (old.get("entity_status") or "").strip()
    old_norm = old_name or old.get("norm_name", "")
    new_norm = new_name or new.get("norm_name", "")
    same_name = _same_entity_name(old_norm, new_norm, Decimal(str(score)))
    marker = _has_marker(new.get("raw_name", new.get("name", "")),
                         new.get("erp", ""), new.get("note", ""))
    dormant = old_status in DORMANT_STATUSES
    balance = Decimal(str(old.get("open_balance") or "0.00"))
    evidence = {
        "old_status": old_status,
        "old_dormant": dormant,
        "same_name": same_name,
        "name_score": f"{Decimal(str(score)):.4f}",
        "tax_changed": bool(old_tax and new_tax and old_tax != new_tax),
        "bank_changed": bool(old.get("bank_account") and new.get("bank_account")
                             and old["bank_account"] != new["bank_account"]),
        "succession_marker": marker,
        "old_balance": str(balance),
        "old_tax": old_tax,
        "new_tax": new_tax,
        "old_name": old_norm,
        "new_name": new_norm,
        "reason_codes": [],
    }

    # 税号相同：同一主体，不属于编码复用接替。
    if old_tax and old_tax == new_tax:
        evidence["reason_codes"] = [S_SAME_SUBJECT]
        return Assessment(VERDICT_SEPARATE, S_SAME_SUBJECT, evidence)

    codes = [S_CODE_REUSED]
    if evidence["tax_changed"]:
        codes.append(S_NEW_TAX_CHANGED)
    if balance > 0:
        codes.append(S_OLD_UNSETTLED)

    support = int(same_name) + int(dormant) + int(marker)
    if dormant and (same_name or marker) and support >= 2:
        evidence["support"] = support
        evidence["reason_codes"] = codes + [S_SUCCESSOR_CONFIRMED]
        return Assessment(VERDICT_SUCCESSOR, S_SUCCESSOR_CONFIRMED, evidence)

    evidence["support"] = support
    if same_name or marker:
        evidence["reason_codes"] = codes + [S_SUCCESSOR_CANDIDATE]
        return Assessment(VERDICT_SEPARATE, S_SUCCESSOR_CANDIDATE, evidence)

    evidence["reason_codes"] = codes + [S_NEW_SEPARATE]
    return Assessment(VERDICT_SEPARATE, S_NEW_SEPARATE, evidence)
