"""编码复用扫描与受影响记录：从采信结果里找出同码不同主体。

扫描口径：同一 ``supplier_no``（集团编码）下出现两个不同 ``supplier_id``，
且税号不同或一侧缺失。税号完全相同的属于同一主体改名/补录，不生成接替案件。

每案都产出：
- 旧/新主体两侧的身份字段，供页面并排核对；
- 受影响记录：旧主体的未结余额与稳定账户、新主体的税号与开户行变化、
  按名称合并的阻断提示、付款侧风险（账户未核验 / 重复付款）；
- 判定结论由 ``classify.assess`` 给出，这里只负责取证据。
"""
from __future__ import annotations

from decimal import Decimal

from ...config import DORMANT_STATUSES

from ...reasons import (
    S_ACCOUNT_BLOCKED,
    S_BALANCE_RETAINED,
    S_BALANCE_TRANSFER,
    S_CODE_REUSED,
    S_DUPLICATE_PAYMENT_RISK,
    S_NAME_MERGE_BLOCKED,
    S_NEW_BANK_CHANGED,
    S_NEW_SEPARATE,
    S_NEW_TAX_CHANGED,
    S_OLD_RETIRED,
    S_OLD_UNSETTLED,
)
from . import classify

OLD, NEW = "old", "new"


def _amount(value) -> Decimal:
    try:
        return Decimal(str(value or "0.00"))
    except Exception:
        return Decimal("0.00")


def _impact(side: str, record: dict, code: str, *, field: str = "", old_value: str = "",
            new_value: str = "", amount: Decimal | None = None, detail: str = "") -> dict:
    return {
        "supplier_id": record["supplier_id"],
        "side": side,
        "impact_code": code,
        "field": field,
        "old_value": old_value,
        "new_value": new_value,
        "amount": str((amount if amount is not None else Decimal("0.00")).quantize(
            Decimal("0.01"))),
        "detail": detail,
    }


def build_cases(records, score_for) -> list:
    """records: 采信记录；score_for(a, b) 返回两个归一名得分。

    每个集团编码至多一个旧主体：优先取已注销、其次取有未结余额、最后取编号
    最小者；其余记录都作为新主体各成一案。这样三条以上记录共用编码也不会
    生成重复案件号。
    """
    by_code = {}
    for record in records:
        code = (record.get("supplier_no") or "").strip()
        if code:
            by_code.setdefault(code, []).append(record)

    cases = []
    for code in sorted(by_code):
        group = _rank_entities(by_code[code])
        if len(group) < 2:
            continue
        old = group[0]
        for new in group[1:]:
            if old.get("tax_id") and old.get("tax_id") == new.get("tax_id"):
                continue
            score = score_for(old.get("norm_name") or old["raw_name"],
                              new.get("norm_name") or new["raw_name"])
            assessment = classify.assess(old, new, score)
            cases.append(_case(code, old, new, assessment, score))

    for code in sorted(by_code):
        subset = [case for case in cases if case["code"] == code]
        for case in subset:
            # 案件号优先由集团编码决定：同一编码在任意批次里都是同一个案件号，
            # 重跑、增删文件都不会让已批准案件与新建案件错位。一个编码下出现
            # 多个新主体时追加新主体编号，保证唯一。
            case["case_id"] = (f"SC-{code}" if len(subset) == 1
                               else f"SC-{code}-{case['new_id']}")
    return cases


def _rank_entities(group: list) -> list:
    """按“旧主体优先”排序：已注销 → 有未结余额 → 编号小。"""
    return sorted(group, key=lambda item: (
        (item.get("entity_status") or "").strip() not in DORMANT_STATUSES,
        -int(_amount(item.get("open_balance")) > 0),
        item["supplier_id"],
    ))


def _case(code: str, old: dict, new: dict, assessment, score: Decimal) -> dict:
    balance = _amount(old.get("open_balance"))
    impacts = [
        _impact(OLD, old, S_CODE_REUSED, field="supplier_no", old_value="", new_value=code,
                detail="编码在旧主体之后被重新分配给新主体"),
    ]
    if balance > 0:
        impacts.append(_impact(OLD, old, S_OLD_UNSETTLED, field="open_balance",
                               old_value=f"{balance:.2f}", new_value="",
                               amount=balance, detail="旧主体仍有未结余额，付款前必须定归属"))
    if assessment.verdict == classify.VERDICT_SUCCESSOR:
        impacts.append(_impact(OLD, old, S_OLD_RETIRED, field="entity_status",
                               old_value=old.get("entity_status", ""), new_value="退役",
                               detail="接替成立，旧主体退役"))
        impacts.append(_impact(OLD, old, S_BALANCE_TRANSFER, field="open_balance",
                               old_value=f"{balance:.2f}", new_value="0.00", amount=balance,
                               detail="未结余额结转新主体，旧主体不再可付"))
    else:
        if assessment.evidence.get("old_dormant"):
            impacts.append(_impact(OLD, old, S_OLD_RETIRED, field="entity_status",
                                   old_value=old.get("entity_status", ""), new_value="退役",
                                   detail="旧主体已注销，正式退役"))
        if balance > 0:
            impacts.append(_impact(OLD, old, S_BALANCE_RETAINED, field="open_balance",
                                   old_value=f"{balance:.2f}", new_value=f"{balance:.2f}",
                                   amount=balance,
                                   detail="未结余额留在旧主体，继续按原账户结算"))

    old_tax, new_tax = old.get("tax_id", ""), new.get("tax_id", "")
    if old_tax != new_tax:
        impacts.append(_impact(NEW, new, S_NEW_TAX_CHANGED, field="tax_id",
                               old_value=old_tax, new_value=new_tax,
                               detail="新主体税号与旧主体不同"))
    old_bank, new_bank = old.get("bank_account", ""), new.get("bank_account", "")
    if old_bank != new_bank and (old_bank or new_bank):
        impacts.append(_impact(NEW, new, S_NEW_BANK_CHANGED, field="bank_account",
                               old_value=old_bank, new_value=new_bank,
                               detail="新主体开户行/账号与旧主体不同"))
    if assessment.evidence.get("same_name"):
        impacts.append(_impact(NEW, new, S_NAME_MERGE_BLOCKED, field="name",
                               old_value=old.get("raw_name", ""),
                               new_value=new.get("raw_name", ""),
                               detail="名称相同但税号不同，禁止按名称合并"))
    if assessment.verdict != classify.VERDICT_SUCCESSOR:
        impacts.append(_impact(NEW, new, S_ACCOUNT_BLOCKED, field="bank_account",
                               old_value="", new_value=new_bank,
                               detail="新主体账户未核验，付款前需人工确认"))
        if balance > 0 and (old.get("entity_status") or "").strip() not in DORMANT_STATUSES:
            impacts.append(_impact(NEW, new, S_DUPLICATE_PAYMENT_RISK, field="open_balance",
                                   old_value=f"{balance:.2f}", new_value="0.00",
                                   detail="旧主体仍可结算，新主体另行启用，存在重复付款风险"))
    elif not new.get("tax_id"):
        impacts.append(_impact(NEW, new, S_NEW_SEPARATE, field="tax_id",
                               detail="新主体缺税号，接替依据需人工补齐"))
    return {
        "code": code,
        "old_id": old["supplier_id"],
        "new_id": new["supplier_id"],
        "old": old,
        "new": new,
        "verdict": assessment.verdict,
        "reason_code": assessment.reason_code,
        "evidence": {**assessment.evidence, "score": f"{score:.4f}"},
        "impacts": impacts,
    }
