"""Auto-fill and lock «Базовая ставка» on buyer-funnel deals.

On each run (cron):
1. For new deals without a snapshot — fill empty UF from motivation CSV by
   ASSIGNED_BY_ID, then store a snapshot.
2. Revert unauthorized changes by brokers/ROPs (same pattern as SOURCE lock).
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
    count_deal_base_rate_snapshots,
    get_deal_base_rate_snapshot,
    init_db,
    upsert_deal_base_rate_snapshot,
)
from fill_buyer_base_rate import (  # noqa: E402
    DEFAULT_CSV,
    UF_BASE_RATE,
    build_broker_rate_map,
    load_csv_rates,
)
from notify import send_user_chat_message  # noqa: E402
from tools import _as_list  # noqa: E402

logger = logging.getLogger(__name__)

BUYERS_FUNNEL_LABEL = "Покупатели"


def _list_buyer_deals(
    bx: Bitrix,
    *,
    category_id: int,
    modified_since: str | None = None,
) -> list[dict[str, Any]]:
    """List buyer deals, optionally filtered by DATE_MODIFY."""
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
                    "ASSIGNED_BY_ID",
                    "DATE_MODIFY",
                    "MODIFY_BY_ID",
                    "CATEGORY_ID",
                    UF_BASE_RATE,
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


def load_rate_by_user(settings: Settings, bx: Bitrix) -> dict[int, str]:
    """Build ASSIGNED_BY_ID → base rate map from CSV + Bitrix users."""
    csv_path = Path(settings.buyer_base_rate_csv or str(DEFAULT_CSV))
    if not csv_path.is_file():
        logger.error("Base-rate CSV not found: %s", csv_path)
        return {}
    csv_rows = load_csv_rates(csv_path)
    users = list(bx.get_all("user.get", {}) or [])
    if not isinstance(users, list):
        users = _as_list(users)
    rate_by_user, unmatched, _names = build_broker_rate_map(csv_rows, users)
    if unmatched:
        logger.warning("CSV brokers not found in Bitrix: %s", unmatched)
    return rate_by_user


def set_deal_base_rate(
    bx: Bitrix,
    deal_id: int,
    rate: str,
    *,
    dry_run: bool,
) -> dict[str, Any]:
    """Write UF_BASE_RATE on a deal (honors dry_run)."""
    if dry_run:
        logger.info(
            "DRY_RUN: would set deal #%s %s=%r",
            deal_id,
            UF_BASE_RATE,
            rate,
        )
        return {"dry_run_skipped": True, "deal_id": deal_id, "rate": rate}

    result = bx.call(
        "crm.deal.update",
        {"id": deal_id, "fields": {UF_BASE_RATE: rate}},
    )
    return {"ok": bool(result), "deal_id": deal_id, "rate": rate}


def revert_deal_base_rate(
    bx: Bitrix,
    deal_id: int,
    rate: str,
    *,
    dry_run: bool,
) -> dict[str, Any]:
    """Restore base rate on a deal (honors dry_run)."""
    if dry_run:
        logger.info(
            "DRY_RUN: would revert deal #%s %s -> %r",
            deal_id,
            UF_BASE_RATE,
            rate,
        )
        return {"dry_run_skipped": True, "deal_id": deal_id, "rate": rate}

    result = bx.call(
        "crm.deal.update",
        {"id": deal_id, "fields": {UF_BASE_RATE: rate}},
    )
    return {"ok": bool(result), "deal_id": deal_id, "rate": rate}


def process_buyer_deals(
    bx: Bitrix,
    deals: list[dict[str, Any]],
    rate_by_user: dict[int, str],
    restricted_ids: set[int],
    labels: dict[int, str],
    settings: Settings,
    now_iso: str,
) -> dict[str, int]:
    """Auto-fill empty rates for new deals and enforce snapshot lock."""
    stats = {
        "checked": 0,
        "filled": 0,
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
        current = _clean_str(deal.get(UF_BASE_RATE))
        assigned = _coerce_int(deal.get("ASSIGNED_BY_ID"))
        modifier = _coerce_int(deal.get("MODIFY_BY_ID"))
        snapshot = get_deal_base_rate_snapshot(did)

        if snapshot is None:
            expected = rate_by_user.get(assigned)
            if not current and expected is not None:
                try:
                    set_deal_base_rate(
                        bx, did, expected, dry_run=settings.dry_run,
                    )
                    stats["filled"] += 1
                    logger.info(
                        "Filled base rate on deal #%s assigned=%s -> %r",
                        did,
                        assigned,
                        expected,
                    )
                    # Don't snapshot empty value in dry-run — live run must still fill.
                    if settings.dry_run:
                        continue
                    current = expected
                except Exception:
                    stats["errors"] += 1
                    logger.exception(
                        "Failed to fill base rate on deal #%s", did,
                    )
                    continue
            upsert_deal_base_rate_snapshot(did, current, now_iso)
            stats["new_snapshots"] += 1
            continue

        if snapshot == current:
            stats["unchanged"] += 1
            continue

        if modifier in restricted_ids:
            try:
                revert_deal_base_rate(
                    bx, did, snapshot, dry_run=settings.dry_run,
                )
                if not settings.dry_run:
                    upsert_deal_base_rate_snapshot(did, snapshot, now_iso)
                stats["reverted"] += 1
                who = labels.get(modifier, f"ID:{modifier}")
                title = _clean_str(deal.get("TITLE")) or f"#{did}"
                logger.warning(
                    "Reverted base rate on deal #%s (%s): %r -> %r by %s",
                    did,
                    title,
                    current,
                    snapshot,
                    who,
                )
                if settings.contact_source_lock_notify and not settings.dry_run:
                    notify_uid = settings.contact_source_lock_notify_user
                    msg = (
                        f"Откат поля «Базовая ставка» у сделки "
                        f"({BUYERS_FUNNEL_LABEL}).\n"
                        f"Кто менял: {who}\n"
                        f"Сделка: [url=/crm/deal/details/{did}/]{title}[/url]\n"
                        f"Восстановлено: {snapshot or '—'} "
                        f"(попытка: {current or '—'})."
                    )
                    try:
                        send_user_chat_message(notify_uid, msg)
                    except Exception:
                        logger.exception(
                            "Failed to notify user %s about base-rate lock",
                            notify_uid,
                        )
            except Exception:
                stats["errors"] += 1
                logger.exception("Failed to revert deal #%s base rate", did)
        else:
            upsert_deal_base_rate_snapshot(did, current, now_iso)
            stats["allowed_updates"] += 1
            logger.info(
                "Accepted base-rate change on deal #%s by user %s: %r -> %r",
                did,
                modifier,
                snapshot,
                current,
            )

    return stats


def print_ui_checklist(restricted_ids: set[int], labels: dict[int, str]) -> None:
    """Print CRM UI steps for base-rate field lock."""
    print(f"\n=== UI checklist (воронка «{BUYERS_FUNNEL_LABEL}») ===")
    print("1. CRM → Настройки → Права доступа")
    print(
        "2. Роли брокеров и РОПов → Сделки → права на поля → "
        "«Базовая ставка» → запрет."
    )
    print("3. Админские роли оставить с правом изменения.")
    print(f"\nRestricted users ({len(restricted_ids)}):")
    for uid in sorted(restricted_ids):
        print(f"  #{uid} {labels.get(uid, '')}")


def run(settings: Settings | None = None) -> dict[str, Any]:
    """Entry: auto-fill and lock base rate on buyer deals."""
    settings = settings or get_settings()
    setup_logging(settings.log_level)
    init_db()

    if not settings.buyer_base_rate_lock_enabled:
        logger.info("BUYER_BASE_RATE_LOCK_ENABLED=false — skip")
        return {"skipped": True}

    category_id = int(settings.buyers_category_id or 18)
    bx = Bitrix(settings.b24_webhook_url)
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()

    users = _list_active_users(bx)
    restricted_ids, labels = collect_restricted_user_ids(users, settings)
    rate_by_user = load_rate_by_user(settings, bx)
    logger.info(
        "Buyer base-rate lock (category=%s): restricted=%s rates=%s dry_run=%s "
        "snapshots=%s",
        category_id,
        len(restricted_ids),
        len(rate_by_user),
        settings.dry_run,
        count_deal_base_rate_snapshots(),
    )
    print_ui_checklist(restricted_ids, labels)

    lookback = max(1, int(settings.contact_source_lock_lookback_minutes))
    since = (now - timedelta(minutes=lookback)).isoformat()
    since_filter = (
        now - timedelta(minutes=lookback)
    ).astimezone(timezone(timedelta(hours=3))).strftime("%Y-%m-%dT%H:%M:%S+03:00")

    deals = _list_buyer_deals(
        bx,
        category_id=category_id,
        modified_since=since_filter,
    )
    logger.info("Buyer deals modified since %s: %s", since_filter, len(deals))

    stats = process_buyer_deals(
        bx,
        deals,
        rate_by_user,
        restricted_ids,
        labels,
        settings,
        now_iso,
    )
    result = {
        "category_id": category_id,
        "restricted_users": len(restricted_ids),
        "broker_rates": len(rate_by_user),
        "lookback_minutes": lookback,
        "since": since,
        **stats,
    }
    logger.info("Buyer base-rate lock finished: %s", result)
    return result


def main() -> None:
    run()


if __name__ == "__main__":
    main()
