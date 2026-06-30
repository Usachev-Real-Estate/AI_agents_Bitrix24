"""Run audit and send full combined report to general chat only (no dept chats)."""

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

_SRC_DIR = Path(__file__).resolve().parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from config import get_settings, setup_logging  # noqa: E402
from graph import (  # noqa: E402
    _build_user_map,
    _filter_zero_entity_violations,
    _group_by_department,
    run_audit_v2,
)
from notify import send_chat_message_chunked  # noqa: E402
from tools import (  # noqa: E402
    _build_crm_link,
    _coerce_int,
    _lead_status_id,
    humanize_violation_reason,
    LEAD_STATUS_AGENT,
    LEAD_STATUS_CONVERTED,
    LEAD_STATUS_JUNK,
    LEAD_STATUS_NECELEVOY,
    LEAD_STATUS_NEW,
    LEAD_STATUS_SHARED,
)

logger = logging.getLogger(__name__)


def _dedupe_violations(violations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[Any, ...]] = set()
    unique: list[dict[str, Any]] = []
    for v in violations:
        key = (
            v.get("entity_type"),
            _coerce_int(v.get("entity_id", 0)),
            v.get("rule"),
        )
        if key not in seen:
            seen.add(key)
            unique.append(v)
    return _filter_zero_entity_violations(unique)


def _format_violation_line(
    v: dict[str, Any],
    user_map: dict[int, str],
    *,
    buyers_deals: list[dict[str, Any]],
    sellers_deals: list[dict[str, Any]],
    raw_leads: list[dict[str, Any]],
) -> list[str]:
    sev = v.get("severity", "?")
    icon = {"very high": "🔴🔴", "high": "🔴", "medium": "🟡"}.get(sev, "⚪")
    entity_type = str(v.get("entity_type", "?"))
    entity_id = _coerce_int(v.get("entity_id", 0))
    reason = humanize_violation_reason(
        v,
        buyers_deals=buyers_deals,
        sellers_deals=sellers_deals,
        leads=raw_leads,
    )
    link = _build_crm_link(entity_type, entity_id)
    uid = _coerce_int(v.get("responsible_id", 0))
    user_display = user_map.get(uid, f"ID:{uid}")
    name_only = user_display.split(" (")[0] if " (" in user_display else user_display
    etype_label = "Лид" if entity_type == "lead" else "Сделка"
    days_info = ""
    details = v.get("details", {})
    if isinstance(details, dict):
        days = details.get("days_on_stage") or details.get("days_since_last_comment")
        if days is not None and days != "" and days < 999:
            days_info = f" ({days} дн.)"
    return [
        f"{icon} {etype_label} #{entity_id} | {name_only} | {reason}{days_info}",
        f"   {link}",
    ]


