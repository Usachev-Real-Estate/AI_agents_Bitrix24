# Промпт для Cursor — ФИО, названия этапов, исключение "Агент"

> Скопируйте содержимое ниже и отправьте в Cursor (Agent mode).
> Обновляет: `src/graph.py`, `src/prompts.py`.

---

## Задача 1: Фамилия Имя в каждой строке отчёта

В `report_dispatcher`, в блоке формирования строки нарушения, добавить имя сотрудника из `user_map`:

**Было:**
```python
lines.append(
    f"{icon} [{rule}] {entity_type[0].upper()}#{entity_id} | {reason}"
)
```

**Стало:**
```python
uid = v.get("responsible_id", 0)
user_display = user_map.get(uid, f"ID:{uid}")
# Extract just the name without department
name_only = user_display.split(" (")[0] if " (" in user_display else user_display
etype_label = "Лид" if entity_type == "lead" else "Сделка"
lines.append(
    f"{icon} {etype_label} #{entity_id} | {name_only} | {reason}"
)
```

---

## Задача 2: Подразделения в Bitrix24

Документация Bitrix24 REST API:
- **Где хранятся:** `user.get(ID)` → поле `UF_DEPARTMENT` (массив ID подразделений)
- **Как получить название:** `department.get({"ID": dept_id})` → поле `NAME`
- **Список всех:** `department.list()` 

В коде уже реализовано в `_build_user_map` (step-22). Убедись что `department.get` вызывается и возвращает `NAME`.

---

## Задача 3: `L#1436` → `Лид #1436`

Заменить формат в `report_dispatcher`:
- `L#` → `Лид #`
- `D#` → `Сделка #`

(Реализовано в задаче 1 через `etype_label`)

---

## Задача 4: Исключить этап "Агент" из проверки

В `LEAD_ANALYST_PROMPT`, Правило 3, добавить исключение:

**Было:**
```
Триггер: status_id НЕ "NEW" И НЕ "SPAM" И НЕ "WON" И НЕ "LOSE" И НЕ содержит "QUALIFIED"
```

**Стало:**
```
Триггер: status_id НЕ "NEW" И НЕ "SPAM" И НЕ "WON" И НЕ "LOSE"
И НЕ содержит "QUALIFIED" (Квалифицирован)
И НЕ содержит "AGENT" (Агент)
И НЕ содержит "UC_52VG81" (Агент — проверь, возможно это код этапа "Агент")

ВАЖНО: если status_name содержит "Агент" — пропускай, это не нарушение.
```

---

## Проверка

1. `.\make.cmd lint`
2. `.\make.cmd dry-run` — в отчёте: `🔴 Лид #1436 | Яковлев Александр | ...`
