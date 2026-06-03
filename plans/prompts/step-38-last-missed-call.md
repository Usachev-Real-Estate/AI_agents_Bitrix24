# Промпт для Cursor — Последний пропущенный + фильтр дат в статистике

> Скопируйте содержимое ниже и отправьте в Cursor (Agent mode).
> Обновляет: `src/prompts.py`, `src/graph.py`.

---

## Задача 1: Проверять только ПОСЛЕДНИЙ пропущенный звонок

В BUYER_CALLS_PROMPT и MISSED_CALLS_PROMPT заменить правило:

**Было:**
```
Для сделки где есть звонок с status="missed": проверь что после него есть исходящий...
```

**Стало:**
```
Найди САМЫЙ ПОСЛЕДНИЙ по start_date звонок с status="missed".
Проверь что после него (start_date позже) есть исходящий (call_type="outgoing").
Если нет — НАРУШЕНИЕ. Предыдущие пропущенные — игнорируй.
```

---

## Задача 2: Статистика звонков только за период REPORT_SINCE

В `report_dispatcher`, при подсчёте `📞 Входящих/Исходящих`, фильтровать звонки по дате:

```python
    settings = get_settings()
    since_date = settings.report_since if settings.report_since else "2000-01-01"
    
    for call in e.get("calls", []):
        call_date = str(call.get("start_date", ""))[:10]
        if call_date < since_date:
            continue  # skip calls before report period
        if call.get("call_type") == "incoming":
            incoming_total += 1
        elif call.get("call_type") == "outgoing":
            outgoing_total += 1
```

---

## Проверка

1. `.\make.cmd lint`
2. `.\make.cmd dry-run` — 1 нарушение на сущность (последний пропущенный), статистика только за период
