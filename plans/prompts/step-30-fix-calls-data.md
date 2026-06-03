# Промпт для Cursor — Починить получение данных о звонках

> Скопируйте содержимое ниже и отправьте в Cursor (Agent mode).
> Обновляет: `src/tools.py`.

**Проблема:** `Agent 6: 0/28 deals have call data` — `_fetch_user_calls_for_audit` возвращает пустые списки. Фильтр `CRM_ENTITY_ID` для сделок слишком узкий.

---

## Задача 1: Убрать фильтр CRM_ENTITY_ID для сделок

В `get_deals_by_funnel_with_timeline`, строки 787-793:

**Было:**
```python
            try:
                record["calls"] = _fetch_user_calls_for_audit(
                    assigned_id,
                    hours_ago=720,
                    crm_entity_type="DEAL",
                    crm_entity_id=did,
                )
```

**Стало:**
```python
            try:
                record["calls"] = _fetch_user_calls_for_audit(
                    assigned_id,
                    hours_ago=720,
                )
```

---

## Задача 2: Добавить логгирование ответа voximplant

В `_fetch_user_calls_for_audit`, после вызова `bx.call("voximplant.statistic.get", ...)`:

Добавить:
```python
        raw = bx.call("voximplant.statistic.get", {...})
        records = _as_list(raw)
        logger.debug(
            "voximplant: user_id=%s, records=%d, raw_type=%s",
            user_id, len(records), type(raw).__name__,
        )
```

---

## Задача 3: Проверить права вебхука

В `test_b24.py` или отдельным скриптом проверить доступность `voximplant.statistic.get`:

```python
# В test_b24.py добавить:
def test_voximplant(bx):
    try:
        result = bx.call("voximplant.statistic.get", {
            "filter": {
                "PORTAL_USER_ID": 1,
                ">=CALL_START_DATE": "2024-01-01 00:00:00",
                "<=CALL_START_DATE": "2026-12-31 23:59:59",
            },
        })
        print(f"Voximplant: {type(result).__name__}, records: {len(_as_list(result))}")
        return True
    except Exception as e:
        print(f"Voximplant error: {e}")
        return False
```

---

## Проверка

1. `.\make.cmd lint`
2. `.\make.cmd dry-run` — `Agent 6: X/28 deals have call data` где X > 0
