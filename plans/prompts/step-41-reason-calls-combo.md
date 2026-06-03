# Промпт для Cursor — Вернуть полные причины + агрессивный дебаг звонков

> Скопируйте содержимое ниже и отправьте в Cursor (Agent mode).
> Обновляет: `src/prompts.py`, `src/graph.py`.

---

## Задача 1: Вернуть полные описания причин

В `LEAD_ANALYST_PROMPT`, в формате ответа, заменить reason на ПОЛНОЕ описание:

Вместо коротких:
```
"reason": "Новый"
```

Требовать:
```
"reason": "Лид в статусе 'Новый' более 2 часов без комментария ответственного (прошло X часов)"
```

Добавить в промпт явное требование:
```
reason ДОЛЖЕН быть развёрнутым (минимум 30 символов) и содержать конкретику: какой статус, сколько времени прошло, что именно нарушено.
```

---

## Задача 2: Логгировать ВСЕ missed звонки в консоль (DEBUG → INFO)

В `missed_calls_controller` и `buyer_calls_controller`, ДО отправки LLM, вывести в INFO:

```python
    # Count missed calls
    total_missed = 0
    entities_with_missed = 0
    for entity in entities:
        has_missed = any(c.get("status") == "missed" for c in entity.get("calls", []))
        if has_missed:
            entities_with_missed += 1
            total_missed += sum(1 for c in entity.get("calls", []) if c.get("status") == "missed")
    
    logger.info(
        "Agent N: %d/%d entities with missed calls (%d total missed)",
        entities_with_missed, len(entities), total_missed,
    )
```

---

## Задача 3: Для лида #1538 — вывести ВСЕ его звонки

В `missed_calls_controller`, после лога:

```python
    for lead in leads:
        if lead.get("lead_id") == 1538:
            logger.info(
                "LEAD 1538 CALLS: %s",
                json.dumps(lead.get("calls", []), ensure_ascii=False, default=str)[:500],
            )
```

---

## Проверка

1. `.\make.cmd lint`
2. `.\make.cmd dry-run` — смотреть логи: `entities with missed calls`, `LEAD 1538 CALLS`
