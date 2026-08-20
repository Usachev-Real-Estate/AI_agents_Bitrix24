"""One-off: remind ROP chats about buyers show-stage deals without active plan.

Open deals on «Первый показ» / «Повторный показ» without a non-overdue
activity (показ / встреча с клиентом / связаться с клиентом) → department
chats with ❗❗❗ and ROP mention (except Vera Volkova).

Usage:
  DRY_RUN=true  .venv/bin/python scripts/send_show_plan_reminders.py
  DRY_RUN=false .venv/bin/python scripts/send_show_plan_reminders.py
"""

from __future__ import annotations

import logging
import re
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from config import Settings, get_settings, setup_logging  # noqa: E402
from notify import send_chat_message_chunked  # noqa: E402
from tools import (  # noqa: E402
    _as_list,
    _build_crm_link,
    _bx_get_all_sync,
    _coerce_int,
)

logger = logging.getLogger(__name__)

MSK = ZoneInfo("Europe/Moscow")

STAGE_FIRST = "C18:UC_UFPFKK"
STAGE_REPEAT = "C18:UC_DVW1P9"
STAGE_NAMES = {
    STAGE_FIRST: "Первый показ",
    STAGE_REPEAT: "Повторный показ",
}

_PLAN_RE = re.compile(
    r"(показ|встреч\w*\s+с\s+клиент|встреч\w*\s+с\s+покупател|"
    r"связ\w*\s+с\s+клиент|связаться|"
    r"meeting|showing|viewing)",
    re.I,
)

# Do not @mention this ROP (Вера Волкова / dept 50)
SKIP_ROP_MENTION_DEPT_IDS = frozenset({50})
SKIP_ROP_MENTION_NAMES = frozenset({"вера волкова"})


def _parse_dt(val: Any) -> datetime | None:
    if not val:
        return None
    s = str(val).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=MSK)
    return dt.astimezone(MSK)


def _due_of(act: dict[str, Any]) -> datetime | None:
    return (
        _parse_dt(act.get("DEADLINE"))
        or _parse_dt(act.get("END_TIME"))
        or _parse_dt(act.get("START_TIME"))
    )


def _is_plan_activity(act: dict[str, Any]) -> bool:
    subject = str(act.get("SUBJECT") or "")
    if _PLAN_RE.search(subject):
        return True
    return str(act.get("TYPE_ID") or "") == "1"


def _is_active_plan(act: dict[str, Any], now: datetime) -> bool:
    if not _is_plan_activity(act):
        return False
    due = _due_of(act)
    if due is None:
        return True
    return due >= now


def _fetch_open_acts(deal_id: int) -> list[dict[str, Any]]:
    raw = _bx_get_all_sync(
        "crm.activity.list",
        {
            "filter": {
                "OWNER_TYPE_ID": 2,
                "OWNER_ID": deal_id,
                "COMPLETED": "N",
            },
            "select": [
                "ID",
                "SUBJECT",
                "COMPLETED",
                "DEADLINE",
                "START_TIME",
                "END_TIME",
                "TYPE_ID",
            ],
        },
    )
    items = raw if isinstance(raw, list) else _as_list(raw)
    return [a for a in items if isinstance(a, dict)]


def _load_users() -> dict[int, dict[str, Any]]:
    users = _bx_get_all_sync("user.get", {"filter": {"ACTIVE": True}})
    out: dict[int, dict[str, Any]] = {}
    for u in users if isinstance(users, list) else _as_list(users):
        if not isinstance(u, dict):
            continue
        uid = _coerce_int(u.get("ID"))
        if uid:
            out[uid] = u
    return out


def _user_name(user: dict[str, Any] | None, fallback: str = "") -> str:
    if not user:
        return fallback or "—"
    return (
        f"{user.get('NAME') or ''} {user.get('LAST_NAME') or ''}".strip()
        or fallback
        or "—"
    )


def _primary_dept(user: dict[str, Any] | None) -> int:
    if not user:
        return 0
    depts = user.get("UF_DEPARTMENT") or []
    if isinstance(depts, list) and depts:
        return _coerce_int(depts[0])
    return 0


def _load_dept_names() -> dict[int, str]:
    raw = _bx_get_all_sync("department.get", {})
    out: dict[int, str] = {}
    for d in raw if isinstance(raw, list) else _as_list(raw):
        if isinstance(d, dict):
            out[_coerce_int(d.get("ID"))] = str(d.get("NAME") or "")
    return out


