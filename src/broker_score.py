"""Broker scorecard: CRM violations + owner KPI over 30 days."""

import asyncio
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Ensure src/ is on sys.path when running as script
_SRC_DIR = Path(__file__).resolve().parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from fast_bitrix24 import Bitrix
from config import get_settings
from db import get_connection, init_db

logger = logging.getLogger(__name__)


# ── Данные ─────────────────────────────────────────────
def get_broker_info(broker_id: int) -> dict | None:
    """Имя и отдел из таблицы brokers."""
    try:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT responsible_id, responsible_name, department "
                "FROM brokers WHERE responsible_id = ?",
                (broker_id,)
            ).fetchone()
            if row:
                return {
                    "id": row[0],
                    "name": row[1] or f"ID: {row[0]}",
                    "department": row[2] or "Без отдела"
                }
    except Exception as e:
        logger.error(f"DB Error getting broker info: {e}")
    return None


def get_violations(broker_id: int, since_date: str) -> dict:
    """Нарушения CRM за период."""
    result = {"total": 0, "by_rule": {}, "by_type": {"lead": 0, "deal": 0, "missed_call": 0}}
    try:
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT entity_type, rule "
                "FROM violations "
                "WHERE responsible_id = ? AND detected_at >= ?",
                (broker_id, since_date)
            ).fetchall()

            result["total"] = len(rows)
            for entity_type, rule in rows:
                if rule not in result["by_rule"]:
                    result["by_rule"][rule] = 0
                result["by_rule"][rule] += 1

                # Classify type
                if "missed_callback" in rule:
                    result["by_type"]["missed_call"] += 1
                elif entity_type == "lead":
                    result["by_type"]["lead"] += 1
                elif entity_type == "deal":
                    result["by_type"]["deal"] += 1

    except Exception as e:
        logger.error(f"DB Error getting violations: {e}")
    return result


async def get_owner_contacts(bx: Bitrix, broker_id: int, since_date: str, type_id: str) -> dict:
    """Контакты-собственники за период."""
    result = {"total": 0, "contacts": []}
    try:
        contacts = await bx.get_all("crm.contact.list", {
            "filter": {
                "ASSIGNED_BY_ID": broker_id,
                ">=DATE_CREATE": since_date,
                "TYPE_ID": type_id
            },
            "select": ["ID", "NAME", "LAST_NAME", "DATE_CREATE"]
        })
        result["total"] = len(contacts)
        for c in contacts:
            name = ((c.get("NAME") or "") + " " + (c.get("LAST_NAME") or "")).strip() or "Без имени"
            d_str = c.get("DATE_CREATE")
            if d_str:
                try:
                    d_parsed = datetime.fromisoformat(d_str).strftime("%d.%m.%Y")
                except Exception:
                    d_parsed = d_str[:10]
            else:
                d_parsed = "неизвестно"

            result["contacts"].append({"name": name, "date": d_parsed})

    except Exception as e:
        logger.error(f"API Error getting contacts: {e}")
    return result


# ── Оценка ─────────────────────────────────────────────
def score_broker(violations: dict, owners: dict, kpi_target: int) -> dict:
    """Рассчитать оценку: green/yellow/red + рекомендации."""
    v_total = violations["total"]
    o_total = owners["total"]

    # Violations score
    if v_total <= 2:
        v_score = "🟢 Хорошо"
        v_color = "green"
    elif v_total <= 5:
        v_score = "🟡 Средне"
        v_color = "yellow"
    else:
        v_score = "🔴 Плохо"
        v_color = "red"

    # Owners score
    if o_total >= kpi_target:
        o_score = "🟢 Хорошо"
        o_color = "green"
    elif o_total >= kpi_target / 2:
        o_score = "🟡 Средне"
        o_color = "yellow"
    else:
        o_score = "🔴 Плохо"
        o_color = "red"

    # Overall score
    if v_color == "green" and o_color == "green":
        overall = "🟢 ОТЛИЧНО"
    elif v_color == "red" and o_color == "red":
        overall = "🔴 ПЛОХО"
    else:
        overall = "🟡 СРЕДНЕ"

    # Recommendations (from Settings.rules_advice / RULES_ADVICE_JSON)
    advice_map = get_settings().rules_advice
    advices = []
    for rule in violations["by_rule"].keys():
        advice = advice_map.get(rule)
        if advice and advice not in advices:
            advices.append(advice)

    if o_total < kpi_target:
        advices.append(f"Увеличить количество добавляемых собственников (осталось {kpi_target - o_total} до KPI)")

    return {
        "overall": overall,
        "violations": {"text": f"{v_total} ({v_score.split()[1].lower()})", "color": v_color},
        "owners": {"text": f"{o_total} ({o_score.split()[1].lower()})", "color": o_color},
        "advices": advices
    }


