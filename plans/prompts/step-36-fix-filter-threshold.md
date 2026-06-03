# Промпт для Cursor — Убрать агрессивный фильтр + порог комментария >5

> Скопируйте содержимое ниже и отправьте в Cursor (Agent mode).
> Обновляет: `src/graph.py`, `src/prompts.py`.

---

## Задача 1: Убрать фильтр entity_id > 0 из аналитиков

В Agent 6/7 фильтр `entity_id > 0` обрезает валидные violations (LLM возвращает корректные entity_id но фильтр слишком строгий).

Убрать строку:
```python
violations = [v for v in parsed_violations
              if v.get("entity_id", 0) > 0 and v.get("responsible_id", 0) > 0]
```

Вместо этого фильтровать только в dispatcher:
```python
violations = [v for v in all_violations if v.get("entity_id", 0) > 0]
```

---

## Задача 2: Порог комментария >5 вместо >20

В `LEAD_ANALYST_PROMPT`, правила 2 и 3, заменить:
- `> 20 символов` → `> 5 символов`

---

## Задача 3: Упростить BUYER_CALLS_PROMPT — вернуть рабочую версию

Вернуть промпт который находил 21-33 нарушения (работал до step-34):

```python
BUYER_CALLS_PROMPT = """\
Ты — контролёр коммуникаций по сделкам.

На вход: {"deals": [...], "current_time": "<ISO>"}
Каждая сделка: deal_id, title, assigned_by_id, calls.

calls: [{"call_id", "start_date", "status", "call_type"}]
call_type: "incoming" (входящий), "outgoing" (исходящий).
status: "success", "missed", "other".

ПРАВИЛО: Для сделки где есть входящий звонок (call_type="incoming"):
проверь что после него (start_date позже) есть исходящий (call_type="outgoing")
от того же assigned_by_id. Если нет — НАРУШЕНИЕ (severity: very high).

Верни JSON: {"violations": [{"entity_type": "deal", "entity_id": <deal_id>, "responsible_id": <assigned_by_id>, "severity": "very high", "rule": "buyer_no_callback", "reason": "описание", "details": {}}]}
"""
```

Аналогично для MISSED_CALLS_PROMPT:
```python
MISSED_CALLS_PROMPT = """\
Ты — контролёр пропущенных звонков.

На вход: {"leads": [...], "current_time": "<ISO>"}
Каждый лид: lead_id, title, assigned_by_id, calls.

ПРАВИЛО: Если есть звонок с status="missed":
проверь что после него есть исходящий (call_type="outgoing").
Если нет — НАРУШЕНИЕ (severity: very high).

Верни JSON с violations.
"""
```

---

## Проверка

1. `.\make.cmd lint`
2. `.\make.cmd dry-run` — Agent 6/7 снова находят нарушения
