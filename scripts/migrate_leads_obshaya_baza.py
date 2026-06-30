"""Migrate old NEW leads to deals in funnel «Общая база» / stage «Покупатели».

Test mode: processes only the oldest matching leads (see TEST_LIMIT).
"""

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
    _as_list,
    _build_crm_link,
    _bx_get_all_sync,
    _clean_str,
    _coerce_int,
    _get_bitrix,
    is_mutation_allowed,
)

logger = logging.getLogger(__name__)

LEAD_STATUS_NEW = "NEW"
LEAD_MIGRATE_BEFORE = "2026-06-10 00:00:00"
DEAL_CATEGORY_ID = 26
DEAL_STAGE_ID = "C26:PREPARATION"
DEAL_TITLE = "Не отработанный Лид"
CONTACT_DEFAULT_NAME = "Не отработанный Лид"
CONTACT_TYPE_ID = "UC_QBHQQT"
DEFAULT_TEST_LIMIT = 3


@dataclass
class TimelineTransferResult:
    """Outcome of copying lead timeline to a deal."""

    header_added: bool = False
    comments_bound: int = 0
    comments_failed: int = 0
    activities_bound: int = 0
    activities_failed: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class MigrationResult:
    """Outcome of a single lead migration attempt."""

    lead_id: int
    title: str
    date_create: str
    status: str  # ok | dry_run | error | skipped
    contact_id: int = 0
    deal_id: int = 0
    error: str = ""
    timeline: TimelineTransferResult | None = None


@dataclass
class MigrationReport:
    """Aggregated migration run report."""

    results: list[MigrationResult] = field(default_factory=list)

    @property
    def ok_count(self) -> int:
        return sum(1 for r in self.results if r.status == "ok")

    @property
    def dry_count(self) -> int:
        return sum(1 for r in self.results if r.status == "dry_run")

    @property
    def error_count(self) -> int:
        return sum(1 for r in self.results if r.status == "error")


def _multifield(raw: Any) -> list[dict[str, str]]:
    """Normalize PHONE/EMAIL multifield from Bitrix24."""
    if not raw:
        return []
    items = raw if isinstance(raw, list) else _as_list(raw)
    out: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        value = _clean_str(item.get("VALUE"))
        if not value:
            continue
        value_type = _clean_str(item.get("VALUE_TYPE")) or "WORK"
        out.append({"VALUE": value, "VALUE_TYPE": value_type})
    return out


def _contact_name(lead: dict[str, Any]) -> str:
    """Contact NAME: from lead or default."""
    name = _clean_str(lead.get("NAME"))
    if name:
        return name
    return CONTACT_DEFAULT_NAME


def _fetch_user_name(user_id: int, cache: dict[int, str]) -> str:
    """Resolve Bitrix user ID to display name."""
    if not user_id:
        return "—"
    if user_id in cache:
        return cache[user_id]
    try:
        raw = _get_bitrix().call("user.get", {"ID": user_id})
        if isinstance(raw, list) and raw:
            raw = raw[0]
        if isinstance(raw, dict):
            name = f"{raw.get('NAME', '')} {raw.get('LAST_NAME', '')}".strip()
            cache[user_id] = name or f"ID:{user_id}"
            return cache[user_id]
    except Exception:
        pass
    cache[user_id] = f"ID:{user_id}"
    return cache[user_id]


def _phones_display(lead: dict[str, Any]) -> str:
    """Format lead phones for timeline header."""
    phones = _multifield(lead.get("PHONE"))
    if not phones:
        return "—"
    return ", ".join(p["VALUE"] for p in phones)


def _emails_display(lead: dict[str, Any]) -> str:
    """Format lead emails for timeline header."""
    emails = _multifield(lead.get("EMAIL"))
    if not emails:
        return ""
    return ", ".join(e["VALUE"] for e in emails)


def _format_comment_date(created: str) -> str:
    """Format CREATED timestamp for comment prefix."""
    if not created:
        return "?"
    if "T" in created:
        return created[:10]
    return created[:10] if len(created) >= 10 else created


def _lead_header_comment(lead: dict[str, Any], user_cache: dict[int, str]) -> str:
    """Build inline summary comment for deal timeline (no lead link)."""
    title = _clean_str(lead.get("TITLE"))
    date_create = _clean_str(lead.get("DATE_CREATE"))
    created = _format_comment_date(date_create)
    assigned = _coerce_int(lead.get("ASSIGNED_BY_ID"))
    source = _clean_str(lead.get("SOURCE_ID")) or "—"
    assigned_name = _fetch_user_name(assigned, user_cache)
    lead_comments = _clean_str(lead.get("COMMENTS"))

    lines = [
        "📋 Перенесён из лида (автоматически)",
        f"Название: {title}",
        f"Телефон: {_phones_display(lead)}",
    ]
    emails = _emails_display(lead)
    if emails:
        lines.append(f"Email: {emails}")
    lines.extend([
        f"Создан: {created}",
        f"Источник: {source}",
        f"Ответственный: {assigned_name}",
    ])
    if lead_comments:
        lines.append(f"Комментарий к лиду: {lead_comments}")
    return "\n".join(lines)


