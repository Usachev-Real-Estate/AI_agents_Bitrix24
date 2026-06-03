# Промпт для Cursor — v2 Фаза 3: Dispatcher (отчёт пользователю #154)

> Скопируйте содержимое ниже и отправьте в Cursor (Agent mode).
> Обновляет: `src/graph.py`, `src/main.py`.

**Суть:** после всех аналитиков — сформатировать violations в отчёт и отправить пользователю #154 через `im.notify`. Без создания задач.

---

## Задача 1: Добавить узел `report_dispatcher` в `src/graph.py`

Добавить ПОСЛЕ `missed_calls_controller`, ПЕРЕД `build_graph_v2`:

```python
async def report_dispatcher(state: AuditState, settings: Settings) -> AuditState:
    """Format violations and send report to user #154 via im.notify."""
    violations = state.get("violations", [])
    logger.info("Dispatcher: formatting report for %d violations", len(violations))

    # Format report
    now = state.get("current_time", "")
    report_lines = [
        "b24-ai-auditor v2 — сводка нарушений",
        f"Дата: {now[:19]}" if now else "",
        "",
    ]

    if not violations:
        report_lines.append("Нарушений не найдено.")
    else:
        # Count by severity
        high = [v for v in violations if v.get("severity") == "high"]
        very_high = [v for v in violations if v.get("severity") == "very high"]
        medium = [v for v in violations if v.get("severity") == "medium"]

        report_lines.append(f"Всего нарушений: {len(violations)}")
        report_lines.append(f"  Очень высокие: {len(very_high)}")
        report_lines.append(f"  Высокие: {len(high)}")
        report_lines.append(f"  Средние: {len(medium)}")
        report_lines.append("")

        # List all violations
        for v in violations:
            sev = v.get("severity", "?")
            icon = {"very high": "🔴🔴", "high": "🔴", "medium": "🟡"}.get(sev, "⚪")
            entity_type = v.get("entity_type", "?")
            entity_id = v.get("entity_id", "?")
            responsible = v.get("responsible_id", "?")
            rule = v.get("rule", "?")
            reason = v.get("reason", "—")
            report_lines.append(
                f"{icon} [{rule}] {entity_type}#{entity_id} | "
                f"Ответственный: #{responsible} | {reason}"
            )

    report = "\n".join(report_lines)

    # Truncate to 2000 chars (Bitrix24 limit)
    if len(report) > 2000:
        report = report[:1990] + "\n...[обрезано]"

    # Send to user #154 via im.notify
    try:
        from fast_bitrix24 import Bitrix
        bx = Bitrix(settings.b24_webhook_url)
        result = bx.call(
            "im.notify",
            {
                "to": 154,
                "message": report,
                "type": "SYSTEM",
            },
        )
        logger.info("Dispatcher: report sent to user #154: %s", result)
    except Exception as exc:
        logger.exception("Dispatcher: failed to send report to user #154")

    messages = list(state.get("messages", []))
    messages.append(f"dispatcher: report sent ({len(violations)} violations)")

    return {
        **state,
        "messages": messages,
        "status": "completed",
    }
```

---

## Задача 2: Обновить `build_graph_v2()` — добавить `report_dispatcher`

Заменить ВСЕ три `seller_collector`, `missed_calls_controller`, `buyer_calls_controller → END` на слияние через `report_dispatcher`:

```python
def build_graph_v2(settings: Settings):
    """Build v2 audit graph: 3 collectors → 4 analysts → report dispatcher.

    Args:
        settings: Application settings.

    Returns:
        Compiled LangGraph runnable.
    """
    graph = StateGraph(AuditState)

    # Wrappers
    async def lead_col(state: AuditState) -> AuditState:
        return await lead_collector(state, settings)

    async def buyer_col(state: AuditState) -> AuditState:
        return await buyer_collector(state, settings)

    async def seller_col(state: AuditState) -> AuditState:
        return await seller_collector(state, settings)

    async def lead_an(state: AuditState) -> AuditState:
        return await lead_analyst(state, settings)

    async def buyer_deal_an(state: AuditState) -> AuditState:
        return await buyer_deal_analyst(state, settings)

    async def buyer_calls(state: AuditState) -> AuditState:
        return await buyer_calls_controller(state, settings)

    async def missed_calls(state: AuditState) -> AuditState:
        return await missed_calls_controller(state, settings)

    async def dispatcher(state: AuditState) -> AuditState:
        return await report_dispatcher(state, settings)

    # Nodes
    graph.add_node("lead_collector", lead_col)
    graph.add_node("buyer_collector", buyer_col)
    graph.add_node("seller_collector", seller_col)
    graph.add_node("lead_analyst", lead_an)
    graph.add_node("buyer_deal_analyst", buyer_deal_an)
    graph.add_node("buyer_calls_controller", buyer_calls)
    graph.add_node("missed_calls_controller", missed_calls)
    graph.add_node("report_dispatcher", dispatcher)

    # Parallel collectors from START
    graph.add_edge(START, "lead_collector")
    graph.add_edge(START, "buyer_collector")
    graph.add_edge(START, "seller_collector")

    # Collector → Analyst chains
    graph.add_edge("lead_collector", "lead_analyst")
    graph.add_edge("lead_analyst", "missed_calls_controller")
    graph.add_edge("missed_calls_controller", "report_dispatcher")

    graph.add_edge("buyer_collector", "buyer_deal_analyst")
    graph.add_edge("buyer_deal_analyst", "buyer_calls_controller")
    graph.add_edge("buyer_calls_controller", "report_dispatcher")

    graph.add_edge("seller_collector", "report_dispatcher")

    # Dispatcher → END
    graph.add_edge("report_dispatcher", END)

    return graph.compile()
```

---

## Задача 3: Обновить `run_audit_v2()` — логировать violations и статус

```python
async def run_audit_v2(settings: Settings) -> AuditState:
    """Run v2 audit: 3 collectors → 4 analysts → report dispatcher.

    Args:
        settings: Application settings.

    Returns:
        Final AuditState with violations and status.
    """
    from datetime import datetime, timezone

    app = build_graph_v2(settings)
    initial: AuditState = {
        "raw_leads": [],
        "raw_buyers_deals": [],
        "raw_sellers_deals": [],
        "violations": [],
        "current_time": datetime.now(timezone.utc).isoformat(),
        "dry_run": settings.dry_run,
        "messages": [],
    }
    return await app.ainvoke(initial)
```

---

## Задача 4: Обновить лог в `main.py`

```python
result = await run_audit_v2(settings)
logger.info(
    "Audit V2 finished: status=%s, leads=%d, buyer_deals=%d, "
    "seller_deals=%d, violations=%d",
    result.get("status"),
    len(result.get("raw_leads", [])),
    len(result.get("raw_buyers_deals", [])),
    len(result.get("raw_sellers_deals", [])),
    len(result.get("violations", [])),
)
```

---

## Проверка

1. `.\make.cmd lint` — должен пройти
2. `.\make.cmd dry-run` — полный пайплайн:
   - 3 коллектора → 4 аналитика (с чанкингом) → report_dispatcher
3. В логах: `Dispatcher: report sent to user #154: ...`
4. Пользователь #154 получает уведомление в Битрикс24
