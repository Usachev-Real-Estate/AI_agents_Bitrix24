"""Generate and send weekly broker performance report."""

from datetime import datetime, timedelta, timezone
import logging
import sys
from pathlib import Path

# Ensure src/ is on sys.path when running as script
_SRC_DIR = Path(__file__).resolve().parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from db import get_weekly_stats, init_db, purge_test_violations
from notify import send_chat_message_chunked, _bx_call_sync
from config import get_settings

logger = logging.getLogger(__name__)
EXCLUDED_WEEKLY_DEPARTMENTS = {"ТО"}


def _is_active_user(user_id: int) -> bool:
    """Return True when Bitrix user is active; keep True on transient errors."""
    try:
        result = _bx_call_sync("user.get", {"ID": user_id})
        if isinstance(result, dict):
            if "ACTIVE" in result:
                active = result.get("ACTIVE", True)
                return str(active).upper() not in {"N", "FALSE", "0"}
            # Backward-compatible branch for wrapped responses.
            for value in result.values():
                if isinstance(value, list) and value and isinstance(value[0], dict):
                    active = value[0].get("ACTIVE", True)
                    return str(active).upper() not in {"N", "FALSE", "0"}
        if isinstance(result, list) and result and isinstance(result[0], dict):
            active = result[0].get("ACTIVE", True)
            return str(active).upper() not in {"N", "FALSE", "0"}
    except Exception as exc:
        logger.warning("Failed to resolve user ACTIVE flag for id=%s: %s", user_id, exc)
    return True


def filter_active_brokers(violators: list[dict], clean: list[dict]) -> tuple[list[dict], list[dict]]:
    """Exclude terminated users from report sections."""
    user_ids = {
        int(row.get("responsible_id") or 0)
        for row in (violators + clean)
        if int(row.get("responsible_id") or 0) > 0
    }
    active_map = {uid: _is_active_user(uid) for uid in user_ids}
    return (
        [row for row in violators if active_map.get(int(row.get("responsible_id") or 0), True)],
        [row for row in clean if active_map.get(int(row.get("responsible_id") or 0), True)],
    )


def filter_weekly_scope(violators: list[dict], clean: list[dict]) -> tuple[list[dict], list[dict]]:
    """Apply weekly report visibility rules."""
    violators, clean = filter_active_brokers(violators, clean)
    return (
        [row for row in violators if (row.get("department") or "").strip() not in EXCLUDED_WEEKLY_DEPARTMENTS],
        [row for row in clean if (row.get("department") or "").strip() not in EXCLUDED_WEEKLY_DEPARTMENTS],
    )


def _get_rule_advice() -> dict[str, str]:
    settings = get_settings()
    advice = settings.rules_advice
    if not advice:
        # Fallback to defaults
        return {
            "lead_rule_1": "обратить внимание на скорость квалификации лидов (статус «Новый» > 2 часов)",
            "lead_rule_2": "указывать причину перевода лида в «Спам» (JUNK)",
            "lead_rule_3": "указывать причину перевода лида в нецелевые",
            "lead_missed_callback": "не пропускать входящие звонки по лидам без обратного",
            "buyer_stage_1": "не задерживать сделки на этапе «Первый контакт» более 1 дня (стадия снята)",
            "buyer_stage_2": "не держать сделки на этапе «Подбор» более 2 дней без комментария",
            "buyer_stage_3": "на этапе «Показ» обязательно держать актуальное запланированное дело и будущую дату показа, иначе переносить сделку дальше",
            "buyer_stage_4": "не забывать комментировать сделки на этапах Переговоры / Дожим / Офер / Задаток / Сделка",
            "buyer_stage_5": "регулярно комментировать сделки в «Отложенном спросе»",
            "buyer_missed_callback": "не пропускать входящие звонки по сделкам без обратного",
        }
    return advice


