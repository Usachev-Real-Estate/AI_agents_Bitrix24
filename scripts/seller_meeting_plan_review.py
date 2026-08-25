"""One-off: sellers NEW-stage deals without an action plan.

A deal is OK if it has a broker/ROP comment with a next-step plan
or a live (not overdue) activity whose subject/description is a real plan.
Plain «связаться с клиентом» / call / write-back is a violation.

Sends the list to CONTACT_SOURCE_LOCK_NOTIFY_USER (personal chat), not ROP chats.

Usage:
  DRY_RUN=true  .venv/bin/python scripts/seller_meeting_plan_review.py
  DRY_RUN=false .venv/bin/python scripts/seller_meeting_plan_review.py
"""

from __future__ import annotations

import logging
import re
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from config import get_settings, setup_logging  # noqa: E402
from notify import send_user_chat_message_chunked  # noqa: E402
from tools import (  # noqa: E402
    MAX_TIMELINE_WORKERS,
    SELLER_STAGE_NEW,
    _as_list,
    _build_broker_dept_map,
    _build_crm_link,
    _build_rop_map,
    _bx_get_all_sync,
    _coerce_int,
    _clean_str,
    _fetch_deal_activities,
    _fetch_entity_timeline,
    _open_activity_due_datetime,
    _strip_lead_markup,
    _allowed_comment_authors,
)

logger = logging.getLogger(__name__)
MSK = ZoneInfo("Europe/Moscow")

PLAN_RE = re.compile(
    r"встреч|показ|договор|документ|оценк|выезд|замер|"
    r"фотограф|фотосъем|сделать фото|"
    r"реклам|подпис|"
    r"\bключ(и|ей|а|ом|ами)?\b|"
    r"задаток|эксклюзив|офер|осмотр|назнач|"
    r"приед|приеду|выеха|подготов|"
    r"отправить|отправлю|отправим|"
    r"соглас|презентац|коммерческ|\bкп\b|бронь|эксклюз|"
    r"подборк|выставить|пробив|обсуд",
    re.I,
)

# Past/refusal mentions of a meeting are not a next-step plan.
NOT_A_PLAN_RE = re.compile(
    r"отказал\w*\s+\w*\s*встреч|не готов\w*\s+к\s+встреч|"
    r"был[аи]?\s+на\s+встреч|не договорил",
    re.I,
)

GENERIC_CONTACT_RE = re.compile(
    r"связ\w*\s+с\s+(клиент|покупател|собственник)|"
    r"\bсвязаться\b|\bсозвон|\bпозвон|\bперезвон|"
    r"написать(\s+(клиенту|собственнику|ему|ей))?|"
    r"контакт(\s+с\s+клиентом)?|"
    r"входящ\w*\s+звон|исходящ\w*\s+звон|"
    r"^звонок$|^call$|^связ(аться|ываться)$",
    re.I,
)


def _norm(text: str) -> str:
    return _strip_lead_markup(text).lower().strip()


def has_action_plan(text: str) -> bool:
    """True when text names a next step beyond generic contact."""
    cleaned = _norm(text)
    if not cleaned:
        return False
    if not PLAN_RE.search(cleaned):
        return False
    # A refusal/past meeting alone is not a further-action plan.
    remainder = NOT_A_PLAN_RE.sub(" ", cleaned)
    return bool(PLAN_RE.search(remainder))


def is_generic_contact(text: str) -> bool:
    cleaned = _norm(text)
    if not cleaned:
        return False
    if has_action_plan(cleaned):
        return False
    return bool(GENERIC_CONTACT_RE.search(cleaned))


def _is_live_activity(activity: dict[str, Any], now: datetime) -> bool:
    if str(activity.get("COMPLETED") or "N").upper() == "Y":
        return False
    due = _open_activity_due_datetime(activity)
    if due is None:
        return True
    if due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)
    return due > now


def _activity_blob(activity: dict[str, Any]) -> str:
    return f"{activity.get('SUBJECT') or ''} {activity.get('DESCRIPTION') or ''}"


def _preview(text: str, limit: int = 120) -> str:
    cleaned = _strip_lead_markup(text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1] + "…"


def _user_name(user: dict[str, Any]) -> str:
    first = _clean_str(user.get("NAME"))
    last = _clean_str(user.get("LAST_NAME"))
    full = f"{first} {last}".strip()
    return full or f"ID:{user.get('ID')}"


