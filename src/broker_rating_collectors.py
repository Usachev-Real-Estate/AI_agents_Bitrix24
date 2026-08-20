"""Collect Bitrix24 metrics for broker CRM rating."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_SRC_DIR = Path(__file__).resolve().parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from config import Settings, get_settings, setup_logging  # noqa: E402
from db import (  # noqa: E402
    count_violations_on_date,
    init_db,
    list_active_brokers_from_db,
    upsert_broker_daily_metric,
    upsert_broker_shared_lead,
)
from notify import _bx_call_sync  # noqa: E402
from task_auditor import classify_tasks  # noqa: E402
from tools import LEAD_STATUS_SHARED, _bx_get_all_sync  # noqa: E402

logger = logging.getLogger(__name__)

EXCLUDED_RATING_DEPARTMENTS = {"ТО", "Битрикс", "Бэк-офис"}
_ROP_POSITION_HINTS = (
    "руководитель отдела продаж",
    "роп",
)


def _parse_dt(value: str | None) -> datetime | None:
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


def _is_active_user(user: dict[str, Any]) -> bool:
    active = user.get("ACTIVE", True)
    return str(active).upper() not in {"N", "FALSE", "0"} and active is not False


def _is_rop_user(user: dict[str, Any]) -> bool:
    pos = str(user.get("WORK_POSITION") or "").strip().lower()
    return any(hint in pos for hint in _ROP_POSITION_HINTS)


def fetch_users_by_departments(dept_ids: list[int]) -> list[dict[str, Any]]:
    """Return active users in sales departments."""
    users: list[dict[str, Any]] = []
    seen: set[int] = set()
    for dept_id in dept_ids:
        try:
            batch = _bx_get_all_sync("user.get", {
                "FILTER": {"UF_DEPARTMENT": dept_id, "ACTIVE": True},
                "SELECT": [
                    "ID", "NAME", "LAST_NAME", "UF_DEPARTMENT", "ACTIVE",
                    "WORK_POSITION", "LAST_ACTIVITY_DATE", "LAST_LOGIN",
                ],
            })
        except Exception as exc:
            logger.warning("user.get failed for dept %s: %s", dept_id, exc)
            continue
        for u in batch or []:
            uid = int(u.get("ID") or 0)
            if uid <= 0 or uid in seen:
                continue
            seen.add(uid)
            users.append(u)
    return users


def list_eligible_brokers_for_rating(settings: Settings | None = None) -> list[dict[str, Any]]:
    """Active sales brokers only: no ROP, no terminated, no service accounts."""
    cfg = settings or get_settings()
    users = fetch_users_by_departments(cfg.broker_rating_sales_dept_ids)
    db_brokers = {
        int(b["responsible_id"]): b for b in list_active_brokers_from_db()
    }

    try:
        depts = _bx_get_all_sync("department.get", {})
        dept_map = {
            int(d.get("ID") or d.get("id") or 0): str(d.get("NAME") or d.get("name") or "")
            for d in (depts or [])
        }
    except Exception as exc:
        logger.warning("department.get failed: %s", exc)
        dept_map = {}

    exclude_ids = set(cfg.owner_exclude_user_ids)
    exclude_ids.update(cfg.broker_rating_exclude_user_ids)
    exclude_ids.add(int(cfg.buyer_commission_pool_user_id))
    exclude_ids.add(int(cfg.admin_user_id))

    eligible: list[dict[str, Any]] = []
    for u in users:
        uid = int(u.get("ID") or 0)
        if uid <= 0 or uid in exclude_ids:
            continue
        if not _is_active_user(u):
            continue
        if _is_rop_user(u):
            continue

        u_depts = u.get("UF_DEPARTMENT") or []
        primary_dept = int(u_depts[0]) if u_depts else 0
        dept_name = dept_map.get(primary_dept, "").strip()
        if dept_name in EXCLUDED_RATING_DEPARTMENTS:
            continue

        db_row = db_brokers.get(uid, {})
        if db_row.get("department") in EXCLUDED_RATING_DEPARTMENTS:
            continue

        name = ((u.get("NAME") or "") + " " + (u.get("LAST_NAME") or "")).strip()
        if name in cfg.contact_source_lock_exclude_names:
            continue
        if name in cfg.task_auditor_exclude_users:
            continue

        eligible.append({
            "responsible_id": uid,
            "responsible_name": name or db_row.get("responsible_name") or f"ID {uid}",
            "department": dept_name or db_row.get("department") or "",
            "department_id": primary_dept or db_row.get("department_id"),
            "lead_count": int(db_row.get("lead_count") or 0),
            "deal_count": int(db_row.get("deal_count") or 0),
        })

    eligible = [
        b for b in eligible
        if int(b.get("lead_count") or 0) > 0 or int(b.get("deal_count") or 0) > 0
    ]
    eligible.sort(key=lambda b: str(b.get("responsible_name") or ""))
    return eligible


def user_had_crm_visit(user: dict[str, Any], metric_date: str) -> bool:
    """True if LAST_ACTIVITY_DATE or LAST_LOGIN falls on metric_date."""
    for field in ("LAST_ACTIVITY_DATE", "LAST_LOGIN"):
        raw = user.get(field)
        if isinstance(raw, dict):
            raw = raw.get("date") or raw.get("value") or ""
        dt = _parse_dt(str(raw) if raw else None)
        if dt and dt.strftime("%Y-%m-%d") == metric_date:
            return True
    return False


def collect_shared_leads(since: str, now_iso: str) -> int:
    """Detect leads moved to «Общие лиды» and cache in DB."""
    count = 0
    try:
        leads = _bx_get_all_sync("crm.lead.list", {
            "filter": {
                "STATUS_ID": LEAD_STATUS_SHARED,
                ">=DATE_MODIFY": since[:10],
            },
            "select": ["ID", "ASSIGNED_BY_ID", "DATE_MODIFY", "MODIFY_BY_ID"],
        })
    except Exception as exc:
        logger.warning("crm.lead.list shared leads failed: %s", exc)
        return 0

    for lead in leads or []:
        lead_id = int(lead.get("ID") or 0)
        if lead_id <= 0:
            continue
        broker_id = int(lead.get("MODIFY_BY_ID") or lead.get("ASSIGNED_BY_ID") or 0)
        if broker_id <= 0:
            continue
        moved_at = str(lead.get("DATE_MODIFY") or now_iso)
        upsert_broker_shared_lead(lead_id, broker_id, moved_at, now_iso)
        count += 1
    return count


def fetch_broker_tasks_sync(broker_id: int) -> list[dict[str, Any]]:
    """Fetch active tasks for a broker."""
    try:
        result = _bx_get_all_sync("tasks.task.list", {
            "filter": {"RESPONSIBLE_ID": broker_id},
            "select": ["ID", "TITLE", "DEADLINE", "STATUS", "REAL_STATUS", "CREATED_DATE"],
        })
    except Exception as exc:
        logger.warning("tasks.task.list failed for broker %s: %s", broker_id, exc)
        return []

    if isinstance(result, dict):
        if "tasks" in result:
            return result["tasks"] or []
        if "task" in result:
            return result["task"] or []
    if isinstance(result, list):
        return result
    return []


def fetch_important_feed_posts(since: str, settings: Settings) -> list[dict[str, Any]]:
    """Return important feed posts with reader IDs since date."""
    posts: list[dict[str, Any]] = []
    try:
        raw = _bx_call_sync("log.blogpost.get", {"start": 0})
    except Exception as exc:
        logger.warning("log.blogpost.get failed: %s", exc)
        return posts

    items = raw if isinstance(raw, list) else (raw.get("result") if isinstance(raw, dict) else [])
    if not isinstance(items, list):
        return posts

    since_dt = _parse_dt(since) or datetime.now(timezone.utc) - timedelta(days=30)
    news_tag = (settings.broker_rating_news_tag or "").strip().lower()

    for item in items:
        post_id = int(item.get("ID") or 0)
        if post_id <= 0:
            continue
        published = _parse_dt(str(item.get("DATE_PUBLISH") or ""))
        if published and published < since_dt:
            continue

        is_important = bool(item.get("UF_BLOG_POST_IMPRTNT"))
        if not is_important and news_tag:
            title = str(item.get("TITLE") or "").lower()
            text = str(item.get("DETAIL_TEXT") or "").lower()
            if news_tag not in title and news_tag not in text:
                continue
        elif not is_important and not news_tag:
            continue

        readers: list[int] = []
        try:
            read_raw = _bx_call_sync("log.blogpost.getusers.important", {"POST_ID": post_id})
            reader_ids = read_raw if isinstance(read_raw, list) else (
                read_raw.get("result") if isinstance(read_raw, dict) else []
            )
            readers = [int(x) for x in (reader_ids or [])]
        except Exception as exc:
            logger.debug("getusers.important post %s: %s", post_id, exc)

        posts.append({"post_id": post_id, "readers": readers})

    return posts


def broker_read_important_posts(broker_id: int, posts: list[dict[str, Any]]) -> int:
    """Count important posts read by broker."""
    return sum(1 for p in posts if broker_id in (p.get("readers") or []))


async def collect_daily_metrics(settings: Settings | None = None) -> dict[str, int]:
    """Collect and persist daily metrics for all sales brokers."""
    cfg = settings or get_settings()
    init_db()
    now = datetime.now(timezone.utc)
    metric_date = now.strftime("%Y-%m-%d")
    now_iso = now.isoformat()

    users = fetch_users_by_departments(cfg.broker_rating_sales_dept_ids)
    broker_ids = {
        int(b["responsible_id"]) for b in list_eligible_brokers_for_rating(cfg)
    }

    since = (now - timedelta(days=cfg.broker_rating_period_days)).isoformat()
    collect_shared_leads(since, now_iso)
    important_posts = fetch_important_feed_posts(since, cfg)

    stats = {"brokers": 0, "crm_visits": 0, "clean_days": 0, "shared_leads": 0}
    user_map = {int(u.get("ID") or 0): u for u in users}

    for broker_id in sorted(b for b in broker_ids if b > 0):
        user = user_map.get(broker_id, {})
        had_visit = 1 if user and user_had_crm_visit(user, metric_date) else 0

        tasks = fetch_broker_tasks_sync(broker_id)
        classified = classify_tasks(tasks, now)
        overdue_today = len(classified.get("overdue") or [])

        violations_today = count_violations_on_date(broker_id, metric_date)
        is_weekday = now.weekday() < 5
        clean_day = 1 if is_weekday and violations_today == 0 else 0

        news_read = broker_read_important_posts(broker_id, important_posts)
        news_flag = 1 if news_read > 0 else 0

        upsert_broker_daily_metric(
            metric_date,
            broker_id,
            had_crm_visit=had_visit,
            clean_day=clean_day,
            violations_today=violations_today,
            news_read_today=news_flag,
            tasks_overdue_today=overdue_today,
        )
        stats["brokers"] += 1
        stats["crm_visits"] += had_visit
        stats["clean_days"] += clean_day

    return stats


def fetch_all_broker_tasks(broker_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    """Fetch tasks for multiple brokers (sync)."""
    result: dict[int, list[dict[str, Any]]] = {}
    for bid in broker_ids:
        result[bid] = fetch_broker_tasks_sync(bid)
    return result


async def async_main_daily() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    stats = await collect_daily_metrics(settings)
    logger.info("Daily broker rating metrics: %s", stats)
    print(f"Daily metrics collected: {stats}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Broker rating metric collectors")
    parser.add_argument("--daily", action="store_true", help="Collect daily metrics")
    args = parser.parse_args()
    if args.daily:
        asyncio.run(async_main_daily())
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
