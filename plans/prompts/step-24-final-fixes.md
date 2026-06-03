# Промпт для Cursor — Группировка по отделам + статусы + звонки

> Скопируйте содержимое ниже и отправьте в Cursor (Agent mode).
> Обновляет: `src/tools.py`, `src/graph.py`.

---

## Задача 1: Группировка отчётов по подразделениям

Убедиться что `_build_user_map` возвращает `user_id → "Фамилия Имя (Отдел)"`.
Добавить `_group_by_department` и перестроить `report_dispatcher`.

### 1a. Добавить функцию в `src/graph.py` (перед `report_dispatcher`):

```python
def _group_by_department(
    violations: list[dict],
    user_map: dict[int, str],
) -> dict[str, list[dict]]:
    """Group violations by department name extracted from user_map.

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
        if "(" in user_display and ")" in user_display:
            dept = user_display.split("(")[-1].rstrip(")")
        else:
            dept = "Без отдела"
        groups.setdefault(dept, []).append(v)
    return groups
```

### 1b. Переписать блок отправки в `report_dispatcher`:

Вместо трёх отчётов (Лиды/Покупатели/Продавцы) — группировка по отделам:

```python
    # Group by department
    dept_groups = _group_by_department(violations, user_map)

    try:
        for dept_name, dept_violations in sorted(dept_groups.items()):
            lead_v = [v for v in dept_violations if v.get("entity_type") == "lead"]
            deal_v = [v for v in dept_violations if v.get("entity_type") == "deal"]

            lines = [
                f"b24-ai-auditor v2 — Отдел: {dept_name}",
                f"Дата: {now}",
                f"Сотрудников с нарушениями: {len(set(v.get('responsible_id', 0) for v in dept_violations))}",
                f"Всего нарушений: {len(dept_violations)}",
                f"  Лиды: {len(lead_v)}",
                f"  Сделки: {len(deal_v)}",
                "",
            ]

            for v in dept_violations:
                sev = v.get("severity", "?")
                icon = {"very high": "🔴🔴", "high": "🔴", "medium": "🟡"}.get(sev, "⚪")
                entity_type = v.get("entity_type", "?")
                entity_id = v.get("entity_id", 0)
                reason = v.get("reason", "—")
                link = _build_crm_link(entity_type, entity_id)
                rule = v.get("rule", "?")

                lines.append(
                    f"{icon} [{rule}] {entity_type[0].upper()}#{entity_id} | {reason}"
                )
                lines.append(f"   {link}")

            report = "\n".join(lines)
            send_chat_message_chunked(REPORT_CHAT_ID, report)

        logger.info("Dispatcher: reports sent for %d departments", len(dept_groups))
    except Exception:
        logger.exception("Dispatcher: failed to send reports to chat %d", REPORT_CHAT_ID)
```

---

## Задача 2: Статусы на русском (не коды)

Добавить в `src/tools.py` функцию `_fetch_status_names()` и встроить в коллекторы.

### 2a. Добавить хелпер:

```python
def _fetch_lead_status_names() -> dict[str, str]:
    """Fetch lead status ID → human-readable name mapping.

    Returns:
        Dict status_id → status_name.
    """
    try:
        bx = _get_bitrix()
        raw = bx.get_all("crm.status.list", {
            "filter": {"ENTITY_ID": "STATUS"},
        })
        result: dict[str, str] = {}
        for item in raw:
            if isinstance(item, dict):
                sid = str(item.get("STATUS_ID") or "")
                name = str(item.get("NAME") or "")
                if sid and name:
                    result[sid] = name
        return result
    except Exception:
        return {}
```

### 2b. В `get_all_leads_with_timeline` — добавить поле `status_name`:

После получения `status_id`, резолвить имя:
```python
status_names = _fetch_lead_status_names()
...
"status_id": str(lead.get("STATUS_ID") or ""),
"status_name": status_names.get(str(lead.get("STATUS_ID") or ""), ""),
```

### 2c. В `LEAD_ANALYST_PROMPT` — указать что `status_name` содержит читаемое имя:

Добавить в описание полей: `status_id` (код), `status_name` (русское название).
И в `reason` использовать `status_name`, а не `status_id`.

---

## Задача 3: Отчёт по звонкам — добавить данные о звонках в коллекторы

**Проблема:** `crm.timeline.comment.list` возвращает только текстовые комментарии, НЕ записи о звонках. Агенты 6/7 не находят нарушений потому что в данных нет упоминаний звонков.

**Решение:** добавить получение звонков через `voximplant.statistic.get` (уже есть в `check_calls`) и включить их в данные.

### 3a. Добавить в `get_all_leads_with_timeline`:

После получения таймлайна для лида, добавить запрос звонков:
```python
# Fetch calls for this lead's responsible user
try:
    calls_data = check_calls.invoke({
        "user_id": assigned_id,
        "hours_ago": 720,  # 30 days
    })
    lead_record["calls"] = calls_data.get("calls", [])
except Exception:
    lead_record["calls"] = []
```

### 3b. В `MISSED_CALLS_PROMPT` — использовать `calls` вместо поиска по тексту:

```python
MISSED_CALLS_PROMPT = """\
Ты — контролёр пропущенных звонков по лидам.

На вход подаётся JSON: {"leads": [...], "current_time": "<ISO>"}
Каждый лид: lead_id, title, assigned_by_id, calls (список звонков с полями: call_id, duration, start_date, status).

Найди нарушения. Severity: very high.

═══════════════════════════════════════
ПРАВИЛО — Пропущенный без обратного звонка
Severity: very high
═══════════════════════════════════════
Для каждого лида проверь его calls:
1. Если есть звонок со status="missed" или duration=0 — это пропущенный.
2. После него должен быть звонок со status="success" от того же пользователя
   с start_date ПОЗЖЕ даты пропущенного.
3. Если такого нет — НАРУШЕНИЕ.

details: lead_id, title, missed_call_date, has_callback (bool)
"""
```

Аналогично для `BUYER_CALLS_PROMPT`.

---

## Проверка

1. `.\make.cmd lint`
2. `.\make.cmd dry-run` — отчёты по отделам, статусы на русском, звонки детектятся
