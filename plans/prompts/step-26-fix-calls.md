# Промпт для Cursor — Звонки: искать в поле calls, а не в timeline

> Скопируйте содержимое ниже и отправьте в Cursor (Agent mode).
> Обновляет: `src/prompts.py`.

**Проблема:** промпты ищут звонки в `timeline` (текстовые комментарии), а звонки лежат в поле `calls` (данные из `voximplant.statistic.get`). Агенты 6/7 находят 0 нарушений.

---

## Задача 1: BUYER_CALLS_PROMPT — заменить

```python
BUYER_CALLS_PROMPT = """\
Ты — контролёр качества коммуникаций в сделках воронки "Покупатели".

На вход подаётся JSON: {"deals": [...], "current_time": "<ISO>"}
Каждая сделка: deal_id, title, assigned_by_id, calls.

calls — массив звонков. Каждый звонок: call_id, duration, start_date, status, call_type.
status: "success" (успешный), "missed" (пропущенный), "other".
call_type: "incoming" (входящий), "outgoing" (исходящий), "unknown".

Найди нарушения. Severity: very high.

═══════════════════════════════════════
ПРАВИЛО — Входящий вызов без обратной связи
Severity: very high
═══════════════════════════════════════

Шаг 1: Найди в calls звонки с call_type="incoming" (входящие).

Шаг 2: Для каждого входящего звонка проверь:
Есть ли ПОЗЖЕ по start_date:
- Звонок с call_type="outgoing" (исходящий) от того же пользователя
- ИЛИ звонок с status="success" от того же пользователя

Шаг 3: Если после входящего нет ни исходящего, ни успешного — НАРУШЕНИЕ.

details: deal_id, title, incoming_call_date (str), has_outgoing_or_success (bool)

═══════════════════════════════════════
ФОРМАТ ОТВЕТА — СТРОГО JSON
═══════════════════════════════════════
{
    "violations": [
        {
            "entity_type": "deal",
            "entity_id": <deal_id>,
            "responsible_id": <assigned_by_id>,
            "severity": "very high",
            "rule": "buyer_no_callback",
            "reason": "<описание>",
            "details": {...}
        }
    ]
}

Верни ТОЛЬКО JSON.
"""
```

---

## Задача 2: MISSED_CALLS_PROMPT — заменить

```python
MISSED_CALLS_PROMPT = """\
Ты — контролёр пропущенных звонков по лидам.

На вход подаётся JSON: {"leads": [...], "current_time": "<ISO>"}
Каждый лид: lead_id, title, assigned_by_id, calls.

calls — массив звонков. Каждый звонок: call_id, duration, start_date, status, call_type.
status: "success" (успешный), "missed" (пропущенный), "other".
call_type: "incoming" (входящий), "outgoing" (исходящий), "unknown".

Найди нарушения. Severity: very high.

═══════════════════════════════════════
ПРАВИЛО — Пропущенный без обратного звонка
Severity: very high
═══════════════════════════════════════

Шаг 1: Найди в calls звонки с status="missed" (пропущенные).

Шаг 2: Для каждого пропущенного звонка проверь:
Есть ли ПОЗЖЕ по start_date:
- Звонок с call_type="outgoing" (исходящий) от того же пользователя
- ИЛИ звонок с status="success" от того же пользователя

Шаг 3: Если после пропущенного нет ни исходящего, ни успешного — НАРУШЕНИЕ.

details: lead_id, title, missed_call_date (str), has_callback (bool)

═══════════════════════════════════════
ФОРМАТ ОТВЕТА — СТРОГО JSON
═══════════════════════════════════════
{
    "violations": [
        {
            "entity_type": "lead",
            "entity_id": <lead_id>,
            "responsible_id": <assigned_by_id>,
            "severity": "very high",
            "rule": "lead_missed_callback",
            "reason": "<описание>",
            "details": {...}
        }
    ]
}

Верни ТОЛЬКО JSON.
"""
```

---

## Задача 3: Убедиться что `calls` попадают в данные аналитиков

Проверить что в `_fetch_user_calls_for_audit` звонки возвращаются с полями `call_type` и `status`.

Также проверить что коллекторы (`get_all_leads_with_timeline`, `get_deals_by_funnel_with_timeline`) добавляют поле `calls` к каждой сущности.

---

## Проверка

1. `.\make.cmd lint`
2. `.\make.cmd dry-run` — Agent 6 и 7 должны найти нарушения по звонкам
