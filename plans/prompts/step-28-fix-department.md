# Промпт для Cursor — Отделы + Квалифицирован + Звонки

> Скопируйте содержимое ниже и отправьте в Cursor (Agent mode).
> Обновляет: `src/graph.py`, `src/prompts.py`.

---

## Задача 1: Починить `_build_user_map` — добавить лог и fallback

Добавить логгирование чтобы понять почему `UF_DEPARTMENT` пустой:

```python
    for uid in user_ids:
        try:
            raw = _bx_call_sync("user.get", {"ID": uid})
            user = None
            if isinstance(raw, dict) and raw:
                user = raw
            elif isinstance(raw, list) and raw:
                user = raw[0] if isinstance(raw[0], dict) else None

            if user:
                first = str(user.get("NAME") or "")
                last = str(user.get("LAST_NAME") or "")
                name = f"{last} {first}".strip() or f"ID:{uid}"

                dept_raw = user.get("UF_DEPARTMENT")
                logger.debug("User %s: UF_DEPARTMENT=%s (type=%s)", uid, dept_raw, type(dept_raw).__name__)

                dept_name = ""
                if isinstance(dept_raw, list) and dept_raw:
                    for dept_id in dept_raw:
                        try:
                            dept_info = _bx_call_sync("department.get", {"ID": dept_id})
                            if isinstance(dept_info, list) and dept_info:
                                dname = str(dept_info[0].get("NAME") or "")
                                if dname:
                                    dept_name = dname
                                    break
                        except Exception:
                            pass
                    if not dept_name:
                        dept_name = f"Отдел#{dept_raw[0]}"
                elif isinstance(dept_raw, (int, str)) and dept_raw:
                    dept_name = str(dept_raw)

                display = f"{name} ({dept_name})" if dept_name else name
                user_map[uid] = display
            else:
                user_map[uid] = f"ID:{uid}"
        except Exception as exc:
            logger.warning("Failed to fetch user %s: %s", uid, exc)
            user_map[uid] = f"ID:{uid}"

    logger.info("User map: %d users, departments: %d",
                len(user_map),
                sum(1 for v in user_map.values() if "(" in v))
    return user_map
```

---

## Задача 2: Исключить "Квалифицирован" в LEAD_ANALYST_PROMPT

В `LEAD_ANALYST_PROMPT`, Правило 3:

**Было:**
```
Триггер: status_id НЕ "NEW" И НЕ "SPAM" И НЕ "WON" И НЕ "LOSE"
```

**Стало:**
```
Триггер: status_id НЕ "NEW" И НЕ "SPAM" И НЕ "WON" И НЕ "LOSE"
И НЕ содержит "QUALIFIED" (Квалифицирован) — эти лиды проверять НЕ надо.
```

Также добавить в описание правил (перед форматом ответа):
```
ВАЖНО: Лиды со status_name "Квалифицирован" пропускай — они не проверяются.
```

---

## Задача 3: Проверить что `calls` есть в данных аналитиков

В `buyer_calls_controller` и `missed_calls_controller` перед отправкой LLM добавить лог:

```python
    # Log call data presence
    entities_with_calls = sum(1 for e in entities if e.get("calls"))
    logger.info("Agent N: %d/%d entities have call data", entities_with_calls, len(entities))
```

Где `entities` = leads или deals соответственно.

---

## Задача 4: Упростить BUYER_CALLS_PROMPT (если calls есть но violations = 0)

Если логи покажут что `calls` есть, но violations = 0 — заменить BUYER_CALLS_PROMPT на ещё более простой:

```python
BUYER_CALLS_PROMPT = """\
Ты — контролёр качества коммуникаций.

На вход: {"deals": [...], "current_time": "<ISO>"}
Каждая сделка: deal_id, title, assigned_by_id, calls.

calls — массив. Каждый звонок: {call_id, duration, start_date, status, call_type}.
call_type: "incoming" (входящий), "outgoing" (исходящий).
status: "success", "missed", "other".

ПРАВИЛО: Для каждой сделки, где есть ХОТЯ БЫ ОДИН звонок с call_type="incoming":
проверь что после него (по start_date) есть звонок с call_type="outgoing"
или status="success". Если нет — НАРУШЕНИЕ (severity: very high).

Верни JSON: {"violations": [{"entity_type": "deal", "entity_id": ..., "responsible_id": ..., "severity": "very high", "rule": "buyer_no_callback", "reason": "...", "details": {...}}]}
"""
```

---

## Проверка

1. `.\make.cmd lint`
2. `.\make.cmd dry-run` — смотреть логи: `UF_DEPARTMENT=...`, `entities have call data`
