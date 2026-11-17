"""页面渲染：模板环境、通用上下文、格式化过滤器与 6 个页面路由。"""
from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from . import store
from .config import APP_NAME, RULE_VERSION, SUCCESSION_FILES, TZ_DISPLAY
from .manifest import parse_utc
from .reasons import all_codes
from .modules.m1_intake import service as intake_service
from .modules.m2_normalize import service as normalize_service
from .modules.m3_blocks import service as block_service
from .modules.m4_pairs import service as pair_service
from .modules.m5_clusters import service as cluster_service
from .modules.m6_reviews import service as review_service
from .modules.m8_succession import service as succession_service

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

NAV = (
    ("ledger", "台账", "/"),
    ("normalize", "归一", "/normalize"),
    ("pairs", "候选对", "/pairs"),
    ("clusters", "合并簇", "/clusters"),
    ("reviews", "复核", "/reviews"),
    ("succession", "接替", "/succession"),
    ("export", "导出", "/export"),
)


def _decimal(value) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def money(value) -> str:
    return f"{_decimal(value):,.2f}"


def intcomma(value) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "0"


def score(value) -> str:
    return f"{_decimal(value):.4f}"


def pct(part, total) -> str:
    whole = _decimal(total)
    if whole == 0:
        return "0.0%"
    return f"{(Decimal(str(part)) / whole * 100):.1f}%"


def ts(value) -> str:
    if not value:
        return ""
    moment = parse_utc(str(value))
    return moment.astimezone(ZoneInfo(TZ_DISPLAY)).strftime("%Y-%m-%d %H:%M:%S")


def date_only(value) -> str:
    if not value:
        return ""
    moment = parse_utc(str(value))
    return moment.astimezone(ZoneInfo(TZ_DISPLAY)).strftime("%Y-%m-%d")


templates.env.filters["money"] = money
templates.env.filters["intcomma"] = intcomma
templates.env.filters["score"] = score
templates.env.filters["pct"] = pct
templates.env.filters["ts"] = ts
templates.env.filters["date_only"] = date_only

router = APIRouter(include_in_schema=False)


def run_briefs(limit: int = 5) -> list:
    rows = []
    for run in store.list_runs()[:limit]:
        counts = json.loads(run["counts_json"] or "{}")
        rows.append({
            "run_id": run["run_id"],
            "created_at": run["created_at"],
            "elapsed_ms": run["elapsed_ms"],
            "accepted": counts.get("accepted", 0),
            "quarantined": counts.get("quarantined", 0),
            "input_rows": counts.get("input_rows", 0),
        })
    return rows


def counts_of(run) -> dict:
    if not run:
        return {}
    return json.loads(run.get("counts_json") or "{}")


def current_run(force: bool = False) -> dict:
    """页面与 API 共用入口：没有批次时按内置样例自动采信一次。"""
    return intake_service.ensure_run(force=force)


def base_context(request: Request, page: str, title: str, run) -> dict:
    return {
        "request": request,
        "app_name": APP_NAME,
        "rule_version": RULE_VERSION,
        "tz_label": "UTC+08:00",
        "nav": NAV,
        "page": page,
        "title": title,
        "run": run,
        "counts": counts_of(run),
        "reasons": all_codes(),
        "verdicts": {"auto": "自动", "review": "复核", "reject": "拒绝"},
        "empty_states": {"header_only": "仅表头", "blank_rows": "全空行", "no_row": "无数据行"},
    }


def render(request: Request, template: str, context: dict) -> HTMLResponse:
    return templates.TemplateResponse(request=request, name=template, context=context)


@router.get("/", response_class=HTMLResponse)
def page_ledger(request: Request):
    run = current_run()
    run_id = run["run_id"]
    normalize_service.ensure_normalized(run_id)
    block_service.ensure_blocks(run_id)
    pair_service.ensure_pairs(run_id)
    summary = cluster_service.ensure_clusters(run_id)
    ledger = cluster_service.reconcile(run_id)
    context = base_context(request, "ledger", "台账总览", run)
    context.update({
        "ledger": ledger,
        "summary": summary,
        "clusters": store.list_clusters(run_id)[:8],
        "pairs": store.list_pairs(run_id)[:8],
        "runs": run_briefs(5),
        "diagnostics": intake_service.file_diagnostics(run_id),
        "records": store.list_records(run_id)[:24],
        "quarantine": store.list_quarantine(run_id)[:12],
        "conflicts": store.list_conflicts(run_id)[:8],
    })
    return render(request, "index.html", context)


