import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DB_PATH = Path("data/violations.db")
ROUTINE_AUDIT_SINCE_CUTOFF = "2026-06-01"
# Lead volume must not gate routine runs: production already exceeds 500–1000 leads.
# Keep the arg for call-site compatibility / future diagnostics.
ROUTINE_AUDIT_MAX_LEADS = 10_000
# Cron fires at :00; allow a few minutes of container/startup skew.
ROUTINE_AUDIT_MINUTE_MAX = 5

logger = logging.getLogger(__name__)


def _parse_iso_datetime(value: str) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def is_routine_audit_run(report_since: str, total_leads: int, run_time: str = "") -> bool:
    """Определить, является ли запуск штатным cron-аудитом.

    Criteria: report_since not earlier than cutoff, weekday, hour in {7, 14} UTC,
    minute within startup skew. Lead count is only a safety ceiling for pathological
    full-history dumps — not a normal production filter.
    """
    since = (report_since or "").strip()
    if since and since < ROUTINE_AUDIT_SINCE_CUTOFF:
        return False
    if total_leads > ROUTINE_AUDIT_MAX_LEADS:
        return False
    dt = _parse_iso_datetime(run_time) or datetime.now(timezone.utc)
    # Main audit cron schedule: weekdays 07:00 and 14:00 UTC.
    if dt.weekday() >= 5:
        return False
    if dt.hour not in {7, 14}:
        return False
    if dt.minute > ROUTINE_AUDIT_MINUTE_MAX:
        return False
    return True


def get_connection() -> sqlite3.Connection:
    """Return connection with WAL mode for better concurrent reads."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    """Create tables if not exist (idempotent)."""
    with get_connection() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS audit_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_time TEXT NOT NULL,            -- ISO: 2026-06-15T10:00:00+00:00
            total_leads INTEGER DEFAULT 0,
            total_buyer_deals INTEGER DEFAULT 0,
            total_seller_deals INTEGER DEFAULT 0,
            total_violations INTEGER DEFAULT 0
        );
        """)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS violations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            audit_run_id INTEGER NOT NULL REFERENCES audit_runs(id),
            entity_type TEXT NOT NULL,         -- 'lead' или 'deal'
            entity_id INTEGER NOT NULL,
            responsible_id INTEGER NOT NULL,
            responsible_name TEXT NOT NULL,
            department TEXT NOT NULL DEFAULT '',
            department_id INTEGER,
            rule TEXT NOT NULL,                -- 'lead_rule_1', 'buyer_stage_3', ...
            severity TEXT NOT NULL,            -- 'high', 'medium', 'very high'
            reason TEXT NOT NULL,
            detected_at TEXT NOT NULL,         -- ISO
            UNIQUE(audit_run_id, entity_type, entity_id, rule)
        );
        """)

        _migrate_audit_runs(conn)

        conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_violations_responsible
            ON violations(responsible_id, detected_at);
        """)

        conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_violations_detected
            ON violations(detected_at);
        """)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS brokers (
            responsible_id INTEGER PRIMARY KEY,
            responsible_name TEXT NOT NULL,
            department TEXT NOT NULL DEFAULT '',
            department_id INTEGER,
            lead_count INTEGER DEFAULT 0,     -- лидов в последнем аудите
            deal_count INTEGER DEFAULT 0,     -- сделок в последнем аудите
            last_seen TEXT NOT NULL           -- дата последнего аудита
        );
        """)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS opened_leads_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_time TEXT NOT NULL,
            leads_fixed INTEGER NOT NULL DEFAULT 0
        );
        """)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS exclusive_expiry_notifications (
            item_id INTEGER NOT NULL,
            end_date TEXT NOT NULL,
            days_before INTEGER NOT NULL,
            assigned_by_id INTEGER NOT NULL,
            notified_at TEXT NOT NULL,
            PRIMARY KEY (item_id, end_date, days_before)
        );
        """)
        _migrate_exclusive_expiry_notifications(conn)