# ── Форматирование ─────────────────────────────────────
def format_scorecard(broker: dict, violations: dict, owners: dict,
                     score: dict, since_date: str, now: datetime, kpi_target: int, prev_week_owners: int = 0) -> str:
    """Сформировать текстовый отчёт."""
    lines = [
        "b24-ai-auditor — Скоринг брокера",
        f"Дата: {now.strftime('%d.%m.%Y')}",
        "",
        f"👤 {broker['name']} ({broker['department']})",
        "═══════════════════════════════",
        "",
        "📋 НАРУШЕНИЯ CRM (за 30 дней)"
    ]

    # Violations details
    if violations["total"] == 0:
        lines.append("  ✅ Нет нарушений")
    else:
        if violations["by_type"]["lead"] > 0:
            lines.append(f"  Лиды: {violations['by_type']['lead']}")
            for rule, count in violations["by_rule"].items():
                if "lead" in rule and "missed_callback" not in rule:
                    lines.append(f"    • {rule}: {count} раз(а)")

        if violations["by_type"]["deal"] > 0:
            lines.append(f"  Сделки: {violations['by_type']['deal']}")
            for rule, count in violations["by_rule"].items():
                if "buyer" in rule and "missed_callback" not in rule:
                    lines.append(f"    • {rule}: {count} раз(а)")

        if violations["by_type"]["missed_call"] > 0:
            lines.append(f"  Звонки: {violations['by_type']['missed_call']}")
            for rule, count in violations["by_rule"].items():
                if "missed_callback" in rule:
                    lines.append(f"    • {rule}: {count} раз(а)")

    lines.append("")
    lines.append("🏠 НОВЫЕ СОБСТВЕННИКИ (за 30 дней)")
    lines.append(f"  Добавлено: {owners['total']}")

    pct = int(owners['total'] / kpi_target * 100) if kpi_target else 0
    if owners['total'] >= kpi_target:
        lines.append(f"  KPI ({kpi_target}/мес): ✅ выполнено ({pct}%)")
    else:
        lines.append(f"  KPI ({kpi_target}/мес): ❌ не выполнено ({pct}%)")

    if prev_week_owners > 0:
        delta = owners["total"] - prev_week_owners
        if delta > 0:
            lines.append(f"  Динамика: ↑ +{delta} за неделю")
        elif delta < 0:
            lines.append(f"  Динамика: ↓ {delta} за неделю")

    lines.extend([
        "",
        "═══════════════════════════════",
        f"📊 ОЦЕНКА: {score['overall']}",
        "═══════════════════════════════",
        f"🔴 Нарушения: {score['violations']['text']}",
        f"🟢 Собственники: {score['owners']['text']}",
    ])

    if score["advices"]:
        lines.append("⚠️ Рекомендации:")
        for adv in score["advices"]:
            lines.append(f"  • {adv.capitalize()}")

    return "\n".join(lines)


async def build_scorecard_for_broker(
    broker_id: int,
    bx: Bitrix,
    settings,
) -> str | None:
    """Build scorecard report for a broker. Returns report string or None on error."""
    now = datetime.now(timezone.utc)
    since_date = (now - timedelta(days=30)).strftime("%Y-%m-%d")
    
    broker = get_broker_info(broker_id)
    if not broker:
        try:
            users = await bx.get_all("user.get", {"FILTER": {"ID": broker_id}})
            if users:
                u = users[0]
                name = ((u.get("NAME") or "") + " " + (u.get("LAST_NAME") or "")).strip() or f"ID: {broker_id}"
                broker = {"id": broker_id, "name": name, "department": "API (отдел неизвестен)"}
            else:
                return None
        except Exception as e:
            logger.error(f"Error fetching user from API: {e}")
            return None

    violations = get_violations(broker_id, since_date)
    owners = await get_owner_contacts(bx, broker_id, since_date, settings.contact_owner_type_id)
    
    prev_since_date = (now - timedelta(days=37)).strftime("%Y-%m-%d")
    prev_owners = await get_owner_contacts(bx, broker_id, prev_since_date, settings.contact_owner_type_id)
    prev_week_only = prev_owners["total"] - owners["total"]

    score = score_broker(violations, owners, settings.owner_kpi_target)
    return format_scorecard(broker, violations, owners, score, since_date, now, settings.owner_kpi_target, prev_week_owners=prev_week_only)


# ── Главная ────────────────────────────────────────────
async def async_main():
    if len(sys.argv) < 2:
        print("Usage: python src/broker_score.py <BROKER_ID>")
        sys.exit(1)

    try:
        broker_id = int(sys.argv[1])
    except ValueError:
        print("Error: BROKER_ID must be an integer")
        sys.exit(1)

    settings = get_settings()
    init_db()

    # Enable logging output to console if needed
    logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))

    bx = Bitrix(settings.b24_webhook_url)
    
    report = await build_scorecard_for_broker(broker_id, bx, settings)
    if not report:
        error_msg = f"❌ Брокер с ID {broker_id} не найден ни в БД, ни в API"
        print(error_msg)
        if not settings.dry_run:
            from notify import send_chat_message_chunked
            send_chat_message_chunked(settings.report_chat_id, error_msg)
        return

    print("\n" + report + "\n")

    # Отправка в чат
    if str(settings.dry_run).lower() != "true" and settings.dry_run is not True:
        logger.info("Sending report to chat %s", settings.report_chat_id)
        from notify import send_chat_message_chunked
        send_chat_message_chunked(settings.report_chat_id, report)
    else:
        logger.info("DRY_RUN=True, skipping sending message.")


def main():
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