def _copied_comment_text(comment: dict[str, Any], user_cache: dict[int, str]) -> str:
    """Format lead comment as new deal comment with original date and author."""
    created = _format_comment_date(_clean_str(comment.get("CREATED")))
    author_id = _coerce_int(comment.get("AUTHOR_ID"))
    author = _fetch_user_name(author_id, user_cache)
    text = _clean_str(comment.get("COMMENT"))
    return f"[{created} | {author}]\n{text}"


def transfer_lead_timeline(
    lead: dict[str, Any],
    deal_id: int,
    *,
    dry_run: bool,
) -> TimelineTransferResult:
    """Copy lead timeline (header, comments, activities) to deal."""
    lead_id = _coerce_int(lead.get("ID"))
    result = TimelineTransferResult()
    bx = _get_bitrix()
    user_cache: dict[int, str] = {}

    comments_raw = bx.get_all(
        "crm.timeline.comment.list",
        {
            "filter": {"ENTITY_ID": lead_id, "ENTITY_TYPE": "lead"},
            "select": ["ID", "AUTHOR_ID", "COMMENT", "CREATED"],
        },
    )
    comments = comments_raw if isinstance(comments_raw, list) else _as_list(comments_raw)
    comments = [c for c in comments if isinstance(c, dict) and _clean_str(c.get("COMMENT"))]

    acts_raw = _bx_get_all_sync(
        "crm.activity.list",
        {
            "filter": {"OWNER_TYPE_ID": 1, "OWNER_ID": lead_id},
            "select": ["ID", "SUBJECT", "PROVIDER_TYPE_ID"],
        },
    )
    activities = acts_raw if isinstance(acts_raw, list) else _as_list(acts_raw)

    if dry_run:
        result.header_added = True
        result.comments_bound = len(comments)
        result.activities_bound = len(activities)
        return result

    try:
        bx.call(
            "crm.timeline.comment.add",
            {
                "fields": {
                    "ENTITY_ID": deal_id,
                    "ENTITY_TYPE": "deal",
                    "COMMENT": _lead_header_comment(lead, user_cache),
                },
            },
        )
        result.header_added = True
    except Exception as exc:
        result.errors.append(f"header: {exc}")

    for comment in comments:
        try:
            bx.call(
                "crm.timeline.comment.add",
                {
                    "fields": {
                        "ENTITY_ID": deal_id,
                        "ENTITY_TYPE": "deal",
                        "COMMENT": _copied_comment_text(comment, user_cache),
                    },
                },
            )
            result.comments_bound += 1
        except Exception as exc:
            comment_id = _coerce_int(comment.get("ID"))
            result.comments_failed += 1
            result.errors.append(f"comment #{comment_id}: {exc}")
        time.sleep(0.1)

    for activity in activities:
        if not isinstance(activity, dict):
            continue
        activity_id = _coerce_int(activity.get("ID"))
        if not activity_id:
            continue
        try:
            bound = bx.call(
                "crm.activity.binding.add",
                {
                    "activityId": activity_id,
                    "entityTypeId": 2,
                    "entityId": deal_id,
                },
            )
            if bound:
                result.activities_bound += 1
            else:
                # Уже привязано ранее — считаем успехом
                result.activities_bound += 1
        except Exception as exc:
            err = str(exc)
            if "ACTIVITY_IS_ALREADY_BOUND" in err or "already bound" in err.lower():
                result.activities_bound += 1
            else:
                result.activities_failed += 1
                result.errors.append(f"activity #{activity_id}: {exc}")
        time.sleep(0.1)

    return result


def fetch_candidate_leads(limit: int) -> list[dict[str, Any]]:
    """Fetch oldest NEW leads created before cutoff."""
    raw = _bx_get_all_sync(
        "crm.lead.list",
        {
            "filter": {
                "STATUS_ID": LEAD_STATUS_NEW,
                "<DATE_CREATE": LEAD_MIGRATE_BEFORE,
            },
            "select": [
                "ID",
                "TITLE",
                "NAME",
                "LAST_NAME",
                "SECOND_NAME",
                "DATE_CREATE",
                "ASSIGNED_BY_ID",
                "PHONE",
                "EMAIL",
                "SOURCE_ID",
                "STATUS_ID",
                "COMMENTS",
            ],
        },
    )
    leads = raw if isinstance(raw, list) else _as_list(raw)
    valid = [d for d in leads if isinstance(d, dict)]
    valid.sort(key=lambda d: _clean_str(d.get("DATE_CREATE")))
    return valid[:limit]


