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

        conn.execute("""
        CREATE TABLE IF NOT EXISTS contact_source_snapshots (
            contact_id INTEGER PRIMARY KEY,
            source_id TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        );
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS deal_source_snapshots (
            deal_id INTEGER PRIMARY KEY,
            source_id TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        );
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS deal_base_rate_snapshots (
            deal_id INTEGER PRIMARY KEY,
            base_rate TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        );
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS buyer_commission_notifications (
            deal_id INTEGER NOT NULL,
            recipient_role TEXT NOT NULL,
            recipient_user_id INTEGER NOT NULL,
            notified_at TEXT NOT NULL,
            PRIMARY KEY (deal_id, recipient_role)
        );
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS buyer_commission_enforcements (
            deal_id INTEGER NOT NULL,
            enforce_date TEXT NOT NULL,
            previous_assigned_by_id INTEGER NOT NULL DEFAULT 0,
            new_assigned_by_id INTEGER NOT NULL DEFAULT 0,
            enforced_at TEXT NOT NULL,
            PRIMARY KEY (deal_id, enforce_date)
        );
        """)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS broker_shared_leads (
            lead_id INTEGER NOT NULL,
            previous_assigned_by_id INTEGER NOT NULL,
            moved_at TEXT NOT NULL,
            detected_at TEXT NOT NULL,
            UNIQUE(lead_id, moved_at)
        );
        """)
        conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_broker_shared_leads_broker
            ON broker_shared_leads(previous_assigned_by_id, moved_at);
        """)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS broker_daily_metrics (
            metric_date TEXT NOT NULL,
            responsible_id INTEGER NOT NULL,
            had_crm_visit INTEGER NOT NULL DEFAULT 0,
            clean_day INTEGER NOT NULL DEFAULT 0,
            violations_today INTEGER NOT NULL DEFAULT 0,
            news_read_today INTEGER NOT NULL DEFAULT 0,
            tasks_overdue_today INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (metric_date, responsible_id)
        );
        """)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS broker_ratings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_date TEXT NOT NULL,
            responsible_id INTEGER NOT NULL,
            responsible_name TEXT NOT NULL,
            department TEXT NOT NULL DEFAULT '',
            department_id INTEGER,
            score REAL NOT NULL,
            tier TEXT NOT NULL,
            crm_score REAL NOT NULL,
            portfolio_score REAL NOT NULL,
            tasks_score REAL NOT NULL,
            engagement_score REAL NOT NULL,
            violations_count INTEGER NOT NULL DEFAULT 0,
            shared_leads_count INTEGER NOT NULL DEFAULT 0,
            pool_deals_count INTEGER NOT NULL DEFAULT 0,
            overdue_tasks INTEGER NOT NULL DEFAULT 0,
            tasks_closed_on_time INTEGER NOT NULL DEFAULT 0,
            crm_visit_days INTEGER NOT NULL DEFAULT 0,
            news_read_ratio REAL NOT NULL DEFAULT 0,
            clean_days INTEGER NOT NULL DEFAULT 0,
            rank_overall INTEGER,
            rank_in_dept INTEGER,
            UNIQUE(snapshot_date, responsible_id)
        );
        """)
        conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_broker_ratings_date
            ON broker_ratings(snapshot_date);
        """)
        conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_broker_ratings_responsible
            ON broker_ratings(responsible_id, snapshot_date);
        """)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS lead_quality_findings (
            lead_id INTEGER NOT NULL,
            rule TEXT NOT NULL,
            responsible_id INTEGER NOT NULL DEFAULT 0,
            reason TEXT NOT NULL DEFAULT '',
            status_name TEXT NOT NULL DEFAULT '',
            found_at TEXT NOT NULL,
            notified_at TEXT,
            PRIMARY KEY (lead_id, rule)
        );
        """)
        conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_lead_quality_findings_found
            ON lead_quality_findings(found_at);
        """)


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
    """Add routine/full-scan columns; one-time reclassify/purge only on first add."""
    added_columns = False
    for ddl in (
        "ALTER TABLE audit_runs ADD COLUMN report_since TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE audit_runs ADD COLUMN is_routine INTEGER NOT NULL DEFAULT 1",
    ):
        try:
            conn.execute(ddl)
            added_columns = True
        except sqlite3.OperationalError:
            pass

    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    done = conn.execute(
        "SELECT 1 FROM schema_meta WHERE key = 'audit_runs_routine_purged'",
    ).fetchone()
    if done:
        return

    # Columns already existed in production — do not reclassify/purge again
    # (that would wipe FORCE_ROUTINE_AUDIT / off-schedule saves).
    if not added_columns:
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) "
            "VALUES ('audit_runs_routine_purged', '1')",
        )
        return

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
    conn.execute(
        "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('audit_runs_routine_purged', '1')",
    )