def _build_rop_by_dept(users: dict[int, dict[str, Any]]) -> dict[int, int]:
    """department_id → ROP user_id (broader than tools._build_rop_map)."""
    rop_map: dict[int, int] = {}
    for uid, u in users.items():
        pos = str(u.get("WORK_POSITION") or "")
        if "РОП" not in pos and "Руководитель отдела продаж" not in pos:
            continue
        depts = u.get("UF_DEPARTMENT") or []
        if not isinstance(depts, list) or not depts:
            continue
        dept_id = _coerce_int(depts[0])
        if dept_id and dept_id not in rop_map:
            rop_map[dept_id] = uid
    return rop_map


def collect_deals_without_active_plan(
    now: datetime,
) -> list[dict[str, Any]]:
    """Return deal records missing a non-overdue show/meeting/contact plan."""
    deals = _bx_get_all_sync(
        "crm.deal.list",
        {
            "filter": {
                "CATEGORY_ID": 18,
                "CLOSED": "N",
                "STAGE_ID": [STAGE_FIRST, STAGE_REPEAT],
            },
            "select": ["ID", "TITLE", "STAGE_ID", "ASSIGNED_BY_ID"],
        },
    )
    if not isinstance(deals, list):
        deals = _as_list(deals)

    deal_map = {
        _coerce_int(d["ID"]): d
        for d in deals
        if isinstance(d, dict) and _coerce_int(d.get("ID"))
    }

    missing: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=10) as pool:
        futs = {pool.submit(_fetch_open_acts, did): did for did in deal_map}
        for fut in as_completed(futs):
            did = futs[fut]
            deal = deal_map[did]
            try:
                acts = fut.result()
            except Exception:
                logger.exception("activity.list failed deal_id=%s", did)
                acts = []
            active = [a for a in acts if _is_active_plan(a, now)]
            if active:
                continue
            overdue_plans = [
                a for a in acts if _is_plan_activity(a) and not _is_active_plan(a, now)
            ]
            reason = (
                "только просроченное дело — запланируйте новое"
                if overdue_plans
                else "нет запланированного дела"
            )
            missing.append(
                {
                    "deal_id": did,
                    "title": str(deal.get("TITLE") or "").strip() or "—",
                    "stage_id": str(deal.get("STAGE_ID") or ""),
                    "stage_name": STAGE_NAMES.get(
                        str(deal.get("STAGE_ID") or ""),
                        str(deal.get("STAGE_ID") or "—"),
                    ),
                    "assigned_by_id": _coerce_int(deal.get("ASSIGNED_BY_ID")),
                    "reason": reason,
                    "has_overdue_plan": bool(overdue_plans),
                }
            )

    missing.sort(key=lambda r: (r["stage_name"], r["assigned_by_id"], r["deal_id"]))
    return missing


def _should_mention_rop(dept_id: int, rop_user: dict[str, Any] | None) -> bool:
    if dept_id in SKIP_ROP_MENTION_DEPT_IDS:
        return False
    name = _user_name(rop_user).lower()
    if name in SKIP_ROP_MENTION_NAMES:
        return False
    return rop_user is not None


def format_dept_report(
    *,
    dept_name: str,
    deals: list[dict[str, Any]],
    users: dict[int, dict[str, Any]],
    rop_id: int,
    now: datetime,
) -> str:
    """Build BBCode report for one department chat."""
    lines = [
        "❗❗❗ Напоминание: Покупатели — нет актуального плана дела",
    ]
    rop_user = users.get(rop_id) if rop_id else None
    sample_uid = deals[0]["assigned_by_id"] if deals else 0
    sample_dept = _primary_dept(users.get(sample_uid))
    if rop_id and rop_user and _should_mention_rop(sample_dept, rop_user):
        rop_name = _user_name(rop_user, f"ID {rop_id}")
        lines.append(f"[USER={rop_id}]{rop_name}[/USER]")

    lines.extend(
        [
            f"Отдел: {dept_name}",
            f"Дата: {now.strftime('%Y-%m-%d %H:%M')} МСК",
            f"Сделок: {len(deals)}",
            "Нужно запланировать дело: показ / встреча с клиентом / связаться с клиентом.",
            "",
        ]
    )

    for row in deals:
        broker = _user_name(
            users.get(row["assigned_by_id"]),
            f"ID {row['assigned_by_id']}",
        )
        lines.append(
            f"❗❗❗ Сделка #{row['deal_id']} | {broker} | "
            f"{row['stage_name']} | {row['reason']}"
        )
        lines.append("   → Запланируйте дело в карточке сделки")
        lines.append(f"   {_build_crm_link('deal', row['deal_id'])}")

    return "\n".join(lines) + "\n"