def migrate_lead(lead: dict[str, Any], *, dry_run: bool) -> MigrationResult:
    """Convert one lead: new contact + deal + mark CONVERTED."""
    lead_id = _coerce_int(lead.get("ID"))
    title = _clean_str(lead.get("TITLE"))
    date_create = _clean_str(lead.get("DATE_CREATE"))
    phones = _multifield(lead.get("PHONE"))

    if not phones:
        return MigrationResult(
            lead_id=lead_id,
            title=title,
            date_create=date_create,
            status="error",
            error="нет телефона",
        )

    if dry_run:
        return MigrationResult(
            lead_id=lead_id,
            title=title,
            date_create=date_create,
            status="dry_run",
        )

    bx = _get_bitrix()
    assigned = _coerce_int(lead.get("ASSIGNED_BY_ID")) or 1

    contact_fields: dict[str, Any] = {
        "NAME": _contact_name(lead),
        "TYPE_ID": CONTACT_TYPE_ID,
        "PHONE": phones,
        "ASSIGNED_BY_ID": assigned,
        "OPENED": "Y",
    }
    last_name = _clean_str(lead.get("LAST_NAME"))
    second_name = _clean_str(lead.get("SECOND_NAME"))
    if last_name:
        contact_fields["LAST_NAME"] = last_name
    if second_name:
        contact_fields["SECOND_NAME"] = second_name
    emails = _multifield(lead.get("EMAIL"))
    if emails:
        contact_fields["EMAIL"] = emails
    source_id = _clean_str(lead.get("SOURCE_ID"))
    if source_id:
        contact_fields["SOURCE_ID"] = source_id

    try:
        contact_id = _coerce_int(bx.call("crm.contact.add", {"fields": contact_fields}))
        if not contact_id:
            return MigrationResult(
                lead_id=lead_id,
                title=title,
                date_create=date_create,
                status="error",
                error="crm.contact.add не вернул ID",
            )

        deal_fields: dict[str, Any] = {
            "TITLE": DEAL_TITLE,
            "CATEGORY_ID": DEAL_CATEGORY_ID,
            "STAGE_ID": DEAL_STAGE_ID,
            "ASSIGNED_BY_ID": assigned,
            "CONTACT_IDS": [contact_id],
            "OPENED": "Y",
            "CLOSED": "N",
        }
        if source_id:
            deal_fields["SOURCE_ID"] = source_id

        deal_id = _coerce_int(bx.call("crm.deal.add", {"fields": deal_fields}))
        if not deal_id:
            return MigrationResult(
                lead_id=lead_id,
                title=title,
                date_create=date_create,
                status="error",
                contact_id=contact_id,
                error="crm.deal.add не вернул ID",
            )

        bx.call(
            "crm.deal.contact.add",
            {
                "id": deal_id,
                "fields": {"CONTACT_ID": contact_id, "IS_PRIMARY": "Y"},
            },
        )

        timeline = transfer_lead_timeline(lead, deal_id, dry_run=False)

        bx.call(
            "crm.lead.update",
            {
                "id": lead_id,
                "fields": {"STATUS_ID": "CONVERTED"},
            },
        )

        return MigrationResult(
            lead_id=lead_id,
            title=title,
            date_create=date_create,
            status="ok",
            contact_id=contact_id,
            deal_id=deal_id,
            timeline=timeline,
        )
    except Exception as exc:
        logger.exception("migrate_lead failed lead_id=%s", lead_id)
        return MigrationResult(
            lead_id=lead_id,
            title=title,
            date_create=date_create,
            status="error",
            error=str(exc),
        )