def purge_test_violations() -> int:
    """Delete violations from runs explicitly marked non-routine (is_routine=0)."""
    init_db()
    with get_connection() as conn:
        to_delete = [
            int(r[0])
            for r in conn.execute(
                "SELECT id FROM audit_runs WHERE is_routine = 0",
            ).fetchall()
        ]
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


def count_contact_source_snapshots() -> int:
    """Return number of stored contact SOURCE_ID snapshots."""
    init_db()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM contact_source_snapshots"
        ).fetchone()
        return int(row[0] if row else 0)


def get_contact_source_snapshot(contact_id: int) -> str | None:
    """Return stored SOURCE_ID for contact, or None if missing."""
    init_db()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT source_id FROM contact_source_snapshots WHERE contact_id = ?",
            (contact_id,),
        ).fetchone()
        return None if row is None else str(row[0] or "")


def upsert_contact_source_snapshot(
    contact_id: int,
    source_id: str,
    updated_at: str,
) -> None:
    """Insert or update SOURCE_ID snapshot for a contact."""
    init_db()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO contact_source_snapshots (contact_id, source_id, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(contact_id) DO UPDATE SET
                source_id = excluded.source_id,
                updated_at = excluded.updated_at
            """,
            (contact_id, source_id or "", updated_at),
        )


def bulk_upsert_contact_source_snapshots(
    rows: list[tuple[int, str, str]],
) -> int:
    """Upsert many snapshots. Each row: (contact_id, source_id, updated_at)."""
    if not rows:
        return 0
    init_db()
    with get_connection() as conn:
        conn.executemany(
            """
            INSERT INTO contact_source_snapshots (contact_id, source_id, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(contact_id) DO UPDATE SET
                source_id = excluded.source_id,
                updated_at = excluded.updated_at
            """,
            rows,
        )
        return len(rows)


def count_deal_source_snapshots() -> int:
    """Return number of stored deal SOURCE_ID snapshots."""
    init_db()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM deal_source_snapshots"
        ).fetchone()
        return int(row[0] if row else 0)


def get_deal_source_snapshot(deal_id: int) -> str | None:
    """Return stored SOURCE_ID for deal, or None if missing."""
    init_db()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT source_id FROM deal_source_snapshots WHERE deal_id = ?",
            (deal_id,),
        ).fetchone()
        return None if row is None else str(row[0] or "")


def upsert_deal_source_snapshot(
    deal_id: int,
    source_id: str,
    updated_at: str,
) -> None:
    """Insert or update SOURCE_ID snapshot for a deal."""
    init_db()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO deal_source_snapshots (deal_id, source_id, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(deal_id) DO UPDATE SET
                source_id = excluded.source_id,
                updated_at = excluded.updated_at
            """,
            (deal_id, source_id or "", updated_at),
        )


def count_deal_base_rate_snapshots() -> int:
    """Return number of stored deal base-rate snapshots."""
    init_db()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM deal_base_rate_snapshots"
        ).fetchone()
        return int(row[0] if row else 0)


def get_deal_base_rate_snapshot(deal_id: int) -> str | None:
    """Return stored base rate for deal, or None if missing."""
    init_db()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT base_rate FROM deal_base_rate_snapshots WHERE deal_id = ?",
            (deal_id,),
        ).fetchone()
        return None if row is None else str(row[0] or "")


