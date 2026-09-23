"""Generate and send weekly broker performance report."""

from datetime import datetime, timedelta, timezone
import logging
import sys
from pathlib import Path

# Ensure src/ is on sys.path when running as script
_SRC_DIR = Path(__file__).resolve().parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from db import (  # noqa: E402
    get_previous_rating_snapshot,
    get_resolution_stats,
    get_weekly_stats,
    init_db,
    purge_test_violations,
)
from notify import send_chat_message_chunked, _bx_call_sync  # noqa: E402
from config import get_settings, quiet_http_clients  # noqa: E402
from broker_rating import (  # noqa: E402
    compute_all_ratings,
    format_rating_leaderboard,
    get_rating_period,
    persist_ratings_snapshot,
)
from broker_rating_collectors import (  # noqa: E402
    fetch_all_broker_tasks,
    fetch_important_feed_posts,
)

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


def filter_active_brokers(
    violators: list[dict],
    clean: list[dict],
) -> tuple[list[dict], list[dict]]:
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
        [
            row for row in violators
            if (row.get("department") or "").strip() not in EXCLUDED_WEEKLY_DEPARTMENTS
        ],
        [
            row for row in clean
            if (row.get("department") or "").strip() not in EXCLUDED_WEEKLY_DEPARTMENTS
        ],
    )


def _get_rule_advice() -> dict[str, str]:
    settings = get_settings()
    advice = settings.rules_advice
    if not advice:
        # Fallback to defaults
        return {
            "lead_rule_1": (
                "обратить внимание на скорость квалификации лидов "
                "(статус «Новый» > 2 часов)"
            ),
            "lead_rule_2": "указывать причину перевода лида в «Спам» (JUNK)",
            "lead_rule_3": "указывать причину перевода лида в нецелевые",
            "lead_missed_callback": "не пропускать входящие звонки по лидам без обратного",
            "buyer_stage_1": (
                "не задерживать сделки на этапе «Первый контакт» "
                "более 1 дня (стадия снята)"
            ),
            "buyer_stage_2": "не держать сделки на этапе «Подбор» более 2 дней без комментария",
            "buyer_stage_3": (
                "на «Первый показ» и «Повторный показ» — живое дело "
                "с датой не дальше 14 дней от этапа"
            ),
            "buyer_stage_4": "устаревшее правило (стадии Переговоры/Дожим сняты с аудита)",
            "buyer_stage_5": (
                "на этапе «Отложенный спрос» держать запланированное дело "
                "в карточке сделки"
            ),
            "buyer_podbor_stale": "не держать сделки на этапе «Подбор» более 7 дней",
            "buyer_ofer_comment": (
                "на этапе «Офер» оставлять развёрнутый комментарий "
                "(от 30 символов)"
            ),
            "buyer_lost_no_reason": "на «Сделка проиграна» нужен комментарий брокера/РОПа или дело",
            "buyer_agent_no_comment": "при переводе в «Агент» оставлять комментарий",
            "buyer_missed_callback": "не пропускать входящие звонки по сделкам без обратного",
            "lead_new_over_24h": (
                "квалифицировать лид «Новый» за 24 часа, "
                "иначе он уйдёт в «Общие лиды»"
            ),
            "seller_stage_stale": (
                "на этапах воронки Продавцы оставлять комментарий "
                "(на «Подготовке в рекламу» — комментарий или дело) "
                "в срок регламента"
            ),
            "seller_deferred_no_activity": (
                "на этапе «Отложенная продажа» держать запланированное дело"
            ),
            "seller_negotiations_max": "не держать сделки на «Переговорах» более 14 дней",
            "seller_lost_no_reason": (
                "на «Сделка проиграна» нужен комментарий брокера/РОПа или дело"
            ),
            "seller_afina_id_missing": "на «Закрытая продажа» и «Поиск клиента» заполнять ID Афины",
            "general_base_no_plan": (
                "в «Общей базе» за 2 дня после переноса запланировать дело "
                "или написать комментарий с планом дальнейших действий"
            ),
        }
    return advice