def format_weekly_report(violators: list[dict], clean: list[dict], week_start: str, now: datetime, dept_name: str = None, prev_total_violations: int = 0) -> str:
    """Format the weekly report message."""
    start_date = datetime.fromisoformat(week_start).strftime("%d.%m.%Y")
    end_date = now.strftime("%d.%m.%Y")

    title = "b24-ai-auditor — Недельный отчёт по брокерам"
    if dept_name:
        title += f" (Отдел: {dept_name})"

    lines = [
        title,
        f"Отчёт сформирован с начала недели (с {start_date} по {end_date})",
        "",
        "═══════════════════════════════",
        f"🔴 БРОКЕРЫ С НАРУШЕНИЯМИ ({len(violators)})",
        "═══════════════════════════════",
        ""
    ]

    total_violations = 0
    for i, v in enumerate(violators, 1):
        total_violations += v['total_violations']
        # If dept_name is set, we don't need to show department for each broker
        dept_str = "" if dept_name else (f" ({v['department']})" if v['department'] else "")
        lines.append(f"{i}. 🔴 {v['responsible_name']}{dept_str} — {v['total_violations']} нарушений")

        if v['lead_violations'] > 0:
            lines.append(f"   📋 Лиды: {v['lead_violations']}")

        if v['missed_call_violations'] > 0:
            lines.append(f"   📞 Звонки: {v['missed_call_violations']}")

        if v['deal_violations'] > 0:
            lines.append(f"   🏠 Сделки: {v['deal_violations']}")

        # Add advices
        rules = str(v.get('rules') or "").split(',')
        advices_added = set()
        advice_map = _get_rule_advice()
        for rule in rules:
            if not rule:
                continue
            advice = advice_map.get(rule.strip())
            if advice and advice not in advices_added:
                lines.append(f"   ⚠️ Рекомендация: {advice}")
                advices_added.add(advice)
        lines.append("")

    lines.extend([
        "═══════════════════════════════",
        f"🟢 ЧИСТЫЕ БРОКЕРЫ ({len(clean)})",
        "═══════════════════════════════",
        "(без нарушений, с активными лидами/сделками)",
        ""
    ])

    for i, c in enumerate(clean, 1):
        dept_str = "" if dept_name else (f" ({c['department']})" if c['department'] else "")
        lines.append(f"{i}. 🟢 {c['responsible_name']}{dept_str} — {c['lead_count']} лидов, {c['deal_count']} сделок")

    lines.append("")
    lines.extend([
        "═══════════════════════════════",
        "📊 СВОДКА",
        "═══════════════════════════════",
    ])

    total_brokers = len(violators) + len(clean)
    lines.append(f"Всего брокеров с лидами/сделками: {total_brokers}")
    if total_brokers > 0:
        v_percent = (len(violators) / total_brokers) * 100
        c_percent = (len(clean) / total_brokers) * 100
        lines.append(f"С нарушениями: {len(violators)} ({v_percent:.0f}%)")
        lines.append(f"Чистых: {len(clean)} ({c_percent:.0f}%)")
    else:
        lines.append(f"С нарушениями: {len(violators)}")
        lines.append(f"Чистых: {len(clean)}")

    lines.append(f"Всего нарушений: {total_violations}")

    if prev_total_violations > 0 or total_violations > 0:
        delta = total_violations - prev_total_violations
        if delta > 0:
            trend = f"↑ +{delta} к прошлой неделе"
        elif delta < 0:
            trend = f"↓ {delta} к прошлой неделе"
        else:
            trend = "без изменений"
        lines.append(f"Динамика: {trend}")

    if dept_name:
        total_lead = sum(v['lead_violations'] for v in violators)
        total_deal = sum(v['deal_violations'] for v in violators)
        total_missed = sum(v['missed_call_violations'] for v in violators)
        if total_violations > 0:
            lines.append(f"По типам: 📋лиды {total_lead} ({(total_lead/total_violations)*100:.0f}%), 🏠сделки {total_deal} ({(total_deal/total_violations)*100:.0f}%), 📞звонки {total_missed} ({(total_missed/total_violations)*100:.0f}%)")

    return "\n".join(lines)


def main():
    init_db()
    purge_test_violations()
    settings = get_settings()
    now = datetime.now(timezone.utc)
    # Week-to-date: from Monday 00:00 UTC till now.
    week_start_dt = (now - timedelta(days=now.weekday())).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    week_start = week_start_dt.isoformat()
    week_end = now.isoformat()

    violators, clean = get_weekly_stats(week_start, week_end, latest_only=True)
    violators, clean = filter_weekly_scope(violators, clean)

    prev_week_start_dt = week_start_dt - timedelta(days=7)
    prev_week_start = prev_week_start_dt.isoformat()
    prev_week_end = week_start_dt.isoformat()
    prev_violators, _ = get_weekly_stats(prev_week_start, prev_week_end, latest_only=True)
    prev_violators, _ = filter_weekly_scope(prev_violators, [])
    prev_total = sum(v['total_violations'] for v in prev_violators)

    # 1. Main report (all departments)
    main_report = format_weekly_report(
        violators, clean, week_start, now,
        prev_total_violations=prev_total,
    )
    print("Generated MAIN report:")
    print(main_report)
    print("-" * 40)

    if not settings.dry_run:
        send_chat_message_chunked(settings.report_chat_id, main_report)
        print("MAIN Report sent to chat", settings.report_chat_id)
    else:
        print("DRY_RUN=True, skipping sending MAIN message.")

    # 2. Department reports
    dept_map = settings.dept_chat_map
    if not dept_map:
        print("No department mapping found, skipping department reports.")
        return

    # Group unique department IDs
    unique_depts = set()
    for v in violators + clean:
        if v.get("department_id"):
            unique_depts.add(v["department_id"])

    for dept_id in unique_depts:
        chat_id = dept_map.get(str(dept_id)) or dept_map.get(dept_id)
        if not chat_id:
            continue

        # Find department name
        dept_name = "Неизвестный отдел"
        for v in violators + clean:
            if v.get("department_id") == dept_id and v.get("department"):
                dept_name = v["department"]
                break

        dept_violators = [v for v in violators if v.get("department_id") == dept_id]
        dept_clean = [c for c in clean if c.get("department_id") == dept_id]

        if not dept_violators and not dept_clean:
            continue

        dept_prev_violators = [v for v in prev_violators if v.get("department_id") == dept_id]
        dept_prev_total = sum(v['total_violations'] for v in dept_prev_violators)

        dept_report = format_weekly_report(
            dept_violators, dept_clean, week_start, now, dept_name=dept_name,
            prev_total_violations=dept_prev_total,
        )
        print(f"Generated report for DEPT {dept_name} (chat {chat_id}):")
        print(dept_report)
        print("-" * 40)

        if not settings.dry_run:
            send_chat_message_chunked(chat_id, dept_report)
            print(f"Report for DEPT {dept_name} sent to chat {chat_id}")
        else:
            print(f"DRY_RUN=True, skipping sending DEPT {dept_name} message to {chat_id}.")


if __name__ == "__main__":
    main()
