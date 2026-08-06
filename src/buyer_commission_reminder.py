"""Remind brokers/ROPs to fill OPPORTUNITY (Комиссия) on buyer deals.

While the field is empty on open deals in воронка «Покупатели»:
- broker gets a personal reminder every N hours (default 2);
- their ROP every M hours (default 1).

At deadline hour (default 19:00 Europe/Moscow) deals with empty OPPORTUNITY
are reassigned to the shared pool user («общая база»).
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fast_bitrix24 import Bitrix

_SRC_DIR = Path(__file__).resolve().parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from config import Settings, get_settings, setup_logging  # noqa: E402
from db import (  # noqa: E402
    get_buyer_commission_notified_at,
    init_db,
    mark_buyer_commission_enforced,
    mark_buyer_commission_notified,
    was_buyer_commission_enforced,
)
from notify import send_user_chat_message  # noqa: E402
from tools import (  # noqa: E402
    BUYERS_STAGE_NAMES,
    _as_list,
    _build_broker_dept_map,
    _bx_get_all_sync,
    _coerce_int,
)

# Broader than audit's exact WORK_POSITION match — covers «вторички» / trailing spaces.
_ROP_POSITION_HINTS = (
    "руководитель отдела продаж",
    "роп",
)

logger = logging.getLogger(__name__)

ROLE_BROKER = "broker"
ROLE_ROP = "rop"


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


def is_opportunity_empty(value: Any) -> bool:
    """True when OPPORTUNITY is missing or zero."""
    if value is None or value == "":
        return True
    if isinstance(value, (list, dict)):
        return not value
    text = str(value).strip()
    if not text:
        return True
    amount = text.split("|", 1)[0].strip()
    try:
        return float(amount) == 0.0
    except ValueError:
        return False


def is_notify_due(
    last_notified_at: str | None,
    interval_hours: float,
    now: datetime,
) -> bool:
    """True if never notified or interval elapsed."""
    if interval_hours <= 0:
        return True
    last = _parse_dt(last_notified_at)
    if last is None:
        return True
    return (now - last) >= timedelta(hours=interval_hours)


def list_open_buyer_deals(bx: Bitrix, category_id: int) -> list[dict[str, Any]]:
    """List open deals in buyers funnel with commission-related fields."""
    items: list[dict[str, Any]] = []
    start = 0
    while True:
        raw = bx.call(
            "crm.deal.list",
            {
                "filter": {"CATEGORY_ID": category_id, "CLOSED": "N"},
                "select": [
                    "ID",
                    "TITLE",
                    "STAGE_ID",
                    "ASSIGNED_BY_ID",
                    "OPPORTUNITY",
                    "CURRENCY_ID",
                    "CATEGORY_ID",
                ],
                "start": start,
            },
            raw=True,
        )
        if not isinstance(raw, dict):
            break
        batch = _as_list(raw.get("result"))
        items.extend(batch)
        nxt = raw.get("next")
        if nxt is None:
            break
        start = int(nxt)
    return items


def select_empty_commission_deals(
    deals: list[dict[str, Any]],
    *,
    pool_user_id: int,
) -> list[dict[str, Any]]:
    """Keep open deals with empty OPPORTUNITY not already on pool user."""
    selected: list[dict[str, Any]] = []
    for deal in deals:
        deal_id = _coerce_int(deal.get("ID"))
        assigned = _coerce_int(deal.get("ASSIGNED_BY_ID"))
        if deal_id <= 0 or assigned <= 0:
            continue
        if assigned == pool_user_id:
            continue
        if not is_opportunity_empty(deal.get("OPPORTUNITY")):
            continue
        selected.append(
            {
                "id": deal_id,
                "title": str(deal.get("TITLE") or ""),
                "stage_id": str(deal.get("STAGE_ID") or ""),
                "assigned_by_id": assigned,
                "opportunity": deal.get("OPPORTUNITY"),
            }
        )
    return selected


def build_commission_rop_map() -> dict[int, int]:
    """department_id → ROP user_id via WORK_POSITION substring match."""
    try:
        users = _bx_get_all_sync("user.get", {"FILTER": {"ACTIVE": True}})
    except Exception:
        logger.exception("Failed to load users for commission ROP map")
        return {}

    rop_map: dict[int, int] = {}
    for user in _as_list(users):
        if not isinstance(user, dict):
            continue
        uid = _coerce_int(user.get("ID"))
        pos = str(user.get("WORK_POSITION") or "").strip().lower()
        if not uid or not pos:
            continue
        if not any(hint in pos for hint in _ROP_POSITION_HINTS):
            continue
        depts = user.get("UF_DEPARTMENT") or []
        if not isinstance(depts, list):
            continue
        for dept in depts:
            dept_id = _coerce_int(dept)
            if dept_id and dept_id not in rop_map:
                rop_map[dept_id] = uid
    return rop_map


def resolve_rop_for_broker(
    broker_id: int,
    broker_dept_map: dict[int, int],
    rop_map: dict[int, int],
) -> int:
    """Return ROP user id for broker's primary department, or 0."""
    dept_id = broker_dept_map.get(broker_id) or 0
    if not dept_id:
        return 0
    return int(rop_map.get(dept_id) or 0)


