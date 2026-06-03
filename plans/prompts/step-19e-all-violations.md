# Промпт для Cursor — Все нарушения + авто-чанкинг сообщений

> Скопируйте содержимое ниже и отправьте в Cursor (Agent mode).
> Обновляет: `src/graph.py` (только `_format_report`).

**Проблема:** топ-10 обрезает 789 нарушений. Нужно ВСЕ.

**Решение:** компактный формат (1 строка = 1 нарушение) + авто-разбивка на сообщения по ~2000 символов.

---

## Задача: Заменить `_format_report` на новую версию

Заменить ВСЮ функцию `_format_report`:

```python
def _format_and_send(
    bx: Any,
    title: str,
    violations: list[dict],
    now: str,
    extra: str,
    entity_count: int,
) -> int:
    """Format violations into compact messages and send to user #154.

    Args:
        bx: Bitrix24 client instance.
        title: Report title (Лиды / Сделки Покупатели / etc.).
        violations: Violations to report.
        now: Timestamp string.
        extra: Extra info line.
        entity_count: Total entities in this category.

    Returns:
        Number of messages sent.
    """
    if not violations:
        msg = (
            f"b24-ai-auditor v2 — {title}\n"
            f"Дата: {now}\n\n"
            f"{extra}\n"
            f"Нарушений не найдено."
        )
        bx.call("im.notify.personal.add", {"USER_ID": 154, "MESSAGE": msg[:2000]})
        return 1

    very_high = [v for v in violations if v.get("severity") == "very high"]
    high = [v for v in violations if v.get("severity") == "high"]
    medium = [v for v in violations if v.get("severity") == "medium"]

    severity_order = {"very high": 0, "high": 1, "medium": 2}
    sorted_v = sorted(
        violations,
        key=lambda v: severity_order.get(v.get("severity", "medium"), 99),
    )

    # Header for first message
    header = (
        f"b24-ai-auditor v2 — {title}\n"
        f"Дата: {now}\n"
        f"{extra}\n"
        f"Нарушений: {len(violations)}"
    )
    if very_high:
        header += f" | 🔴🔴{len(very_high)}"
    if high:
        header += f" | 🔴{len(high)}"
    if medium:
        header += f" | 🟡{len(medium)}"
    header += "\n\n"

    # Compact format: one line per violation
    lines: list[str] = []
    for v in sorted_v:
        sev = v.get("severity", "?")
        icon = {"very high": "🔴🔴", "high": "🔴", "medium": "🟡"}.get(sev, "⚪")
        entity_type = v.get("entity_type", "?")
        entity_id = v.get("entity_id", 0)
        responsible = v.get("responsible_id", "?")
        reason = v.get("reason", "—")
        rule = v.get("rule", "?")
        link = _build_crm_link(entity_type, entity_id)

        # Compact line: icon, ID, responsible, rule, reason, link
        line = (
            f"{icon} {entity_type[0].upper()}#{entity_id} "
            f"Отв:#{responsible} [{rule}] {reason}\n"
            f"   {link}"
        )
        lines.append(line)

    # Chunk into messages of ~1800 chars (leaving room for header)
    MESSAGE_MAX = 1800
    messages_sent = 0

    current_chunk = header
    for line in lines:
        if len(current_chunk) + len(line) > MESSAGE_MAX:
            # Send current chunk
            bx.call(
                "im.notify.personal.add",
                {"USER_ID": 154, "MESSAGE": current_chunk[:2000]},
            )
            messages_sent += 1
            current_chunk = f"{title} (продолжение)\n\n" + line
        else:
            current_chunk += line + "\n"

    # Send final chunk
    if current_chunk.strip():
        bx.call(
            "im.notify.personal.add",
            {"USER_ID": 154, "MESSAGE": current_chunk[:2000]},
        )
        messages_sent += 1

    # Summary footer
    if messages_sent > 1:
        footer = (
            f"{title}: всего {len(violations)} нарушений "
            f"({messages_sent} сообщений). "
            f"Полный лог: logs/audit.log"
        )
        bx.call(
            "im.notify.personal.add",
            {"USER_ID": 154, "MESSAGE": footer[:2000]},
        )
        messages_sent += 1

    return messages_sent
```

---

## Задача 2: Обновить `report_dispatcher` — использовать `_format_and_send`

Заменить блок отправки в `report_dispatcher`:

**Было:**
```python
    reports = [
        ("Лиды", lead_report),
        ("Сделки (Покупатели)", buyer_report),
        ("Сделки (Продавцы)", seller_report),
    ]

    try:
        from fast_bitrix24 import Bitrix
        bx = Bitrix(settings.b24_webhook_url)
        for label, report in reports:
            msg = report[:2000]
            ...
```

**Стало:**
```python
    try:
        from fast_bitrix24 import Bitrix
        bx = Bitrix(settings.b24_webhook_url)

        # Report 1: Leads
        n1 = _format_and_send(
            bx, "Лиды", lead_violations, now,
            f"Всего лидов: {len(state.get('raw_leads', []))}",
            len(state.get("raw_leads", [])),
        )

        # Report 2: Buyer Deals
        n2 = _format_and_send(
            bx, "Сделки (Покупатели)", buyer_violations, now,
            f"Всего сделок: {len(state.get('raw_buyers_deals', []))}",
            len(state.get("raw_buyers_deals", [])),
        )

        # Report 3: Seller Deals (summary only)
        seller_count = len(state.get("raw_sellers_deals", []))
        seller_msg = (
            f"b24-ai-auditor v2 — Сделки (Продавцы)\n"
            f"Дата: {now}\n\n"
            f"Всего сделок: {seller_count}\n"
            f"Аналитики для продавцов не настроены."
        )
        bx.call(
            "im.notify.personal.add",
            {"USER_ID": 154, "MESSAGE": seller_msg[:2000]},
        )
        n3 = 1

        total_msgs = n1 + n2 + n3
        logger.info(
            "Dispatcher: %d reports sent in %d messages",
            3, total_msgs,
        )
    except Exception as exc:
        logger.exception("Dispatcher: failed to send reports")
```

---

## Проверка

1. `.\make.cmd lint`
2. `.\make.cmd dry-run` — пользователь #154 получит:
   - Лиды: ~325 нарушений в ~8-10 сообщениях
   - Сделки Покупатели: ~464 нарушения в ~12-15 сообщениях
   - Сделки Продавцы: 1 сообщение (сводка)
   - В каждом сообщении — компактный формат со ссылками