def upsert_deal_base_rate_snapshot(
    deal_id: int,
    base_rate: str,
    updated_at: str,
) -> None:
    """Insert or update base-rate snapshot for a deal."""
    init_db()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO deal_base_rate_snapshots (deal_id, base_rate, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(deal_id) DO UPDATE SET
                base_rate = excluded.base_rate,
                updated_at = excluded.updated_at
            """,
            (deal_id, base_rate or "", updated_at),
        )


def get_buyer_commission_notified_at(
    deal_id: int,
    recipient_role: str,
) -> str | None:
    """Return last notification timestamp for deal+role, or None."""
    init_db()
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT notified_at FROM buyer_commission_notifications
            WHERE deal_id = ? AND recipient_role = ?
            """,
            (deal_id, recipient_role),
        ).fetchone()
        return None if row is None else str(row[0] or "")


def mark_buyer_commission_notified(
    deal_id: int,
    recipient_role: str,
    recipient_user_id: int,
    notified_at: str,
) -> None:
    """Upsert last commission-reminder notification for deal+role."""
    init_db()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO buyer_commission_notifications (
                deal_id, recipient_role, recipient_user_id, notified_at
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(deal_id, recipient_role) DO UPDATE SET
                recipient_user_id = excluded.recipient_user_id,
                notified_at = excluded.notified_at
            """,
            (deal_id, recipient_role, recipient_user_id, notified_at),
        )


def was_buyer_commission_enforced(deal_id: int, enforce_date: str) -> bool:
    """Return True if deal was already moved to pool on enforce_date."""
    init_db()
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM buyer_commission_enforcements
            WHERE deal_id = ? AND enforce_date = ?
            """,
            (deal_id, enforce_date),
        ).fetchone()
        return row is not None


def mark_buyer_commission_enforced(
    deal_id: int,
    enforce_date: str,
    previous_assigned_by_id: int,
    new_assigned_by_id: int,
    enforced_at: str,
) -> None:
    """Persist that a deal was moved to the shared pool on enforce_date."""
    init_db()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO buyer_commission_enforcements (
                deal_id, enforce_date, previous_assigned_by_id,
                new_assigned_by_id, enforced_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                deal_id,
                enforce_date,
                previous_assigned_by_id,
                new_assigned_by_id,
                enforced_at,
            ),
        )


# ── Broker rating ────────────────────────────────────────

def upsert_broker_shared_lead(
    lead_id: int,
    previous_assigned_by_id: int,
    moved_at: str,
    detected_at: str,
) -> None:
    """Persist a lead moved to «Общие лиды»."""
    init_db()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO broker_shared_leads (
                lead_id, previous_assigned_by_id, moved_at, detected_at
            ) VALUES (?, ?, ?, ?)
            """,
            (lead_id, previous_assigned_by_id, moved_at, detected_at),
        )


def count_shared_leads_for_broker(
    broker_id: int,
    since: str,
    until: str | None = None,
) -> int:
    """Count leads moved to shared queue attributed to broker."""
    init_db()
    upper = until or datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) FROM broker_shared_leads
            WHERE previous_assigned_by_id = ?
              AND moved_at >= ?
              AND moved_at < ?
            """,
            (broker_id, since, upper),
        ).fetchone()
        return int(row[0] if row else 0)


def count_pool_deals_for_broker(
    broker_id: int,
    since: str,
    until: str | None = None,
) -> int:
    """Count deals moved to shared pool attributed to broker."""
    init_db()
    upper = until or datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) FROM buyer_commission_enforcements
            WHERE previous_assigned_by_id = ?
              AND enforced_at >= ?
              AND enforced_at < ?
            """,
            (broker_id, since, upper),
        ).fetchone()
        return int(row[0] if row else 0)


def upsert_broker_daily_metric(
    metric_date: str,
    responsible_id: int,
    *,
    had_crm_visit: int = 0,
    clean_day: int = 0,
    violations_today: int = 0,
    news_read_today: int = 0,
    tasks_overdue_today: int = 0,
) -> None:
    """Insert or update daily broker metrics."""
    init_db()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO broker_daily_metrics (
                metric_date, responsible_id, had_crm_visit, clean_day,
                violations_today, news_read_today, tasks_overdue_today
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(metric_date, responsible_id) DO UPDATE SET
                had_crm_visit = excluded.had_crm_visit,
                clean_day = excluded.clean_day,
                violations_today = excluded.violations_today,
                news_read_today = excluded.news_read_today,
                tasks_overdue_today = excluded.tasks_overdue_today
            """,
            (
                metric_date,
                responsible_id,
                had_crm_visit,
                clean_day,
                violations_today,
                news_read_today,
                tasks_overdue_today,
            ),
        )


