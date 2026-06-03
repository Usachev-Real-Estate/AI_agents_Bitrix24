# Промпт для Cursor — Полные описания + отдельный отчёт по звонкам + названия отделов

> Скопируйте содержимое ниже и отправьте в Cursor (Agent mode).
> Обновляет: `src/graph.py`.

---

## Задача 1: Вернуть полное описание нарушения (reason)

В `report_dispatcher`, в строке форматирования заменить reason на ПОЛНОЕ описание.

Проверить что `reason` не обрезается. Текущий формат:
```python
f"{icon} [{rule}] {entity_type[0].upper()}#{entity_id} | {reason}"
```

Должен выводить ПОЛНЫЙ reason, который дал аналитик. Если в логах reason короткий — проблема в аналитике, а не в формате.

**Дополнительно:** для этапов сделок добавить `days_on_stage` из `details` если есть:
```python
days_info = ""
details = v.get("details", {})
if isinstance(details, dict):
    days = details.get("days_on_stage") or details.get("days_since_last_comment")
    if days:
        days_info = f" ({days} дн.)"
```

---

## Задача 2: Отдельный отчёт по звонкам для каждого отдела

В `report_dispatcher`, после основного отчёта по отделу, добавить ОТДЕЛЬНЫЙ блок для звонков:

```python
    # After department report, add CALLS section
    call_violations = [v for v in dept_violations
                       if v.get("severity") == "very high"]

    if call_violations:
        call_lines = [
            f"📞 ЗВОНКИ — Отдел: {dept_name}",
            f"Нарушений по звонкам: {len(call_violations)}",
            "",
        ]
        for v in call_violations:
            entity_type = v.get("entity_type", "?")
            entity_id = v.get("entity_id", 0)
            reason = v.get("reason", "—")
            link = _build_crm_link(entity_type, entity_id)
            etype_label = "Лид" if entity_type == "lead" else "Сделка"
            call_lines.append(f"🔴🔴 {etype_label} #{entity_id} | {reason}")
            call_lines.append(f"   {link}")

        call_report = "\n".join(call_lines)
        send_chat_message_chunked(REPORT_CHAT_ID, call_report)
```

---

## Задача 3: Названия отделов (не "Отдел#44")

Проверить `department.get` — возможно он возвращает данные в другом формате.

Добавить обработку `dict` ответа:
```python
dept_info = _bx_call_sync("department.get", {"ID": dept_id})
if isinstance(dept_info, dict) and dept_info:
    dname = str(dept_info.get("NAME") or "")
elif isinstance(dept_info, list) and dept_info:
    dname = str(dept_info[0].get("NAME") or "") if isinstance(dept_info[0], dict) else ""
```

Плюс добавить лог: `logger.debug("Department %s → %s", dept_id, dname)`

---

## Проверка

1. `.\make.cmd lint`
2. `.\make.cmd dry-run` — полные описания + отдельный блок `📞 ЗВОНКИ` после каждого отдела
