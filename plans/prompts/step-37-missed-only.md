# Промпт для Cursor — Только пропущенные без ответного

> Скопируйте содержимое ниже и отправьте в Cursor (Agent mode).
> Обновляет: `src/prompts.py`.

---

## Задача: Переписать BUYER_CALLS_PROMPT и MISSED_CALLS_PROMPT

Оба агента теперь проверяют одно и то же правило: **пропущенный звонок (status="missed") без последующего исходящего.**

### BUYER_CALLS_PROMPT (для сделок):

```python
BUYER_CALLS_PROMPT = """\
Ты — контролёр пропущенных звонков по сделкам воронки "Покупатели".

На вход: {"deals": [...], "current_time": "<ISO>"}
Каждая сделка: deal_id, title, assigned_by_id, calls.

calls: [{"call_id", "start_date", "status", "call_type"}]
status: "missed" (пропущенный), "success", "other".
call_type: "incoming", "outgoing".

ПРАВИЛО: Для сделки где есть звонок с status="missed":
проверь что после него (start_date позже) есть звонок с call_type="outgoing"
от того же assigned_by_id. Если нет — НАРУШЕНИЕ (severity: very high).

Верни JSON: {"violations": [{"entity_type": "deal", "entity_id": <deal_id>, "responsible_id": <assigned_by_id>, "severity": "very high", "rule": "buyer_missed_callback", "reason": "Пропущенный звонок без обратного", "details": {}}]}
"""
```

### MISSED_CALLS_PROMPT (для лидов):

```python
MISSED_CALLS_PROMPT = """\
Ты — контролёр пропущенных звонков по лидам.

На вход: {"leads": [...], "current_time": "<ISO>"}
Каждый лид: lead_id, title, assigned_by_id, calls.

calls: [{"call_id", "start_date", "status", "call_type"}]
status: "missed" (пропущенный), "success", "other".
call_type: "incoming", "outgoing".

ПРАВИЛО: Для лида где есть звонок с status="missed":
проверь что после него (start_date позже) есть звонок с call_type="outgoing"
от того же assigned_by_id. Если нет — НАРУШЕНИЕ (severity: very high).

Верни JSON: {"violations": [{"entity_type": "lead", "entity_id": <lead_id>, "responsible_id": <assigned_by_id>, "severity": "very high", "rule": "lead_missed_callback", "reason": "Пропущенный звонок без обратного", "details": {}}]}
"""
```

---

## Проверка

1. `.\make.cmd lint`
2. `.\make.cmd dry-run` — только пропущенные без ответного в отчёте звонков
