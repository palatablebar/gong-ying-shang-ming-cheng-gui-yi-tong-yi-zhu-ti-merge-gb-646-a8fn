"""SQLite 存储：批次运行清单、采信结果、候选对、合并簇、复核决定、
编码复用接替案件、正式主数据、台账分录与发布快照。

金额一律以字符串（Decimal 原值）存取，不在数据库层做浮点运算。
正式主数据的写入只允许经 ``transaction()`` 一次性提交，保证主数据与新主体
记录同进同退。
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .config import DB_PATH as DEFAULT_DB_PATH

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS runs (
        run_id TEXT PRIMARY KEY,
        input_sha256 TEXT NOT NULL,
        rule_version TEXT NOT NULL,
        params_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        elapsed_ms INTEGER NOT NULL,
        counts_json TEXT NOT NULL,
        files_json TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS records (
        run_id TEXT NOT NULL,
        supplier_id TEXT NOT NULL,
        supplier_no TEXT NOT NULL,
        raw_name TEXT NOT NULL,
        norm_name TEXT NOT NULL DEFAULT '',
        norm_codes TEXT NOT NULL DEFAULT '[]',
        tax_id TEXT NOT NULL DEFAULT '',
        city TEXT NOT NULL DEFAULT '',
        bank_account TEXT NOT NULL DEFAULT '',
        erp TEXT NOT NULL DEFAULT '',
        ref_master_id TEXT NOT NULL DEFAULT '',
        ytd_amount TEXT NOT NULL DEFAULT '0.00',
        open_balance TEXT NOT NULL DEFAULT '0.00',
        entity_status TEXT NOT NULL DEFAULT '',
        flags TEXT NOT NULL DEFAULT '[]',
        source_file TEXT NOT NULL DEFAULT '',
        line_no INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (run_id, supplier_id)
    )""",
    """CREATE TABLE IF NOT EXISTS quarantine (
        run_id TEXT NOT NULL,
        seq INTEGER NOT NULL,
        source_file TEXT NOT NULL,
        line_no INTEGER NOT NULL,
        raw_line TEXT NOT NULL,
        reason_code TEXT NOT NULL,
        detail TEXT NOT NULL DEFAULT ''
    )""",
    """CREATE TABLE IF NOT EXISTS conflicts (
        run_id TEXT NOT NULL,
        supplier_id TEXT NOT NULL,
        field TEXT NOT NULL,
        kept TEXT NOT NULL,
        rejected TEXT NOT NULL,
        reason_code TEXT NOT NULL,
        source_file TEXT NOT NULL,
        line_no INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS blocks (
        run_id TEXT NOT NULL,
        block_key TEXT NOT NULL,
        member_count INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS block_pairs (
        run_id TEXT NOT NULL,
        pair_id TEXT NOT NULL,
        left_id TEXT NOT NULL,
        right_id TEXT NOT NULL,
        shared_keys TEXT NOT NULL DEFAULT '[]',
        PRIMARY KEY (run_id, pair_id)
    )""",
    """CREATE TABLE IF NOT EXISTS pair_scores (
        run_id TEXT NOT NULL,
        pair_id TEXT NOT NULL,
        left_id TEXT NOT NULL,
        right_id TEXT NOT NULL,
        dice TEXT NOT NULL,
        ratio TEXT NOT NULL,
        score TEXT NOT NULL,
        verdict TEXT NOT NULL,
        reason_code TEXT NOT NULL,
        detail TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (run_id, pair_id)
    )""",
    """CREATE TABLE IF NOT EXISTS clusters (
        run_id TEXT NOT NULL,
        cluster_id TEXT NOT NULL,
        rep_id TEXT NOT NULL,
        tax_id TEXT NOT NULL DEFAULT '',
        city TEXT NOT NULL DEFAULT '',
        member_count INTEGER NOT NULL,
        total_amount TEXT NOT NULL,
        conflict INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (run_id, cluster_id)
    )""",
    """CREATE TABLE IF NOT EXISTS cluster_members (
        run_id TEXT NOT NULL,
        cluster_id TEXT NOT NULL,
        supplier_id TEXT NOT NULL,
        share_amount TEXT NOT NULL,
        PRIMARY KEY (run_id, cluster_id, supplier_id)
    )""",
    """CREATE TABLE IF NOT EXISTS decisions (
        run_id TEXT NOT NULL,
        pair_id TEXT NOT NULL,
        decision TEXT NOT NULL,
        expected_version INTEGER NOT NULL,
        version INTEGER NOT NULL,
        operator TEXT NOT NULL,
        decided_at TEXT NOT NULL,
        PRIMARY KEY (run_id, pair_id)
    )""",
    # 编码复用接替案件：编码在旧主体注销后被重新分配，按 (run_id, code) 一案。
    """CREATE TABLE IF NOT EXISTS succession_cases (
        run_id TEXT NOT NULL,
        case_id TEXT NOT NULL,
        code TEXT NOT NULL,
        old_id TEXT NOT NULL,
        new_id TEXT NOT NULL,
        verdict TEXT NOT NULL,
        reason_code TEXT NOT NULL,
        decided_verdict TEXT NOT NULL DEFAULT '',
        evidence_json TEXT NOT NULL DEFAULT '{}',
        state TEXT NOT NULL DEFAULT 'pending',
        expected_version INTEGER NOT NULL DEFAULT 0,
        version INTEGER NOT NULL DEFAULT 0,
        operator TEXT NOT NULL DEFAULT '',
        decided_at TEXT NOT NULL DEFAULT '',
        published_at TEXT NOT NULL DEFAULT '',
        receipt_id TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (run_id, case_id)
    )""",
    # 受影响记录：旧主体未结余额与稳定账户、新主体税号与开户行变化逐条留证。
    """CREATE TABLE IF NOT EXISTS succession_impacts (
        run_id TEXT NOT NULL,
        case_id TEXT NOT NULL,
        seq INTEGER NOT NULL,
        supplier_id TEXT NOT NULL,
        side TEXT NOT NULL,
        impact_code TEXT NOT NULL,
        field TEXT NOT NULL DEFAULT '',
        old_value TEXT NOT NULL DEFAULT '',
        new_value TEXT NOT NULL DEFAULT '',
        amount TEXT NOT NULL DEFAULT '0.00',
        detail TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (run_id, case_id, seq)
    )""",
    # 正式主数据：一次发布一版，旧版整版保留，不做逐行覆盖。
    """CREATE TABLE IF NOT EXISTS master_versions (
        version INTEGER PRIMARY KEY,
        run_id TEXT NOT NULL,
        receipt_id TEXT NOT NULL,
        state TEXT NOT NULL,
        published_at TEXT NOT NULL,
        operator TEXT NOT NULL DEFAULT '',
        note TEXT NOT NULL DEFAULT '',
        counts_json TEXT NOT NULL DEFAULT '{}',
        prior_version INTEGER NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS master_entities (
        version INTEGER NOT NULL,
        entity_id TEXT NOT NULL,
        supplier_no TEXT NOT NULL,
        name TEXT NOT NULL,
        tax_id TEXT NOT NULL DEFAULT '',
        city TEXT NOT NULL DEFAULT '',
        bank_account TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'active',
        balance TEXT NOT NULL DEFAULT '0.00',
        cluster_id TEXT NOT NULL DEFAULT '',
        origin TEXT NOT NULL DEFAULT 'ledger',
        basis TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (version, entity_id)
    )""",
    """CREATE TABLE IF NOT EXISTS master_ledger_entries (
        version INTEGER NOT NULL,
        entry_id TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        payable_amount TEXT NOT NULL DEFAULT '0.00',
        receivable_amount TEXT NOT NULL DEFAULT '0.00',
        receivable_owner TEXT NOT NULL DEFAULT '',
        detail TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (version, entry_id)
    )""",
    """CREATE TABLE IF NOT EXISTS publish_receipts (
        receipt_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        payload_sha256 TEXT NOT NULL,
        version INTEGER NOT NULL,
        prior_version INTEGER NOT NULL,
        state TEXT NOT NULL,
        created_at TEXT NOT NULL,
        committed_at TEXT NOT NULL DEFAULT '',
        detail TEXT NOT NULL DEFAULT ''
    )""",
    # 被替换的旧版整版快照：失败或回滚时可原样恢复，不依赖重新计算。
    """CREATE TABLE IF NOT EXISTS master_snapshots (
        version INTEGER PRIMARY KEY,
        run_id TEXT NOT NULL,
        state TEXT NOT NULL,
        reason TEXT NOT NULL DEFAULT '',
        entities_json TEXT NOT NULL,
        ledger_json TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_records_run ON records(run_id)",
    "CREATE INDEX IF NOT EXISTS idx_quarantine_run ON quarantine(run_id)",
    "CREATE INDEX IF NOT EXISTS idx_pairs_run ON pair_scores(run_id)",
    "CREATE INDEX IF NOT EXISTS idx_succession_run ON succession_cases(run_id)",
    "CREATE INDEX IF NOT EXISTS idx_impacts_case ON succession_impacts(run_id, case_id)",
    "CREATE INDEX IF NOT EXISTS idx_entities_version ON master_entities(version)",
    "CREATE INDEX IF NOT EXISTS idx_ledger_version ON master_ledger_entries(version)",
)

TABLES = ("runs", "records", "quarantine", "conflicts", "blocks", "block_pairs",
          "pair_scores", "clusters", "cluster_members", "decisions",
          "succession_cases", "succession_impacts", "master_versions",
          "master_entities", "master_ledger_entries", "publish_receipts",
          "master_snapshots")


def db_path() -> Path:
    """按环境变量解析，便于测试指向临时库。"""
    return Path(os.environ.get("MERGE_DB", str(DEFAULT_DB_PATH)))


def connect(path=None) -> sqlite3.Connection:
    target = Path(path or db_path())
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(path=None) -> None:
    with connect(path) as conn:
        for statement in SCHEMA:
            conn.execute(statement)
        _migrate(conn)
        conn.commit()


# 旧库升级：只做加列，不动既有数据；已有行用默认值补齐。
_MIGRATIONS = {
    "records": {
        "open_balance": "TEXT NOT NULL DEFAULT '0.00'",
        "entity_status": "TEXT NOT NULL DEFAULT ''",
    },
    "succession_cases": {
        "decided_verdict": "TEXT NOT NULL DEFAULT ''",
    },
}


def _migrate(conn) -> None:
    for table, columns in _MIGRATIONS.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not existing:
            continue
        for name, definition in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def reset_db(path=None) -> None:
    with connect(path) as conn:
        for table in TABLES:
            conn.execute(f"DELETE FROM {table}")
        conn.commit()


def _rows(conn, sql, params=()):
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def _one(conn, sql, params=()):
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row else None


# ---- runs ----

def save_run(run: dict) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM runs WHERE run_id = ?", (run["run_id"],))
        conn.execute(
            "INSERT INTO runs (run_id, input_sha256, rule_version, params_json, created_at,"
            " elapsed_ms, counts_json, files_json) VALUES (?,?,?,?,?,?,?,?)",
            (run["run_id"], run["input_sha256"], run["rule_version"], run["params_json"],
             run["created_at"], run["elapsed_ms"], run["counts_json"], run["files_json"]),
        )
        conn.commit()


def get_run(run_id: str):
    with connect() as conn:
        return _one(conn, "SELECT * FROM runs WHERE run_id = ?", (run_id,))


def latest_run():
    with connect() as conn:
        return _one(conn, "SELECT * FROM runs ORDER BY created_at DESC, run_id DESC LIMIT 1")


def list_runs() -> list:
    with connect() as conn:
        return _rows(conn, "SELECT * FROM runs ORDER BY created_at DESC, run_id DESC")


def update_run_counts(run_id: str, counts: dict) -> None:
    with connect() as conn:
        conn.execute("UPDATE runs SET counts_json = ? WHERE run_id = ?",
                     (json.dumps(counts, ensure_ascii=False, sort_keys=True), run_id))
        conn.commit()


# ---- 采信结果 ----

def clear_stage(run_id: str, stage: str) -> None:
    tables = {
        "intake": ("records", "quarantine", "conflicts"),
        "normalize": (),
        "blocks": ("blocks", "block_pairs"),
        "pairs": ("pair_scores",),
        "clusters": ("clusters", "cluster_members"),
    }[stage]
    with connect() as conn:
        for table in tables:
            conn.execute(f"DELETE FROM {table} WHERE run_id = ?", (run_id,))
        conn.commit()


def save_records(run_id: str, records) -> None:
    with connect() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO records (run_id, supplier_id, supplier_no, raw_name,"
            " norm_name, norm_codes, tax_id, city, bank_account, erp, ref_master_id, ytd_amount,"
            " open_balance, entity_status, flags, source_file, line_no)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(run_id, r["supplier_id"], r["supplier_no"], r["raw_name"], r.get("norm_name", ""),
              r.get("norm_codes", "[]"), r["tax_id"], r["city"], r["bank_account"], r["erp"],
              r["ref_master_id"], str(r["ytd_amount"]), str(r.get("open_balance", "0.00")),
              r.get("entity_status", ""), r.get("flags", "[]"),
              r["source_file"], r["line_no"]) for r in records],
        )
        conn.commit()


def list_records(run_id: str) -> list:
    with connect() as conn:
        return _rows(conn, "SELECT * FROM records WHERE run_id = ? ORDER BY supplier_id", (run_id,))


def get_record(run_id: str, supplier_id: str):
    with connect() as conn:
        return _one(conn, "SELECT * FROM records WHERE run_id = ? AND supplier_id = ?",
                    (run_id, supplier_id))


def update_norm(run_id: str, supplier_id: str, norm_name: str, norm_codes: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE records SET norm_name = ?, norm_codes = ?"
                     " WHERE run_id = ? AND supplier_id = ?",
                     (norm_name, norm_codes, run_id, supplier_id))
        conn.commit()


def save_quarantine(run_id: str, rows) -> None:
    with connect() as conn:
        conn.executemany(
            "INSERT INTO quarantine (run_id, seq, source_file, line_no, raw_line, reason_code,"
            " detail) VALUES (?,?,?,?,?,?,?)",
            [(run_id, i, r["source_file"], r["line_no"], r["raw_line"], r["reason_code"],
              r.get("detail", "")) for i, r in enumerate(rows, 1)],
        )
        conn.commit()


def list_quarantine(run_id: str) -> list:
    with connect() as conn:
        return _rows(conn, "SELECT * FROM quarantine WHERE run_id = ? ORDER BY seq", (run_id,))


def save_conflicts(run_id: str, rows) -> None:
    with connect() as conn:
        conn.executemany(
            "INSERT INTO conflicts (run_id, supplier_id, field, kept, rejected, reason_code,"
            " source_file, line_no) VALUES (?,?,?,?,?,?,?,?)",
            [(run_id, r["supplier_id"], r["field"], r["kept"], r["rejected"], r["reason_code"],
              r["source_file"], r["line_no"]) for r in rows],
        )
        conn.commit()


def list_conflicts(run_id: str) -> list:
    with connect() as conn:
        return _rows(conn, "SELECT * FROM conflicts WHERE run_id = ? ORDER BY supplier_id", (run_id,))


# ---- 分块 ----

def save_blocks(run_id: str, blocks) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM blocks WHERE run_id = ?", (run_id,))
        conn.executemany("INSERT INTO blocks (run_id, block_key, member_count) VALUES (?,?,?)",
                         [(run_id, key, count) for key, count in blocks])
        conn.commit()


def list_blocks(run_id: str) -> list:
    with connect() as conn:
        return _rows(conn, "SELECT * FROM blocks WHERE run_id = ? ORDER BY member_count DESC,"
                           " block_key", (run_id,))


def save_block_pairs(run_id: str, pairs) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM block_pairs WHERE run_id = ?", (run_id,))
        conn.executemany(
            "INSERT INTO block_pairs (run_id, pair_id, left_id, right_id, shared_keys)"
            " VALUES (?,?,?,?,?)",
            [(run_id, p["pair_id"], p["left_id"], p["right_id"], p["shared_keys"]) for p in pairs],
        )
        conn.commit()


def list_block_pairs(run_id: str) -> list:
    with connect() as conn:
        return _rows(conn, "SELECT * FROM block_pairs WHERE run_id = ? ORDER BY pair_id", (run_id,))


# ---- 候选对 ----

def save_pairs(run_id: str, pairs) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM pair_scores WHERE run_id = ?", (run_id,))
        conn.executemany(
            "INSERT INTO pair_scores (run_id, pair_id, left_id, right_id, dice, ratio, score,"
            " verdict, reason_code, detail) VALUES (?,?,?,?,?,?,?,?,?,?)",
            [(run_id, p["pair_id"], p["left_id"], p["right_id"], str(p["dice"]), str(p["ratio"]),
              str(p["score"]), p["verdict"], p["reason_code"], p.get("detail", "")) for p in pairs],
        )
        conn.commit()


def list_pairs(run_id: str, verdict: str = "") -> list:
    with connect() as conn:
        if verdict:
            return _rows(conn, "SELECT * FROM pair_scores WHERE run_id = ? AND verdict = ?"
                               " ORDER BY score DESC, pair_id", (run_id, verdict))
        return _rows(conn, "SELECT * FROM pair_scores WHERE run_id = ?"
                           " ORDER BY score DESC, pair_id", (run_id,))


def get_pair(run_id: str, pair_id: str):
    with connect() as conn:
        return _one(conn, "SELECT * FROM pair_scores WHERE run_id = ? AND pair_id = ?",
                    (run_id, pair_id))


# ---- 合并簇 ----

def save_clusters(run_id: str, clusters, members) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM clusters WHERE run_id = ?", (run_id,))
        conn.execute("DELETE FROM cluster_members WHERE run_id = ?", (run_id,))
        conn.executemany(
            "INSERT INTO clusters (run_id, cluster_id, rep_id, tax_id, city, member_count,"
            " total_amount, conflict) VALUES (?,?,?,?,?,?,?,?)",
            [(run_id, c["cluster_id"], c["rep_id"], c["tax_id"], c["city"], c["member_count"],
              str(c["total_amount"]), 1 if c["conflict"] else 0) for c in clusters],
        )
        conn.executemany(
            "INSERT INTO cluster_members (run_id, cluster_id, supplier_id, share_amount)"
            " VALUES (?,?,?,?)",
            [(run_id, m["cluster_id"], m["supplier_id"], str(m["share_amount"])) for m in members],
        )
        conn.commit()


def list_clusters(run_id: str) -> list:
    with connect() as conn:
        return _rows(conn, "SELECT * FROM clusters WHERE run_id = ?"
                           " ORDER BY member_count DESC, cluster_id", (run_id,))


def list_cluster_members(run_id: str, cluster_id: str = "") -> list:
    with connect() as conn:
        if cluster_id:
            return _rows(conn, "SELECT * FROM cluster_members WHERE run_id = ? AND cluster_id = ?"
                               " ORDER BY supplier_id", (run_id, cluster_id))
        return _rows(conn, "SELECT * FROM cluster_members WHERE run_id = ?"
                           " ORDER BY cluster_id, supplier_id", (run_id,))


# ---- 复核 ----

def get_decision(run_id: str, pair_id: str):
    with connect() as conn:
        return _one(conn, "SELECT * FROM decisions WHERE run_id = ? AND pair_id = ?",
                    (run_id, pair_id))


def list_decisions(run_id: str, since: str = "", until: str = "") -> list:
    with connect() as conn:
        sql = "SELECT * FROM decisions WHERE run_id = ?"
        params = [run_id]
        if since:
            sql += " AND decided_at >= ?"
            params.append(since)
        if until:
            sql += " AND decided_at < ?"
            params.append(until)
        sql += " ORDER BY decided_at DESC, pair_id"
        return _rows(conn, sql, tuple(params))


def save_decision(decision: dict) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO decisions (run_id, pair_id, decision, expected_version,"
            " version, operator, decided_at) VALUES (?,?,?,?,?,?,?)",
            (decision["run_id"], decision["pair_id"], decision["decision"],
             decision["expected_version"], decision["version"], decision["operator"],
             decision["decided_at"]),
        )
        conn.commit()


def count(run_id: str, table: str) -> int:
    if table not in TABLES:
        raise ValueError(table)
    with connect() as conn:
        row = conn.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE run_id = ?", (run_id,)).fetchone()
        return int(row["n"])


# ---- 编码复用接替案件 ----

def save_succession_cases(run_id: str, cases) -> None:
    """重建接替案件：保留已批准/已发布案件，避免重算把人工决定冲掉。"""
    with connect() as conn:
        keep = {row["case_id"] for row in conn.execute(
            "SELECT case_id FROM succession_cases WHERE run_id = ?"
            " AND state IN ('approved', 'published')", (run_id,))}
        conn.execute("DELETE FROM succession_cases WHERE run_id = ?"
                     " AND state NOT IN ('approved', 'published')", (run_id,))
        conn.execute("DELETE FROM succession_impacts WHERE run_id = ? AND case_id NOT IN"
                     " (SELECT case_id FROM succession_cases WHERE run_id = ?)",
                     (run_id, run_id))
        rows, impact_rows = [], []
        for case in cases:
            if case["case_id"] in keep:
                continue
            rows.append((run_id, case["case_id"], case["code"], case["old_id"],
                         case["new_id"], case["verdict"], case["reason_code"],
                         json.dumps(case.get("evidence", {}), ensure_ascii=False,
                                    sort_keys=True), case.get("state", "pending"),
                         case.get("version", 0)))
            for seq, impact in enumerate(case.get("impacts", []), 1):
                impact_rows.append((run_id, case["case_id"], seq, impact["supplier_id"],
                                    impact["side"], impact["impact_code"],
                                    impact.get("field", ""), impact.get("old_value", ""),
                                    impact.get("new_value", ""),
                                    str(impact.get("amount", "0.00")),
                                    impact.get("detail", "")))
        conn.executemany(
            "INSERT OR REPLACE INTO succession_cases (run_id, case_id, code, old_id, new_id,"
            " verdict, reason_code, evidence_json, state, version)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
        conn.executemany(
            "INSERT INTO succession_impacts (run_id, case_id, seq, supplier_id, side,"
            " impact_code, field, old_value, new_value, amount, detail)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)", impact_rows)
        conn.commit()


def list_succession_cases(run_id: str) -> list:
    with connect() as conn:
        return _rows(conn, "SELECT * FROM succession_cases WHERE run_id = ?"
                           " ORDER BY code, case_id", (run_id,))


def get_succession_case(run_id: str, case_id: str):
    with connect() as conn:
        return _one(conn, "SELECT * FROM succession_cases WHERE run_id = ? AND case_id = ?",
                    (run_id, case_id))


def list_succession_impacts(run_id: str, case_id: str = "") -> list:
    with connect() as conn:
        if case_id:
            return _rows(conn, "SELECT * FROM succession_impacts"
                               " WHERE run_id = ? AND case_id = ? ORDER BY seq",
                         (run_id, case_id))
        return _rows(conn, "SELECT * FROM succession_impacts WHERE run_id = ?"
                           " ORDER BY case_id, seq", (run_id,))


def record_succession_decision(run_id: str, case_id: str, expected_version: int,
                               decision: str, operator: str, decided_at: str,
                               state: str = "approved") -> int:
    """带版本乐观锁写入接替决定；返回受影响行数，0 表示被别人抢先改过。"""
    with connect() as conn:
        cursor = conn.execute(
            "UPDATE succession_cases SET state = ?, decided_verdict = ?,"
            " version = version + 1, expected_version = ?, operator = ?, decided_at = ?"
            " WHERE run_id = ? AND case_id = ? AND version = ?",
            (state, decision, expected_version, operator, decided_at, run_id, case_id,
             expected_version),
        )
        conn.commit()
        return cursor.rowcount


# ---- 正式主数据与发布 ----

@contextmanager
def transaction():
    """单一事务：正式主数据、台账与回执同进同退，异常整体回滚。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def latest_master_version() -> int:
    with connect() as conn:
        row = conn.execute("SELECT MAX(version) AS v FROM master_versions").fetchone()
        return int(row["v"] or 0)