def build_rating_section(
    dept_name: str | None = None,
    dept_id: int | None = None,
    full_list: bool = True,
) -> tuple[str, list]:
    """Build rating leaderboard text and ratings list."""
    from broker_rating_collectors import list_eligible_brokers_for_rating

    since_iso, until_iso, since_dt, until_dt = get_rating_period()
    settings = get_settings()
    important_posts = fetch_important_feed_posts(since_iso, settings)
    brokers = list_eligible_brokers_for_rating(settings)
    if dept_id is not None:
        brokers = [b for b in brokers if b.get("department_id") == dept_id]
    elif dept_name:
        brokers = [
            b for b in brokers
            if dept_name.lower() in (b.get("department") or "").lower()
        ]

    broker_ids = [int(b["responsible_id"]) for b in brokers]
    tasks_by_broker = fetch_all_broker_tasks(broker_ids) if broker_ids else {}

    ratings = compute_all_ratings(
        since_iso,
        until_iso,
        brokers=brokers,
        tasks_by_broker=tasks_by_broker,
        important_posts=important_posts,
        settings=settings,
    )

    prev = get_previous_rating_snapshot(until_dt.strftime("%Y-%m-%d"))
    text = format_rating_leaderboard(
        ratings,
        since_dt,
        until_dt,
        prev_scores=prev,
        dept_name=dept_name,
        full_list=full_list,
    )
    return text, ratings


def format_resolution_section(stats: dict) -> str:
    """Lifecycle block: how fast found problems actually get fixed.

    Counting violations alone cannot distinguish a team that fixes everything
    same-day from one that lets the same cards rot — both show the same number.
    """
    opened = int(stats.get("opened") or 0)
    resolved = int(stats.get("resolved") or 0)
    still_open = int(stats.get("still_open") or 0)
    median = float(stats.get("median_hours_to_fix") or 0.0)

    if not opened and not resolved and not still_open:
        return ""

    if median >= 24:
        speed = f"{median / 24:.1f} дн."
    else:
        speed = f"{median:.0f} ч"

    lines = [
        "⏱ Исправление нарушений",
        f"Появилось за период: {opened}",
        f"Исправлено за период: {resolved}",
        f"Остаётся открытыми: {still_open}",
    ]
    if resolved:
        lines.append(f"Медианное время до исправления: {speed}")
    return "\n".join(lines)


def format_weekly_report(
    violators: list[dict],
    clean: list[dict],
    week_start: str,
    now: datetime,
    dept_name: str = None,
    prev_total_violations: int = 0,
    rating_section: str | None = None,
    resolution_section: str | None = None,
) -> str:
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
    ]

    if resolution_section:
        lines.extend([resolution_section, ""])

    if rating_section:
        lines.extend([
            rating_section,
            "",
            "═══════════════════════════════",
            "",
        ])

    lines.extend([
        "═══════════════════════════════",
        f"🔴 БРОКЕРЫ С НАРУШЕНИЯМИ ({len(violators)})",
        "═══════════════════════════════",
        ""
    ])

    total_violations = 0
    for i, v in enumerate(violators, 1):
        total_violations += v['total_violations']
        # If dept_name is set, we don't need to show department for each broker
        dept_str = "" if dept_name else (f" ({v['department']})" if v['department'] else "")
        lines.append(
            f"{i}. 🔴 {v['responsible_name']}{dept_str} — "
            f"{v['total_violations']} нарушений"
        )

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
        lines.append(
            f"{i}. 🟢 {c['responsible_name']}{dept_str} — "
            f"{c['lead_count']} лидов, {c['deal_count']} сделок"
        )

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
            lines.append(
                f"По типам: 📋лиды {total_lead} "
                f"({(total_lead/total_violations)*100:.0f}%), "
                f"🏠сделки {total_deal} "
                f"({(total_deal/total_violations)*100:.0f}%), "
                f"📞звонки {total_missed} "
                f"({(total_missed/total_violations)*100:.0f}%)"
            )

    return "\n".join(lines)


def main():
    init_db()
    quiet_http_clients()
    settings = get_settings()
    # purge_test_violations() удаляет строки из violations/audit_runs. Генератор
    # отчёта не должен чистить базу как побочный эффект — только по явному флагу
    # и никогда в DRY_RUN. Ручной запуск: python src/weekly_report.py --purge
    if "--purge" in sys.argv and not settings.dry_run:
        removed = purge_test_violations()
        print(f"Purged {removed} non-routine violations")
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

    rating_text, all_ratings = build_rating_section()
    snapshot_date = now.strftime("%Y-%m-%d")
    if all_ratings:
        persist_ratings_snapshot(all_ratings, snapshot_date)

    resolution_text = format_resolution_section(
        get_resolution_stats(week_start, week_end),
    )

    # 1. Main report (all departments)
    main_report = format_weekly_report(
        violators, clean, week_start, now,
        prev_total_violations=prev_total,
        rating_section=rating_text,
        resolution_section=resolution_text,
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

        dept_rating_text, _ = build_rating_section(dept_name=dept_name, dept_id=dept_id)

        dept_report = format_weekly_report(
            dept_violators, dept_clean, week_start, now, dept_name=dept_name,
            prev_total_violations=dept_prev_total,
            rating_section=dept_rating_text,
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
