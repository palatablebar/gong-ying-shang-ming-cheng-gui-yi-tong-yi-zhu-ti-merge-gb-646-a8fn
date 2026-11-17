"""M8 编码复用接替：判定证据、按名合并阻断、一次性切换、回滚与重跑幂等。"""
from __future__ import annotations

from decimal import Decimal

import pytest

from app import store
from app.config import SUCCESSION_FILES
from app.errors import Conflict, DomainError
from app.modules.m1_intake import service as intake_service
from app.modules.m5_clusters import service as cluster_service
from app.modules.m8_succession import classify, detect, service


@pytest.fixture()
def lifecycle(db):
    run = intake_service.ingest(files=SUCCESSION_FILES)
    cluster_service.ensure_clusters(run["run_id"])
    return run


def _approve_all(run_id: str) -> list:
    cases = service.list_cases(run_id)
    for case in cases:
        service.decide(run_id, case["case_id"], case["effective_verdict"], case["version"])
    return service.list_cases(run_id)


# ---- 判定边界 ----

def test_same_tax_is_not_code_reuse():
    """税号相同是同一主体改名/补录，不能当编码复用处理。"""
    old = {"supplier_id": "S-1", "supplier_no": "0001", "tax_id": "T1",
           "entity_status": "注销", "open_balance": "100.00", "norm_name": "甲"}
    new = {"supplier_id": "S-2", "supplier_no": "0001", "tax_id": "T1",
           "entity_status": "存续", "open_balance": "0.00", "norm_name": "甲"}
    assert detect.build_cases([old, new], lambda a, b: Decimal("1.000000")) == []
    result = classify.assess(old, new, Decimal("1.000000"))
    assert result.verdict == classify.VERDICT_SEPARATE
    assert result.reason_code == "S_SAME_SUBJECT"


def test_dormant_plus_same_name_is_succession():
    """旧主体已注销且名称一致：证据 2 条，判主体接替。"""
    old = {"supplier_id": "S-1", "supplier_no": "0001", "raw_name": "甲公司",
           "tax_id": "T1", "entity_status": "注销", "open_balance": "18000.00",
           "bank_account": "B1", "norm_name": "甲"}
    new = {"supplier_id": "S-2", "supplier_no": "0001", "raw_name": "甲公司",
           "tax_id": "T2", "entity_status": "存续", "open_balance": "0.00",
           "bank_account": "B2", "norm_name": "甲"}
    result = classify.assess(old, new, Decimal("1.000000"))
    assert result.verdict == classify.VERDICT_SUCCESSOR
    assert result.reason_code == "S_SUCCESSOR_CONFIRMED"
    assert result.evidence["support"] == 2
    assert result.evidence["tax_changed"] is True
    assert result.evidence["bank_changed"] is True


def test_same_name_but_old_active_is_only_candidate():
    """名称相同但旧主体仍存续：不得仅凭名称把旧账划给新主体。"""
    old = {"supplier_id": "S-1", "supplier_no": "0001", "raw_name": "甲公司",
           "tax_id": "T1", "entity_status": "存续", "open_balance": "7200.00",
           "bank_account": "B1", "norm_name": "甲"}
    new = {"supplier_id": "S-2", "supplier_no": "0001", "raw_name": "甲公司",
           "tax_id": "T2", "entity_status": "存续", "open_balance": "0.00",
           "bank_account": "B2", "norm_name": "甲"}
    result = classify.assess(old, new, Decimal("1.000000"))
    assert result.verdict == classify.VERDICT_SEPARATE
    assert result.reason_code == "S_SUCCESSOR_CANDIDATE"


def test_marker_plus_dormant_is_succession():
    """旧主体注销且新主体带“承继”标记：即使名称不同也判接替。"""
    old = {"supplier_id": "S-1", "supplier_no": "0001", "raw_name": "甲公司",
           "tax_id": "T1", "entity_status": "注销", "open_balance": "5400.00",
           "bank_account": "B1", "norm_name": "甲物流"}
    new = {"supplier_id": "S-2", "supplier_no": "0001", "raw_name": "乙公司",
           "tax_id": "T2", "entity_status": "存续", "open_balance": "0.00",
           "bank_account": "B2", "norm_name": "乙供应链", "erp": "ERP-G 承继"}
    result = classify.assess(old, new, Decimal("0.300000"))
    assert result.verdict == classify.VERDICT_SUCCESSOR
    assert result.evidence["succession_marker"] is True