def _migrate_exclusive_expiry_notifications(conn: sqlite3.Connection) -> None:
    """Upgrade PK to (item_id, end_date, days_before) for 7/3/1 milestones."""
    rows = conn.execute(
        "SELECT sql FROM sqlite_master "
        "WHERE type='table' AND name='exclusive_expiry_notifications'"
    ).fetchone()
    ddl = (rows[0] or "") if rows else ""
    if "days_before" in ddl and "PRIMARY KEY (item_id, end_date, days_before)" in ddl:
        return
    if "exclusive_expiry_notifications" not in ddl:
        return

    conn.execute("""
    CREATE TABLE IF NOT EXISTS exclusive_expiry_notifications_new (
        item_id INTEGER NOT NULL,
        end_date TEXT NOT NULL,
        days_before INTEGER NOT NULL,
        assigned_by_id INTEGER NOT NULL,
        notified_at TEXT NOT NULL,
        PRIMARY KEY (item_id, end_date, days_before)
    );
    """)
    try:
        conn.execute("""
        INSERT OR IGNORE INTO exclusive_expiry_notifications_new (
            item_id, end_date, days_before, assigned_by_id, notified_at
        )
        SELECT item_id, end_date, days_before, assigned_by_id, notified_at
        FROM exclusive_expiry_notifications
        """)
    except sqlite3.OperationalError:
        pass
    conn.execute("DROP TABLE IF EXISTS exclusive_expiry_notifications")
    conn.execute(
        "ALTER TABLE exclusive_expiry_notifications_new "
        "RENAME TO exclusive_expiry_notifications"
    )


def _migrate_audit_runs(conn: sqlite3.Connection) -> None:
    """Add routine/full-scan columns and purge big-report violations once."""
    for ddl in (
        "ALTER TABLE audit_runs ADD COLUMN report_since TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE audit_runs ADD COLUMN is_routine INTEGER NOT NULL DEFAULT 1",
    ):
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            pass

    rows = conn.execute(
        "SELECT id, run_time, total_leads, report_since FROM audit_runs",
    ).fetchall()
    for row in rows:
        rid = int(row[0])
        run_time = str(row[1] or "")
        total_leads = int(row[2] or 0)
        report_since = str(row[3] or "")
        routine = is_routine_audit_run(report_since, total_leads, run_time)
        conn.execute(
            "UPDATE audit_runs SET is_routine = ? WHERE id = ?",
            (1 if routine else 0, rid),
        )

    deleted = conn.execute("""
        DELETE FROM violations
        WHERE audit_run_id IN (SELECT id FROM audit_runs WHERE is_routine = 0)
    """).rowcount
    if deleted:
        logger.info("Removed %d violations from non-routine audit runs", deleted)


def purge_test_violations() -> int:
    """Delete violations from runs that are not routine cron runs."""
    init_db()
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        runs = conn.execute(
            "SELECT id, run_time, total_leads, report_since FROM audit_runs",
        ).fetchall()
        to_delete: list[int] = []
        for row in runs:
            rid = int(row["id"])
            run_time = str(row["run_time"] or "")
            total_leads = int(row["total_leads"] or 0)
            report_since = str(row["report_since"] or "")
            if not is_routine_audit_run(report_since, total_leads, run_time):
                to_delete.append(rid)
        if not to_delete:
            return 0
        placeholders = ",".join("?" for _ in to_delete)
        deleted_viol = conn.execute(
            f"DELETE FROM violations WHERE audit_run_id IN ({placeholders})",
            to_delete,
        ).rowcount
        conn.execute(
            f"DELETE FROM audit_runs WHERE id IN ({placeholders})",
            to_delete,
        )
        logger.info(
            "Purged %d test violations from %d audit runs",
            deleted_viol,
            len(to_delete),
        )
        return deleted_viol


