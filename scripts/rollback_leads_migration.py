"""Rollback lead migration: restore leads to NEW, delete created deals and contacts."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from config import get_settings, setup_logging  # noqa: E402
from notify import send_chat_message_chunked  # noqa: E402
from tools import (  # noqa: E402
    _bx_get_all_sync,
    _coerce_int,
    _get_bitrix,
    is_mutation_allowed,
)

logger = logging.getLogger(__name__)

DEAL_CATEGORY_ID = 26
DEAL_STAGE_ID = "C26:PREPARATION"
DEAL_TITLE = "Не отработанный Лид"
LEAD_MIGRATE_BEFORE = "2026-06-10 00:00:00"
MIGRATION_DAY = "2026-06-21 00:00:00"
# Converted on the same day but not part of our migration
EXCLUDE_LEAD_IDS = {750, 1794}


@dataclass
class RollbackReport:
    deal_ok: int = 0
    deal_total: int = 0
    contact_ok: int = 0
    contact_total: int = 0
    lead_ok: int = 0
    lead_total: int = 0
    errors: list[str] = field(default_factory=list)


def _deal_contact_id(bx: Any, deal_id: int) -> int:
    raw = bx.call("crm.deal.contact.items.get", {"id": deal_id})
    if isinstance(raw, dict) and "CONTACT_ID" in raw:
        return _coerce_int(raw.get("CONTACT_ID"))
    rows = raw if isinstance(raw, list) else list(raw.values()) if isinstance(raw, dict) else []
    for row in rows:
        if isinstance(row, dict):
            cid = _coerce_int(row.get("CONTACT_ID"))
            if cid:
                return cid
    return 0


def discover_targets() -> tuple[list[int], list[int], list[int]]:
    """Find migrated deals, contacts, and leads to rollback."""
    deals_raw = _bx_get_all_sync(
        "crm.deal.list",
        {
            "filter": {
                "CATEGORY_ID": DEAL_CATEGORY_ID,
                "STAGE_ID": DEAL_STAGE_ID,
                "TITLE": DEAL_TITLE,
            },
            "select": ["ID"],
        },
    )
    deals = deals_raw if isinstance(deals_raw, list) else list(deals_raw.values())
    deal_ids = sorted(_coerce_int(d["ID"]) for d in deals if isinstance(d, dict))

    bx = _get_bitrix()
    contact_ids: list[int] = []
    for deal_id in deal_ids:
        cid = _deal_contact_id(bx, deal_id)
        if cid:
            contact_ids.append(cid)

    leads_raw = _bx_get_all_sync(
        "crm.lead.list",
        {
            "filter": {
                "STATUS_ID": "CONVERTED",
                "<DATE_CREATE": LEAD_MIGRATE_BEFORE,
                ">=DATE_MODIFY": MIGRATION_DAY,
            },
            "select": ["ID"],
        },
    )
    leads = leads_raw if isinstance(leads_raw, list) else list(leads_raw.values())
    lead_ids = sorted(
        _coerce_int(l["ID"])
        for l in leads
        if isinstance(l, dict) and _coerce_int(l.get("ID")) not in EXCLUDE_LEAD_IDS
    )

    return lead_ids, deal_ids, contact_ids


def rollback(*, dry_run: bool, send_chat: bool) -> RollbackReport:
    lead_ids, deal_ids, contact_ids = discover_targets()
    logger.info(
        "Rollback targets: leads=%d deals=%d contacts=%d",
        len(lead_ids),
        len(deal_ids),
        len(contact_ids),
    )

    report = RollbackReport(
        deal_total=len(deal_ids),
        contact_total=len(contact_ids),
        lead_total=len(lead_ids),
    )
    bx = _get_bitrix()

    for deal_id in deal_ids:
        if dry_run:
            report.deal_ok += 1
        else:
            try:
                bx.call("crm.deal.delete", {"id": deal_id})
                report.deal_ok += 1
            except Exception as exc:
                report.errors.append(f"deal #{deal_id}: {exc}")
                logger.exception("deal delete failed id=%s", deal_id)
            time.sleep(0.15)

    for contact_id in contact_ids:
        if dry_run:
            report.contact_ok += 1
        else:
            try:
                bx.call("crm.contact.delete", {"id": contact_id})
                report.contact_ok += 1
            except Exception as exc:
                report.errors.append(f"contact #{contact_id}: {exc}")
                logger.exception("contact delete failed id=%s", contact_id)
            time.sleep(0.15)

    for lead_id in lead_ids:
        if dry_run:
            report.lead_ok += 1
        else:
            try:
                bx.call(
                    "crm.lead.update",
                    {"id": lead_id, "fields": {"STATUS_ID": "NEW"}},
                )
                report.lead_ok += 1
            except Exception as exc:
                report.errors.append(f"lead #{lead_id}: {exc}")
                logger.exception("lead revert failed id=%s", lead_id)
            time.sleep(0.15)

    text = format_report(report, dry_run=dry_run, lead_ids=lead_ids, deal_ids=deal_ids, contact_ids=contact_ids)
    print(text)

    if send_chat:
        send_chat_message_chunked(get_settings().report_chat_id, text)

    return report


def format_report(
    report: RollbackReport,
    *,
    dry_run: bool,
    lead_ids: list[int],
    deal_ids: list[int],
    contact_ids: list[int],
) -> str:
    mode = "DRY-RUN" if dry_run else "ОТКАТ"
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    deal_errors = [e for e in report.errors if e.startswith("deal")]
    contact_errors = [e for e in report.errors if e.startswith("contact")]
    lead_errors = [e for e in report.errors if e.startswith("lead")]

    lines = [
        f"↩️ Откат миграции лидов ({mode}, {now})",
        f"Сделки удалены: {report.deal_ok}/{report.deal_total}"
        + (f" (#{deal_ids[0]}–#{deal_ids[-1]})" if deal_ids else ""),
        f"Контакты удалены: {report.contact_ok}/{report.contact_total}",
        f"Лиды → NEW: {report.lead_ok}/{report.lead_total}",
        "",
    ]
    if deal_errors or contact_errors or lead_errors:
        lines.append("Ошибки:")
        for err in (deal_errors + contact_errors + lead_errors)[:20]:
            lines.append(f"  • {err}")
        extra = len(deal_errors) + len(contact_errors) + len(lead_errors) - 20
        if extra > 0:
            lines.append(f"  … ещё {extra}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Rollback lead migration")
    parser.add_argument("--dry-run", action="store_true", help="Preview only")
    parser.add_argument("--no-chat", action="store_true", help="Skip chat report")
    args = parser.parse_args()

    settings = get_settings()
    setup_logging(settings.log_level)

    dry_run = args.dry_run or not is_mutation_allowed(settings)
    if dry_run and not args.dry_run:
        logger.warning("DRY_RUN=true in .env — preview mode")

    rollback(dry_run=dry_run, send_chat=not args.no_chat)


if __name__ == "__main__":
    main()
