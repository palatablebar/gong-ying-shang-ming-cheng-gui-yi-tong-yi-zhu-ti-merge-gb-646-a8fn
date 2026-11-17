"""M8 路由：接替案件、批准、发布计划、一次性发布、回执、正式主数据与恢复。"""
from __future__ import annotations

import csv
import io

from fastapi import APIRouter, Response
from pydantic import BaseModel, Field

from ... import store
from ..m1_intake import service as intake_service
from ..m5_clusters import service as cluster_service
from . import service

router = APIRouter(prefix="/api", tags=["m8-succession"])

MASTER_HEADER = ("主体编号", "集团编码", "名称", "税号", "城市", "开户行账号", "状态",
                 "未结余额(元)", "台账说明", "接替依据")


class CaseDecision(BaseModel):
    verdict: str = Field(min_length=1, max_length=16)
    expected_version: int = Field(ge=0)
    operator: str = Field(default="采购主数据组", max_length=40)


class PublishRequest(BaseModel):
    payload_sha256: str = Field(default="", max_length=64)
    expected_prior_version: int = Field(ge=0)
    operator: str = Field(default="采购主数据组", max_length=40)


def _run() -> dict:
    run = intake_service.ensure_run()
    cluster_service.ensure_clusters(run["run_id"])
    return run


@router.get("/succession")
def succession_queue(rebuild: bool = False):
    run = _run()
    cases = service.list_cases(run["run_id"], rebuild=rebuild)
    return {"run_id": run["run_id"], "stats": service.statistics(run["run_id"]),
            "total": len(cases), "items": cases}


@router.get("/succession/plan")
def succession_plan():
    run = _run()
    plan = service.build_plan(run["run_id"])
    return {**plan, "entities": plan["entities"], "ledger": plan["ledger"]}


@router.get("/succession/master")
def succession_master(version: int = 0):
    return service.master(version or None)


@router.get("/succession/master.csv")
def succession_master_csv(version: int = 0):
    master = service.master(version or None)
    if not master["version"]:
        return Response(content="尚未发布正式主数据\n", media_type="text/plain; charset=utf-8",
                        status_code=404)
    ledger = {entry["entity_id"]: entry for entry in master["ledger"]}
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(MASTER_HEADER)
    for entity in master["entities"]:
        entry = ledger.get(entity["entity_id"], {})
        writer.writerow([
            entity["entity_id"], entity["supplier_no"], entity["name"],
            entity["tax_id"], entity["city"], entity["bank_account"], entity["status"],
            entity["balance"], entry.get("detail", ""), entity["basis"],
        ])
    filename = f"master-v{master['version']}.csv"
    return Response(
        content="﻿" + buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/succession/receipts")
def succession_receipts(limit: int = 20):
    return {"items": service.receipts(limit)}


@router.get("/succession/snapshots")
def succession_snapshots():
    return {"items": store.list_snapshots(),
            "versions": store.list_master_versions()}


@router.post("/succession/{case_id}/decision")
def succession_decision(case_id: str, body: CaseDecision):
    run = _run()
    case = service.decide(run["run_id"], case_id, body.verdict, body.expected_version,
                          body.operator)
    return {"run_id": run["run_id"], "case": case,
            "stats": service.statistics(run["run_id"])}


@router.post("/succession/publish")
def succession_publish(body: PublishRequest):
    run = _run()
    receipt = service.publish(run["run_id"], body.payload_sha256,
                              body.expected_prior_version, body.operator)
    return {"run_id": run["run_id"], "receipt": receipt,
            "master": service.master(receipt["version"])}


@router.post("/succession/restore/{version}")
def succession_restore(version: int):
    master = service.restore(version)
    return {"restored": master["meta"]["note"], "master": master}
