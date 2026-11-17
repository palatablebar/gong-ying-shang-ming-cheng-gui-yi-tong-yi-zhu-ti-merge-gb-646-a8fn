"""M8 编码复用接替编排：判定案件、人工确认、发布计划、一次性切换与回滚。

发布采用“回执 + 单事务”的方式：
- 预览计划产出确定性 ``payload_sha256``，发布请求必须带回同一个摘要，
  否则视为页面停留期间数据已变（``S_PAYLOAD_DRIFT``）。
- ``receipt_id`` 由批次、输入摘要与计划摘要共同决定；同一批同一计划重复
  发布直接返回原回执，不新增版本、不重复写台账。
- 正式主数据、台账分录、案件状态与回执写在同一个 SQLite 事务里；任一步
  失败整体回滚，旧版整版保留在 ``master_snapshots``，可原样恢复。
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal

from ... import store
from ...config import RULE_VERSION
from ...errors import Conflict, DomainError, NotFound
from ...manifest import canonical_json, utc_iso, utc_now
from ...reasons import (
    S_ALREADY_PUBLISHED,
    S_BAD_VERDICT,
    S_BALANCE_RETAINED,
    S_NOT_APPROVED,
    S_OLD_RETIRED,
    S_PAYLOAD_DRIFT,
    S_PUBLISH_FAILED,
    S_RECEIPT_CONFLICT,
    S_RECORD_ID_CONFLICT,
    S_STALE_CASE,
    S_STALE_MASTER,
    S_UNKNOWN_CASE,
    V_UNKNOWN_RUN,
)
from ..m2_normalize import service as normalize_service
from ..m4_pairs import similarity
from ..m5_clusters import service as cluster_service
from . import classify, detect

CENT = Decimal("0.01")
APPROVED = "approved"
PUBLISHED = "published"
PENDING = "pending"
ALLOWED_VERDICTS = (classify.VERDICT_SUCCESSOR, classify.VERDICT_SEPARATE)

# 故障注入钩子：测试把它替换成抛异常的函数，验证事务整体回滚。
_after_write_hook = None


def _money(value) -> Decimal:
    return Decimal(str(value or "0.00")).quantize(CENT)


def _score(left: str, right: str) -> Decimal:
    return similarity.score(left, right)["score"]


def ensure_cases(run_id: str, rebuild: bool = False) -> list:
    """按当前批次重建接替案件；已批准/已发布的案件保留人工结论。"""
    run = store.get_run(run_id)
    if not run:
        raise NotFound(V_UNKNOWN_RUN, run_id)
    normalize_service.ensure_normalized(run_id)
    records = store.list_records(run_id)
    if not records:
        raise NotFound(V_UNKNOWN_RUN, run_id)
    if not rebuild and store.count(run_id, "succession_cases"):
        return store.list_succession_cases(run_id)
    cases = detect.build_cases(records, _score)
    store.save_succession_cases(run_id, cases)
    return store.list_succession_cases(run_id)


def case_payload(row, records=None) -> dict:
    evidence = json.loads(row.get("evidence_json") or "{}")
    payload = {
        **row,
        "evidence": evidence,
        "decided_verdict": row.get("decided_verdict") or "",
        "proposed_verdict": row["verdict"],
        "effective_verdict": row.get("decided_verdict") or row["verdict"],
    }
    if records:
        payload["old"] = records.get(row["old_id"], {})
        payload["new"] = records.get(row["new_id"], {})
    return payload


def list_cases(run_id: str, rebuild: bool = False) -> list:
    rows = ensure_cases(run_id, rebuild=rebuild)
    records = {item["supplier_id"]: item for item in store.list_records(run_id)}
    impacts = {}
    for impact in store.list_succession_impacts(run_id):
        impacts.setdefault(impact["case_id"], []).append(impact)
    payloads = []
    for row in rows:
        payload = case_payload(row, records)
        payload["impacts"] = impacts.get(row["case_id"], [])
        payload["bound_balance"] = str(sum(
            (_money(item["amount"]) for item in payload["impacts"]
             if item["impact_code"] == "S_OLD_UNSETTLED"), Decimal("0.00")))
        payloads.append(payload)
    return payloads


def get_case(run_id: str, case_id: str) -> dict:
    ensure_cases(run_id)
    row = store.get_succession_case(run_id, case_id)
    if not row:
        raise NotFound(S_UNKNOWN_CASE, case_id)
    records = {item["supplier_id"]: item for item in store.list_records(run_id)}
    payload = case_payload(row, records)
    payload["impacts"] = store.list_succession_impacts(run_id, case_id)
    return payload


def decide(run_id: str, case_id: str, verdict: str, expected_version: int,
           operator: str = "采购主数据组") -> dict:
    if verdict not in ALLOWED_VERDICTS:
        raise DomainError(S_BAD_VERDICT, verdict)
    case = get_case(run_id, case_id)
    if case["state"] == PUBLISHED:
        raise Conflict(S_ALREADY_PUBLISHED, case_id)
    if expected_version != case["version"]:
        raise Conflict(S_STALE_CASE, f"当前版本 {case['version']}")
    if verdict == classify.VERDICT_SUCCESSOR:
        support = int(case["evidence"].get("support", 0))
        if support < 2:
            raise DomainError(S_BAD_VERDICT, "接替证据不足，不能直接确认接替")
    updated = store.record_succession_decision(run_id, case_id, expected_version, verdict,
                                               operator, utc_iso(utc_now()))
    if not updated:
        raise Conflict(S_STALE_CASE, f"当前版本 {case['version']}")
    return get_case(run_id, case_id)


# ---- 发布计划 ----

def _clusters_context(run_id: str):
    cluster_service.ensure_clusters(run_id)
    records = {row["supplier_id"]: row for row in store.list_records(run_id)}
    clusters = store.list_clusters(run_id)
    rep_of = {cluster["cluster_id"]: cluster["rep_id"] for cluster in clusters}
    cluster_of = {row["supplier_id"]: rep_of.get(row["cluster_id"], row["cluster_id"])
                  for row in store.list_cluster_members(run_id)}
    balances = {}
    for member in store.list_cluster_members(run_id):
        record = records.get(member["supplier_id"], {})
        balances[member["cluster_id"]] = balances.get(
            member["cluster_id"], Decimal("0.00")) + _money(record.get("open_balance"))
    return records, clusters, cluster_of, balances


def _baseline_entities(run_id: str, records, clusters, balances) -> tuple:
    entities, collisions = {}, []
    for cluster in clusters:
        rep = records.get(cluster["rep_id"], {})
        city = cluster["city"]
        entities[cluster["rep_id"]] = {
            "entity_id": cluster["rep_id"],
            "supplier_no": rep.get("supplier_no", ""),
            "name": rep.get("raw_name", ""),
            "tax_id": cluster["tax_id"],
            "city": city,
            "bank_account": rep.get("bank_account", ""),
            "status": "active",
            "balance": _money(balances.get(cluster["cluster_id"], "0.00")),
            "cluster_id": cluster["cluster_id"],
            "origin": "ledger",
            "basis": "",
            "action": "keep",
        }
    prior = store.latest_master_version()
    if prior:
        prior_rows = store.list_master_entities(prior)
        index = {(row["supplier_no"], row["tax_id"]): row for row in prior_rows}
        for entity in entities.values():
            match = index.get((entity["supplier_no"], entity["tax_id"]))
            if match is None:
                continue
            # 已发布版本是该主体的正式台账状态：重跑不得把源数据的余额再加一遍。
            entity["balance"] = _money(match["balance"])
            entity["status"] = match["status"]
            entity["basis"] = match["basis"]
            entity["origin"] = "master"
        carried_codes = {(entity["supplier_no"], entity["tax_id"])
                         for entity in entities.values()}
        for row in prior_rows:
            if (row["supplier_no"], row["tax_id"]) in carried_codes:
                continue
            entity = dict(row)
            entity["balance"] = _money(entity["balance"])
            entity["action"] = "carry"
            entity["origin"] = "carried"
            if entity["entity_id"] in entities:
                # 同一记录号在本批已代表另一个税号的主体：不能用旧版覆盖本批主体，
                # 也不能默默丢掉旧版余额，直接阻断发布让人工处理。
                collisions.append({"entity_id": entity["entity_id"],
                                   "prior_tax": entity["tax_id"],
                                   "current_tax": entities[entity["entity_id"]]["tax_id"]})
                continue
            entities[entity["entity_id"]] = entity
    return entities, collisions


def _apply_cases(entities, cases, cluster_of) -> tuple:
    blocked = []
    for case in cases:
        if case["state"] not in (APPROVED, PUBLISHED):
            blocked.append({"case_id": case["case_id"], "reason_code": S_NOT_APPROVED,
                            "detail": "接替案件尚未批准"})
            continue
        verdict = case.get("decided_verdict") or case["verdict"]
        old_entity = cluster_of.get(case["old_id"])
        new_entity = cluster_of.get(case["new_id"])
        if not old_entity or not new_entity or old_entity == new_entity:
            blocked.append({"case_id": case["case_id"], "reason_code": S_BAD_VERDICT,
                            "detail": "旧新主体落在同一合并簇，需先拆分"})
            continue
        old = entities.get(old_entity)
        new = entities.get(new_entity)
        if old is None or new is None:
            blocked.append({"case_id": case["case_id"], "reason_code": S_BAD_VERDICT,
                            "detail": "正式主数据缺少对应主体"})
            continue
        if old_entity == new_entity or old["tax_id"] == new["tax_id"]:
            # 同税号同簇说明二者本就是一个主体：编码复用不成立，拒绝发布。
            blocked.append({"case_id": case["case_id"], "reason_code": S_BAD_VERDICT,
                            "detail": "旧新主体税号一致，不构成编码复用"})
            continue
        if verdict == classify.VERDICT_SUCCESSOR:
            moved = _money(old["balance"])
            new["balance"] = _money(new["balance"]) + moved
            old["balance"] = Decimal("0.00")
            old["status"] = "retired"
            old["action"] = "retire"
            old["basis"] = f"{case['case_id']}:{S_OLD_RETIRED}"
            new["status"] = "active"
            new["action"] = "successor"
            new["basis"] = f"{case['case_id']}:S_BALANCE_TRANSFER"
        else:
            # 另行启用：旧主体若仍在存续，不能仅凭一次编码复用就退役；
            # 只有源数据已注销/吊销时才正式落为退役，其余保留可结算状态。
            if case["evidence"].get("old_dormant"):
                old["status"] = "retired"
                old["action"] = "retire_retain"
            else:
                old["status"] = "active"
                old["action"] = "retain"
            old["basis"] = f"{case['case_id']}:{S_BALANCE_RETAINED}"
            new["status"] = "pending_account"
            new["action"] = "separate"
            new["basis"] = f"{case['case_id']}:{S_BALANCE_RETAINED}"
    return blocked


def _ledger_entries(entities) -> list:
    entries = []
    for entity_id in sorted(entities):
        entity = entities[entity_id]
        balance = _money(entity["balance"])
        if entity["status"] == "retired" and balance == 0:
            continue
        if entity["status"] == "pending_account":
            # 另行启用的新主体即使当前没有应付，也保留一条台账说明“暂缓付款”，
            # 让付款岗看到该主体已启用但账户尚未核验。
            detail = "新主体另行启用，账户未核验，暂缓付款"
        elif entity["status"] == "retired":
            detail = "旧主体退役，仅结算保留余额"
        elif entity.get("action") == "successor":
            detail = "接替成立，余额结转至新主体"
        elif entity.get("action") == "retain":
            detail = "旧主体仍存续，余额留在旧主体按原账户结算"
        else:
            detail = "正常结算"
        entries.append({
            "entry_id": f"LE-{entity_id}",
            "entity_id": entity_id,
            "payable_amount": balance,
            "receivable_amount": Decimal("0.00"),
            "receivable_owner": "集团内部往来",
            "detail": detail,
        })
    return entries


def build_plan(run_id: str) -> dict:
    run = store.get_run(run_id)
    if not run:
        raise NotFound(V_UNKNOWN_RUN, run_id)
    cases = list_cases(run_id)
    records, clusters, cluster_of, balances = _clusters_context(run_id)
    entities, collisions = _baseline_entities(run_id, records, clusters, balances)
    blocked = _apply_cases(entities, cases, cluster_of)
    for collision in collisions:
        blocked.append({"case_id": collision["entity_id"],
                        "reason_code": S_RECORD_ID_CONFLICT,
                        "detail": "本批记录号已代表另一税号主体，旧版余额无法安全承接"})
    ledger = _ledger_entries(entities)

    record_balance = sum((_money(row.get("open_balance")) for row in records.values()),
                         Decimal("0.00"))
    baseline = sum((_money(entity["balance"]) for entity in entities.values()
                    if entity.get("origin") in ("ledger", "master")), Decimal("0.00"))
    ledger_total = sum((entry["payable_amount"] for entry in ledger), Decimal("0.00"))
    entity_total = sum((_money(entity["balance"]) for entity in entities.values()),
                       Decimal("0.00"))
    predecessor = store.latest_master_version()
    checks = [
        {"name": "余额归属守恒", "left": str(entity_total), "right": str(ledger_total),
         "ok": entity_total == ledger_total},
        {"name": "记录余额入账", "left": str(record_balance), "right": str(baseline),
         "ok": baseline == record_balance},
        {"name": "无待判案件", "left": sum(1 for item in cases if item["state"] == PENDING),
         "right": 0, "ok": not any(item["state"] == PENDING for item in cases)},
        {"name": "无阻断案件", "left": len(blocked), "right": 0, "ok": not blocked},
    ]
    payload = {
        "run_id": run_id,
        "input_sha256": run["input_sha256"],
        "rule_version": RULE_VERSION,
        "prior_version": predecessor,
        "cases": [{"case_id": item["case_id"], "verdict": item["effective_verdict"],
                   "version": item["version"], "state": item["state"]} for item in cases],
        "entities": [{"entity_id": key, **{name: str(value) if name == "balance" else value
                                          for name, value in entity.items()
                                          if name != "entity_id"}}
                     for key, entity in sorted(entities.items())],
        "ledger": [{"entry_id": entry["entry_id"], "entity_id": entry["entity_id"],
                    "payable": str(entry["payable_amount"])} for entry in ledger],
    }
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return {
        "run_id": run_id,
        "prior_version": predecessor,
        "expected_prior_version": predecessor,
        "payload_sha256": digest,
        "cases": cases,
        "entities": [_public_entity(key, entity) for key, entity in
                     sorted(entities.items())],
        "ledger": [{**entry, "payable_amount": str(entry["payable_amount"]),
                    "receivable_amount": str(entry["receivable_amount"])}
                   for entry in ledger],
        "blocked": blocked,
        "checks": checks,
        "ok": all(check["ok"] for check in checks),
        "record_balance": str(record_balance),
        "entity_balance": str(entity_total),
    }


def _public_entity(entity_id: str, entity: dict) -> dict:
    """对外一律字符串金额，避免 JSON 浮点误差；内部运算仍用 Decimal。"""
    public = {**entity, "entity_id": entity_id}
    public["balance"] = str(_money(entity.get("balance", "0.00")))
    return public


# ---- 一次性发布 ----

def _receipt_id(run_id: str, payload_sha: str) -> str:
    digest = hashlib.sha256(f"{run_id}|{payload_sha}".encode("utf-8")).hexdigest()
    return "RC-" + digest[:12].upper()


class _PublishAbort(Exception):
    """事务内发现的并发冲突；回滚后按稳定原因码返回 409。"""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code} {detail}".strip())


def _find_committed(run_id: str, payload_sha: str = "", prior_version: int = 0):
    """若该批已有成功回执，返回回执视图；摘要/旧版本对不上时抛 409。"""
    for item in store.list_receipts(1000):
        if item["run_id"] != run_id or item["state"] != "committed":
            continue
        if payload_sha and payload_sha != item["payload_sha256"]:
            raise Conflict(S_PAYLOAD_DRIFT, f"已发布 {item['payload_sha256']}")
        if prior_version and prior_version != item["prior_version"]:
            raise Conflict(S_RECEIPT_CONFLICT, item["receipt_id"])
        return {**_receipt_view(item), "replayed": True}
    return None


def publish(run_id: str, expected_payload_sha256: str,
            expected_prior_version: int, operator: str = "采购主数据组") -> dict:
    # 同一批已成功发布过：直接回放原回执，不再迁移余额、不再新开台账版本。
    # 调用方若显式带上了不同的计划摘要或旧版本，仍按漂移处理，而不是静默回放。
    replayed = _find_committed(run_id, expected_payload_sha256, expected_prior_version)
    if replayed:
        return replayed

    plan = build_plan(run_id)
    if plan["blocked"]:
        raise DomainError(S_NOT_APPROVED,
                          json.dumps(plan["blocked"], ensure_ascii=False))
    if expected_payload_sha256 and expected_payload_sha256 != plan["payload_sha256"]:
        # 并发发布可能在上面的回放检查之后已经落库：此时应回放对方回执，
        # 而不是把同一批的第二个请求误报成计划漂移。
        replayed = _find_committed(run_id, expected_payload_sha256, expected_prior_version)
        if replayed:
            return replayed
        raise Conflict(S_PAYLOAD_DRIFT, f"当前 {plan['payload_sha256']}")
    if not expected_payload_sha256:
        expected_payload_sha256 = plan["payload_sha256"]
    if expected_prior_version != plan["prior_version"]:
        raise Conflict(S_STALE_MASTER, f"当前版本 {plan['prior_version']}")

    receipt_id = _receipt_id(run_id, expected_payload_sha256)
    existing = store.get_receipt(receipt_id)
    if existing and existing["state"] == "committed":
        if existing["payload_sha256"] != expected_payload_sha256:
            raise Conflict(S_RECEIPT_CONFLICT, receipt_id)
        return _receipt_view(existing)

    now = utc_iso(utc_now())
    prior = plan["prior_version"]
    version = prior + 1
    try:
        with store.transaction() as conn:
            # 事务内再查一次：两个请求同时通过上面的预检时，只有一个能拿到写锁。
            locked = conn.execute(
                "SELECT version FROM master_versions ORDER BY version DESC LIMIT 1"
            ).fetchone()
            locked_prior = int(locked["version"]) if locked else 0
            if locked_prior != prior:
                raise _PublishAbort(S_STALE_MASTER, f"当前版本 {locked_prior}")
            existing = conn.execute(
                "SELECT receipt_id, payload_sha256 FROM publish_receipts"
                " WHERE receipt_id = ?", (receipt_id,)).fetchone()
            if existing and existing["payload_sha256"] != expected_payload_sha256:
                raise _PublishAbort(S_RECEIPT_CONFLICT, receipt_id)
            arrived = conn.execute(
                "SELECT receipt_id FROM publish_receipts WHERE run_id = ?"
                " AND state = 'committed' LIMIT 1", (run_id,)).fetchone()
            if arrived:
                raise _PublishAbort(S_ALREADY_PUBLISHED, arrived["receipt_id"])
            if prior:
                store.snapshot_master(conn, prior, run_id, "发布前整版保留")
            store.save_receipt(conn, {
                "receipt_id": receipt_id,
                "run_id": run_id,
                "payload_sha256": expected_payload_sha256,
                "version": version,
                "prior_version": prior,
                "state": "writing",
                "created_at": now,
                "committed_at": "",
                "detail": "",
            })
            counts = {"entities": 0, "retired": 0, "successor": 0, "separate": 0,
                      "payable": "0.00"}
            entity_rows, ledger_rows = [], []
            for entity in plan["entities"]:
                entity_rows.append((
                    version, entity["entity_id"], entity["supplier_no"], entity["name"],
                    entity["tax_id"], entity["city"], entity["bank_account"],
                    entity["status"], str(_money(entity["balance"])), entity["cluster_id"],
                    entity.get("origin", "ledger"), entity.get("basis", ""),
                ))
                counts["entities"] += 1
                if entity["status"] == "retired":
                    counts["retired"] += 1
                if entity.get("action") == "successor":
                    counts["successor"] += 1
                if entity.get("action") == "separate":
                    counts["separate"] += 1
            for entry in plan["ledger"]:
                ledger_rows.append((
                    version, entry["entry_id"], entry["entity_id"],
                    str(_money(entry["payable_amount"])),
                    str(_money(entry["receivable_amount"])),
                    entry["receivable_owner"], entry["detail"],
                ))
            conn.executemany(
                "INSERT INTO master_entities (version, entity_id, supplier_no, name, tax_id,"
                " city, bank_account, status, balance, cluster_id, origin, basis)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", entity_rows)
            conn.executemany(
                "INSERT INTO master_ledger_entries (version, entry_id, entity_id,"
                " payable_amount, receivable_amount, receivable_owner, detail)"
                " VALUES (?,?,?,?,?,?,?)", ledger_rows)
            counts["payable"] = str(sum(
                (_money(entry["payable_amount"]) for entry in plan["ledger"]),
                Decimal("0.00")))
            conn.execute(
                "INSERT INTO master_versions (version, run_id, receipt_id, state,"
                " published_at, operator, note, counts_json, prior_version)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (version, run_id, receipt_id, "active", now, operator,
                 "编码复用接替一次性切换", canonical_json(counts), prior))
            for case in plan["cases"]:
                if case["state"] != APPROVED:
                    continue
                conn.execute(
                    "UPDATE succession_cases SET state = 'published', published_at = ?,"
                    " receipt_id = ? WHERE run_id = ? AND case_id = ?",
                    (now, receipt_id, run_id, case["case_id"]))
            if _after_write_hook is not None:
                _after_write_hook(conn, version)
            conn.execute(
                "UPDATE publish_receipts SET state = 'committed', committed_at = ?,"
                " detail = ? WHERE receipt_id = ?",
                (now, canonical_json(counts), receipt_id))
            store.snapshot_master(conn, version, run_id, "本次发布")
            if prior:
                conn.execute("UPDATE master_versions SET state = 'superseded'"
                             " WHERE version = ?", (prior,))
    except _PublishAbort as abort:
        raise Conflict(abort.code, abort.detail) from abort
    except Exception as error:  # noqa: BLE001 - 统一转成可读原因码并保留旧版
        raise DomainError(S_PUBLISH_FAILED, str(error), status=500) from error
    return _receipt_view(store.get_receipt(receipt_id))



def _receipt_view(receipt: dict) -> dict:
    detail = receipt.get("detail") or "{}"
    try:
        counts = json.loads(detail)
    except ValueError:
        counts = {}
    return {**receipt, "counts": counts}


def receipts(limit: int = 20) -> list:
    return [_receipt_view(row) for row in store.list_receipts(limit)]


def master(version: int | None = None) -> dict:
    target = version or store.latest_master_version()
    if not target:
        return {"version": 0, "entities": [], "ledger": [], "counts": {}}
    meta = store.get_master_version(target)
    if not meta:
        raise NotFound(V_UNKNOWN_RUN, str(target))
    return {
        "version": target,
        "meta": meta,
        "entities": store.list_master_entities(target),
        "ledger": store.list_master_entries(target),
        "counts": json.loads(meta["counts_json"] or "{}"),
    }


def restore(version: int, operator: str = "采购主数据组") -> dict:
    """把整版快照恢复为一个新版本；原版本与快照都不删除，可继续追溯。"""
    snapshot = store.get_snapshot(version)
    if not snapshot:
        raise NotFound(V_UNKNOWN_RUN, f"快照 {version}")
    current = store.latest_master_version()
    target = current + 1
    now = utc_iso(utc_now())
    receipt_id = f"RS-{target:06d}"
    try:
        with store.transaction() as conn:
            if current:
                store.snapshot_master(conn, current, snapshot["run_id"], "恢复前整版保留")
            for entity in snapshot["entities"]:
                conn.execute(
                    "INSERT INTO master_entities (version, entity_id, supplier_no, name,"
                    " tax_id, city, bank_account, status, balance, cluster_id, origin, basis)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (target, entity["entity_id"], entity["supplier_no"], entity["name"],
                     entity["tax_id"], entity["city"], entity["bank_account"],
                     entity["status"], entity["balance"], entity["cluster_id"],
                     entity["origin"], entity["basis"]))
            for entry in snapshot["ledger"]:
                conn.execute(
                    "INSERT INTO master_ledger_entries (version, entry_id, entity_id,"
                    " payable_amount, receivable_amount, receivable_owner, detail)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (target, entry["entry_id"], entry["entity_id"],
                     entry["payable_amount"], entry["receivable_amount"],
                     entry["receivable_owner"], entry["detail"]))
            conn.execute(
                "INSERT INTO master_versions (version, run_id, receipt_id, state,"
                " published_at, operator, note, counts_json, prior_version)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (target, snapshot["run_id"], receipt_id, "restored", now, operator,
                 f"恢复自版本 {version}", canonical_json({"restored_from": version}),
                 current))
            store.save_receipt(conn, {
                "receipt_id": receipt_id, "run_id": snapshot["run_id"],
                "payload_sha256": f"restore:{version}", "version": target,
                "prior_version": current, "state": "committed", "created_at": now,
                "committed_at": now, "detail": canonical_json({"restored_from": version}),
            })
            conn.execute("UPDATE master_versions SET state = 'superseded'"
                         " WHERE version = ?", (current,))
            store.snapshot_master(conn, target, snapshot["run_id"], "恢复生成的版本")
    except Exception as error:  # noqa: BLE001
        raise DomainError(S_PUBLISH_FAILED, str(error), status=500) from error
    return master(target)


def statistics(run_id: str) -> dict:
    cases = list_cases(run_id)
    return {
        "run_id": run_id,
        "total": len(cases),
        "pending": sum(1 for row in cases if row["state"] == PENDING),
        "approved": sum(1 for row in cases if row["state"] == APPROVED),
        "published": sum(1 for row in cases if row["state"] == PUBLISHED),
        "successor": sum(1 for row in cases if row["effective_verdict"]
                         == classify.VERDICT_SUCCESSOR),
        "separate": sum(1 for row in cases if row["effective_verdict"]
                        == classify.VERDICT_SEPARATE),
        "balance": str(sum((_money(row["bound_balance"]) for row in cases),
                           Decimal("0.00"))),
    }
