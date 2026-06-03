# Промпт для Cursor — Звонки через crm.activity.list вместо voximplant

> Скопируйте содержимое ниже и отправьте в Cursor (Agent mode).
> Обновляет: `src/tools.py`.

**Проблема:** `voximplant.statistic.get` возвращает пустой результат через вебхук (нет прав).

**Решение:** использовать `crm.activity.list` с `PROVIDER_TYPE_ID=CALL` — звонки хранятся как дела.

---

## Задача 1: Переписать `_fetch_user_calls_for_audit` на `crm.activity.list`

Заменить ВСЮ функцию:

```python
def _fetch_user_calls_for_audit(
    user_id: int,
    hours_ago: int = 720,
    crm_entity_type: str | None = None,
    crm_entity_id: int | None = None,
) -> list[dict[str, Any]]:
    """Fetch call history via crm.activity.list (CALL provider).

    Args:
        user_id: Bitrix24 user ID.
        hours_ago: Lookback window in hours.
        crm_entity_type: CRM entity type (LEAD, DEAL).
        crm_entity_id: CRM entity ID.

    Returns:
        Normalized call list with status, call_type, start_date.
    """
    if not user_id:
        return []

    now = datetime.now(timezone.utc)
    date_from = (now - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S")

    activity_filter: dict[str, Any] = {
        "PROVIDER_TYPE_ID": "CALL",
        "RESPONSIBLE_ID": user_id,
        ">=CREATED": date_from,
    }
    if crm_entity_type and crm_entity_id:
        activity_filter["OWNER_TYPE_ID"] = crm_entity_type
        activity_filter["OWNER_ID"] = crm_entity_id

    try:
        bx = _get_bitrix()
        raw = bx.get_all("crm.activity.list", {
            "filter": activity_filter,
            "select": ["ID", "DIRECTION", "COMPLETED", "CREATED", "SUBJECT"],
        })

        calls: list[dict[str, Any]] = []
        for item in (raw if isinstance(raw, list) else _as_list(raw)):
            if not isinstance(item, dict):
                continue
            direction = str(item.get("DIRECTION") or "")
            completed = str(item.get("COMPLETED") or "N")

            call_type = "unknown"
            if direction == "1":
                call_type = "incoming"
            elif direction == "2":
                call_type = "outgoing"

            status = "success" if completed == "Y" else "missed"

            calls.append({
                "call_id": str(item.get("ID") or ""),
                "duration": 0,
                "start_date": str(item.get("CREATED") or ""),
                "status": status,
                "call_type": call_type,
            })

        logger.debug("crm.activity.list: user_id=%s, calls=%d", user_id, len(calls))
        return calls
    except Exception:
        logger.exception("_fetch_user_calls_for_audit failed user_id=%s", user_id)
        return []
```

---

## Задача 2: Убрать старый код voximplant из коллекторов

В `get_all_leads_with_timeline` и `get_deals_by_funnel_with_timeline` вызовы `_fetch_user_calls_for_audit` остаются без изменений — функция теперь работает через `crm.activity.list`.

---

## Проверка

1. `.\make.cmd lint`
2. `.\make.cmd dry-run` — `Agent 6: X/29 deals have call data` где X > 0
