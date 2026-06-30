# step-58-fix-999-days-display.md

## Проблема

В отчёте для сделок без комментариев выводится:
```
🟡 Сделка #12736 | Мамонтов Иван | На этапе «Показ» нет комментариев. (999 дн.)
```

`(999 дн.)` — бессмысленно рядом с «нет комментариев». 999 — это внутренний маркер «комментариев нет», он не должен показываться пользователю.

## Причина

В [`graph.py:694`](src/graph.py:694) `days_info` не фильтрует значение 999:

```python
if days is not None and days != "":
    days_info = f" ({days} дн.)"
```

## Задача

В [`report_dispatcher()`](src/graph.py:688-695) не показывать `(X дн.)`, когда `days >= 999`.

---

## Исправление

### Где

Файл: `src/graph.py`, функция `report_dispatcher`, строка 694.

### Текущий код

```python
                days_info = ""
                details = v.get("details", {})
                if isinstance(details, dict):
                    days = details.get("days_on_stage") or details.get(
                        "days_since_last_comment",
                    )
                    if days is not None and days != "":
                        days_info = f" ({days} дн.)"
```

### Новый код

```python
                days_info = ""
                details = v.get("details", {})
                if isinstance(details, dict):
                    days = details.get("days_on_stage") or details.get(
                        "days_since_last_comment",
                    )
                    if days is not None and days != "" and days < 999:
                        days_info = f" ({days} дн.)"
```

**Изменение:** добавлено `and days < 999` в условие.

---

## Проверка

```bash
python src/main.py  # DRY_RUN
```

Убедиться, что:
- ✅ `нет комментариев` — без `(999 дн.)`
- ✅ `последний комментарий более 7 дней назад. (7 дн.)` — с днями
- ✅ `Сделка находится на этапе «Первый контакт» более 1 дня. (3.9 дн.)` — с днями
