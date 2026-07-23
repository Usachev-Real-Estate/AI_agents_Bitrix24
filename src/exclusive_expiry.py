"""Exclusive smart-process expiry reminders (7 days before end date)."""

from __future__ import annotations

import logging
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import httpx

# Ensure src/ is on sys.path when running as script
_SRC_DIR = Path(__file__).resolve().parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from config import Settings, get_settings, setup_logging  # noqa: E402
from db import mark_exclusive_expiry_notified, was_exclusive_expiry_notified  # noqa: E402
from notify import send_user_chat_message  # noqa: E402

logger = logging.getLogger(__name__)


def _as_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("items", "result", "types"):
            inner = payload.get(key)
            if isinstance(inner, list):
                return [item for item in inner if isinstance(item, dict)]
            if isinstance(inner, dict) and key == "result":
                nested = inner.get("items")
                if isinstance(nested, list):
                    return [item for item in nested if isinstance(item, dict)]
    return []


def _coerce_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _rest_call(
    method: str,
    params: dict[str, Any] | None = None,
    *,
    webhook_url: str | None = None,
) -> dict[str, Any]:
    """Call Bitrix REST preserving camelCase params (needed for crm.item.*)."""
    webhook = (webhook_url or get_settings().b24_webhook_url).rstrip("/") + "/"
    response = httpx.post(webhook + method, json=params or {}, timeout=60.0)
    response.raise_for_status()
    data = response.json()
    if data.get("error"):
        raise RuntimeError(
            f"{method} failed: {data.get('error')} {data.get('error_description')}"
        )
    return data


def resolve_notify_recipients(
    assigned_by_id: int,
    extra_user_ids: list[int],
) -> list[int]:
    """Responsible + fixed watchers, unique, positive IDs only."""
    recipients: list[int] = []
    seen: set[int] = set()
    for uid in [assigned_by_id, *extra_user_ids]:
        user_id = _coerce_int(uid)
        if user_id <= 0 or user_id in seen:
            continue
        seen.add(user_id)
        recipients.append(user_id)
    return recipients


def verify_notify_sender(settings: Settings) -> int:
    """Return webhook owner id; warn if it is not EXCLUSIVE_NOTIFY_FROM_USER_ID."""
    data = _rest_call("profile", webhook_url=settings.exclusive_notify_webhook)
    result = data.get("result") or {}
    owner_id = _coerce_int(
        result.get("ID") or result.get("id") if isinstance(result, dict) else 0
    )
    expected = settings.exclusive_notify_from_user_id
    if owner_id and owner_id != expected:
        logger.warning(
            "Exclusive notify webhook belongs to user_id=%s, but "
            "EXCLUSIVE_NOTIFY_FROM_USER_ID=%s. Messages will be sent as user %s. "
            "Create an incoming webhook under user %s and set "
            "EXCLUSIVE_NOTIFY_WEBHOOK_URL.",
            owner_id,
            expected,
            owner_id,
            expected,
        )
    elif owner_id == expected:
        logger.info("Exclusive notify sender verified: user_id=%s", owner_id)
    return owner_id


def parse_end_date(value: Any) -> date | None:
    """Parse Bitrix date/datetime field to a calendar date."""
    if value is None or value == "":
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text or text.startswith("0000-00-00"):
        return None
    if "T" in text:
        text = text.split("T", 1)[0]
    elif " " in text:
        text = text.split(" ", 1)[0]
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def format_address(value: Any) -> str:
    """Normalize CRM address UF value to a short readable string."""
    if value is None or value == "":
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("ADDRESS_1", "address_1", "ADDRESS", "address", "TEXT", "text"):
            part = value.get(key)
            if part:
                return str(part).strip()
        city = value.get("CITY") or value.get("city") or ""
        street = value.get("ADDRESS_1") or value.get("address_1") or ""
        joined = ", ".join(str(p).strip() for p in (city, street) if p)
        return joined
    return str(value).strip()


def build_item_url(settings: Settings, item_id: int) -> str:
    """Build CRM card URL for an exclusive item."""
    url = settings.b24_webhook_url
    domain = url.split("/rest/")[0] if "/rest/" in url else url.rstrip("/")
    return f"{domain}/crm/type/{settings.exclusive_entity_type_id}/details/{item_id}/"