def deal_url(webhook_url: str, deal_id: int) -> str:
    """Build CRM deal card URL from webhook base."""
    domain = (
        webhook_url.split("/rest/")[0]
        if "/rest/" in webhook_url
        else webhook_url.rstrip("/")
    )
    return f"{domain}/crm/deal/details/{deal_id}/"


def format_deals_block(deals: list[dict[str, Any]], webhook_url: str) -> str:
    """Format deal list for chat message."""
    lines: list[str] = []
    for deal in deals:
        stage = BUYERS_STAGE_NAMES.get(deal["stage_id"], deal["stage_id"] or "—")
        title = deal["title"] or f"Сделка #{deal['id']}"
        url = deal_url(webhook_url, int(deal["id"]))
        lines.append(
            f"🔵 Сделка #{deal['id']} | {title} | этап «{stage}»\n"
            f"   {url}"
        )
    return "\n".join(lines)


def format_reminder_message(
    deals: list[dict[str, Any]],
    *,
    deadline_hour: int,
    role: str,
    webhook_url: str,
) -> str:
    """Build reminder text with 19:00 warning."""
    role_label = "брокеру" if role == ROLE_BROKER else "РОПу"
    header = (
        f"⚠️ Напоминание {role_label}: не заполнена «Комиссия» "
        f"(поле «Сумма») в сделках воронки «Покупатели».\n\n"
        f"Если не заполнить до {deadline_hour:02d}:00 сегодня — "
        f"сделка уйдет в общую базу.\n"
    )
    return header + "\n" + format_deals_block(deals, webhook_url)


def format_enforce_message(
    deals: list[dict[str, Any]],
    *,
    webhook_url: str,
) -> str:
    """Build notice after deals were moved to the shared pool."""
    header = (
        "⛔ Сделки перенесены в общую базу: не была заполнена «Комиссия» "
        "(Сумма) до дедлайна.\n"
    )
    return header + "\n" + format_deals_block(deals, webhook_url)


def load_active_user_ids() -> set[int]:
    """Return IDs of active Bitrix users (skip fired/dismissed)."""
    try:
        users = _bx_get_all_sync("user.get", {"FILTER": {"ACTIVE": True}})
    except Exception:
        logger.exception("Failed to load active users")
        return set()
    return {
        uid
        for u in _as_list(users)
        if isinstance(u, dict) and (uid := _coerce_int(u.get("ID"))) > 0
    }


def collect_due_notifications(
    deals: list[dict[str, Any]],
    *,
    broker_dept_map: dict[int, int],
    rop_map: dict[int, int],
    broker_interval_hours: float,
    rop_interval_hours: float,
    now: datetime,
    active_user_ids: set[int] | None = None,
) -> tuple[dict[int, list[dict[str, Any]]], dict[int, list[dict[str, Any]]]]:
    """Group deals due for broker/ROP reminders by recipient user id."""
    broker_groups: dict[int, list[dict[str, Any]]] = defaultdict(list)
    rop_groups: dict[int, list[dict[str, Any]]] = defaultdict(list)

    for deal in deals:
        deal_id = int(deal["id"])
        broker_id = int(deal["assigned_by_id"])

        if (
            (active_user_ids is None or broker_id in active_user_ids)
            and is_notify_due(
                get_buyer_commission_notified_at(deal_id, ROLE_BROKER),
                broker_interval_hours,
                now,
            )
        ):
            broker_groups[broker_id].append(deal)

        rop_id = resolve_rop_for_broker(broker_id, broker_dept_map, rop_map)
        if (
            rop_id > 0
            and rop_id != broker_id
            and (active_user_ids is None or rop_id in active_user_ids)
            and is_notify_due(
                get_buyer_commission_notified_at(deal_id, ROLE_ROP),
                rop_interval_hours,
                now,
            )
        ):
            rop_groups[rop_id].append(deal)

    return broker_groups, rop_groups


def _send_digest(
    user_id: int,
    message: str,
    *,
    dry_run: bool,
) -> int:
    if dry_run:
        logger.info(
            "DRY_RUN: would notify user_id=%s chars=%s",
            user_id,
            len(message),
        )
        return 0
    return send_user_chat_message(user_id, message, system=True)


