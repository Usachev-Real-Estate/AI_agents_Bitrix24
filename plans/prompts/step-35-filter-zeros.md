# Промпт для Cursor — Фильтр entity_id=0 + анти-дубликаты

> Скопируйте содержимое ниже и отправьте в Cursor (Agent mode).
> Обновляет: `src/graph.py`.

---

## Задача 1: Фильтровать violations с entity_id=0

В `_parse_violations_json` (или в каждом аналитике после парсинга) добавить фильтр:

```python
# Filter out violations with missing entity_id
violations = [v for v in parsed_violations
              if v.get("entity_id", 0) > 0 and v.get("responsible_id", 0) > 0]
```

Добавить в КАЖДЫЙ аналитик (Agent 4–7) после `_parse_violations_json`.

---

## Задача 2: Убрать дубликаты — dedup в dispatcher

В `report_dispatcher`, после группировки по отделам, добавить дедупликацию:

```python
    # Deduplicate violations (same entity_id + same rule = duplicate)
    seen = set()
    unique_violations = []
    for v in violations:
        key = (v.get("entity_type"), v.get("entity_id"), v.get("rule"))
        if key not in seen:
            seen.add(key)
            unique_violations.append(v)
    violations = unique_violations
```

---

## Задача 3: Фильтровать "Без отдела" если violations=0

Если все нарушения в "Без отдела" имеют entity_id=0 — не слать этот отчёт.

---

## Проверка

1. `.\make.cmd lint`
2. `.\make.cmd dry-run` — без `Сделка #0`, без дубликатов