def format_report(report: MigrationReport, *, dry_run: bool, limit: int) -> str:
    """Build human-readable report for chat."""
    mode = "DRY-RUN" if dry_run else "ТЕСТ"
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    lines = [
        f"🔄 Миграция лидов → Общая база / Покупатели ({mode}, {now})",
        f"Фильтр: NEW, создан до 10.06.2026 | взято самых старых: {limit}",
        f"Сделка: «{DEAL_TITLE}» | воронка {DEAL_CATEGORY_ID} | {DEAL_STAGE_ID}",
        f"Контакт: всегда новый, тип {CONTACT_TYPE_ID}",
        "",
        f"Успешно: {report.ok_count} | Dry-run: {report.dry_count} | Ошибки: {report.error_count}",
        "",
    ]

    for i, r in enumerate(report.results, 1):
        created = r.date_create[:10] if r.date_create else "?"
        if r.status == "ok":
            lines.append(f"{i}. ✅ Лид #{r.lead_id} ({created}) → контакт #{r.contact_id}, сделка #{r.deal_id}")
            lines.append(f"   {r.title[:60]}")
            lines.append(f"   {_build_crm_link('deal', r.deal_id)}")
            if r.timeline:
                tl = r.timeline
                lines.append(
                    f"   Таймлайн: шапка={'да' if tl.header_added else 'нет'}, "
                    f"комм. скопир.={tl.comments_bound}, звонки={tl.activities_bound}"
                )
                if tl.errors:
                    lines.append(f"   ⚠️ {tl.errors[0][:80]}")
        elif r.status == "dry_run":
            lines.append(f"{i}. 🔍 [dry-run] Лид #{r.lead_id} ({created}) — {r.title[:60]}")
        else:
            lines.append(f"{i}. ❌ Лид #{r.lead_id} ({created}) — {r.error}")
        lines.append("")

    return "\n".join(lines).rstrip()


def run(limit: int, *, dry_run: bool, send_chat: bool) -> MigrationReport:
    """Execute migration for the oldest `limit` matching leads."""
    leads = fetch_candidate_leads(limit)
    logger.info("Found %d candidate leads (limit=%d)", len(leads), limit)

    report = MigrationReport()
    for lead in leads:
        result = migrate_lead(lead, dry_run=dry_run)
        report.results.append(result)
        logger.info(
            "lead_id=%s status=%s contact=%s deal=%s",
            result.lead_id,
            result.status,
            result.contact_id,
            result.deal_id,
        )
        if not dry_run and result.status == "ok":
            time.sleep(0.3)

    text = format_report(report, dry_run=dry_run, limit=limit)
    print(text)

    if send_chat:
        settings = get_settings()
        send_chat_message_chunked(settings.report_chat_id, text)

    return report


def _normalize_entity(raw: Any) -> dict[str, Any]:
    """Unwrap fast_bitrix24 single-entity response."""
    if not isinstance(raw, dict):
        return {}
    if "ID" in raw or "id" in raw:
        return raw
    for value in raw.values():
        if isinstance(value, dict) and ("ID" in value or "id" in value):
            return value
    return raw


def run_timeline_test(lead_id: int, deal_id: int, *, dry_run: bool, send_chat: bool) -> None:
    """Transfer timeline from an existing lead/deal pair (for testing)."""
    bx = _get_bitrix()
    lead_raw = bx.call("crm.lead.get", {"id": lead_id})
    lead = _normalize_entity(lead_raw)
    if not lead or _coerce_int(lead.get("ID")) != lead_id:
        raise SystemExit(f"Lead #{lead_id} not found")

    tl = transfer_lead_timeline(lead, deal_id, dry_run=dry_run)
    mode = "DRY-RUN" if dry_run else "ТЕСТ"
    lines = [
        f"📎 Перенос таймлайна ({mode})",
        f"Лид #{lead_id} → сделка #{deal_id}",
        f"Шапка: {'да' if tl.header_added else 'нет'}",
        f"Комментарии скопированы: {tl.comments_bound} (ошибок: {tl.comments_failed})",
        f"Звонки привязаны: {tl.activities_bound} (ошибок: {tl.activities_failed})",
        "",
        _build_crm_link("deal", deal_id),
    ]
    if tl.errors:
        lines.append("")
        lines.append("Ошибки:")
        lines.extend(f"  • {e}" for e in tl.errors[:5])

    text = "\n".join(lines)
    print(text)
    if send_chat:
        send_chat_message_chunked(get_settings().report_chat_id, text)


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Migrate NEW leads to Общая база funnel")
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_TEST_LIMIT,
        help=f"Number of oldest leads to process (default {DEFAULT_TEST_LIMIT})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview only, no CRM mutations",
    )
    parser.add_argument(
        "--no-chat",
        action="store_true",
        help="Do not send report to Bitrix24 chat",
    )
    parser.add_argument(
        "--transfer-timeline",
        metavar="LEAD_ID:DEAL_ID",
        help="Test timeline transfer for existing pair, e.g. 204:12974",
    )
    args = parser.parse_args()

    settings = get_settings()
    setup_logging(settings.log_level)

    dry_run = args.dry_run or not is_mutation_allowed(settings)
    if dry_run and not args.dry_run:
        logger.warning("DRY_RUN=true in .env — running in preview mode")

    if args.transfer_timeline:
        lead_s, deal_s = args.transfer_timeline.split(":", 1)
        run_timeline_test(
            int(lead_s),
            int(deal_s),
            dry_run=dry_run,
            send_chat=not args.no_chat,
        )
        return

    run(args.limit, dry_run=dry_run, send_chat=not args.no_chat)


if __name__ == "__main__":
    main()
