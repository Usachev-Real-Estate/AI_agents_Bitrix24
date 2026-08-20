"""Lock contact SOURCE_ID for brokers and ROPs (revert unauthorized changes).

Bitrix REST cannot set field-level CRM permissions for system SOURCE_ID.
This job snapshots contact sources and reverts changes made by brokers/ROPs.
Also prints UI checklist for a proper grey-out in CRM access rights when an
admin can configure roles in the Bitrix24 interface.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fast_bitrix24 import Bitrix

_SRC_DIR = Path(__file__).resolve().parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from config import Settings, get_settings, setup_logging  # noqa: E402
from db import (  # noqa: E402
    count_contact_source_snapshots,
    get_contact_source_snapshot,
    init_db,
    upsert_contact_source_snapshot,
)
from notify import send_user_chat_message  # noqa: E402

logger = logging.getLogger(__name__)

# Время портала: Bitrix отдаёт и фильтрует DATE_MODIFY в МСК.
PORTAL_TZ = timezone(timedelta(hours=3))


def _coerce_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _clean_str(value: Any) -> str:
    return str(value or "").strip()


def _user_display_name(user: dict[str, Any]) -> str:
    return (
        f"{_clean_str(user.get('NAME'))} {_clean_str(user.get('LAST_NAME'))}".strip()
        or f"ID:{user.get('ID')}"
    )


def _is_rop(user: dict[str, Any], substrings: list[str]) -> bool:
    position = _clean_str(user.get("WORK_POSITION"))
    if not position:
        return False
    return any(s and s in position for s in substrings)


def _dept_ids(user: dict[str, Any]) -> set[int]:
    raw = user.get("UF_DEPARTMENT") or []
    if not isinstance(raw, list):
        return set()
    return {_coerce_int(x) for x in raw if _coerce_int(x) > 0}


def collect_restricted_user_ids(
    users: list[dict[str, Any]],
    settings: Settings,
) -> tuple[set[int], dict[int, str]]:
    """Return broker+ROP user IDs that must not change SOURCE_ID.

    Returns:
        (restricted_ids, id_to_label)
    """
    sales_depts = set(settings.owner_sales_dept_ids)
    exclude_ids = set(settings.contact_source_lock_exclude_user_ids)
    exclude_ids.add(_coerce_int(settings.admin_user_id))
    exclude_ids.add(_coerce_int(settings.b24_user_id))
    exclude_names = {
        n.casefold() for n in settings.contact_source_lock_exclude_names
    }
    rop_substr = settings.contact_source_lock_rop_position_substr

    restricted: set[int] = set()
    labels: dict[int, str] = {}

    for user in users:
        uid = _coerce_int(user.get("ID"))
        if uid <= 0 or uid in exclude_ids:
            continue
        name = _user_display_name(user)
        if name.casefold() in exclude_names:
            continue

        depts = _dept_ids(user)
        is_rop = _is_rop(user, rop_substr)
        in_sales = bool(depts & sales_depts)
        if not (is_rop or in_sales):
            continue

        # Skip pure company/root accounts parked in many depts with empty role
        if name == "Агентство Недвижимости":
            continue

        restricted.add(uid)
        role = "РОП" if is_rop else "брокер"
        labels[uid] = f"{name} ({role})"

    return restricted, labels


def _list_active_users(bx: Bitrix) -> list[dict[str, Any]]:
    return list(
        bx.get_all(
            "user.get",
            {"FILTER": {"ACTIVE": True}},
        )
        or []
    )


def _list_contacts(
    bx: Bitrix,
    *,
    modified_since: str | None = None,
) -> list[dict[str, Any]]:
    filt: dict[str, Any] = {}
    if modified_since:
        filt[">=DATE_MODIFY"] = modified_since
    # Manual pagination avoids fast_bitrix24 batch OPERATION_TIME_LIMIT on large portals.
    items: list[dict[str, Any]] = []
    start = 0
    while True:
        raw = bx.call(
            "crm.contact.list",
            {
                "filter": filt,
                "select": [
                    "ID",
                    "TITLE",
                    "NAME",
                    "LAST_NAME",
                    "SOURCE_ID",
                    "DATE_MODIFY",
                    "MODIFY_BY_ID",
                    "DATE_CREATE",
                    "CREATED_BY_ID",
                ],
                "start": start,
            },
            raw=True,
        )
        if not isinstance(raw, dict):
            break
        batch = raw.get("result") or []
        if isinstance(batch, list):
            items.extend([x for x in batch if isinstance(x, dict)])
        nxt = raw.get("next")
        if nxt is None:
            break
        # Bitrix обязан двигать курсор вперёд. Если не двигает — выходим,
        # иначе цикл крутится вечно и список растёт до OOM.
        if int(nxt) <= start:
            logger.warning(
                "Pagination stalled at start=%s (next=%s) — stopping", start, nxt,
            )
            break
        start = int(nxt)
    return items


def seed_snapshots_if_needed(bx: Bitrix, now_iso: str) -> int:
    """No full seed: ~70k contacts hit Bitrix OPERATION_TIME_LIMIT.

    Snapshots are created lazily when a contact first appears in the lookback window.
    """
    existing = count_contact_source_snapshots()
    logger.info(
        "SOURCE snapshots present: %s (lazy mode, no full seed)",
        existing,
    )
    return 0


def revert_contact_source(
    bx: Bitrix,
    contact_id: int,
    source_id: str,
    *,
    dry_run: bool,
) -> dict[str, Any]:
    """Restore SOURCE_ID on a contact (honors dry_run)."""
    if dry_run:
        logger.info(
            "DRY_RUN: would revert contact #%s SOURCE_ID -> %r",
            contact_id,
            source_id,
        )
        return {"dry_run_skipped": True, "contact_id": contact_id, "source_id": source_id}

    result = bx.call(
        "crm.contact.update",
        {
            "id": contact_id,
            "fields": {"SOURCE_ID": source_id},
        },
    )
    return {"ok": bool(result), "contact_id": contact_id, "source_id": source_id}


def process_modified_contacts(
    bx: Bitrix,
    contacts: list[dict[str, Any]],
    restricted_ids: set[int],
    labels: dict[int, str],
    settings: Settings,
    now_iso: str,
) -> dict[str, int]:
    """Compare snapshots and revert unauthorized SOURCE_ID changes."""
    stats = {
        "checked": 0,
        "unchanged": 0,
        "allowed_updates": 0,
        "reverted": 0,
        "new_snapshots": 0,
        "errors": 0,
    }

    for contact in contacts:
        cid = _coerce_int(contact.get("ID"))
        if cid <= 0:
            continue
        stats["checked"] += 1
        current = _clean_str(contact.get("SOURCE_ID"))
        modifier = _coerce_int(contact.get("MODIFY_BY_ID"))
        snapshot = get_contact_source_snapshot(cid)

        if snapshot is None:
            upsert_contact_source_snapshot(cid, current, now_iso)
            stats["new_snapshots"] += 1
            continue

        if snapshot == current:
            stats["unchanged"] += 1
            continue

        # Source changed relative to snapshot
        if modifier in restricted_ids:
            try:
                revert_contact_source(
                    bx, cid, snapshot, dry_run=settings.dry_run,
                )
                if not settings.dry_run:
                    # Keep snapshot as the locked value; do not adopt illegal change.
                    upsert_contact_source_snapshot(cid, snapshot, now_iso)
                stats["reverted"] += 1
                who = labels.get(modifier, f"ID:{modifier}")
                title = (
                    f"{_clean_str(contact.get('NAME'))} "
                    f"{_clean_str(contact.get('LAST_NAME'))}"
                ).strip() or f"#{cid}"
                logger.warning(
                    "Reverted SOURCE_ID on contact #%s (%s): %r -> %r by %s",
                    cid,
                    title,
                    current,
                    snapshot,
                    who,
                )
                if settings.contact_source_lock_notify and not settings.dry_run:
                    notify_uid = settings.contact_source_lock_notify_user
                    msg = (
                        "Откат поля «Источник» у контакта.\n"
                        f"Последний редактор карточки: {who}\n"
                        f"Контакт: [url=/crm/contact/details/{cid}/]{title}[/url]\n"
                        f"Восстановлено: {snapshot or '—'} "
                        f"(попытка: {current or '—'})."
                    )
                    try:
                        send_user_chat_message(notify_uid, msg)
                    except Exception:
                        logger.exception(
                            "Failed to notify user %s about SOURCE lock", notify_uid,
                        )
            except Exception:
                stats["errors"] += 1
                logger.exception("Failed to revert contact #%s SOURCE_ID", cid)
        else:
            # Allowed actor (admin / back-office / etc.): accept new value
            upsert_contact_source_snapshot(cid, current, now_iso)
            stats["allowed_updates"] += 1
            logger.info(
                "Accepted SOURCE_ID change on contact #%s by user %s: %r -> %r",
                cid,
                modifier,
                snapshot,
                current,
            )

    return stats


def print_ui_checklist(restricted_ids: set[int], labels: dict[int, str]) -> None:
    """Print CRM UI steps for a hard field lock (preferred long-term)."""
    print("\n=== UI checklist (CRM → Права доступа) ===")
    print("1. Открыть: https://b24-po7frr.bitrix24.ru/crm/configs/")
    print("   или CRM → Настройки → Права доступа")
    print("2. Найти роли, назначенные брокерам и РОПам.")
    print("3. Контакты → права на поля → «Источник» → запретить изменение.")
    print("4. Админские роли оставить с правом изменения.")
    print(f"\nRestricted users ({len(restricted_ids)}):")
    for uid in sorted(restricted_ids):
        print(f"  #{uid} {labels.get(uid, '')}")


def run(settings: Settings | None = None) -> dict[str, Any]:
    """Entry: seed snapshots if needed, then enforce SOURCE_ID lock."""
    settings = settings or get_settings()
    setup_logging(settings.log_level)
    init_db()

    if not settings.contact_source_lock_enabled:
        logger.info("CONTACT_SOURCE_LOCK_ENABLED=false — skip")
        return {"skipped": True}

    bx = Bitrix(settings.b24_webhook_url)
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()

    users = _list_active_users(bx)
    restricted_ids, labels = collect_restricted_user_ids(users, settings)
    logger.info(
        "SOURCE lock restricted users: %s (dry_run=%s)",
        len(restricted_ids),
        settings.dry_run,
    )
    if "--checklist" in sys.argv:
        print_ui_checklist(restricted_ids, labels)

    seeded = seed_snapshots_if_needed(bx, now_iso)

    lookback = max(1, int(settings.contact_source_lock_lookback_minutes))
    since_dt = now - timedelta(minutes=lookback)
    since = since_dt.isoformat()
    # Bitrix фильтрует DATE_MODIFY по времени портала (МСК).
    since_filter = since_dt.astimezone(PORTAL_TZ).strftime("%Y-%m-%dT%H:%M:%S%z")
    since_filter = since_filter[:-2] + ":" + since_filter[-2:]

    contacts = _list_contacts(bx, modified_since=since_filter)
    logger.info(
        "Contacts modified since %s: %s", since_filter, len(contacts),
    )

    stats = process_modified_contacts(
        bx, contacts, restricted_ids, labels, settings, now_iso,
    )
    result = {
        "seeded": seeded,
        "restricted_users": len(restricted_ids),
        "lookback_minutes": lookback,
        "since": since,
        **stats,
    }
    logger.info("SOURCE lock finished: %s", result)
    return result


def main() -> None:
    run()


if __name__ == "__main__":
    main()