def send_reminder_digests(
    broker_groups: dict[int, list[dict[str, Any]]],
    rop_groups: dict[int, list[dict[str, Any]]],
    settings: Settings,
    now: datetime,
) -> dict[str, int]:
    """Send digests and persist notification timestamps."""
    sent_broker = 0
    sent_rop = 0
    now_iso = now.astimezone(timezone.utc).isoformat()
    deadline = int(settings.buyer_commission_deadline_hour)

    for user_id, deals in sorted(broker_groups.items()):
        msg = format_reminder_message(
            deals,
            deadline_hour=deadline,
            role=ROLE_BROKER,
            webhook_url=settings.b24_webhook_url,
        )
        try:
            _send_digest(user_id, msg, dry_run=settings.dry_run)
            if not settings.dry_run:
                for deal in deals:
                    mark_buyer_commission_notified(
                        int(deal["id"]), ROLE_BROKER, user_id, now_iso
                    )
            sent_broker += 1
        except Exception:
            logger.exception("Broker commission reminder failed: user_id=%s", user_id)

    for user_id, deals in sorted(rop_groups.items()):
        msg = format_reminder_message(
            deals,
            deadline_hour=deadline,
            role=ROLE_ROP,
            webhook_url=settings.b24_webhook_url,
        )
        try:
            _send_digest(user_id, msg, dry_run=settings.dry_run)
            if not settings.dry_run:
                for deal in deals:
                    mark_buyer_commission_notified(
                        int(deal["id"]), ROLE_ROP, user_id, now_iso
                    )
            sent_rop += 1
        except Exception:
            logger.exception("ROP commission reminder failed: user_id=%s", user_id)

    return {
        "broker_digests": sent_broker,
        "rop_digests": sent_rop,
        "broker_deals": sum(len(v) for v in broker_groups.values()),
        "rop_deals": sum(len(v) for v in rop_groups.values()),
    }


def reassign_deal_to_pool(
    bx: Bitrix,
    deal_id: int,
    pool_user_id: int,
    *,
    dry_run: bool,
) -> bool:
    """Set ASSIGNED_BY_ID to pool user. Returns True on success / dry-run."""
    if dry_run:
        logger.info(
            "DRY_RUN: would reassign deal_id=%s → pool_user_id=%s",
            deal_id,
            pool_user_id,
        )
        return True
    bx.call(
        "crm.deal.update",
        {"id": deal_id, "fields": {"ASSIGNED_BY_ID": pool_user_id}},
    )
    return True


def run_enforce(
    settings: Settings,
    deals: list[dict[str, Any]],
    *,
    broker_dept_map: dict[int, int],
    rop_map: dict[int, int],
    bx: Bitrix,
    now_local: datetime,
    active_user_ids: set[int] | None = None,
) -> dict[str, Any]:
    """Move empty-commission deals to pool and notify affected users."""
    if not settings.buyer_commission_enforce_enabled:
        logger.info("BUYER_COMMISSION_ENFORCE_ENABLED=false — skip enforce")
        return {"enforced": 0, "skipped": True}

    pool_user_id = int(settings.buyer_commission_pool_user_id)
    enforce_date = now_local.date().isoformat()
    now_iso = now_local.astimezone(timezone.utc).isoformat()

    to_move = [
        d for d in deals
        if not was_buyer_commission_enforced(int(d["id"]), enforce_date)
    ]
    moved: list[dict[str, Any]] = []
    errors = 0

    for deal in to_move:
        deal_id = int(deal["id"])
        prev = int(deal["assigned_by_id"])
        try:
            ok = reassign_deal_to_pool(
                bx, deal_id, pool_user_id, dry_run=settings.dry_run
            )
            if not ok:
                errors += 1
                continue
            if not settings.dry_run:
                mark_buyer_commission_enforced(
                    deal_id, enforce_date, prev, pool_user_id, now_iso
                )
            moved.append(deal)
        except Exception:
            errors += 1
            logger.exception("Failed to reassign deal_id=%s to pool", deal_id)

    notify_groups: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for deal in moved:
        broker_id = int(deal["assigned_by_id"])
        if active_user_ids is None or broker_id in active_user_ids:
            notify_groups[broker_id].append(deal)
        rop_id = resolve_rop_for_broker(broker_id, broker_dept_map, rop_map)
        if rop_id > 0 and (active_user_ids is None or rop_id in active_user_ids):
            notify_groups[rop_id].append(deal)

    digests = 0
    for user_id, user_deals in sorted(notify_groups.items()):
        # Deduplicate deals if user is both broker and ROP somehow.
        uniq: dict[int, dict[str, Any]] = {int(d["id"]): d for d in user_deals}
        msg = format_enforce_message(
            list(uniq.values()),
            webhook_url=settings.b24_webhook_url,
        )
        try:
            _send_digest(user_id, msg, dry_run=settings.dry_run)
            digests += 1
        except Exception:
            logger.exception("Enforce notify failed: user_id=%s", user_id)

    return {
        "enforced": len(moved),
        "errors": errors,
        "notify_digests": digests,
        "enforce_date": enforce_date,
        "pool_user_id": pool_user_id,
        "dry_run": settings.dry_run,
    }