def classify_deal(
    deal: dict[str, Any],
    timeline: list[dict[str, Any]],
    activities: list[dict[str, Any]],
    allowed_authors: set[int],
    now: datetime,
) -> dict[str, Any]:
    plan_comments: list[str] = []
    generic_comments: list[str] = []
    other_comments: list[str] = []
    for item in timeline:
        if not isinstance(item, dict):
            continue
        if allowed_authors and _coerce_int(item.get("author_id")) not in allowed_authors:
            continue
        text = str(item.get("comment") or "")
        if not _norm(text):
            continue
        if has_action_plan(text):
            plan_comments.append(_preview(text))
        elif is_generic_contact(text):
            generic_comments.append(_preview(text))
        else:
            other_comments.append(_preview(text))

    plan_acts: list[str] = []
    generic_acts: list[str] = []
    other_live_acts: list[str] = []
    overdue_or_done = 0
    for activity in activities:
        if not isinstance(activity, dict):
            continue
        blob = _activity_blob(activity)
        preview = _preview(str(activity.get("SUBJECT") or blob))
        if not _is_live_activity(activity, now):
            overdue_or_done += 1
            continue
        if has_action_plan(blob):
            plan_acts.append(preview)
        elif is_generic_contact(blob) or not _norm(blob):
            generic_acts.append(preview or "(пустое дело)")
        else:
            other_live_acts.append(preview)

    ok = bool(plan_comments or plan_acts)
    if ok:
        why = ""
    elif generic_comments or generic_acts:
        bits = []
        if generic_acts:
            bits.append("дело «" + (generic_acts[0] or "связаться") + "»")
        if generic_comments:
            bits.append("комментарий без плана")
        why = "только «связаться с клиентом» / звонок, нет плана дальнейших действий"
        if bits:
            why += f" ({'; '.join(bits)})"
    elif other_comments or other_live_acts:
        sample = (other_comments or other_live_acts)[0]
        why = f"есть касание, но нет плана дальнейших действий: «{sample}»"
    elif overdue_or_done:
        why = (
            "нет живого дела с планом и нет комментария с планом "
            "(есть только закрытые/просроченные дела)"
        )
    else:
        why = "нет комментария с планом и нет запланированного дела"

    return {
        "ok": ok,
        "why": why,
        "plan_comments": plan_comments,
        "plan_acts": plan_acts,
        "generic_acts": generic_acts,
        "generic_comments": generic_comments,
        "other_comments": other_comments,
        "other_live_acts": other_live_acts,
    }


def fetch_new_seller_deals() -> list[dict[str, Any]]:
    settings = get_settings()
    raw = _bx_get_all_sync(
        "crm.deal.list",
        {
            "filter": {
                "CATEGORY_ID": settings.sellers_category_id,
                "STAGE_ID": SELLER_STAGE_NEW,
                "CLOSED": "N",
            },
            "select": [
                "ID",
                "TITLE",
                "STAGE_ID",
                "ASSIGNED_BY_ID",
                "DATE_CREATE",
            ],
        },
    )
    deals: list[dict[str, Any]] = []
    for item in raw if isinstance(raw, list) else _as_list(raw):
        if not isinstance(item, dict):
            continue
        deal_id = _coerce_int(item.get("ID"))
        if deal_id <= 0:
            continue
        deals.append(
            {
                "deal_id": deal_id,
                "title": _clean_str(item.get("TITLE")),
                "assigned_by_id": _coerce_int(item.get("ASSIGNED_BY_ID")),
                "date_create": _clean_str(item.get("DATE_CREATE")),
            }
        )
    deals.sort(key=lambda d: d["deal_id"])
    return deals


def load_user_names(user_ids: set[int]) -> dict[int, str]:
    names: dict[int, str] = {}
    raw = _bx_get_all_sync("user.get", {})
    for user in raw if isinstance(raw, list) else _as_list(raw):
        if not isinstance(user, dict):
            continue
        uid = _coerce_int(user.get("ID"))
        if uid in user_ids:
            names[uid] = _user_name(user)
    return names