@router.get("/normalize", response_class=HTMLResponse)
def page_normalize(request: Request):
    run = current_run()
    run_id = run["run_id"]
    summary = normalize_service.ensure_normalized(run_id)
    context = base_context(request, "normalize", "名称归一", run)
    context.update({
        "records": store.list_records(run_id)[:60],
        "summary": summary,
    })
    return render(request, "normalize.html", context)


@router.get("/pairs", response_class=HTMLResponse)
def page_pairs(request: Request):
    run = current_run()
    run_id = run["run_id"]
    pair_service.ensure_pairs(run_id)
    pairs = store.list_pairs(run_id)
    reuse = [row for row in pair_service.details(run_id) if row["reason_code"] == "P_CODE_REUSE"]
    context = base_context(request, "pairs", "候选对", run)
    context.update({
        "pairs": pairs[:60],
        "reuse_pairs": reuse,
        "stats": pair_service.verdict_stats(run_id),
        "blocks": store.list_blocks(run_id)[:10],
        "block_summary": block_service.summary(run_id),
        "decisions": {d["pair_id"]: d for d in store.list_decisions(run_id)},
    })
    return render(request, "pairs.html", context)


@router.get("/clusters", response_class=HTMLResponse)
def page_clusters(request: Request):
    run = current_run()
    run_id = run["run_id"]
    cluster_service.ensure_clusters(run_id)
    members = store.list_cluster_members(run_id)
    by_cluster = {}
    for member in members:
        by_cluster.setdefault(member["cluster_id"], []).append(member)
    context = base_context(request, "clusters", "合并簇", run)
    context.update({
        "clusters": store.list_clusters(run_id)[:40],
        "members": by_cluster,
        "ledger": cluster_service.reconcile(run_id),
    })
    return render(request, "clusters.html", context)


@router.get("/reviews", response_class=HTMLResponse)
def page_reviews(request: Request):
    run = current_run()
    run_id = run["run_id"]
    queue = review_service.queue_items(run_id)
    context = base_context(request, "reviews", "复核队列", run)
    context.update({
        "queue": queue[:40],
        "stats": review_service.queue_stats(run_id),
        "decisions": store.list_decisions(run_id)[:40],
        "references": review_service.reference_issues(run_id),
        "runs": run_briefs(8),
        "succession_cases": succession_service.list_cases(run_id),
        "succession_stats": succession_service.statistics(run_id),
    })
    return render(request, "reviews.html", context)


@router.get("/succession", response_class=HTMLResponse)
def page_succession(request: Request, batch: str = ""):
    if batch == "lifecycle":
        run = intake_service.ingest(files=SUCCESSION_FILES)
    else:
        run = current_run()
    run_id = run["run_id"]
    cluster_service.ensure_clusters(run_id)
    cases = succession_service.list_cases(run_id)
    plan = succession_service.build_plan(run_id)
    master = succession_service.master()
    context = base_context(request, "succession", "编码复用接替", run)
    context.update({
        "cases": cases,
        "stats": succession_service.statistics(run_id),
        "plan": plan,
        "master": master,
        "receipts": succession_service.receipts(8),
        "snapshots": store.list_snapshots(),
        "succession_reasons": {
            "S_SUCCESSOR_CONFIRMED": "证据充分，判主体接替",
            "S_SUCCESSOR_CANDIDATE": "疑似接替，需人工确认",
            "S_NEW_SEPARATE": "新主体另行启用",
        },
        "impact_labels": {
            "S_CODE_REUSED": "编码复用",
            "S_OLD_UNSETTLED": "旧主体未结余额",
            "S_OLD_RETIRED": "旧主体退役",
            "S_BALANCE_TRANSFER": "余额结转新主体",
            "S_BALANCE_RETAINED": "余额留旧主体",
            "S_NEW_TAX_CHANGED": "新主体税号变化",
            "S_NEW_BANK_CHANGED": "新主体开户行变化",
            "S_NAME_MERGE_BLOCKED": "禁止按名称合并",
            "S_ACCOUNT_BLOCKED": "账户未核验",
            "S_DUPLICATE_PAYMENT_RISK": "重复付款风险",
            "S_NEW_SEPARATE": "新主体另行启用",
        },
    })
    return render(request, "succession.html", context)


@router.get("/export", response_class=HTMLResponse)
def page_export(request: Request):
    run = current_run()
    run_id = run["run_id"]
    cluster_service.ensure_clusters(run_id)
    context = base_context(request, "export", "主数据导出", run)
    context.update({
        "ledger": cluster_service.reconcile(run_id),
        "clusters": store.list_clusters(run_id)[:12],
        "quarantine": store.list_quarantine(run_id)[:12],
        "files": json.loads(run.get("files_json") or "[]"),
    })
    return render(request, "export.html", context)
