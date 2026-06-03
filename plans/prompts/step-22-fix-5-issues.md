# Промпт для Cursor — 5 исправлений: лиды, таймлайн, звонки, отделы, отчёты по отделам

> Скопируйте содержимое ниже и отправьте в Cursor (Agent mode).
> Обновляет: `src/prompts.py`, `src/graph.py`, `src/notify.py`.

---

## Задача 1: Не проверять лиды на этапе "Квалифицирован"

В `LEAD_ANALYST_PROMPT`, Правило 3, заменить:

**Было:**
```
Триггер: status_id НЕ "NEW" И НЕ "SPAM" И НЕ "WON" И НЕ "LOSE"
```

**Стало:**
```
Триггер: status_id НЕ "NEW" И НЕ "SPAM" И НЕ "WON" И НЕ "LOSE" И НЕ содержит "QUALIFIED"
(лиды на этапе "Квалифицирован" проверять НЕ надо)
```

---

## Задача 2: Проверять комментарии ТОЛЬКО в таймлайне

В `LEAD_ANALYST_PROMPT`, Правило 2 и 3 — убрать проверку `comments_field`.
Оставить проверку ТОЛЬКО `timeline`.

**Правило 2 (Спам) — заменить условие:**
```
Было: И (comments_field пусто ИЛИ timeline не содержит комментария с обоснованием)
Стало:  И timeline не содержит комментария с обоснованием причины спама (длиной > 20 символов)
```

**Правило 3 (Нецелевой) — заменить условие:**
```
Было: И (comments_field пусто ИЛИ timeline не содержит комментария с обоснованием)
Стало:  И timeline не содержит комментария с обоснованием (длиной > 20 символов)
```

Также убрать `comments_field_empty` из details (заменить на `has_justification: bool`).

---

## Задача 3: Починить детекцию звонков (Агенты 6 и 7)

Проблема: промпты слишком строгие (отличать «системную запись» от «обсуждения»). Агенты находят 0 нарушений.

**Решение:** упростить — проверять наличие любых упоминаний звонков в таймлайне.

### 3a. BUYER_CALLS_PROMPT — заменить полностью:

```python
BUYER_CALLS_PROMPT = """\
Ты — контролёр качества коммуникаций в сделках воронки "Покупатели".

На вход подаётся JSON: {"deals": [...], "current_time": "<ISO>"}
Каждая сделка: deal_id, title, assigned_by_id, timeline.

Найди нарушения. Severity: very high.

═══════════════════════════════════════
ПРАВИЛО — Входящий вызов без обратной связи
Severity: very high
═══════════════════════════════════════

Шаг 1: Найди в timeline ЛЮБУЮ запись, где comment содержит "входящий"
(регистронезависимо). Это может быть системная запись о звонке ИЛИ
комментарий менеджера "Был входящий от клиента". Проверяй ВСЕ такие записи.

Шаг 2: Для каждой найденной записи о входящем вызове проверь:
Есть ли ПОЗЖЕ по дате (created > дата входящего):
- Запись с "исходящий" в comment (системная или комментарий) от assigned_by_id
- ИЛИ ЛЮБОЙ комментарий от assigned_by_id длиной > 20 символов

Шаг 3: Если ни того, ни другого нет — НАРУШЕНИЕ.

details: deal_id, title, incoming_comment_preview (str), has_outgoing (bool), has_followup_comment (bool)

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

### 3b. MISSED_CALLS_PROMPT — заменить полностью:

```python
MISSED_CALLS_PROMPT = """\
Ты — контролёр пропущенных звонков по лидам.

На вход подаётся JSON: {"leads": [...], "current_time": "<ISO>"}
Каждый лид: lead_id, title, assigned_by_id, timeline.

Найди нарушения. Severity: very high.

═══════════════════════════════════════
ПРАВИЛО — Пропущенный без обратного звонка
Severity: very high
═══════════════════════════════════════

Шаг 1: Найди в timeline ЛЮБУЮ запись, где comment содержит "пропущен"
или "missed" (регистронезависимо).

Шаг 2: Для каждой такой записи проверь:
Есть ли ПОЗЖЕ по дате (created > дата пропущенного):
- Запись с "исходящий" в comment от assigned_by_id
- ИЛИ ЛЮБОЙ комментарий от assigned_by_id длиной > 20 символов

Шаг 3: Если ничего нет — НАРУШЕНИЕ.

details: lead_id, title, missed_comment_preview (str), has_callback (bool)

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

## Задача 4: Названия отделов вместо ID

В `src/graph.py`, в `_build_user_map`, добавить резолвинг названий отделов.

После получения `UF_DEPARTMENT` (список ID отдела), добавить вызов `department.get`:

```python
                # Department - resolve name from ID
                dept_raw = user.get("UF_DEPARTMENT")
                dept_name = ""
                if isinstance(dept_raw, list) and dept_raw:
                    dept_id = dept_raw[0]
                    try:
                        dept_info = bx.call("department.get", {"ID": dept_id})
                        if isinstance(dept_info, list) and dept_info:
                            dept_name = str(dept_info[0].get("NAME") or "")
                    except Exception:
                        dept_name = f"Отдел#{dept_id}"
                elif dept_raw:
                    dept_name = str(dept_raw)

                display = f"{name} ({dept_name})" if dept_name else name
```

---

## Задача 5: Отчёты по отделам

В `src/graph.py`, переписать `report_dispatcher` — слать отдельные сообщения для каждого отдела.

Добавить функцию `_group_by_department`:

```python
def _group_by_department(
    violations: list[dict],
    user_map: dict[int, str],
) -> dict[str, list[dict]]:
    """Group violations by department name.

    Args:
        violations: All violations.
        user_map: user_id → "Name (Department)".

    Returns:
        Dict department_name → list of violations.
    """
    groups: dict[str, list[dict]] = {}

    for v in violations:
        uid = v.get("responsible_id", 0)
        user_display = user_map.get(uid, f"ID:{uid}")

        # Extract department from "Name (Department)"
        if "(" in user_display and ")" in user_display:
            dept = user_display.split("(")[-1].rstrip(")")
        else:
            dept = "Без отдела"

        if dept not in groups:
            groups[dept] = []
        groups[dept].append(v)

    return groups
```

И обновить `report_dispatcher` — после группировки слать по одному сообщению на отдел.

---

## Проверка

1. `.\make.cmd lint`
2. `.\make.cmd dry-run` — каждый отдел получает свой отчёт в чат 22358
