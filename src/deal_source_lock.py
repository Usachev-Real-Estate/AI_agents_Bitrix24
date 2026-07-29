"""Lock deal SOURCE_ID for brokers and ROPs in the sellers funnel.

Same snapshot/revert pattern as contact_source_lock, scoped to one deal category
(«Продавцы» by default via SELLERS_CATEGORY_ID).
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
from contact_source_lock import (  # noqa: E402
    _clean_str,
    _coerce_int,
    _list_active_users,
    collect_restricted_user_ids,
)
from db import (  # noqa: E402
    count_deal_source_snapshots,
    get_deal_source_snapshot,
    init_db,
    upsert_deal_source_snapshot,
)
from notify import send_user_chat_message  # noqa: E402

logger = logging.getLogger(__name__)

SELLERS_FUNNEL_LABEL = "Продавцы"


def _list_deals(
    bx: Bitrix,
    *,
    category_id: int,
    modified_since: str | None = None,
) -> list[dict[str, Any]]:
    filt: dict[str, Any] = {"CATEGORY_ID": category_id}
    if modified_since:
        filt[">=DATE_MODIFY"] = modified_since
    items: list[dict[str, Any]] = []
    start = 0
    while True:
        raw = bx.call(
            "crm.deal.list",
            {
                "filter": filt,
                "select": [
                    "ID",
                    "TITLE",
                    "SOURCE_ID",
                    "DATE_MODIFY",
                    "MODIFY_BY_ID",
                    "CATEGORY_ID",
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
        start = int(nxt)
    return items


def seed_snapshots_if_needed(now_iso: str) -> int:
    """Lazy snapshots only — no full seed on large portals."""
    existing = count_deal_source_snapshots()
    logger.info(
        "Deal SOURCE snapshots present: %s (lazy mode, no full seed)",
        existing,
    )
    return 0


def revert_deal_source(
    bx: Bitrix,
    deal_id: int,
    source_id: str,
    *,
    dry_run: bool,
) -> dict[str, Any]:
    """Restore SOURCE_ID on a deal (honors dry_run)."""
    if dry_run:
        logger.info(
            "DRY_RUN: would revert deal #%s SOURCE_ID -> %r",
            deal_id,
            source_id,
        )
        return {"dry_run_skipped": True, "deal_id": deal_id, "source_id": source_id}

    result = bx.call(
        "crm.deal.update",
        {
            "id": deal_id,
            "fields": {"SOURCE_ID": source_id},
        },
    )
    return {"ok": bool(result), "deal_id": deal_id, "source_id": source_id}


def process_modified_deals(
    bx: Bitrix,
    deals: list[dict[str, Any]],
    restricted_ids: set[int],
    labels: dict[int, str],
    settings: Settings,
    now_iso: str,
) -> dict[str, int]:
    """Compare snapshots and revert unauthorized SOURCE_ID changes on deals."""
    stats = {
        "checked": 0,
        "unchanged": 0,
        "allowed_updates": 0,
        "reverted": 0,
        "new_snapshots": 0,
        "errors": 0,
    }

    for deal in deals:
        did = _coerce_int(deal.get("ID"))
        if did <= 0:
            continue
        stats["checked"] += 1
        current = _clean_str(deal.get("SOURCE_ID"))
        modifier = _coerce_int(deal.get("MODIFY_BY_ID"))
        snapshot = get_deal_source_snapshot(did)

        if snapshot is None:
            upsert_deal_source_snapshot(did, current, now_iso)
            stats["new_snapshots"] += 1
            continue

        if snapshot == current:
            stats["unchanged"] += 1
            continue

        if modifier in restricted_ids:
            try:
                revert_deal_source(
                    bx, did, snapshot, dry_run=settings.dry_run,
                )
                if not settings.dry_run:
                    upsert_deal_source_snapshot(did, snapshot, now_iso)
                stats["reverted"] += 1
                who = labels.get(modifier, f"ID:{modifier}")
                title = _clean_str(deal.get("TITLE")) or f"#{did}"
                logger.warning(
                    "Reverted SOURCE_ID on deal #%s (%s): %r -> %r by %s",
                    did,
                    title,
                    current,
                    snapshot,
                    who,
                )
                if settings.contact_source_lock_notify and not settings.dry_run:
                    notify_uid = settings.contact_source_lock_notify_user
                    msg = (
                        f"Откат поля «Источник» у сделки ({SELLERS_FUNNEL_LABEL}).\n"
                        f"Кто менял: {who}\n"
                        f"Сделка: [url=/crm/deal/details/{did}/]{title}[/url]\n"
                        f"Восстановлено: {snapshot or '—'} "
                        f"(попытка: {current or '—'})."
                    )
                    try:
                        send_user_chat_message(notify_uid, msg)
                    except Exception:
                        logger.exception(
                            "Failed to notify user %s about deal SOURCE lock",
                            notify_uid,
                        )
            except Exception:
                stats["errors"] += 1
                logger.exception("Failed to revert deal #%s SOURCE_ID", did)
        else:
            upsert_deal_source_snapshot(did, current, now_iso)
            stats["allowed_updates"] += 1
            logger.info(
                "Accepted SOURCE_ID change on deal #%s by user %s: %r -> %r",
                did,
                modifier,
                snapshot,
                current,
            )

    return stats


def print_deal_ui_checklist(restricted_ids: set[int], labels: dict[int, str]) -> None:
    """Print CRM UI steps for deal SOURCE field lock."""
    print(f"\n=== UI checklist (воронка «{SELLERS_FUNNEL_LABEL}») ===")
    print("1. CRM → Настройки → Права доступа")
    print("2. Роли брокеров и РОПов → Сделки → права на поля → «Источник» → запрет.")
    print("3. Админские роли оставить с правом изменения.")
    print(f"\nRestricted users ({len(restricted_ids)}):")
    for uid in sorted(restricted_ids):
        print(f"  #{uid} {labels.get(uid, '')}")


def run(settings: Settings | None = None) -> dict[str, Any]:
    """Entry: enforce SOURCE_ID lock on seller-funnel deals."""
    settings = settings or get_settings()
    setup_logging(settings.log_level)
    init_db()

    if not settings.deal_source_lock_enabled:
        logger.info("DEAL_SOURCE_LOCK_ENABLED=false — skip")
        return {"skipped": True}

    category_id = int(settings.sellers_category_id)
    bx = Bitrix(settings.b24_webhook_url)
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()

    users = _list_active_users(bx)
    restricted_ids, labels = collect_restricted_user_ids(users, settings)
    logger.info(
        "Deal SOURCE lock (category=%s) restricted users: %s (dry_run=%s)",
        category_id,
        len(restricted_ids),
        settings.dry_run,
    )
    print_deal_ui_checklist(restricted_ids, labels)

    seeded = seed_snapshots_if_needed(now_iso)

    lookback = max(1, int(settings.contact_source_lock_lookback_minutes))
    since = (now - timedelta(minutes=lookback)).isoformat()
    since_filter = (now - timedelta(minutes=lookback)).astimezone(
        timezone(timedelta(hours=3)),
    ).strftime("%Y-%m-%dT%H:%M:%S+03:00")

    deals = _list_deals(
        bx,
        category_id=category_id,
        modified_since=since_filter,
    )
    logger.info(
        "Seller deals modified since %s: %s", since_filter, len(deals),
    )

    stats = process_modified_deals(
        bx, deals, restricted_ids, labels, settings, now_iso,
    )
    result = {
        "seeded": seeded,
        "category_id": category_id,
        "restricted_users": len(restricted_ids),
        "lookback_minutes": lookback,
        "since": since,
        **stats,
    }
    logger.info("Deal SOURCE lock finished: %s", result)
    return result


def main() -> None:
    run()


if __name__ == "__main__":
    main()