def test_no_evidence_is_separate():
    """无名称、无状态、无标记：新主体另行启用，旧主体不被名称合并。"""
    old = {"supplier_id": "S-1", "supplier_no": "0001", "raw_name": "甲公司",
           "tax_id": "T1", "entity_status": "注销", "open_balance": "0.00",
           "bank_account": "B1", "norm_name": "甲远洋"}
    new = {"supplier_id": "S-2", "supplier_no": "0001", "raw_name": "乙公司",
           "tax_id": "T2", "entity_status": "存续", "open_balance": "0.00",
           "bank_account": "B2", "norm_name": "乙游艇"}
    result = classify.assess(old, new, Decimal("0.100000"))
    assert result.verdict == classify.VERDICT_SEPARATE
    assert result.reason_code == "S_NEW_SEPARATE"


def test_case_id_is_code_based():
    """案件号由编码决定，重跑与文件增减都不会让已批准案件错位。"""
    old = {"supplier_id": "S-1", "supplier_no": "0001", "raw_name": "甲公司",
           "tax_id": "T1", "entity_status": "注销", "open_balance": "0.00",
           "bank_account": "B1", "norm_name": "甲"}
    new = {"supplier_id": "S-2", "supplier_no": "0001", "raw_name": "乙公司",
           "tax_id": "T2", "entity_status": "存续", "open_balance": "0.00",
           "bank_account": "B2", "norm_name": "乙"}
    cases = detect.build_cases([old, new], lambda a, b: Decimal("0.100000"))
    assert cases[0]["case_id"] == "SC-0001"


# ---- 批次扫描 ----

def test_default_batch_has_no_reuse(run):
    """默认六份样例编码唯一，不产生接替案件，既有对账不受影响。"""
    assert service.list_cases(run["run_id"], rebuild=True) == []


def test_lifecycle_cases_detected(lifecycle):
    cases = {case["old_id"]: case for case in service.list_cases(lifecycle["run_id"])}
    assert set(cases) == {"S-0101", "S-0103", "S-0105", "S-0107"}
    assert cases["S-0101"]["effective_verdict"] == "successor"
    assert cases["S-0103"]["effective_verdict"] == "separate"
    assert cases["S-0105"]["effective_verdict"] == "separate"
    assert cases["S-0107"]["effective_verdict"] == "successor"
    assert cases["S-0103"]["evidence"]["same_name"] is True
    assert cases["S-0105"]["evidence"]["old_dormant"] is True
    assert cases["S-0107"]["evidence"]["succession_marker"] is True


def test_impacts_show_balance_and_accounts(lifecycle):
    case = {item["case_id"]: item for item in service.list_cases(lifecycle["run_id"])}
    target = case["SC-000202"]
    codes = {impact["impact_code"] for impact in target["impacts"]}
    assert {"S_CODE_REUSED", "S_OLD_UNSETTLED", "S_BALANCE_TRANSFER",
            "S_NEW_TAX_CHANGED", "S_NEW_BANK_CHANGED", "S_NAME_MERGE_BLOCKED"} <= codes
    unsettled = [item for item in target["impacts"]
                 if item["impact_code"] == "S_OLD_UNSETTLED"]
    assert unsettled[0]["amount"] == "18000.00"
    assert target["bound_balance"] == "18000.00"


def test_name_merge_blocked_in_pairs_and_clusters(lifecycle):
    """名称一致但税号不同：候选对判 P_CODE_REUSE，簇不得把旧新并到一起。"""
    run_id = lifecycle["run_id"]
    pairs = [row for row in store.list_pairs(run_id) if row["reason_code"] == "P_CODE_REUSE"]
    assert {(row["left_id"], row["right_id"]) for row in pairs} == {
        ("S-0101", "S-0102"), ("S-0103", "S-0104")}
    cluster_service.ensure_clusters(run_id)
    members = {row["supplier_id"]: row["cluster_id"]
               for row in store.list_cluster_members(run_id)}
    assert members["S-0101"] != members["S-0102"]
    assert members["S-0103"] != members["S-0104"]


def test_merge_decision_on_reuse_rejected(lifecycle):
    pair = next(row for row in store.list_pairs(lifecycle["run_id"])
                if row["reason_code"] == "P_CODE_REUSE")
    with pytest.raises(DomainError) as error:
        from app.modules.m6_reviews import service as review_service
        review_service.decide(lifecycle["run_id"], pair["pair_id"], "merge", 0)
    assert error.value.code == "V_BAD_DECISION"


# ---- 人工确认与发布门槛 ----

def test_succession_needs_evidence(lifecycle):
    with pytest.raises(DomainError) as error:
        service.decide(lifecycle["run_id"], "SC-000206", "successor", 0)
    assert error.value.code == "S_BAD_VERDICT"