def build_report(
    rows: list[dict[str, Any]],
    total: int,
    now: datetime,
) -> str:
    by_user: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_user[row["responsible_name"]].append(row)

    lines = [
        "Продавцы → «Назначение встречи»: нет плана дальнейших действий",
        f"Дата: {now.astimezone(MSK).strftime('%Y-%m-%d %H:%M')} МСК",
        "",
        "Нарушение: нет комментария с планом и нет живого дела с дальнейшими действиями.",
        "Просто «связаться с клиентом» / звонок / написать — тоже нарушение.",
        f"Сделок на этапе: {total}. Без плана: {len(rows)}.",
        "",
    ]
    for name in sorted(by_user):
        items = by_user[name]
        lines.append(f"{name} — {len(items)}")
        for row in items:
            link = _build_crm_link("deal", row["deal_id"])
            title = row["title"] or ""
            lines.append(f"• Сделка #{row['deal_id']} {title}")
            lines.append(link)
            lines.append(f"  {row['why']}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def main() -> int:
    settings = get_settings()
    setup_logging(settings.log_level)
    now = datetime.now(timezone.utc)

    deals = fetch_new_seller_deals()
    logger.info("NEW seller deals: %d", len(deals))
    if not deals:
        text = (
            "Продавцы → «Назначение встречи»: сделок на этапе нет.\n"
            f"Дата: {now.astimezone(MSK).strftime('%Y-%m-%d %H:%M')} МСК\n"
        )
        recipient = settings.contact_source_lock_notify_user
        if settings.dry_run:
            print(text)
            logger.info("DRY_RUN: skip send to user %s", recipient)
            return 0
        send_user_chat_message_chunked(recipient, text)
        return 0

    timelines: dict[int, list[dict[str, Any]]] = {}
    activities: dict[int, list[dict[str, Any]]] = {}
    deal_ids = [d["deal_id"] for d in deals]
    with ThreadPoolExecutor(max_workers=MAX_TIMELINE_WORKERS) as pool:
        comment_futs = {
            pool.submit(_fetch_entity_timeline, did, "deal"): did for did in deal_ids
        }
        act_futs = {
            pool.submit(_fetch_deal_activities, did): did for did in deal_ids
        }
        for future in as_completed(comment_futs):
            did = comment_futs[future]
            try:
                _, comments, failed = future.result()
                timelines[did] = (
                    [] if failed or not isinstance(comments, list) else comments
                )
            except Exception:
                logger.exception("timeline failed deal=%s", did)
                timelines[did] = []
        for future in as_completed(act_futs):
            did = act_futs[future]
            try:
                _, acts, failed = future.result()
                activities[did] = (
                    [] if failed else [a for a in acts if isinstance(a, dict)]
                )
            except Exception:
                logger.exception("activities failed deal=%s", did)
                activities[did] = []

    assigned_ids = {
        d["assigned_by_id"] for d in deals if d["assigned_by_id"] > 0
    }
    rop_map = _build_rop_map()
    broker_dept_map = _build_broker_dept_map(assigned_ids)
    names = load_user_names(assigned_ids)

    violations: list[dict[str, Any]] = []
    ok_count = 0
    for deal in deals:
        did = deal["deal_id"]
        assigned = deal["assigned_by_id"]
        allowed = _allowed_comment_authors(assigned, broker_dept_map, rop_map)
        result = classify_deal(
            deal,
            timelines.get(did, []),
            activities.get(did, []),
            allowed,
            now,
        )
        if result["ok"]:
            ok_count += 1
            logger.info(
                "OK deal=%s plan_comments=%s plan_acts=%s",
                did,
                result["plan_comments"][:1],
                result["plan_acts"][:1],
            )
            continue
        violations.append(
            {
                "deal_id": did,
                "title": deal["title"],
                "assigned_by_id": assigned,
                "responsible_name": names.get(assigned, f"ID:{assigned}"),
                "why": result["why"],
            }
        )

    report = build_report(violations, len(deals), now)
    print(report)
    logger.info(
        "Meeting plan review: total=%d ok=%d violations=%d dry_run=%s",
        len(deals),
        ok_count,
        len(violations),
        settings.dry_run,
    )

    recipient = settings.contact_source_lock_notify_user
    if settings.dry_run:
        logger.info("DRY_RUN: skip personal send to user %s", recipient)
        return 0
    chunks = send_user_chat_message_chunked(recipient, report)
    logger.info("Sent meeting-plan review to user %s chunks=%s", recipient, chunks)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