def get_master_version(version: int):
    with connect() as conn:
        return _one(conn, "SELECT * FROM master_versions WHERE version = ?", (version,))


def list_master_versions() -> list:
    with connect() as conn:
        return _rows(conn, "SELECT * FROM master_versions ORDER BY version DESC")


def list_master_entities(version: int) -> list:
    with connect() as conn:
        return _rows(conn, "SELECT * FROM master_entities WHERE version = ?"
                           " ORDER BY supplier_no, entity_id", (version,))


def list_master_entries(version: int) -> list:
    with connect() as conn:
        return _rows(conn, "SELECT * FROM master_ledger_entries WHERE version = ?"
                           " ORDER BY entry_id", (version,))


def get_master_entity(version: int, entity_id: str):
    with connect() as conn:
        return _one(conn, "SELECT * FROM master_entities WHERE version = ? AND entity_id = ?",
                    (version, entity_id))


def get_receipt(receipt_id: str):
    with connect() as conn:
        return _one(conn, "SELECT * FROM publish_receipts WHERE receipt_id = ?", (receipt_id,))


def list_receipts(limit: int = 20) -> list:
    with connect() as conn:
        return _rows(conn, "SELECT * FROM publish_receipts ORDER BY created_at DESC,"
                           " receipt_id DESC LIMIT ?", (limit,))