def test_case_version_lock(lifecycle):
    run_id = lifecycle["run_id"]
    service.decide(run_id, "SC-000202", "successor", 0)
    with pytest.raises(Conflict) as error:
        service.decide(run_id, "SC-000202", "separate", 0)
    assert error.value.code == "S_STALE_CASE"


def test_plan_blocked_until_approved(lifecycle):
    plan = service.build_plan(lifecycle["run_id"])
    assert plan["ok"] is False
    assert {item["reason_code"] for item in plan["blocked"]} == {"S_NOT_APPROVED"}
    with pytest.raises(DomainError) as error:
        service.publish(lifecycle["run_id"], plan["payload_sha256"], plan["prior_version"])
    assert error.value.code == "S_NOT_APPROVED"


def test_plan_balances_conserved(lifecycle):
    _approve_all(lifecycle["run_id"])
    plan = service.build_plan(lifecycle["run_id"])
    assert plan["ok"] is True
    checks = {check["name"]: check for check in plan["checks"]}
    assert checks["余额归属守恒"]["ok"] is True
    assert checks["记录余额入账"]["ok"] is True
    assert plan["entity_balance"] == plan["record_balance"] == "30600.00"


def test_first_publish_transfers_balances(lifecycle):
    _approve_all(lifecycle["run_id"])
    plan = service.build_plan(lifecycle["run_id"])
    receipt = service.publish(lifecycle["run_id"], plan["payload_sha256"],
                              plan["prior_version"])
    assert receipt["state"] == "committed"
    assert receipt["version"] == 1
    assert receipt["counts"]["successor"] == 2
    master = service.master()
    by_id = {row["entity_id"]: row for row in master["entities"]}
    assert by_id["S-0101"]["status"] == "retired"
    assert by_id["S-0101"]["balance"] == "0.00"
    assert by_id["S-0102"]["balance"] == "18000.00"
    assert by_id["S-0103"]["status"] == "active"
    assert by_id["S-0103"]["balance"] == "7200.00"
    assert by_id["S-0104"]["status"] == "pending_account"
    assert by_id["S-0105"]["status"] == "retired"
    ledger = {row["entity_id"]: row for row in master["ledger"]}
    assert ledger["S-0102"]["payable_amount"] == "18000.00"
    assert ledger["S-0103"]["payable_amount"] == "7200.00"
    assert "暂缓付款" in ledger["S-0104"]["detail"]


def test_payload_drift_rejected(lifecycle):
    _approve_all(lifecycle["run_id"])
    plan = service.build_plan(lifecycle["run_id"])
    with pytest.raises(Conflict) as error:
        service.publish(lifecycle["run_id"], "0" * 64, plan["prior_version"])
    assert error.value.code == "S_PAYLOAD_DRIFT"


def test_second_batch_uses_prior_master_balance(lifecycle):
    """第二批再接上旧版主数据：余额从正式台账续起，不会把源数据再加一遍。"""
    run_id = lifecycle["run_id"]
    _approve_all(run_id)
    first = service.build_plan(run_id)
    service.publish(run_id, first["payload_sha256"], first["prior_version"])
    second = intake_service.ingest(files=SUCCESSION_FILES + ("erp_e_header_only.csv",))
    service.list_cases(second["run_id"], rebuild=True)
    _approve_all(second["run_id"])
    plan = service.build_plan(second["run_id"])
    assert plan["prior_version"] == 1
    assert plan["entity_balance"] == "30600.00"
    assert plan["ok"] is True
    receipt = service.publish(second["run_id"], plan["payload_sha256"],
                              plan["prior_version"])
    assert receipt["version"] == 2
    assert store.get_master_version(1)["state"] == "superseded"


# ---- 幂等与回滚 ----

def test_publish_replays_with_matching_expectations(lifecycle):
    _approve_all(lifecycle["run_id"])
    plan = service.build_plan(lifecycle["run_id"])
    first = service.publish(lifecycle["run_id"], plan["payload_sha256"],
                            plan["prior_version"])
    second = service.publish(lifecycle["run_id"], plan["payload_sha256"],
                            plan["prior_version"])
    assert second["receipt_id"] == first["receipt_id"]
    assert second["version"] == first["version"] == 1
    assert second.get("replayed") is True
    assert len(store.list_master_versions()) == 1
    assert len(store.list_receipts()) == 1
    with pytest.raises(Conflict) as error:
        service.publish(lifecycle["run_id"], "1" * 64, plan["prior_version"])
    assert error.value.code == "S_PAYLOAD_DRIFT"