def group_by_dept_chat(
    deals: list[dict[str, Any]],
    users: dict[int, dict[str, Any]],
    dept_names: dict[int, str],
    dept_chat_map: dict[int, int],
    rop_by_dept: dict[int, int],
    report_chat_id: int,
) -> list[tuple[int, str, int, list[dict[str, Any]]]]:
    """Return list of (chat_id, dept_name, rop_id, deals). Skip summary chat."""
    by_dept: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in deals:
        uid = row["assigned_by_id"]
        dept_id = _primary_dept(users.get(uid))
        if not dept_id:
            logger.warning("No department for user_id=%s deal=%s", uid, row["deal_id"])
            continue
        by_dept[dept_id].append(row)

    groups: list[tuple[int, str, int, list[dict[str, Any]]]] = []
    for dept_id, rows in sorted(by_dept.items(), key=lambda x: dept_names.get(x[0], str(x[0]))):
        chat_id = dept_chat_map.get(dept_id)
        if not chat_id:
            logger.info(
                "Skip dept_id=%s (%s): no chat mapping (%d deals)",
                dept_id,
                dept_names.get(dept_id, "?"),
                len(rows),
            )
            continue
        if chat_id == report_chat_id:
            logger.info(
                "Skip dept_id=%s: mapped to summary chat %s",
                dept_id,
                report_chat_id,
            )
            continue
        dept_name = dept_names.get(dept_id) or f"Отдел {dept_id}"
        rop_id = int(rop_by_dept.get(dept_id) or 0)
        rows_sorted = sorted(
            rows,
            key=lambda r: (
                r["stage_name"],
                _user_name(users.get(r["assigned_by_id"])),
                r["deal_id"],
            ),
        )
        groups.append((chat_id, dept_name, rop_id, rows_sorted))
    return groups


def run(settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    now = datetime.now(MSK)
    logger.info(
        "Show-plan reminders: DRY_RUN=%s now=%s",
        settings.dry_run,
        now.isoformat(),
    )

    deals = collect_deals_without_active_plan(now)
    users = _load_users()
    dept_names = _load_dept_names()
    rop_by_dept = _build_rop_by_dept(users)
    groups = group_by_dept_chat(
        deals,
        users,
        dept_names,
        settings.dept_chat_map,
        rop_by_dept,
        settings.report_chat_id,
    )

    sent = 0
    previews: list[str] = []
    for chat_id, dept_name, rop_id, rows in groups:
        report = format_dept_report(
            dept_name=dept_name,
            deals=rows,
            users=users,
            rop_id=rop_id,
            now=now,
        )
        previews.append(
            f"=== chat={chat_id} dept={dept_name} rop={rop_id} deals={len(rows)} ===\n"
            f"{report}"
        )
        if settings.dry_run:
            logger.info(
                "DRY_RUN: would send to chat %s (%s), %d deals, %d chars",
                chat_id,
                dept_name,
                len(rows),
                len(report),
            )
            print(previews[-1])
            continue
        try:
            chunks = send_chat_message_chunked(chat_id, report)
            sent += 1
            logger.info(
                "Sent show-plan reminder chat=%s dept=%s deals=%d chunks=%d",
                chat_id,
                dept_name,
                len(rows),
                chunks,
            )
        except Exception:
            logger.exception(
                "Failed to send show-plan reminder chat=%s dept=%s",
                chat_id,
                dept_name,
            )

    result = {
        "status": "dry_run" if settings.dry_run else "ok",
        "deals_total": len(deals),
        "departments": len(groups),
        "sent": sent,
        "dry_run": settings.dry_run,
    }
    logger.info("Show-plan reminders done: %s", result)
    print(result)
    return result


def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    run(settings)


if __name__ == "__main__":
    main()
