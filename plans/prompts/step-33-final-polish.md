# Промпт для Cursor — Дубликаты + статистика звонков + фильтр AUTHOR_ID

> Скопируйте содержимое ниже и отправьте в Cursor (Agent mode).
> Обновляет: `src/graph.py`, `src/tools.py`.

---

## Задача 1: Убрать дубликаты (report_sent с проверкой изменений)

В `report_dispatcher`, вместо полного удаления `report_sent`, хранить `last_violation_count`:

```python
    last_count = state.get("last_violation_count", -1)
    current_count = len(violations)
    if last_count == current_count and state.get("report_sent"):
        logger.info("Dispatcher: violations unchanged (%d), skipping", current_count)
        return state
```

В return добавить:
```python
        "last_violation_count": current_count,
```

И в AuditState добавить поле:
```python
    last_violation_count: int
```

---

## Задача 2: Статистика звонков по отделу

В `report_dispatcher`, для каждого отдела перед списком нарушений добавить подсчёт звонков:

```python
    # Count calls for this department
    dept_user_ids = set()
    for v in dept_violations:
        dept_user_ids.add(v.get("responsible_id", 0))

    incoming_total = 0
    outgoing_total = 0
    for entity_type, entities in [("lead", state.get("raw_leads", [])),
                                   ("deal", state.get("raw_buyers_deals", []))]:
        for e in entities:
            if e.get("assigned_by_id") in dept_user_ids:
                for call in e.get("calls", []):
                    if call.get("call_type") == "incoming":
                        incoming_total += 1
                    elif call.get("call_type") == "outgoing":
                        outgoing_total += 1

    stats_line = f"📞 Входящих: {incoming_total} | Исходящих: {outgoing_total}"
    # Insert stats_line after header
```

---

## Задача 3: Добавить AUTHOR_ID в данные о звонках

В `_fetch_user_calls_for_audit`, добавить `AUTHOR_ID` в select и в выходные данные:

```python
"select": ["ID", "DIRECTION", "COMPLETED", "CREATED", "SUBJECT", "AUTHOR_ID", "RESPONSIBLE_ID"],
```

И в выходном словаре:
```python
calls.append({
    ...
    "author_id": _coerce_int(item.get("AUTHOR_ID")),
    "responsible_id": _coerce_int(item.get("RESPONSIBLE_ID")),
})
```

---

## Задача 4: Уточнить BUYER_CALLS_PROMPT — проверять AUTHOR_ID

Добавить в описание звонков: `author_id` (кто совершил звонок).
И в правиле: исходящий звонок должен быть от того же `author_id` что и ответственный за сделку.

---

## Проверка

1. `.\make.cmd lint`
2. `.\make.cmd dry-run` — без дубликатов, со статистикой звонков