def days_until(end: date, today: date) -> int:
    return (end - today).days


def resolve_due_milestone(
    days_left: int,
    milestones: list[int],
    *,
    catch_up: bool,
) -> int | None:
    """Return which reminder milestone applies today (7 / 3 / 1), if any.

    Exact day always matches. With catch_up, a missed milestone is sent once
    while still before the next smaller milestone (no daily spam).
    """
    if days_left < 0 or not milestones:
        return None

    ordered = sorted({int(m) for m in milestones if int(m) >= 0}, reverse=True)
    if days_left in ordered:
        return days_left

    if not catch_up:
        return None

    # Windows between milestones, e.g. 7→3, 3→1, 1→0 inclusive for last.
    lower_bounds = ordered[1:] + [-1]
    for milestone, lower in zip(ordered, lower_bounds):
        if lower < days_left < milestone:
            return milestone
        if milestone == ordered[-1] and days_left == 0:
            return milestone
    return None


def format_reminder_message(
    *,
    title: str,
    end: date,
    days_left: int,
    item_url: str,
    address: str = "",
) -> str:
    """Build BBCode reminder for personal chat / notification."""
    end_human = end.strftime("%d.%m.%Y")
    if days_left == 0:
        timing = "сегодня истекает срок эксклюзива"
    elif days_left == 1:
        timing = "завтра истекает срок эксклюзива"
    else:
        timing = f"через {days_left} дн. истекает срок эксклюзива"

    lines = [
        f"[B]ВАЖНО:[/B] {timing}",
        "",
        f"Элемент: {title or '—'}",
        f"Дата окончания: {end_human}",
    ]
    if address:
        lines.append(f"Адрес: {address}")
    lines.extend(["", f"Карточка: {item_url}"])
    return "\n".join(lines)


def fetch_exclusive_items(settings: Settings) -> list[dict[str, Any]]:
    """Load exclusive items that have an end date set."""
    end_field = settings.exclusive_end_date_field
    address_field = settings.exclusive_address_field
    items: list[dict[str, Any]] = []
    start = 0

    while True:
        data = _rest_call(
            "crm.item.list",
            {
                "entityTypeId": settings.exclusive_entity_type_id,
                "select": [
                    "id",
                    "title",
                    "stageId",
                    "assignedById",
                    end_field,
                    address_field,
                ],
                "filter": {
                    f"!={end_field}": "",
                },
                "order": {"id": "ASC"},
                "start": start,
            },
        )
        page = _as_list(data.get("result"))
        items.extend(page)
        next_start = data.get("next")
        if next_start is None:
            break
        start = int(next_start)

    return items


def select_due_reminders(
    items: list[dict[str, Any]],
    settings: Settings,
    today: date,
) -> list[dict[str, Any]]:
    """Filter items that need an expiry reminder today."""
    end_field = settings.exclusive_end_date_field
    address_field = settings.exclusive_address_field
    skip_stages = settings.exclusive_skip_stage_ids
    due: list[dict[str, Any]] = []

    for item in items:
        item_id = _coerce_int(item.get("id") or item.get("ID"))
        assigned = _coerce_int(item.get("assignedById") or item.get("assigned_by_id"))
        stage = str(item.get("stageId") or item.get("stage_id") or "")
        if not item_id or not assigned:
            continue
        if stage in skip_stages:
            continue

        end = parse_end_date(item.get(end_field))
        if end is None:
            continue

        days_left = days_until(end, today)
        milestone = resolve_due_milestone(
            days_left,
            settings.exclusive_expiry_milestones,
            catch_up=settings.exclusive_expiry_catch_up,
        )
        if milestone is None:
            continue

        end_key = end.isoformat()
        if was_exclusive_expiry_notified(item_id, end_key, milestone):
            continue

        due.append(
            {
                "item_id": item_id,
                "title": str(item.get("title") or item.get("TITLE") or ""),
                "assigned_by_id": assigned,
                "end_date": end,
                "days_left": days_left,
                "milestone": milestone,
                "address": format_address(item.get(address_field)),
                "stage_id": stage,
            }
        )
    return due


