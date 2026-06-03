# Промпт для Cursor — Найти поле для missed в crm.activity.list

> Скопируйте содержимое ниже и отправьте в Cursor (Agent mode).
> Обновляет: `src/tools.py`.

**Проблема:** `crm.activity.list` возвращает `SUBJECT = "Входящий от ..."` для ВСЕХ входящих, даже пропущенных. SUBJECT-детекция не работает.

---

## Задача: Добавить поля в select и вывести их в лог

В `_fetch_user_calls_for_audit`, изменить select:

```python
"select": [
    "ID", "DIRECTION", "COMPLETED", "CREATED", "SUBJECT",
    "DESCRIPTION", "RESULT_CODE", "RESULT_SUMMARY",
    "RESPONSIBLE_ID", "AUTHOR_ID",
],
```

И в выход добавить:
```python
"completed": str(item.get("COMPLETED") or "?")[0],
"result_code": str(item.get("RESULT_CODE") or "?"),
"description": (str(item.get("DESCRIPTION") or ""))[:80],
```

В лог для лида #1538 вывести ПОЛНУЮ информацию:
```python
logger.info("LEAD 1538 FULL: %s", json.dumps(lead.get("calls", []), ensure_ascii=False, default=str)[:2000])
```

---

## Проверка

Запусти dry-run, смотри `LEAD 1538 FULL` — ищем поле которое отличает пропущенный от принятого.