def get_broker_daily_metrics_summary(
    broker_id: int,
    since: str,
    until: str | None = None,
) -> dict[str, int]:
    """Aggregate daily metrics for a broker in a date range."""
    init_db()
    upper = until or datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT
                COALESCE(SUM(had_crm_visit), 0),
                COALESCE(SUM(clean_day), 0),
                COALESCE(SUM(news_read_today), 0)
            FROM broker_daily_metrics
            WHERE responsible_id = ?
              AND metric_date >= ?
              AND metric_date < ?
            """,
            (broker_id, since[:10], upper[:10]),
        ).fetchone()
        if not row:
            return {"crm_visit_days": 0, "clean_days": 0, "news_read_days": 0}
        return {
            "crm_visit_days": int(row[0]),
            "clean_days": int(row[1]),
            "news_read_days": int(row[2]),
        }


def get_violations_for_broker(
    broker_id: int,
    since: str,
    until: str | None = None,
) -> list[dict[str, Any]]:
    """Return routine violations for broker in period."""
    init_db()
    upper = until or datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT v.entity_type, v.rule, v.severity, v.detected_at
            FROM violations v
            JOIN audit_runs r ON v.audit_run_id = r.id
            WHERE v.responsible_id = ?
              AND v.detected_at >= ?
              AND v.detected_at < ?
              AND r.is_routine = 1
            ORDER BY v.detected_at
            """,
            (broker_id, since, upper),
        ).fetchall()
        return [dict(r) for r in rows]


def count_violations_on_date(broker_id: int, metric_date: str) -> int:
    """Count routine violations detected on a calendar date (YYYY-MM-DD)."""
    init_db()
    day_start = f"{metric_date}T00:00:00+00:00"
    day_end = f"{metric_date}T23:59:59+00:00"
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) FROM violations v
            JOIN audit_runs r ON v.audit_run_id = r.id
            WHERE v.responsible_id = ?
              AND v.detected_at >= ?
              AND v.detected_at <= ?
              AND r.is_routine = 1
            """,
            (broker_id, day_start, day_end),
        ).fetchone()
        return int(row[0] if row else 0)


def save_broker_ratings_snapshot(ratings: list[dict[str, Any]], snapshot_date: str) -> None:
    """Persist weekly rating snapshot for all brokers."""
    init_db()
    if not ratings:
        return
    records = [
        (
            snapshot_date,
            r["responsible_id"],
            r.get("responsible_name", ""),
            r.get("department", ""),
            r.get("department_id"),
            r["score"],
            r["tier"],
            r["crm_score"],
            r["portfolio_score"],
            r["tasks_score"],
            r["engagement_score"],
            r.get("violations_count", 0),
            r.get("shared_leads_count", 0),
            r.get("pool_deals_count", 0),
            r.get("overdue_tasks", 0),
            r.get("tasks_closed_on_time", 0),
            r.get("crm_visit_days", 0),
            r.get("news_read_ratio", 0.0),
            r.get("clean_days", 0),
            r.get("rank_overall"),
            r.get("rank_in_dept"),
        )
        for r in ratings
    ]
    with get_connection() as conn:
        conn.executemany(
            """
            INSERT INTO broker_ratings (
                snapshot_date, responsible_id, responsible_name, department,
                department_id, score, tier, crm_score, portfolio_score,
                tasks_score, engagement_score, violations_count,
                shared_leads_count, pool_deals_count, overdue_tasks,
                tasks_closed_on_time, crm_visit_days, news_read_ratio,
                clean_days, rank_overall, rank_in_dept
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(snapshot_date, responsible_id) DO UPDATE SET
                responsible_name = excluded.responsible_name,
                department = excluded.department,
                department_id = excluded.department_id,
                score = excluded.score,
                tier = excluded.tier,
                crm_score = excluded.crm_score,
                portfolio_score = excluded.portfolio_score,
                tasks_score = excluded.tasks_score,
                engagement_score = excluded.engagement_score,
                violations_count = excluded.violations_count,
                shared_leads_count = excluded.shared_leads_count,
                pool_deals_count = excluded.pool_deals_count,
                overdue_tasks = excluded.overdue_tasks,
                tasks_closed_on_time = excluded.tasks_closed_on_time,
                crm_visit_days = excluded.crm_visit_days,
                news_read_ratio = excluded.news_read_ratio,
                clean_days = excluded.clean_days,
                rank_overall = excluded.rank_overall,
                rank_in_dept = excluded.rank_in_dept
            """,
            records,
        )


def get_previous_rating_snapshot(
    before_date: str,
) -> dict[int, float]:
    """Return {broker_id: score} from latest snapshot before given date."""
    init_db()
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT snapshot_date FROM broker_ratings
            WHERE snapshot_date < ?
            ORDER BY snapshot_date DESC
            LIMIT 1
            """,
            (before_date,),
        ).fetchone()
        if not row:
            return {}
        snap_date = row[0]
        rows = conn.execute(
            """
            SELECT responsible_id, score FROM broker_ratings
            WHERE snapshot_date = ?
            """,
            (snap_date,),
        ).fetchall()
        return {int(r[0]): float(r[1]) for r in rows}