def run_reminders(
    settings: Settings,
    deals: list[dict[str, Any]],
    *,
    broker_dept_map: dict[int, int],
    rop_map: dict[int, int],
    now: datetime,
    active_user_ids: set[int] | None = None,
) -> dict[str, Any]:
    """Send due broker/ROP digests for empty-commission deals."""
    broker_groups, rop_groups = collect_due_notifications(
        deals,
        broker_dept_map=broker_dept_map,
        rop_map=rop_map,
        broker_interval_hours=float(settings.buyer_commission_broker_interval_hours),
        rop_interval_hours=float(settings.buyer_commission_rop_interval_hours),
        now=now,
        active_user_ids=active_user_ids,
    )
    stats = send_reminder_digests(broker_groups, rop_groups, settings, now)
    return {"empty_deals": len(deals), **stats, "dry_run": settings.dry_run}


def run(
    settings: Settings | None = None,
    *,
    force_remind: bool = False,
    force_enforce: bool = False,
) -> dict[str, Any]:
    """Entry point: reminders in daytime window, enforce at deadline hour."""
    settings = settings or get_settings()
    setup_logging(settings.log_level)
    init_db()

    if not settings.buyer_commission_reminder_enabled:
        logger.info("BUYER_COMMISSION_REMINDER_ENABLED=false — skip")
        return {"skipped": True}

    tz = ZoneInfo(settings.buyer_commission_timezone)
    now_local = datetime.now(tz)
    now_utc = now_local.astimezone(timezone.utc)
    start_h = int(settings.buyer_commission_remind_start_hour)
    deadline_h = int(settings.buyer_commission_deadline_hour)
    hour = now_local.hour

    in_remind_window = start_h <= hour < deadline_h
    at_deadline = hour == deadline_h
    do_remind = force_remind or in_remind_window or (force_enforce and in_remind_window)
    # At deadline hour: enforce; also allow --enforce any time.
    do_enforce = force_enforce or at_deadline

    if not do_remind and not do_enforce:
        logger.info(
            "Outside remind/enforce window (local=%s start=%s deadline=%s) — skip",
            now_local.isoformat(),
            start_h,
            deadline_h,
        )
        return {
            "skipped": True,
            "reason": "outside_window",
            "local_time": now_local.isoformat(),
        }

    category_id = int(settings.buyers_category_id or 18)
    pool_user_id = int(settings.buyer_commission_pool_user_id)
    bx = Bitrix(settings.b24_webhook_url)

    raw_deals = list_open_buyer_deals(bx, category_id)
    empty = select_empty_commission_deals(raw_deals, pool_user_id=pool_user_id)
    broker_ids = {int(d["assigned_by_id"]) for d in empty}
    broker_dept_map = _build_broker_dept_map(broker_ids)
    rop_map = build_commission_rop_map()
    active_user_ids = load_active_user_ids()
    logger.info(
        "Commission ROP map: %s departments; active_users=%s",
        len(rop_map),
        len(active_user_ids),
    )

    logger.info(
        "Buyer commission: open=%s empty=%s local=%s remind=%s enforce=%s dry_run=%s",
        len(raw_deals),
        len(empty),
        now_local.isoformat(),
        do_remind,
        do_enforce,
        settings.dry_run,
    )

    result: dict[str, Any] = {
        "local_time": now_local.isoformat(),
        "open_deals": len(raw_deals),
        "empty_deals": len(empty),
        "pool_user_id": pool_user_id,
    }

    if do_remind and empty:
        result["reminders"] = run_reminders(
            settings,
            empty,
            broker_dept_map=broker_dept_map,
            rop_map=rop_map,
            now=now_utc,
            active_user_ids=active_user_ids,
        )
    elif do_remind:
        result["reminders"] = {"empty_deals": 0, "broker_digests": 0, "rop_digests": 0}

    if do_enforce:
        result["enforce"] = run_enforce(
            settings,
            empty,
            broker_dept_map=broker_dept_map,
            rop_map=rop_map,
            bx=bx,
            now_local=now_local,
            active_user_ids=active_user_ids,
        )

    logger.info("Buyer commission finished: %s", result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Buyer commission (OPPORTUNITY) reminders and pool enforce",
    )
    parser.add_argument(
        "--remind",
        action="store_true",
        help="Force reminder pass (ignore daytime window)",
    )
    parser.add_argument(
        "--enforce",
        action="store_true",
        help="Force move empty-commission deals to pool user",
    )
    args = parser.parse_args()
    run(force_remind=args.remind, force_enforce=args.enforce)


if __name__ == "__main__":
    main()
