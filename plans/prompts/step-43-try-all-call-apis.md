# Промпт для Cursor — Пробуем все API для missed calls

> Скопируйте содержимое ниже и отправьте в Cursor (Agent mode).
> Обновляет: `src/tools.py`.

---

## Задача: Переписать `_fetch_user_calls_for_audit` — пробовать 3 подхода

```python
def _fetch_user_calls_for_audit(
    user_id: int,
    hours_ago: int = 720,
    crm_entity_type: str | None = None,
    crm_entity_id: int | None = None,
) -> list[dict[str, Any]]:
    """Fetch calls trying multiple APIs for missed call detection.

    Priority:
    1. voximplant.statistic.get (CALL_FAILED_CODE)
    2. crm.activity.list (COMPLETED=N)
    3. crm.activity.list (DESCRIPTION text search)
    """
    if not user_id:
        return []

    settings = get_settings()
    now = datetime.now(timezone.utc)
    if settings.report_since:
        date_from = settings.report_since + " 00:00:00"
    else:
        date_from = (now - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S")
    date_to = now.strftime("%Y-%m-%d %H:%M:%S")

    calls: list[dict[str, Any]] = []

    # === Approach 1: voximplant.statistic.get ===
    try:
        bx = _get_bitrix()
        vox_filter: dict[str, Any] = {
            "PORTAL_USER_ID": user_id,
            ">=CALL_START_DATE": date_from,
            "<=CALL_START_DATE": date_to,
        }
        if crm_entity_type and crm_entity_id:
            vox_filter["CRM_ENTITY_TYPE"] = crm_entity_type
            vox_filter["CRM_ENTITY_ID"] = crm_entity_id

        raw = bx.call("voximplant.statistic.get", {
            "filter": vox_filter,
            "sort": "CALL_START_DATE",
            "order": "ASC",
        })
        records = _as_list(raw)
        if records:
            logger.info("voximplant OK: user_id=%s, records=%d", user_id, len(records))
            for record in records:
                call_type_num = str(record.get("CALL_TYPE") or "")
                call_type = "incoming" if call_type_num == "2" else (
                    "outgoing" if call_type_num == "1" else "unknown"
                )
                failed_code = _coerce_int(record.get("CALL_FAILED_CODE"))
                duration = _coerce_int(record.get("CALL_DURATION"))
                if duration == 0 or failed_code != 200:
                    status = "missed"
                elif duration > 30:
                    status = "success"
                else:
                    status = "other"

                calls.append({
                    "call_id": str(record.get("CALL_ID") or record.get("ID") or ""),
                    "duration": duration,
                    "start_date": str(record.get("CALL_START_DATE") or ""),
                    "status": status,
                    "call_type": call_type,
                })
            return calls  # Got data, return immediately
        else:
            logger.debug("voximplant empty for user_id=%s", user_id)
    except Exception as exc:
        logger.debug("voximplant failed for user_id=%s: %s", user_id, exc)

    # === Approach 2: crm.activity.list with COMPLETED=N ===
    try:
        bx = _get_bitrix()
        raw2 = bx.get_all("crm.activity.list", {
            "filter": {
                "PROVIDER_TYPE_ID": "CALL",
                "RESPONSIBLE_ID": user_id,
                ">=CREATED": date_from,
                "COMPLETED": "N",
            },
            "select": ["ID", "DIRECTION", "CREATED", "SUBJECT", "COMPLETED"],
        })
        records2 = raw2 if isinstance(raw2, list) else _as_list(raw2)
        if records2:
            logger.info("activity.list COMPLETED=N OK: user_id=%s, records=%d", user_id, len(records2))
            for item in records2:
                if not isinstance(item, dict):
                    continue
                direction = str(item.get("DIRECTION") or "")
                call_type = "incoming" if direction == "1" else (
                    "outgoing" if direction == "2" else "unknown"
                )
                calls.append({
                    "call_id": str(item.get("ID") or ""),
                    "duration": 0,
                    "start_date": str(item.get("CREATED") or ""),
                    "status": "missed",
                    "call_type": call_type,
                })
            return calls
    except Exception as exc:
        logger.debug("activity.list COMPLETED=N failed: %s", exc)

    # === Approach 3: crm.activity.list - all calls, classify by DESCRIPTION ===
    try:
        bx = _get_bitrix()
        raw3 = bx.get_all("crm.activity.list", {
            "filter": {
                "PROVIDER_TYPE_ID": "CALL",
                "RESPONSIBLE_ID": user_id,
                ">=CREATED": date_from,
            },
            "select": ["ID", "DIRECTION", "CREATED", "SUBJECT", "DESCRIPTION", "COMPLETED"],
        })
        records3 = raw3 if isinstance(raw3, list) else _as_list(raw3)
        if records3:
            logger.info("activity.list all OK: user_id=%s, records=%d", user_id, len(records3))
            for item in records3:
                if not isinstance(item, dict):
                    continue
                direction = str(item.get("DIRECTION") or "")
                call_type = "incoming" if direction == "1" else (
                    "outgoing" if direction == "2" else "unknown"
                )
                description = str(item.get("DESCRIPTION") or "").lower()
                subject = str(item.get("SUBJECT") or "").lower()
                combined = description + " " + subject

                is_missed = (
                    "missed" in combined or "пропущен" in combined
                    or "не отвечен" in combined
                )
                status = "missed" if is_missed else "success"

                calls.append({
                    "call_id": str(item.get("ID") or ""),
                    "duration": 0,
                    "start_date": str(item.get("CREATED") or ""),
                    "status": status,
                    "call_type": call_type,
                })
            missed_count = sum(1 for c in calls if c["status"] == "missed")
            logger.info("activity.list text: user_id=%s, total=%d, missed=%d",
                       user_id, len(calls), missed_count)
    except Exception as exc:
        logger.debug("activity.list all failed: %s", exc)

    return calls
```

---

## Проверка

1. `.\make.cmd lint`
2. `.\make.cmd dry-run` — смотреть логи: `voximplant OK`, `COMPLETED=N OK`, `activity.list text`