def test_rerun_same_batch_does_not_double_migrate(lifecycle):
    run_id = lifecycle["run_id"]
    _approve_all(run_id)
    plan = service.build_plan(run_id)
    service.publish(run_id, plan["payload_sha256"], plan["prior_version"])
    intake_service.ingest(files=SUCCESSION_FILES, force=True)
    cases = service.list_cases(run_id)
    assert {case["state"] for case in cases} == {"published"}
    plan_again = service.build_plan(run_id)
    assert plan_again["entity_balance"] == "30600.00"
    assert all(check["ok"] for check in plan_again["checks"])
    replay = service.publish(run_id, plan["payload_sha256"], plan["prior_version"])
    assert replay["version"] == 1
    assert len(store.list_master_versions()) == 1
    total = sum((Decimal(row["payable_amount"])
                 for row in store.list_master_entries(1)), Decimal("0.00"))
    assert total == Decimal("30600.00")


def test_failed_publish_rolls_back_everything(lifecycle, monkeypatch):
    run_id = lifecycle["run_id"]
    _approve_all(run_id)
    plan = service.build_plan(run_id)

    def boom(conn, version):
        conn.execute(
            "INSERT INTO master_entities (version, entity_id, supplier_no, name, tax_id,"
            " city, bank_account, status, balance, cluster_id, origin, basis)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (version, "BAD", "BAD", "坏主体", "", "", "", "active", "1.00", "", "", ""))
        raise RuntimeError("注入故障")

    monkeypatch.setattr(service, "_after_write_hook", boom)
    with pytest.raises(DomainError) as error:
        service.publish(run_id, plan["payload_sha256"], plan["prior_version"])
    assert error.value.code == "S_PUBLISH_FAILED"
    assert store.list_master_versions() == []
    assert store.list_receipts() == []
    assert store.latest_master_version() == 0
    assert {case["state"] for case in service.list_cases(run_id)} == {"approved"}


def test_failure_keeps_prior_version_and_snapshot(lifecycle, monkeypatch):
    """第二次发布失败：旧版整版仍在，且可原样恢复。"""
    run_id = lifecycle["run_id"]
    _approve_all(run_id)
    plan = service.build_plan(run_id)
    service.publish(run_id, plan["payload_sha256"], plan["prior_version"])
    prior_entities = store.list_master_entities(1)

    second = intake_service.ingest(files=SUCCESSION_FILES + ("erp_e_header_only.csv",))
    service.list_cases(second["run_id"], rebuild=True)
    _approve_all(second["run_id"])
    second_plan = service.build_plan(second["run_id"])

    def boom(conn, version):
        raise RuntimeError("注入故障")

    monkeypatch.setattr(service, "_after_write_hook", boom)
    with pytest.raises(DomainError):
        service.publish(second["run_id"], second_plan["payload_sha256"],
                        second_plan["prior_version"])
    monkeypatch.setattr(service, "_after_write_hook", None)

    assert store.latest_master_version() == 1
    assert store.list_master_entities(1) == prior_entities
    snapshot = store.get_snapshot(1)
    assert snapshot is not None
    assert len(snapshot["entities"]) == len(prior_entities)


def test_record_id_reused_by_other_tax_blocks_publish(lifecycle):
    """旧版某记录号本批已代表另一税号主体：不能用旧版余额覆盖，必须阻断。"""
    run_id = lifecycle["run_id"]
    _approve_all(run_id)
    plan = service.build_plan(run_id)
    service.publish(run_id, plan["payload_sha256"], plan["prior_version"])
    # 人为把已发布版本的 S-0103 税号改成与新版不同的值，制造记录号冲突。
    with store.transaction() as conn:
        conn.execute("UPDATE master_entities SET tax_id = 'OLD-TAX'"
                     " WHERE version = 1 AND entity_id = 'S-0103'")
        conn.execute("UPDATE master_snapshots SET entities_json = replace("
                     "entities_json, '91500000MA5X6Y7Z1', 'OLD-TAX') WHERE version = 1")
    second = intake_service.ingest(files=SUCCESSION_FILES + ("erp_e_header_only.csv",))
    service.list_cases(second["run_id"], rebuild=True)
    _approve_all(second["run_id"])
    plan_again = service.build_plan(second["run_id"])
    assert plan_again["ok"] is False
    assert any(item["reason_code"] == "S_RECORD_ID_CONFLICT"
               for item in plan_again["blocked"])
    with pytest.raises(DomainError) as error:
        service.publish(second["run_id"], plan_again["payload_sha256"],
                        plan_again["prior_version"])
    assert error.value.code == "S_NOT_APPROVED"


def test_restore_creates_new_version_and_keeps_old(lifecycle):
    _approve_all(lifecycle["run_id"])
    plan = service.build_plan(lifecycle["run_id"])
    service.publish(lifecycle["run_id"], plan["payload_sha256"], plan["prior_version"])
    restored = service.restore(1)
    assert restored["version"] == 2
    assert len(store.list_master_versions()) == 2
    assert store.get_master_version(1)["state"] == "superseded"
    old_rows = {(row["entity_id"], row["status"], row["balance"])
                for row in store.list_master_entities(1)}
    new_rows = {(row["entity_id"], row["status"], row["balance"])
                for row in store.list_master_entities(2)}
    assert old_rows == new_rows


# ---- API 与页面 ----

def test_succession_api_and_page(client):
    client.get("/succession?batch=lifecycle")
    listing = client.get("/api/succession").json()
    assert listing["stats"]["total"] == 4
    case = listing["items"][0]
    assert case["old"] and case["new"]

    decision = client.post(f"/api/succession/{case['case_id']}/decision",
                           json={"verdict": case["effective_verdict"],
                                 "expected_version": case["version"]})
    assert decision.status_code == 200
    stale = client.post(f"/api/succession/{case['case_id']}/decision",
                        json={"verdict": "separate", "expected_version": case["version"]})
    assert stale.status_code == 409
    assert stale.json()["reason_code"] == "S_STALE_CASE"

    plan = client.get("/api/succession/plan").json()
    page = client.get("/succession")
    assert page.status_code == 200
    assert "编码复用接替" in page.text
    assert "受影响记录" in page.text
    assert "余额归属与对账" in page.text
    assert plan["payload_sha256"][:16] in page.text


def test_succession_publish_api_with_csv(client):
    client.get("/succession?batch=lifecycle")
    for case in client.get("/api/succession").json()["items"]:
        client.post(f"/api/succession/{case['case_id']}/decision",
                    json={"verdict": case["effective_verdict"],
                          "expected_version": case["version"]})
    plan = client.get("/api/succession/plan").json()
    published = client.post("/api/succession/publish",
                            json={"payload_sha256": plan["payload_sha256"],
                                  "expected_prior_version": plan["prior_version"]})
    assert published.status_code == 200
    receipt = published.json()["receipt"]
    assert receipt["state"] == "committed"
    again = client.post("/api/succession/publish",
                        json={"payload_sha256": plan["payload_sha256"],
                              "expected_prior_version": plan["prior_version"]})
    assert again.status_code == 200
    assert again.json()["receipt"]["receipt_id"] == receipt["receipt_id"]

    csv_response = client.get("/api/succession/master.csv")
    assert csv_response.status_code == 200
    assert "未结余额(元)" in csv_response.text
    assert "S-0102" in csv_response.text
    restore = client.post("/api/succession/restore/1")
    assert restore.status_code == 200
    assert client.get("/api/succession/master").json()["version"] == 2


def test_pairs_page_lists_code_reuse(client):
    """编码复用对可能因得分排序落在前 60 条之外，必须单独置顶显示。"""
    client.get("/succession?batch=lifecycle")
    page = client.get("/pairs")
    assert page.status_code == 200
    assert "编码复用对" in page.text
    assert "S-0101" in page.text and "S-0102" in page.text


def test_concurrent_publish_writes_one_version(lifecycle):
    """同一批并发发布：只落一个版本、一份台账，其余线程拿到同一回执。"""
    import threading

    run_id = lifecycle["run_id"]
    _approve_all(run_id)
    plan = service.build_plan(run_id)
    results = []

    def attempt():
        try:
            receipt = service.publish(run_id, plan["payload_sha256"],
                                      plan["prior_version"])
            results.append(("ok", receipt["receipt_id"]))
        except DomainError as error:
            results.append(("err", error.code))

    threads = [threading.Thread(target=attempt) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(store.list_master_versions()) == 1
    assert len(store.list_receipts()) == 1
    receipt_ids = {value for status, value in results if status == "ok"}
    assert receipt_ids and len(receipt_ids) == 1
    total = sum((Decimal(row["payable_amount"])
                 for row in store.list_master_entries(1)), Decimal("0.00"))
    assert total == Decimal("30600.00")


def test_default_pages_unchanged(client):
    for path, marker in (("/", "台账总览"), ("/pairs", "候选对"),
                         ("/clusters", "合并簇")):
        response = client.get(path)
        assert response.status_code == 200
        assert marker in response.text
