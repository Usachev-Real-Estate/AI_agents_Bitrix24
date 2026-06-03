# Промпт для Cursor — Раздельные отчёты + фикс двойного dispatcher

> Скопируйте содержимое ниже и отправьте в Cursor (Agent mode).
> Обновляет: `src/graph.py`.

**Проблемы:**
1. Отчёт из 789 нарушений не влезает в 2000 символов → видна только первая часть
2. Dispatcher срабатывает дважды (fan-in в LangGraph) → violations дублируются

**Решение:**
1. Три раздельных отчёта: Лиды, Сделки (Покупатели), Сделки (Продавцы) — каждый ≤ 2000 символов
2. Флаг `report_sent` в AuditState → блокирует повторную отправку

---

## Задача 1: Добавить `report_sent` в AuditState

В `src/graph.py`, в `AuditState` добавить поле:

```python
class AuditState(TypedDict, total=False):
    ...
    report_sent: bool
```

---

## Задача 2: Заменить `report_dispatcher` на версию с 3 отчётами + флагом

Заменить ВСЮ функцию `report_dispatcher`:

```python
async def report_dispatcher(state: AuditState, settings: Settings) -> AuditState:
    """Split violations into 3 reports and send to user #154.

    Only sends once (report_sent flag prevents duplicate sends from fan-in).
    """
    if state.get("report_sent"):
        logger.info("Dispatcher: report already sent, skipping")
        return state

    violations = state.get("violations", [])
    logger.info("Dispatcher: formatting %d violations into 3 reports",
                len(violations))
    now = state.get("current_time", "")[:19]

    # Split violations by type
    lead_violations = [v for v in violations if v.get("entity_type") == "lead"]
    buyer_violations = [v for v in violations if v.get("entity_type") == "deal"]
    # Seller deals: no analysts yet, just count
    seller_deals_count = len(state.get("raw_sellers_deals", []))

    # --- Report 1: Leads ---
    lead_report = _format_report(
        title="Лиды",
        violations=lead_violations,
        now=now,
        extra=f"Всего лидов в CRM: {len(state.get('raw_leads', []))}",
    )

    # --- Report 2: Buyer Deals ---
    buyer_report = _format_report(
        title="Сделки (Покупатели)",
        violations=buyer_violations,
        now=now,
        extra=f"Всего сделок покупателей: {len(state.get('raw_buyers_deals', []))}",
    )

    # --- Report 3: Seller Deals (summary only, no analysts) ---
    seller_report = (
        f"b24-ai-auditor v2 — Сделки (Продавцы)\n"
        f"Дата: {now}\n\n"
        f"Всего сделок продавцов: {seller_deals_count}\n"
        f"Аналитики для продавцов ещё не настроены.\n"
    )

    reports = [
        ("Лиды", lead_report),
        ("Сделки (Покупатели)", buyer_report),
        ("Сделки (Продавцы)", seller_report),
    ]

    # Send each report
    try:
        from fast_bitrix24 import Bitrix
        bx = Bitrix(settings.b24_webhook_url)
        for label, report in reports:
            msg = report[:2000]
            if len(report) > 2000:
                msg = report[:1990] + "\n...[обрезано]"
            result = bx.call(
                "im.notify.personal.add",
                {"USER_ID": 154, "MESSAGE": msg},
            )
            logger.info("Dispatcher: %s report sent: %s", label, result)
    except Exception as exc:
        logger.exception("Dispatcher: failed to send reports")

    messages = list(state.get("messages", []))
    messages.append(
        f"dispatcher: 3 reports sent "
        f"(leads={len(lead_violations)}, "
        f"buyers={len(buyer_violations)}, "
        f"sellers={seller_deals_count})"
    )

    return {
        **state,
        "messages": messages,
        "status": "completed",
        "report_sent": True,
    }


def _format_report(
    title: str,
    violations: list[dict],
    now: str,
    extra: str = "",
) -> str:
    """Format a single-domain violations report.

    Args:
        title: Report section title.
        violations: List of violation dicts.
        now: Current timestamp string.
        extra: Optional extra info line.

    Returns:
        Formatted report string.
    """
    lines = [
        f"b24-ai-auditor v2 — {title}",
        f"Дата: {now}",
        "",
    ]

    if extra:
        lines.append(extra)

    if not violations:
        lines.append("Нарушений не найдено.")
        return "\n".join(lines)

    very_high = [v for v in violations if v.get("severity") == "very high"]
    high = [v for v in violations if v.get("severity") == "high"]
    medium = [v for v in violations if v.get("severity") == "medium"]

    lines.append(f"Нарушений: {len(violations)}")
    if very_high:
        lines.append(f"  🔴🔴 Критические: {len(very_high)}")
    if high:
        lines.append(f"  🔴 Высокие: {len(high)}")
    if medium:
        lines.append(f"  🟡 Средние: {len(medium)}")
    lines.append("")

    severity_order = {"very high": 0, "high": 1, "medium": 2}
    sorted_v = sorted(
        violations,
        key=lambda v: severity_order.get(v.get("severity", "medium"), 99),
    )

    # Show top 10, then summary
    show = sorted_v[:10]
    for i, v in enumerate(show, 1):
        sev = v.get("severity", "?")
        icon = {"very high": "🔴🔴", "high": "🔴", "medium": "🟡"}.get(sev, "⚪")
        entity_type = v.get("entity_type", "?")
        entity_id = v.get("entity_id", 0)
        responsible = v.get("responsible_id", "?")
        reason = v.get("reason", "—")

        link = _build_crm_link(entity_type, entity_id)

        lines.append(f"{i}. {icon} {entity_type.capitalize()} #{entity_id}")
        lines.append(f"   Отв.: #{responsible} | {reason}")
        lines.append(f"   {link}")
        lines.append("")

    if len(violations) > 10:
        lines.append(f"... и ещё {len(violations) - 10} нарушений.")
        lines.append(f"Полный лог: logs/audit.log")

    return "\n".join(lines)
```

---

## Задача 3: Обновить `run_audit_v2` — добавить `report_sent` в initial state

```python
initial: AuditState = {
    ...
    "report_sent": False,
}
```

---

## Проверка

1. `.\make.cmd lint`
2. `.\make.cmd dry-run` — пользователь #154 получит **3 отдельных уведомления**:
   - Лиды (325 нарушений, топ-10)
   - Сделки Покупатели (464 нарушения, топ-10)
   - Сделки Продавцы (сводка без нарушений)
3. Dispatcher сработает **только 1 раз** (флаг `report_sent`)
