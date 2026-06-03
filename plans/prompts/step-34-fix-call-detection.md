# Промпт для Cursor — Правильная классификация звонков + фильтр по дате

> Скопируйте содержимое ниже и отправьте в Cursor (Agent mode).
> Обновляет: `src/tools.py`, `src/prompts.py`.

**Корень проблемы:** `COMPLETED != "Y"` → `status="missed"` для ВСЕХ звонков. Но для исходящих `COMPLETED=N` — это "не завершён", а не "пропущен".

---

## Задача 1: Исправить `_fetch_user_calls_for_audit` — правильная классификация

Заменить блок классификации:

**Было:**
```python
            status = "success" if completed == "Y" else "missed"
```

**Стало:**
```python
            # Правильная классификация:
            # - Входящий + завершён = success
            # - Входящий + не завершён = missed (пропущен)
            # - Исходящий + завершён = success
            # - Исходящий + не завершён = other (не пропущен!)
            if call_type == "incoming":
                status = "success" if completed == "Y" else "missed"
            elif call_type == "outgoing":
                status = "success" if completed == "Y" else "other"
            else:
                status = "success" if completed == "Y" else "other"
```

---

## Задача 2: Фильтр звонков по `REPORT_SINCE`

В `_fetch_user_calls_for_audit`, вместо `hours_ago`, использовать `REPORT_SINCE`:

```python
    settings = get_settings()
    if settings.report_since:
        date_from = settings.report_since + " 00:00:00"
    else:
        date_from = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S")
```

И в фильтре:
```python
    activity_filter["<=CREATED"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
```

---

## Задача 3: Упростить MISSED_CALLS_PROMPT и BUYER_CALLS_PROMPT

Теперь когда классификация правильная, упростить промпты:

```python
MISSED_CALLS_PROMPT = """\
Ты — контролёр пропущенных звонков.

На вход: {"leads": [...], "current_time": "<ISO>"}
Каждый лид: lead_id, title, assigned_by_id, calls.

calls: [{"call_id", "start_date", "status", "call_type"}]
status: "missed" (пропущенный входящий), "success", "other".
call_type: "incoming", "outgoing".

ПРАВИЛО: Для каждого лида, где есть звонок с status="missed":
проверь что после него (по start_date) есть звонок с call_type="outgoing"
от того же assigned_by_id. Если нет — НАРУШЕНИЕ (severity: very high).

Верни JSON с violations.
"""

BUYER_CALLS_PROMPT = """\
Ты — контролёр коммуникаций по сделкам.

На вход: {"deals": [...], "current_time": "<ISO>"}
Каждая сделка: deal_id, title, assigned_by_id, calls.

ПРАВИЛО: Для каждой сделки, где есть звонок с call_type="incoming":
проверь что после него (по start_date) есть звонок с call_type="outgoing"
от того же assigned_by_id. Если нет — НАРУШЕНИЕ (severity: very high).

Верни JSON с violations.
"""
```

---

## Проверка

1. `.\make.cmd lint`
2. `.\make.cmd dry-run` — ложные срабатывания должны исчезнуть, звонки фильтруются по REPORT_SINCE
