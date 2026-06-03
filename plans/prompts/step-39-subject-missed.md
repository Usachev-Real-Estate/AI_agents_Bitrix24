# Промпт для Cursor — Определять missed по SUBJECT, а не COMPLETED

> Скопируйте содержимое ниже и отправьте в Cursor (Agent mode).
> Обновляет: `src/tools.py`.

**Проблема:** `COMPLETED=N` ненадёжно — может означать "не завершён", а не "пропущен".

**Решение:** проверять `SUBJECT` на "пропущен"/"missed".

---

## Задача: Исправить классификацию в `_fetch_user_calls_for_audit`

**Было:**
```python
            if call_type == "incoming":
                status = "success" if completed == "Y" else "missed"
            elif call_type == "outgoing":
                status = "success" if completed == "Y" else "other"
            else:
                status = "success" if completed == "Y" else "other"
```

**Стало:**
```python
            subject = str(item.get("SUBJECT") or "").lower()
            is_missed = "пропущен" in subject or "missed" in subject

            if call_type == "incoming":
                status = "missed" if is_missed else "success"
            elif call_type == "outgoing":
                status = "success" if completed == "Y" else "other"
            else:
                status = "success" if completed == "Y" else "other"
```

Также добавить `"SUBJECT"` в select если ещё нет:
```python
"select": ["ID", "DIRECTION", "COMPLETED", "CREATED", "SUBJECT"],
```

---

## Проверка

1. `.\make.cmd lint`
2. `.\make.cmd dry-run` — пропущенные определяются по SUBJECT