async def build_full_report(state: dict[str, Any]) -> str:
    """Build summary + all departments in one message."""
    violations = _dedupe_violations(state.get("violations", []))
    now = str(state.get("current_time", ""))[:19]
    raw_leads = state.get("raw_leads", [])
    buyers_deals = state.get("raw_buyers_deals", [])
    sellers_deals = state.get("raw_sellers_deals", [])

    user_map, dept_id_map, inactive_users = await _build_user_map(
        violations, raw_leads, buyers_deals, sellers_deals,
    )

    active_violations = []
    for v in violations:
        uid = _coerce_int(v.get("responsible_id", 0))
        user_display = user_map.get(uid, "")
        dept = ""
        if "(" in user_display and ")" in user_display:
            dept = user_display.split("(")[-1].rstrip(")")
        if uid in inactive_users:
            continue
        if dept in {"Бэк-офис", "Битрикс"}:
            continue
        active_violations.append(v)

    violations = active_violations
    dept_groups = _group_by_department(violations, user_map)

    new_leads_count = sum(1 for lead in raw_leads if _lead_status_id(lead) == LEAD_STATUS_NEW)
    shared_leads_count = sum(
        1 for lead in raw_leads if _lead_status_id(lead) == LEAD_STATUS_SHARED
    )
    qualified_count = sum(
        1 for lead in raw_leads if _lead_status_id(lead) == LEAD_STATUS_CONVERTED
    )
    spam_count = sum(1 for lead in raw_leads if _lead_status_id(lead) == LEAD_STATUS_JUNK)
    necelevoy_count = sum(
        1 for lead in raw_leads if _lead_status_id(lead) == LEAD_STATUS_NECELEVOY
    )
    agent_count = sum(1 for lead in raw_leads if _lead_status_id(lead) == LEAD_STATUS_AGENT)
    audited_buyers = sum(1 for d in buyers_deals if d.get("audit_rule") is not None)

    lines = [
        "b24-ai-auditor v2 — ПРЕДПРОСМОТР (новые правила)",
        f"Дата: {now}",
        "",
        "═══════════════════════════════",
        "📊 СВОДКА",
        "═══════════════════════════════",
        f"Всего нарушений: {len(violations)}",
        f"Отделов: {len(dept_groups)}",
        f"Лидов в CRM: {len(raw_leads)}",
        f"  NEW (rule_1): {new_leads_count}",
        f"  Квалифицирован: {qualified_count}",
        f"  Спам: {spam_count}",
        f"  Нецелевой: {necelevoy_count}",
        f"  Агент: {agent_count}",
        f"  Общие Лиды (без аудита): {shared_leads_count}",
        f"Сделок покупателей: {len(buyers_deals)} (на аудите: {audited_buyers})",
        f"Сделок продавцов: {len(sellers_deals)}",
        "",
    ]

    for dept_name in sorted(dept_groups):
        dept_violations = _filter_zero_entity_violations(dept_groups[dept_name])
        if not dept_violations:
            continue

        lead_v = [v for v in dept_violations if v.get("entity_type") == "lead"]
        deal_v = [v for v in dept_violations if v.get("entity_type") == "deal"]

        dept_user_ids = {
            uid for uid, display in user_map.items() if f"({dept_name})" in display
        }
        violator_ids = {_coerce_int(v.get("responsible_id", 0)) for v in dept_violations}
        clean_in_dept = dept_user_ids - violator_ids - inactive_users

        lines.extend([
            "═══════════════════════════════",
            f"🏢 ОТДЕЛ: {dept_name}",
            "═══════════════════════════════",
            (
                "Сотрудников с нарушениями: "
                f"{len({v.get('responsible_id', 0) for v in dept_violations})}"
            ),
            f"Всего нарушений: {len(dept_violations)}",
            f"  Лиды: {len(lead_v)}",
            f"  Сделки: {len(deal_v)}",
            f"  ✅ Без нарушений: {len(clean_in_dept)}",
            "",
        ])

        for v in dept_violations:
            if "missed_callback" in str(v.get("rule", "")):
                continue
            lines.extend(_format_violation_line(
                v, user_map,
                buyers_deals=buyers_deals,
                sellers_deals=sellers_deals,
                raw_leads=raw_leads,
            ))

        call_violations = [
            v for v in dept_violations
            if "missed_callback" in str(v.get("rule", ""))
        ]
        if call_violations:
            lines.append("")
            lines.append("📞 ЗВОНКИ:")
            for v in call_violations:
                uid = _coerce_int(v.get("responsible_id", 0))
                name_only = user_map.get(uid, f"ID:{uid}").split(" (")[0]
                entity_type = str(v.get("entity_type", "?"))
                entity_id = _coerce_int(v.get("entity_id", 0))
                link = _build_crm_link(entity_type, entity_id)
                lines.append(f"   • {name_only} — {link}")
        lines.append("")

    return "\n".join(lines)


async def async_main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)

    # Audit without dispatcher sending to any chat
    audit_settings = settings.model_copy(update={"dry_run": True})

    logger.info("Running audit for full report preview (no auto-send)...")
    state = await run_audit_v2(audit_settings)
    report = await build_full_report(state)

    print(f"Report length: {len(report)} chars")
    print(report[:2000])
    if len(report) > 2000:
        print("...")

    chunks = send_chat_message_chunked(settings.report_chat_id, report)
    logger.info(
        "Full report sent to chat %d (%d chunks)",
        settings.report_chat_id,
        chunks,
    )
    print(f"Sent to chat {settings.report_chat_id}, {chunks} chunk(s)")


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