def get_broker_rating_history(broker_id: int, limit: int = 12) -> list[dict[str, Any]]:
    """Return recent rating snapshots for a broker."""
    init_db()
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT * FROM broker_ratings
            WHERE responsible_id = ?
            ORDER BY snapshot_date DESC
            LIMIT ?
            """,
            (broker_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def list_active_brokers_from_db() -> list[dict[str, Any]]:
    """Return brokers with active portfolio from latest audit snapshot."""
    init_db()
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT responsible_id, responsible_name, department, department_id,
                   lead_count, deal_count
            FROM brokers
            WHERE lead_count > 0 OR deal_count > 0
            ORDER BY responsible_name
            """
        ).fetchall()
        return [dict(r) for r in rows]


# ── Lead quality (spam / non-target) ─────────────────────

def was_lead_quality_finding_recorded(lead_id: int, rule: str) -> bool:
    """True if this lead+rule was already persisted."""
    init_db()
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM lead_quality_findings
            WHERE lead_id = ? AND rule = ?
            """,
            (lead_id, rule),
        ).fetchone()
        return row is not None


def save_lead_quality_finding(
    lead_id: int,
    rule: str,
    responsible_id: int,
    reason: str,
    status_name: str,
    found_at: str,
    notified_at: str | None = None,
) -> bool:
    """Insert finding. Returns True if newly inserted."""
    init_db()
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO lead_quality_findings (
                lead_id, rule, responsible_id, reason, status_name,
                found_at, notified_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                lead_id,
                rule,
                responsible_id,
                reason,
                status_name,
                found_at,
                notified_at,
            ),
        )
        return cur.rowcount > 0


def mark_lead_quality_notified(
    lead_id: int,
    rule: str,
    notified_at: str,
) -> None:
    """Set notified_at for an existing finding."""
    init_db()
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE lead_quality_findings
            SET notified_at = ?
            WHERE lead_id = ? AND rule = ?
            """,
            (notified_at, lead_id, rule),
        )


def delete_lead_quality_finding(lead_id: int, rule: str | None = None) -> int:
    """Delete finding(s) for lead. If rule is None — all rules for lead."""
    init_db()
    with get_connection() as conn:
        if rule:
            cur = conn.execute(
                "DELETE FROM lead_quality_findings WHERE lead_id = ? AND rule = ?",
                (lead_id, rule),
            )
        else:
            cur = conn.execute(
                "DELETE FROM lead_quality_findings WHERE lead_id = ?",
                (lead_id,),
            )
        return int(cur.rowcount or 0)


def list_lead_quality_findings() -> list[dict[str, Any]]:
    """Return all stored quality findings."""
    init_db()
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT lead_id, rule, responsible_id, reason, status_name,
                   found_at, notified_at
            FROM lead_quality_findings
            ORDER BY lead_id, rule
            """
        ).fetchall()
        return [dict(r) for r in rows]