def save_receipt(conn, receipt: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO publish_receipts (receipt_id, run_id, payload_sha256,"
        " version, prior_version, state, created_at, committed_at, detail)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (receipt["receipt_id"], receipt["run_id"], receipt["payload_sha256"],
         receipt["version"], receipt["prior_version"], receipt["state"],
         receipt["created_at"], receipt.get("committed_at", ""), receipt.get("detail", "")),
    )


def snapshot_master(conn, version: int, run_id: str, reason: str) -> None:
    """把指定版本整版写入快照表；重复调用不改变既有快照。"""
    exists = conn.execute("SELECT 1 FROM master_snapshots WHERE version = ?",
                          (version,)).fetchone()
    if exists or version <= 0:
        return
    entities = [dict(row) for row in conn.execute(
        "SELECT * FROM master_entities WHERE version = ? ORDER BY entity_id", (version,))]
    entries = [dict(row) for row in conn.execute(
        "SELECT * FROM master_ledger_entries WHERE version = ? ORDER BY entry_id", (version,))]
    conn.execute(
        "INSERT INTO master_snapshots (version, run_id, state, reason, entities_json,"
        " ledger_json) VALUES (?,?,?,?,?,?)",
        (version, run_id, "archived", reason,
         json.dumps(entities, ensure_ascii=False, sort_keys=True),
         json.dumps(entries, ensure_ascii=False, sort_keys=True)),
    )


def get_snapshot(version: int):
    with connect() as conn:
        row = _one(conn, "SELECT * FROM master_snapshots WHERE version = ?", (version,))
    if row:
        row["entities"] = json.loads(row["entities_json"])
        row["ledger"] = json.loads(row["ledger_json"])
    return row


def list_snapshots() -> list:
    with connect() as conn:
        return _rows(conn, "SELECT version, run_id, state, reason FROM master_snapshots"
                           " ORDER BY version DESC")

