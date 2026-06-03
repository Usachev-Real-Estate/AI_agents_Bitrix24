# Промпт для Cursor — Диагностика звонков для лида #1538

> Скопируйте содержимое ниже и отправьте в Cursor (Agent mode).
> Обновляет: `src/graph.py`, `src/tools.py`.

---

## Задача: Добавить лог звонков для отладки

### В `src/tools.py`, в `_fetch_user_calls_for_audit`:

Добавить после классификации:
```python
        missed_calls = [c for c in calls if c.get("status") == "missed"]
        if missed_calls:
            logger.info(
                "CALLS: user_id=%s, total=%d, missed=%d, subjects=%s",
                user_id, len(calls), len(missed_calls),
                [c.get("subject_preview", "")[:50] for c in missed_calls[:3]],
            )
```

Также добавить `subject_preview` в выходные данные:
```python
            "subject_preview": (subject or "")[:100],
```

### В `src/graph.py`, в `missed_calls_controller`:

Добавить лог для отладки после анализа:
```python
    # Debug: check if lead 1538 has missed calls
    for lead in leads:
        if lead.get("lead_id") == 1538:
            calls_data = lead.get("calls", [])
            missed = [c for c in calls_data if c.get("status") == "missed"]
            logger.info(
                "DEBUG lead 1538: total_calls=%d, missed=%d, subjects=%s",
                len(calls_data), len(missed),
                [c.get("subject_preview", "")[:50] for c in missed],
            )
```

---

## Проверка

Запусти `.\make.cmd dry-run` и смотри логи:
- `CALLS: user_id=..., missed=N, subjects=[...]`
- `DEBUG lead 1538: total_calls=N, missed=N`