def save_audit_run(
    run_time: str,
    total_leads: int,
    total_buyer_deals: int,
    total_seller_deals: int,
    total_violations: int,
    report_since: str = "",
    is_routine: bool = True,
) -> int:
    """Insert audit_run row, return run_id."""
    with get_connection() as conn:
        cursor = conn.execute("""
        INSERT INTO audit_runs (
            run_time, total_leads, total_buyer_deals, total_seller_deals,
            total_violations, report_since, is_routine
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            run_time,
            total_leads,
            total_buyer_deals,
            total_seller_deals,
            total_violations,
            report_since,
            1 if is_routine else 0,
        ))
        return cursor.lastrowid or 0


def save_violations(audit_run_id: int, violations: list[dict[str, Any]],
                    user_map: dict[int, str], dept_id_map: dict[int, int], now: str) -> int:
    """INSERT OR IGNORE violations, return count saved."""
    if not violations:
        return 0

    records = []
    for v in violations:
        # Some violations might have 'responsible_id' missing or as string
        resp_id = int(v.get("responsible_id") or 0)
        # Extract dept from user map name if it includes (Dept Name)
        resp_name = user_map.get(resp_id, str(resp_id))
        dept = ""
        if "(" in resp_name and ")" in resp_name:
            dept = resp_name.split("(")[-1].rstrip(")")
            resp_name = resp_name.split("(")[0].strip()

        dept_id = dept_id_map.get(resp_id)

        records.append((
            audit_run_id,
            str(v.get("entity_type", "")),
            int(v.get("entity_id") or 0),
            resp_id,
            resp_name,
            dept,
            dept_id,
            str(v.get("rule", "")),
            str(v.get("severity", "")),
            str(v.get("reason", "")),
            now
        ))

    with get_connection() as conn:
        cursor = conn.executemany("""
        INSERT OR IGNORE INTO violations (
            audit_run_id, entity_type, entity_id, responsible_id,
            responsible_name, department, department_id, rule, severity, reason, detected_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, records)
        return cursor.rowcount


def upsert_brokers(
    user_map: dict[int, str],
    dept_id_map: dict[int, int],
    leads: list[dict[str, Any]],
    buyer_deals: list[dict[str, Any]],
    seller_deals: list[dict[str, Any]],
    now: str
) -> None:
    """Update brokers table: name, dept, dept_id, lead/deal counts, last_seen."""
    broker_stats: dict[int, dict[str, Any]] = {}

    # Initialize all brokers from user_map
    for resp_id, resp_name in user_map.items():
        dept = ""
        if "(" in resp_name and ")" in resp_name:
            dept = resp_name.split("(")[-1].rstrip(")")
            resp_name = resp_name.split("(")[0].strip()
        broker_stats[resp_id] = {
            "name": resp_name,
            "dept": dept,
            "dept_id": dept_id_map.get(resp_id),
            "leads": 0,
            "deals": 0
        }

    # Count leads
    for lead in leads:
        rid = int(lead.get("assigned_by_id") or 0)
        if rid in broker_stats:
            broker_stats[rid]["leads"] += 1

    # Count deals
    for deal in buyer_deals + seller_deals:
        rid = int(deal.get("assigned_by_id") or 0)
        if rid in broker_stats:
            broker_stats[rid]["deals"] += 1

    records = []
    for rid, stats in broker_stats.items():
        records.append((
            rid, stats["name"], stats["dept"], stats["dept_id"], stats["leads"], stats["deals"], now
        ))

    with get_connection() as conn:
        conn.executemany("""
        INSERT INTO brokers (
            responsible_id, responsible_name, department, department_id,
            lead_count, deal_count, last_seen
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(responsible_id) DO UPDATE SET
            responsible_name=excluded.responsible_name,
            department=excluded.department,
            department_id=excluded.department_id,
            lead_count=excluded.lead_count,
            deal_count=excluded.deal_count,
            last_seen=excluded.last_seen
        """, records)


def get_weekly_stats(
    week_start: str,
    week_end: str | None = None,
    latest_only: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (violators_list, clean_brokers_list) for weekly report."""
    upper = week_end or datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        latest_run_id: int | None = None
        if latest_only:
            run_row = conn.execute(
                """
                SELECT id
                FROM audit_runs
                WHERE is_routine = 1
                  AND run_time >= ?
                  AND run_time < ?
                ORDER BY run_time DESC
                LIMIT 1
                """,
                (week_start, upper),
            ).fetchone()
            if run_row:
                latest_run_id = int(run_row["id"])

        # Get violators
        if latest_run_id is None and latest_only:
            violators: list[dict[str, Any]] = []
        else:
            use_single_run = 1 if latest_run_id is not None else 0
            run_id_filter = latest_run_id or 0
            violators_cur = conn.execute("""
        SELECT
            v.responsible_id,
            v.responsible_name,
            v.department,
            v.department_id,
            COUNT(*) as total_violations,
            SUM(CASE WHEN v.entity_type = 'lead' AND v.rule NOT LIKE '%missed%' THEN 1 ELSE 0 END)
                as lead_violations,
            SUM(CASE WHEN v.entity_type = 'deal' AND v.rule NOT LIKE '%missed%' THEN 1 ELSE 0 END)
                as deal_violations,
            SUM(CASE WHEN v.rule LIKE '%missed_callback%' THEN 1 ELSE 0 END)
                as missed_call_violations,
            GROUP_CONCAT(DISTINCT v.rule) as rules
        FROM violations v
        JOIN audit_runs r ON v.audit_run_id = r.id
        WHERE v.detected_at >= ?
          AND v.detected_at < ?
          AND r.is_routine = 1
          AND (? = 0 OR v.audit_run_id = ?)
        GROUP BY v.responsible_id
        ORDER BY total_violations DESC
        """, (week_start, upper, use_single_run, run_id_filter))

            violators = [dict(r) for r in violators_cur.fetchall()]

        # Get clean brokers
        if latest_run_id is None and latest_only:
            clean: list[dict[str, Any]] = []
        else:
            use_single_run = 1 if latest_run_id is not None else 0
            run_id_filter = latest_run_id or 0
            clean_cur = conn.execute("""
        SELECT
            b.responsible_id,
            b.responsible_name,
            b.department,
            b.department_id,
            b.lead_count,
            b.deal_count
        FROM brokers b
        LEFT JOIN violations v ON b.responsible_id = v.responsible_id
            AND v.detected_at >= ?
            AND v.detected_at < ?
            AND v.audit_run_id IN (SELECT id FROM audit_runs WHERE is_routine = 1)
            AND (? = 0 OR v.audit_run_id = ?)
        WHERE v.responsible_id IS NULL
            AND (b.lead_count > 0 OR b.deal_count > 0)
        ORDER BY b.responsible_name
        """, (week_start, upper, use_single_run, run_id_filter))

            clean = [dict(r) for r in clean_cur.fetchall()]

        return violators, clean


def was_exclusive_expiry_notified(
    item_id: int,
    end_date: str,
    days_before: int,
) -> bool:
    """Return True if this milestone reminder was already sent."""
    init_db()
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM exclusive_expiry_notifications
            WHERE item_id = ? AND end_date = ? AND days_before = ?
            """,
            (item_id, end_date, days_before),
        ).fetchone()
        return row is not None


def mark_exclusive_expiry_notified(
    item_id: int,
    end_date: str,
    assigned_by_id: int,
    days_before: int,
    notified_at: str,
) -> None:
    """Persist that a milestone expiry reminder was sent (idempotent)."""
    init_db()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO exclusive_expiry_notifications (
                item_id, end_date, days_before, assigned_by_id, notified_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (item_id, end_date, days_before, assigned_by_id, notified_at),
        )