def send_reminder(settings: Settings, reminder: dict[str, Any]) -> dict[str, Any]:
    """Send personal-chat reminders to responsible + fixed watchers."""
    item_id = int(reminder["item_id"])
    assigned = int(reminder["assigned_by_id"])
    end: date = reminder["end_date"]
    days_left = int(reminder["days_left"])
    milestone = int(reminder.get("milestone") or days_left)
    recipients = resolve_notify_recipients(
        assigned,
        settings.exclusive_notify_user_ids,
    )
    message = format_reminder_message(
        title=str(reminder.get("title") or ""),
        end=end,
        days_left=days_left,
        item_url=build_item_url(settings, item_id),
        address=str(reminder.get("address") or ""),
    )

    if settings.dry_run:
        logger.info(
            "DRY_RUN: would notify recipients=%s item_id=%s end=%s "
            "days_left=%s milestone=%s from_user_id=%s",
            recipients,
            item_id,
            end.isoformat(),
            days_left,
            milestone,
            settings.exclusive_notify_from_user_id,
        )
        return {
            "status": "dry_run_skipped",
            "item_id": item_id,
            "recipients": recipients,
            "milestone": milestone,
        }

    message_ids: dict[int, int] = {}
    errors: list[dict[str, Any]] = []
    for user_id in recipients:
        try:
            # SYSTEM=N so message appears from webhook owner (user 1).
            msg_id = send_user_chat_message(
                user_id,
                message,
                system=False,
                webhook_url=settings.exclusive_notify_webhook,
            )
            message_ids[user_id] = msg_id
        except Exception:
            logger.exception(
                "Chat message failed: user_id=%s item_id=%s",
                user_id,
                item_id,
            )
            errors.append({"user_id": user_id, "status": "error"})

    if message_ids:
        now = datetime.now(timezone.utc).isoformat()
        mark_exclusive_expiry_notified(
            item_id,
            end.isoformat(),
            assigned,
            milestone,
            now,
        )

    status = "sent" if message_ids and not errors else (
        "partial" if message_ids else "error"
    )
    return {
        "status": status,
        "item_id": item_id,
        "recipients": recipients,
        "milestone": milestone,
        "message_ids": message_ids,
        "errors": errors,
    }


def run_exclusive_expiry(
    settings: Settings | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Fetch exclusives and send due expiry reminders."""
    settings = settings or get_settings()
    today = today or date.today()

    logger.info(
        "Exclusive expiry check: entityTypeId=%s milestones=%s "
        "catch_up=%s DRY_RUN=%s today=%s notify_users=%s from_user=%s",
        settings.exclusive_entity_type_id,
        settings.exclusive_expiry_milestones,
        settings.exclusive_expiry_catch_up,
        settings.dry_run,
        today.isoformat(),
        settings.exclusive_notify_user_ids,
        settings.exclusive_notify_from_user_id,
    )

    try:
        verify_notify_sender(settings)
    except Exception:
        logger.exception("Failed to verify exclusive notify sender")

    try:
        items = fetch_exclusive_items(settings)
    except Exception:
        logger.exception("Failed to fetch exclusive items")
        return {"status": "error", "fetched": 0, "due": 0, "sent": 0}

    due = select_due_reminders(items, settings, today)
    results: list[dict[str, Any]] = []
    for reminder in due:
        try:
            results.append(send_reminder(settings, reminder))
        except Exception:
            logger.exception(
                "Failed to send exclusive reminder item_id=%s",
                reminder.get("item_id"),
            )
            results.append(
                {
                    "status": "error",
                    "item_id": reminder.get("item_id"),
                    "user_id": reminder.get("assigned_by_id"),
                }
            )

    sent = sum(1 for r in results if r.get("status") in {"sent", "partial"})
    skipped = sum(1 for r in results if r.get("status") == "dry_run_skipped")
    logger.info(
        "Exclusive expiry finished: fetched=%d due=%d sent=%d dry_run_skipped=%d",
        len(items),
        len(due),
        sent,
        skipped,
    )
    return {
        "status": "ok",
        "fetched": len(items),
        "due": len(due),
        "sent": sent,
        "dry_run_skipped": skipped,
        "results": results,
    }


def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    result = run_exclusive_expiry(settings)
    if result.get("status") == "error":
        sys.exit(1)


if __name__ == "__main__":
    main()
